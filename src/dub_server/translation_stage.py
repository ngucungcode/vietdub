"""Checkpointed orchestration for local Vietnamese translation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .llama_translation import LlamaServerTranslator, LlamaTranslationError
from .model_registry import (
    ModelNotFoundError,
    ModelRegistryError,
    ModelVerificationError,
    VerifiedModel,
    resolve_verified_model,
)
from .state import InvalidTransition, JobRecord, JobStage, JobStatus, StateStore
from .timing import TimingProfile
from .transcript import TranscriptError, load_transcript_artifact
from .translation import (
    TranslationBlock,
    TranslationError,
    Translator,
    build_translation_blocks,
    bypass_vietnamese_translation,
    translate_blocks,
)
from .translation_artifact import (
    TranslationArtifact,
    TranslationArtifactError,
    TranslationResult,
    TranslationSegment,
    load_translation_artifact,
    write_translation_artifact,
)

ModelResolver = Callable[[Path, Path, str, str], VerifiedModel]
TranslatorFactory = Callable[[VerifiedModel], Translator]

_BYPASS_MODEL_ID = "translation-bypass"
_BYPASS_TREE_SHA256 = hashlib.sha256(b"translation-bypass-v1").hexdigest()
_BUILDER_VERSION = "translation-blocks-v1"
_NATURAL_PROMPT_VERSION = "natural-duration-v1"


def _speaker_for_row(row: Any, source_segments: Sequence[Any]) -> str | None:
    """Recover a published block's speaker from its source ordinals.

    Translation blocks never merge across speakers, so every source
    ordinal in a block shares one label (or none). The plan rows in
    SQLite carry no speaker column, so the label is re-derived here from
    the source transcript instead of migrating the database.
    """

    ordinals = getattr(row, "source_ordinals", ()) or ()
    for ordinal in ordinals:
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            continue
        if 0 <= ordinal < len(source_segments):
            speaker = getattr(source_segments[ordinal], "speaker", None)
            if isinstance(speaker, str) and speaker.strip():
                return " ".join(speaker.split())[:120]
            return None
    return None


class _StageCancelled(Exception):
    pass


class TranslationStage:
    """Translate a durable source transcript and stop at ``READY_TTS``."""

    def __init__(
        self,
        *,
        models_lock_path: Path,
        models_dir: Path,
        jobs_dir: Path,
        default_translation_model_id: str,
        store: StateStore,
        llama_server_binary: Path | None = None,
        llama_server_port: int = 18081,
        llama_context_size: int = 2048,
        llama_max_output_tokens: int = 512,
        llama_gpu_layers: int = -1,
        llama_startup_timeout_seconds: float = 180.0,
        llama_request_timeout_seconds: float = 120.0,
        translator_factory: TranslatorFactory | None = None,
        model_resolver: ModelResolver = resolve_verified_model,
        shutdown_requested: Callable[[], bool] | None = None,
        # OPT-IN OpenAI backend. When set, the local model resolver is
        # skipped entirely: no GGUF is loaded and no VRAM is used for MT.
        # The caller (see docs/openai-translation.md) must have read the
        # key from a secret file and must have enabled this explicitly.
        openai_translator: Translator | None = None,
    ) -> None:
        model_id = default_translation_model_id.strip()
        if not model_id:
            raise ValueError("Model dịch mặc định không được để trống")
        self._models_lock_path = Path(models_lock_path)
        self._models_dir = Path(models_dir)
        self._jobs_dir = Path(jobs_dir)
        self._default_model_id = model_id
        self._store = store
        self._model_resolver = model_resolver
        self._shutdown_requested = shutdown_requested or (lambda: False)
        self._openai_translator = openai_translator
        if openai_translator is not None:
            # Factory is unused on the OpenAI path, but keep the attribute
            # so the rest of the stage does not need a llama binary.
            self._translator_factory = lambda verified: openai_translator
        elif translator_factory is not None:
            self._translator_factory = translator_factory
        else:
            if llama_server_binary is None:
                raise ValueError("Thiếu đường dẫn llama-server cho backend dịch")
            binary = Path(llama_server_binary)

            def create_translator(verified: VerifiedModel) -> Translator:
                return LlamaServerTranslator(
                    llama_server_binary=binary,
                    model_path=self._model_file(verified),
                    model_id=verified.model_id,
                    port=llama_server_port,
                    context_size=llama_context_size,
                    max_output_tokens=llama_max_output_tokens,
                    gpu_layers=llama_gpu_layers,
                    startup_timeout_seconds=llama_startup_timeout_seconds,
                    request_timeout_seconds=llama_request_timeout_seconds,
                )

            self._translator_factory = create_translator

    async def run(self, job_id: str) -> JobRecord:
        job = self._store.get_job(job_id)
        if self._shutdown_requested() or self._cancelled(job):
            return job
        if job.status is JobStatus.READY_TTS:
            return job
        if job.status not in {JobStatus.READY_TRANSLATION, JobStatus.TRANSLATING}:
            raise InvalidTransition(f"Job {job.id} không ở trạng thái có thể dịch")

        try:
            source_artifact = await self._load_source_artifact(job)
            source = source_artifact.result
            source_digest = source_artifact.sha256
            model_id = (
                _BYPASS_MODEL_ID if self._is_vietnamese(source.language) else self._model_id(job)
            )

            resumed_artifact = await self._load_completed_artifact(
                job,
                source_transcript_sha256=source_digest,
                expected_model_id=model_id,
            )
            if resumed_artifact is not None:
                return self._store.commit_translation_artifact(
                    job.id,
                    resumed_artifact.result,
                    artifact_path=resumed_artifact.path,
                    artifact_sha256=resumed_artifact.sha256,
                )

            if model_id == _BYPASS_MODEL_ID:
                return await self._run_vietnamese_bypass(
                    job,
                    source_artifact=source_artifact,
                )
            return await self._run_model_translation(
                job,
                source_artifact=source_artifact,
                model_id=model_id,
            )
        except _StageCancelled:
            return self._store.get_job(job_id)
        except LlamaTranslationError as error:
            return self._fail(job_id, error.code, error.message_vi, error.retryable)
        except TranslationError as error:
            return self._fail(job_id, error.code, error.message_vi, error.retryable)
        except ModelNotFoundError:
            return self._fail(
                job_id,
                "translation_model_not_found",
                "Model dịch đã chọn không có trong danh mục cục bộ",
                False,
            )
        except (ModelVerificationError, ModelRegistryError):
            return self._fail(
                job_id,
                "translation_model_verification_failed",
                "Model dịch cục bộ bị thiếu hoặc không vượt qua kiểm tra toàn vẹn",
                True,
            )
        except (TranscriptError, TranslationArtifactError):
            return self._fail(
                job_id,
                "translation_artifact_invalid",
                "Transcript hoặc artifact dịch không hợp lệ",
                False,
            )
        except InvalidTransition:
            current = self._store.get_job(job_id)
            if self._cancelled(current):
                return current
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._fail(
                job_id,
                "translation_stage_failed",
                "Không thể hoàn tất bước dịch sang tiếng Việt",
                True,
            )

    async def process(self, job_id: str) -> JobRecord:
        return await self.run(job_id)

    async def _run_vietnamese_bypass(
        self,
        job: JobRecord,
        *,
        source_artifact: Any,
    ) -> JobRecord:
        source = source_artifact.result
        if job.status is JobStatus.READY_TRANSLATION:
            blocks = build_translation_blocks(
                source.segments,
                duration_us=source.duration_us,
                token_counter=self._fallback_token_count,
            )
            translated = bypass_vietnamese_translation(blocks)
            plan_sha = self._plan_sha256(
                translated,
                source_transcript_sha256=source_artifact.sha256,
                model_id=_BYPASS_MODEL_ID,
                model_tree_sha256=_BYPASS_TREE_SHA256,
                prompt_template_id="vietnamese-bypass-v1",
                token_counts=[self._fallback_token_count(item.source_text) for item in translated],
            )
            self._store.initialize_translation_plan(
                job.id,
                translated,
                source_language=source.language,
                target_language="vi",
                model_id=_BYPASS_MODEL_ID,
                model_revision=None,
                model_tree_sha256=_BYPASS_TREE_SHA256,
                source_transcript_sha256=source_artifact.sha256,
                plan_sha256=plan_sha,
                prompt_template_id="vietnamese-bypass-v1",
                source_token_counts=[
                    self._fallback_token_count(item.source_text) for item in translated
                ],
            )
        for stored in self._store.list_translation_blocks(job.id):
            if stored.translated_text is not None:
                continue
            if self._is_cancel_requested(job.id):
                raise _StageCancelled()
            self._store.commit_translation_block(
                job.id,
                stored.ordinal,
                stored.source_text,
                output_token_count=self._fallback_token_count(stored.source_text),
            )
        return await self._publish_and_commit(
            job.id,
            source_transcript_sha256=source_artifact.sha256,
            duration_us=source.duration_us,
            model_revision=None,
            source_segments=source.segments,
        )

    async def _run_model_translation(
        self,
        job: JobRecord,
        *,
        source_artifact: Any,
        model_id: str,
    ) -> JobRecord:
        if self._openai_translator is not None:
            # OpenAI path: no local model to resolve or verify. Provenance
            # is recorded via model_id + this synthetic tree hash, exactly
            # like the Vietnamese bypass model below.
            model_tree_sha256 = hashlib.sha256(
                f"openai-translation-v1:{model_id}".encode()
            ).hexdigest()
            model_revision: str | None = model_id
            prompt_template_id = "openai-translation-v1"
            translator = self._openai_translator
        else:
            verified = await asyncio.to_thread(
                self._model_resolver,
                self._models_lock_path,
                self._models_dir,
                model_id,
                "mt",
            )
            model_tree_sha256 = verified.tree_sha256
            model_revision = str(verified.entry.get("revision") or "") or None
            prompt_template_id = str(
                verified.entry.get("prompt_template_id", "gemma4-translation-v1")
            )
            translator = self._translator_factory(verified)
        if self._is_cancel_requested(job.id):
            raise _StageCancelled()
        try:
            start = getattr(translator, "start", None)
            if callable(start):
                await asyncio.to_thread(start)
            if self._is_cancel_requested(job.id):
                raise _StageCancelled()
            timing_profile = self._timing_profile(job)
            if timing_profile is TimingProfile.NATURAL:
                prompt_template_id = (
                    f"{prompt_template_id}+{_NATURAL_PROMPT_VERSION}"
                )
            source = source_artifact.result
            if job.status is JobStatus.READY_TRANSLATION:
                blocks = await asyncio.to_thread(
                    build_translation_blocks,
                    source.segments,
                    duration_us=source.duration_us,
                    token_counter=translator.count_tokens,
                )
                token_counts = await asyncio.to_thread(
                    lambda: [translator.count_tokens(block.source_text) for block in blocks]
                )
                plan_sha = self._plan_sha256(
                    blocks,
                    source_transcript_sha256=source_artifact.sha256,
                    model_id=model_id,
                    model_tree_sha256=model_tree_sha256,
                    prompt_template_id=prompt_template_id,
                    token_counts=token_counts,
                )
                self._store.initialize_translation_plan(
                    job.id,
                    blocks,
                    source_language=source.language,
                    target_language="vi",
                    model_id=model_id,
                    model_revision=model_revision,
                    model_tree_sha256=model_tree_sha256,
                    source_transcript_sha256=source_artifact.sha256,
                    plan_sha256=plan_sha,
                    prompt_template_id=prompt_template_id,
                    source_token_counts=token_counts,
                )
            else:
                self._validate_resume_checkpoint(
                    job.id,
                    source_transcript_sha256=source_artifact.sha256,
                    model_id=model_id,
                    model_tree_sha256=model_tree_sha256,
                    prompt_template_id=prompt_template_id,
                )

            glossary = job.spec.get("glossary")
            glossary = glossary if isinstance(glossary, dict) else {}
            previous_vi: str | None = None
            for stored in self._store.list_translation_blocks(job.id):
                if stored.translated_text is not None:
                    previous_vi = stored.translated_text
                    continue
                if self._is_cancel_requested(job.id):
                    raise _StageCancelled()
                block = TranslationBlock(
                    stored.start_us,
                    stored.end_us,
                    stored.source_text,
                    source_ordinals=stored.source_ordinals,
                )
                configure_context = getattr(translator, "configure_translation_context", None)
                if callable(configure_context):
                    configure_context(glossary=glossary, context_before_vi=previous_vi)
                translated_text = await asyncio.to_thread(
                    self._translate_one,
                    translator,
                    block,
                    source.language,
                    job.id,
                    timing_profile,
                )
                if self._is_cancel_requested(job.id):
                    raise _StageCancelled()
                output_tokens = await asyncio.to_thread(
                    translator.count_tokens, translated_text
                )
                self._store.commit_translation_block(
                    job.id,
                    stored.ordinal,
                    translated_text,
                    output_token_count=max(output_tokens, 1),
                )
                previous_vi = translated_text
            return await self._publish_and_commit(
                job.id,
                source_transcript_sha256=source_artifact.sha256,
                duration_us=source.duration_us,
                model_revision=model_revision,
                source_segments=source.segments,
            )
        finally:
            await asyncio.to_thread(translator.close)

    def _translate_one(
        self,
        translator: Translator,
        block: TranslationBlock,
        source_language: str,
        job_id: str,
        timing_profile: TimingProfile,
    ) -> str:
        def progress(_completed: int, _total: int) -> None:
            if self._is_cancel_requested(job_id):
                raise _StageCancelled()

        try:
            duration_translation = getattr(
                translator, "translate_batch_for_durations", None
            )
            if timing_profile is TimingProfile.NATURAL and callable(
                duration_translation
            ):
                outputs = duration_translation(
                    [block.source_text],
                    [block.end_us - block.start_us],
                    source_language=source_language,
                    target_language="vi",
                    on_progress=progress,
                )
                return self._single_translation_output(outputs)
            result = translate_blocks(
                translator,
                (block,),
                source_language=source_language,
                on_progress=progress,
            )
        except LlamaTranslationError as error:
            if error.code != "invalid_output":
                raise
            halves = self._split_retry_block(block)
            if halves is None:
                raise
            if timing_profile is TimingProfile.NATURAL and callable(
                duration_translation
            ):
                outputs = duration_translation(
                    [item.source_text for item in halves],
                    [item.end_us - item.start_us for item in halves],
                    source_language=source_language,
                    target_language="vi",
                    on_progress=progress,
                )
            else:
                outputs = translator.translate_batch(
                    [item.source_text for item in halves],
                    source_language=source_language,
                    target_language="vi",
                    on_progress=progress,
                )
            if len(outputs) != 2 or any(not str(value).strip() for value in outputs):
                raise
            return " ".join(" ".join(str(value).split()) for value in outputs)
        return " ".join(item.translated_text for item in result)

    @staticmethod
    def _single_translation_output(outputs: Any) -> str:
        try:
            values = tuple(outputs)
        except TypeError as error:
            raise TranslationError(
                "translation_output_mismatch",
                "Kết quả dịch theo thời lượng không hợp lệ",
                retryable=True,
            ) from error
        if len(values) != 1:
            raise TranslationError(
                "translation_output_mismatch",
                "Số kết quả dịch không khớp số khối nguồn",
                retryable=True,
            )
        normalized = " ".join(str(values[0]).split())
        if not normalized:
            raise TranslationError(
                "translation_output_empty",
                "Model dịch trả về nội dung rỗng",
                retryable=True,
            )
        return normalized

    async def _publish_and_commit(
        self,
        job_id: str,
        *,
        source_transcript_sha256: str,
        duration_us: int,
        model_revision: str | None,
        source_segments: Sequence[Any] = (),
    ) -> JobRecord:
        if self._is_cancel_requested(job_id):
            raise _StageCancelled()
        rows = self._store.list_translation_blocks(job_id)
        if not rows or any(row.translated_text is None for row in rows):
            raise TranslationError(
                "translation_checkpoint_incomplete",
                "Checkpoint dịch chưa hoàn tất tất cả block",
                retryable=True,
            )
        result = TranslationResult(
            source_language=rows[0].source_language,
            target_language=rows[0].target_language,
            duration_us=duration_us,
            source_transcript_sha256=source_transcript_sha256,
            model_id=rows[0].model_id,
            model_revision=model_revision,
            segments=tuple(
                TranslationSegment(
                    row.start_us,
                    row.end_us,
                    row.source_text,
                    row.translated_text or "",
                    speaker=_speaker_for_row(row, source_segments),
                )
                for row in rows
            ),
        )
        artifact = await asyncio.to_thread(
            write_translation_artifact,
            self._artifact_path(job_id),
            result,
        )
        checkpoint = self._store.get_checkpoint(job_id, JobStage.TRANSLATION)
        payload = dict(checkpoint.payload if checkpoint is not None else {})
        payload.update(
            {
                "artifact_ready": True,
                "artifact_path": str(artifact.path),
                "artifact_sha256": artifact.sha256,
            }
        )
        self._store.save_checkpoint(job_id, JobStage.TRANSLATION, payload)
        if self._is_cancel_requested(job_id):
            raise _StageCancelled()
        return self._store.commit_translation_artifact(
            job_id,
            result,
            artifact_path=artifact.path,
            artifact_sha256=artifact.sha256,
        )

    async def _load_source_artifact(self, job: JobRecord) -> Any:
        raw_digest = job.details.get("source_transcript_sha256")
        if not isinstance(raw_digest, str):
            raise TranscriptError("Thiếu SHA-256 transcript nguồn")
        expected_path = self._job_dir(job.id) / "source-transcript.json"
        return await asyncio.to_thread(
            load_transcript_artifact,
            expected_path,
            expected_sha256=raw_digest,
        )

    async def _load_completed_artifact(
        self,
        job: JobRecord,
        *,
        source_transcript_sha256: str,
        expected_model_id: str,
    ) -> TranslationArtifact | None:
        if job.status is not JobStatus.TRANSLATING:
            return None
        checkpoint = self._store.get_checkpoint(job.id, JobStage.TRANSLATION)
        if checkpoint is None:
            return None
        payload = checkpoint.payload
        if not (payload.get("artifact_ready") is True or payload.get("completed") is True):
            return None
        raw_path = payload.get("artifact_path")
        raw_digest = payload.get("artifact_sha256")
        expected_path = self._artifact_path(job.id).resolve(strict=False)
        if (
            not isinstance(raw_path, str)
            or not isinstance(raw_digest, str)
            or Path(raw_path).resolve(strict=False) != expected_path
        ):
            return None
        artifact = await asyncio.to_thread(
            load_translation_artifact,
            expected_path,
            expected_sha256=raw_digest,
            expected_source_transcript_sha256=source_transcript_sha256,
        )
        if artifact.result.model_id != expected_model_id:
            return None
        return artifact

    def _validate_resume_checkpoint(
        self,
        job_id: str,
        *,
        source_transcript_sha256: str,
        model_id: str,
        model_tree_sha256: str,
        prompt_template_id: str,
    ) -> None:
        checkpoint = self._store.get_checkpoint(job_id, JobStage.TRANSLATION)
        rows = self._store.list_translation_blocks(job_id)
        if (
            checkpoint is None
            or not rows
            or checkpoint.payload.get("source_transcript_sha256")
            != source_transcript_sha256
            or checkpoint.payload.get("model_id") != model_id
            or checkpoint.payload.get("model_tree_sha256") != model_tree_sha256
            or checkpoint.payload.get("prompt_template_id") != prompt_template_id
            or len(rows) != checkpoint.payload.get("block_count")
        ):
            raise TranslationError(
                "translation_checkpoint_invalid",
                "Checkpoint dịch không khớp transcript hoặc model hiện tại",
                retryable=False,
            )

    def _fail(
        self,
        job_id: str,
        code: str,
        message_vi: str,
        retryable: bool,
    ) -> JobRecord:
        current = self._store.get_job(job_id)
        if self._cancelled(current) or current.status is JobStatus.READY_TTS:
            return current
        return self._store.update_status(
            job_id,
            JobStatus.FAILED,
            expected_status=current.status,
            stage=JobStage.TRANSLATION,
            details=current.details,
            error_code=code,
            error_message=message_vi,
            retryable=retryable,
        )

    def _is_cancel_requested(self, job_id: str) -> bool:
        return self._shutdown_requested() or self._cancelled(self._store.get_job(job_id))

    @staticmethod
    def _cancelled(job: JobRecord) -> bool:
        return job.cancel_requested or job.status in {
            JobStatus.CANCELLING,
            JobStatus.CANCELLED,
        }

    def _model_id(self, job: JobRecord) -> str:
        models = job.spec.get("models")
        selected = models.get("translation") if isinstance(models, dict) else None
        if isinstance(selected, str) and selected.strip():
            return selected.strip()
        return self._default_model_id

    @staticmethod
    def _timing_profile(job: JobRecord) -> TimingProfile:
        value = job.spec.get("timing_profile")
        if value is None:
            return TimingProfile.STRICT
        try:
            return TimingProfile(value)
        except (TypeError, ValueError) as error:
            raise TranslationError(
                "timing_profile_invalid",
                "Chế độ căn thời lượng lời thuyết minh không hợp lệ",
                retryable=False,
            ) from error

    @staticmethod
    def _is_vietnamese(language: str) -> bool:
        normalized = language.strip().lower().replace("_", "-")
        return normalized == "vi" or normalized.startswith("vi-")

    @staticmethod
    def _fallback_token_count(text: str) -> int:
        return max(1, len(text.split()))

    @staticmethod
    def _model_file(verified: VerifiedModel) -> Path:
        raw = verified.entry.get("model_file")
        if not isinstance(raw, str) or not raw or "\\" in raw:
            raise ModelRegistryError("Model dịch không khai báo model_file an toàn")
        relative = PurePosixPath(raw)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ModelRegistryError("Model dịch khai báo model_file không an toàn")
        candidate = verified.path.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
            root = verified.path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ModelRegistryError("Không tìm thấy file GGUF đã khóa") from exc
        if not resolved.is_file() or resolved.suffix.casefold() != ".gguf":
            raise ModelRegistryError("File model dịch không phải GGUF")
        return resolved

    @staticmethod
    def _plan_sha256(
        blocks: Any,
        *,
        source_transcript_sha256: str,
        model_id: str,
        model_tree_sha256: str,
        prompt_template_id: str,
        token_counts: Any,
    ) -> str:
        payload = {
            "builder_version": _BUILDER_VERSION,
            "source_transcript_sha256": source_transcript_sha256,
            "model_id": model_id,
            "model_tree_sha256": model_tree_sha256,
            "prompt_template_id": prompt_template_id,
            "blocks": [
                {
                    "start_us": block.start_us,
                    "end_us": block.end_us,
                    "source_text": block.source_text,
                    "source_ordinals": list(block.source_ordinals),
                    "source_token_count": count,
                }
                for block, count in zip(blocks, token_counts, strict=True)
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _split_retry_block(
        block: TranslationBlock,
    ) -> tuple[TranslationBlock, TranslationBlock] | None:
        text = block.source_text
        if len(text) < 2 or block.end_us - block.start_us < 2:
            return None
        midpoint = len(text) // 2
        spaces = [index for index, character in enumerate(text[1:-1], 1) if character.isspace()]
        cut = min(spaces, key=lambda value: abs(value - midpoint)) if spaces else midpoint
        left = " ".join(text[:cut].split())
        right = " ".join(text[cut:].split())
        if not left or not right:
            return None
        split_us = block.start_us + round(
            (block.end_us - block.start_us) * len(left) / (len(left) + len(right))
        )
        split_us = max(block.start_us + 1, min(block.end_us - 1, split_us))
        return (
            TranslationBlock(block.start_us, split_us, left, source_ordinals=block.source_ordinals),
            TranslationBlock(split_us, block.end_us, right, source_ordinals=block.source_ordinals),
        )

    def _artifact_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "translated-transcript.json"

    def _job_dir(self, job_id: str) -> Path:
        return self._jobs_dir / job_id


__all__ = ["TranslationStage"]
