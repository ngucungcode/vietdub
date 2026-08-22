"""Checkpointed orchestration for the offline transcript stage.

The stage consumes only local files.  A selected subtitle is parsed directly;
the ASR model, audio decoder, and recognizer are not even constructed on that
path.  ASR artifacts are written atomically before the SQLite transcript is
committed, allowing a process restart to reuse a hash-authenticated artifact.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .asr import (
    FasterWhisperRecognizer,
    LanguageDetectionRequired,
    NoSpeechError,
    TranscriptionError,
)
from .audio_decode import AudioDecodeError, FfmpegAudioDecoder
from .domain import SpeechRecognizer, SubtitleFormat, TranscriptionResult
from .model_registry import (
    ModelNotFoundError,
    ModelRegistryError,
    ModelVerificationError,
    VerifiedModel,
    resolve_verified_model,
)
from .state import (
    InvalidTransition,
    JobRecord,
    JobStage,
    JobStatus,
    StateStore,
)
from .transcript import (
    TranscriptArtifact,
    TranscriptError,
    load_transcript_artifact,
    parse_subtitle_file,
    write_transcript_artifact,
)


class AudioDecoder(Protocol):
    async def decode(
        self,
        media_path: Path,
        output_path: Path,
        *,
        expected_duration_us: int,
        audio_stream_index: int | None = None,
        cancellation: Callable[[], bool] | None = None,
    ) -> Any: ...


ModelResolver = Callable[[Path, Path, str, str], VerifiedModel]
AudioDecoderFactory = Callable[[], AudioDecoder]
RecognizerFactory = Callable[[], SpeechRecognizer]


class _StageCancelled(Exception):
    """Internal control flow raised from a synchronous ASR progress callback."""


class TranscriptionStage:
    """Create and durably commit one source-language transcript."""

    def __init__(
        self,
        *,
        models_lock_path: Path,
        models_dir: Path,
        jobs_dir: Path,
        default_asr_model_id: str,
        compute_type: str,
        store: StateStore,
        audio_decoder_factory: AudioDecoderFactory | None = None,
        recognizer_factory: RecognizerFactory | None = None,
        model_resolver: ModelResolver = resolve_verified_model,
        shutdown_requested: Callable[[], bool] | None = None,
    ) -> None:
        model_id = default_asr_model_id.strip()
        if not model_id:
            raise ValueError("Model ASR mặc định không được để trống")
        if not compute_type.strip():
            raise ValueError("Kiểu tính toán ASR không được để trống")
        self._models_lock_path = Path(models_lock_path)
        self._models_dir = Path(models_dir)
        self._jobs_dir = Path(jobs_dir)
        self._default_asr_model_id = model_id
        self._compute_type = compute_type.strip()
        self._store = store
        self._audio_decoder_factory = audio_decoder_factory or FfmpegAudioDecoder
        self._recognizer_factory = recognizer_factory or FasterWhisperRecognizer
        self._model_resolver = model_resolver
        self._shutdown_requested = shutdown_requested or (lambda: False)

    async def run(self, job_id: str) -> JobRecord:
        """Advance one READY_OFFLINE/transcribing job to READY_TRANSLATION."""

        job = self._store.get_job(job_id)
        if self._shutdown_requested() or self._cancelled(job):
            return job
        if job.status is JobStatus.READY_TRANSLATION:
            return job
        if job.status not in {
            JobStatus.READY_OFFLINE,
            JobStatus.TRANSCRIBING,
            JobStatus.SUBTITLE_SELECTED,
        }:
            raise InvalidTransition(
                f"Job {job.id} không ở trạng thái có thể tạo transcript"
            )

        try:
            duration_us = self._duration_us(job)
            source = self._transcript_source(job)
            model_id = self._asr_model_id(job) if source == "asr" else None
            resumed = await self._load_checkpoint_artifact(
                job,
                source=source,
                duration_us=duration_us,
                model_id=model_id,
            )
            if resumed is not None:
                running = self._ensure_running_status(job.id, resumed.result.source)
                if self._is_cancel_requested(job.id):
                    return self._store.get_job(job.id)
                if resumed.result.source == "asr":
                    self._update_asr_progress(
                        running.id,
                        step="finalizing",
                        processed_us=duration_us,
                        duration_us=duration_us,
                        segment_count=len(resumed.result.segments),
                        stage_progress_permille=1000,
                        overall_progress_permille=449,
                    )
                return self._commit(running, resumed)

            if source == "subtitle":
                return await self._run_subtitle(job, duration_us=duration_us)
            return await self._run_asr(
                job,
                duration_us=duration_us,
                model_id=model_id or self._default_asr_model_id,
            )
        except _StageCancelled:
            return self._store.get_job(job_id)
        except LanguageDetectionRequired as error:
            return self._require_language(job_id, error)
        except NoSpeechError as error:
            return self._fail(job_id, error.code, error.message_vi, error.retryable)
        except AudioDecodeError as error:
            return self._fail(job_id, error.code, error.message_vi, error.retryable)
        except TranscriptionError as error:
            return self._fail(job_id, error.code, error.message_vi, error.retryable)
        except ModelNotFoundError:
            return self._fail(
                job_id,
                "model_not_found",
                "Model ASR đã chọn không có trong danh mục cục bộ",
                False,
            )
        except (ModelVerificationError, ModelRegistryError):
            return self._fail(
                job_id,
                "model_verification_failed",
                "Model ASR cục bộ bị thiếu hoặc không vượt qua kiểm tra toàn vẹn",
                True,
            )
        except TranscriptError:
            return self._fail(
                job_id,
                "transcript_invalid",
                "Không thể tạo transcript hợp lệ",
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
                "transcription_stage_failed",
                "Không thể hoàn tất bước tạo transcript",
                True,
            )

    async def process(self, job_id: str) -> JobRecord:
        """Alias used by worker orchestrators."""

        return await self.run(job_id)

    async def _run_subtitle(
        self,
        job: JobRecord,
        *,
        duration_us: int,
    ) -> JobRecord:
        if job.status is JobStatus.SUBTITLE_SELECTED:
            # A selected subtitle without a reusable artifact is safe to parse
            # again; parsing is deterministic and has no external side effects.
            running = job
        elif job.status is JobStatus.READY_OFFLINE:
            running = job
        else:
            raise InvalidTransition("Trạng thái subtitle transcript không hợp lệ")

        try:
            subtitle_path = self._required_path(
                running.details.get("source_subtitle_path"),
                "Không tìm thấy file phụ đề nguồn",
            )
            selected = running.details.get("selected_subtitle")
            selected_data = selected if isinstance(selected, dict) else {}
            language = self._subtitle_language(running, selected_data)
            subtitle_format = selected_data.get("format")
            result = await asyncio.to_thread(
                parse_subtitle_file,
                subtitle_path,
                language=language,
                duration_us=duration_us,
                subtitle_format=(
                    SubtitleFormat(str(subtitle_format))
                    if isinstance(subtitle_format, str) and subtitle_format
                    else None
                ),
            )
        except (TranscriptError, OSError, ValueError) as error:
            if (
                running.status is JobStatus.READY_OFFLINE
                and str(running.spec.get("subtitle_mode", "prefer")) == "prefer"
            ):
                warned = self._store.append_warning(
                    running.id,
                    "subtitle_transcript_invalid",
                    "Phụ đề không tạo được transcript; hệ thống chuyển sang ASR cục bộ",
                )
                details = {
                    **warned.details,
                    "transcript_source": "asr",
                    "subtitle_fallback_reason": "subtitle_transcript_invalid",
                }
                transcribing = self._store.update_status(
                    warned.id,
                    JobStatus.TRANSCRIBING,
                    expected_status=JobStatus.READY_OFFLINE,
                    stage=JobStage.ASR,
                    progress_permille=max(warned.progress_permille, 275),
                    details=details,
                )
                return await self._run_asr(
                    transcribing,
                    duration_us=duration_us,
                    model_id=self._asr_model_id(transcribing),
                )
            raise TranscriptError("Không thể đọc transcript từ phụ đề đã chọn") from error

        if self._is_cancel_requested(running.id):
            return self._store.get_job(running.id)
        running = self._ensure_running_status(running.id, "subtitle")
        return await self._publish_and_commit(running, result)

    async def _run_asr(
        self,
        job: JobRecord,
        *,
        duration_us: int,
        model_id: str,
    ) -> JobRecord:
        running = self._ensure_running_status(job.id, "asr")
        if self._is_cancel_requested(job.id):
            return self._store.get_job(job.id)
        self._update_asr_progress(
            running.id,
            step="preparing",
            processed_us=0,
            duration_us=duration_us,
            segment_count=0,
            stage_progress_permille=0,
            overall_progress_permille=275,
        )

        verified = await asyncio.to_thread(
            self._model_resolver,
            self._models_lock_path,
            self._models_dir,
            model_id,
            "asr",
        )
        if self._is_cancel_requested(job.id):
            return self._store.get_job(job.id)

        media_path = self._required_path(
            running.details.get("source_media_path"),
            "Không tìm thấy file media nguồn",
        )
        selected_media = self._selected_media(running)
        audio_stream_index = self._optional_non_negative_int(
            selected_media.get("audio_stream_index")
        )
        self._update_asr_progress(
            running.id,
            step="decoding",
            processed_us=0,
            duration_us=duration_us,
            segment_count=0,
            stage_progress_permille=0,
            overall_progress_permille=275,
        )
        pcm_path = self._job_dir(running.id) / "source-audio-16k.wav"
        decoder = self._audio_decoder_factory()
        decoded = await decoder.decode(
            media_path,
            pcm_path,
            expected_duration_us=duration_us,
            audio_stream_index=audio_stream_index,
            cancellation=lambda: self._is_cancel_requested(running.id),
        )
        if self._is_cancel_requested(running.id):
            return self._store.get_job(running.id)

        recognizer = self._recognizer_factory()
        self._update_asr_progress(
            running.id,
            step="recognizing",
            processed_us=0,
            duration_us=duration_us,
            segment_count=0,
            stage_progress_permille=0,
            overall_progress_permille=275,
        )
        last_persisted_end_us = 0
        last_persisted_segment_count = 0
        last_persisted_permille = 0
        received_progress = False

        def progress(end_us: int, segment_count: int) -> None:
            nonlocal last_persisted_end_us
            nonlocal last_persisted_segment_count
            nonlocal last_persisted_permille
            nonlocal received_progress
            if self._is_cancel_requested(running.id):
                raise _StageCancelled()

            bounded_end_us = min(max(int(end_us), 0), duration_us)
            bounded_segment_count = max(int(segment_count), 0)
            stage_progress = min(
                999,
                round(bounded_end_us * 1000 / duration_us),
            )
            should_persist = (
                not received_progress
                or stage_progress - last_persisted_permille >= 10
                or bounded_end_us - last_persisted_end_us >= 2_000_000
                or bounded_segment_count - last_persisted_segment_count >= 10
            )
            if not should_persist:
                return

            overall_progress = 275 + round(stage_progress * 174 / 1000)
            updated = self._update_asr_progress(
                running.id,
                step="recognizing",
                processed_us=bounded_end_us,
                duration_us=duration_us,
                segment_count=bounded_segment_count,
                stage_progress_permille=stage_progress,
                overall_progress_permille=overall_progress,
            )
            if self._shutdown_requested() or self._cancelled(updated):
                raise _StageCancelled()
            received_progress = True
            last_persisted_end_us = bounded_end_us
            last_persisted_segment_count = bounded_segment_count
            last_persisted_permille = stage_progress

        result = await asyncio.to_thread(
            recognizer.transcribe,
            Path(decoded.path),
            model_path=Path(verified.path),
            model_id=model_id,
            compute_type=self._compute_type,
            language=self._source_language(running),
            duration_us=duration_us,
            on_progress=progress,
        )
        if self._is_cancel_requested(running.id):
            return self._store.get_job(running.id)
        self._update_asr_progress(
            running.id,
            step="finalizing",
            processed_us=duration_us,
            duration_us=duration_us,
            segment_count=len(result.segments),
            stage_progress_permille=1000,
            overall_progress_permille=449,
        )
        return await self._publish_and_commit(running, result)

    def _update_asr_progress(
        self,
        job_id: str,
        *,
        step: str,
        processed_us: int,
        duration_us: int,
        segment_count: int,
        stage_progress_permille: int,
        overall_progress_permille: int,
    ) -> JobRecord:
        """Persist bounded ASR progress without advancing the transcript stage."""

        current = self._store.get_job(job_id)
        if self._shutdown_requested() or self._cancelled(current):
            raise _StageCancelled()
        details = {
            **current.details,
            "asr_step": step,
            "asr_processed_us": min(max(processed_us, 0), duration_us),
            "asr_duration_us": duration_us,
            "asr_segment_count": max(segment_count, 0),
            "asr_progress_permille": min(max(stage_progress_permille, 0), 1000),
        }
        bounded_overall = min(max(overall_progress_permille, 275), 449)
        return self._store.update_status(
            job_id,
            JobStatus.TRANSCRIBING,
            expected_status=JobStatus.TRANSCRIBING,
            progress_permille=max(current.progress_permille, bounded_overall),
            details=details,
        )

    async def _publish_and_commit(
        self,
        running: JobRecord,
        result: TranscriptionResult,
    ) -> JobRecord:
        if self._is_cancel_requested(running.id):
            return self._store.get_job(running.id)
        artifact = await asyncio.to_thread(
            write_transcript_artifact,
            self._artifact_path(running.id),
            result,
        )
        if self._is_cancel_requested(running.id):
            return self._store.get_job(running.id)

        # This pre-commit checkpoint is intentional.  A crash after the atomic
        # JSON rename can resume without loading a model or decoding audio.
        self._store.save_checkpoint(
            running.id,
            JobStage.ASR,
            {
                "schema_version": 1,
                "artifact_ready": True,
                "source": result.source,
                "duration_us": result.duration_us,
                "model_id": result.model_id,
                "artifact_path": str(artifact.path),
                "artifact_sha256": artifact.sha256,
            },
        )
        if self._is_cancel_requested(running.id):
            return self._store.get_job(running.id)
        return self._commit(running, artifact)

    def _commit(self, running: JobRecord, artifact: TranscriptArtifact) -> JobRecord:
        expected = (
            JobStatus.TRANSCRIBING
            if artifact.result.source == "asr"
            else JobStatus.SUBTITLE_SELECTED
        )
        return self._store.commit_transcript(
            running.id,
            artifact.result,
            artifact_path=artifact.path,
            artifact_sha256=artifact.sha256,
            expected_status=expected,
        )

    async def _load_checkpoint_artifact(
        self,
        job: JobRecord,
        *,
        source: str,
        duration_us: int,
        model_id: str | None,
    ) -> TranscriptArtifact | None:
        checkpoint = self._store.get_checkpoint(job.id, JobStage.ASR)
        if checkpoint is None:
            return None
        payload = checkpoint.payload
        if not (payload.get("artifact_ready") is True or payload.get("completed") is True):
            return None
        raw_path = payload.get("artifact_path")
        raw_digest = payload.get("artifact_sha256")
        if not isinstance(raw_path, str) or not isinstance(raw_digest, str):
            return None
        expected_path = self._artifact_path(job.id).resolve(strict=False)
        if Path(raw_path).resolve(strict=False) != expected_path:
            return None
        try:
            artifact = await asyncio.to_thread(
                load_transcript_artifact,
                expected_path,
                expected_sha256=raw_digest,
            )
        except TranscriptError:
            return None
        result = artifact.result
        if result.source != source or result.duration_us != duration_us:
            return None
        if source == "asr" and result.model_id != model_id:
            return None
        if source == "subtitle" and result.model_id is not None:
            return None
        return artifact

    def _ensure_running_status(self, job_id: str, source: str) -> JobRecord:
        current = self._store.get_job(job_id)
        if self._cancelled(current):
            return current
        target = (
            JobStatus.TRANSCRIBING if source == "asr" else JobStatus.SUBTITLE_SELECTED
        )
        if current.status is target:
            return current
        if current.status is not JobStatus.READY_OFFLINE:
            raise InvalidTransition("Trạng thái tạo transcript đã thay đổi")
        return self._store.update_status(
            current.id,
            target,
            expected_status=JobStatus.READY_OFFLINE,
            stage=(JobStage.ASR if source == "asr" else JobStage.SUBTITLE),
            progress_permille=max(current.progress_permille, 275),
            details=current.details,
        )

    def _require_language(
        self,
        job_id: str,
        error: LanguageDetectionRequired,
    ) -> JobRecord:
        current = self._store.get_job(job_id)
        if self._cancelled(current):
            return current
        candidates = [
            {"language": language, "probability": probability}
            for language, probability in error.alternatives
        ]
        if not candidates and error.detected_language:
            candidates.append(
                {
                    "language": error.detected_language,
                    "probability": error.probability,
                }
            )
        details = {
            **current.details,
            "source_language_detected": error.detected_language,
            "source_language_probability": error.probability,
            "detected_language": error.detected_language,
            "language_candidates": candidates,
        }
        return self._store.update_status(
            job_id,
            JobStatus.NEEDS_LANGUAGE,
            expected_status=JobStatus.TRANSCRIBING,
            stage=JobStage.ASR,
            details=details,
        )

    def _fail(
        self,
        job_id: str,
        code: str,
        message_vi: str,
        retryable: bool,
    ) -> JobRecord:
        current = self._store.get_job(job_id)
        if self._cancelled(current) or current.status is JobStatus.READY_TRANSLATION:
            return current
        return self._store.update_status(
            job_id,
            JobStatus.FAILED,
            expected_status=current.status,
            stage=JobStage.ASR,
            details=current.details,
            error_code=code,
            error_message=message_vi,
            retryable=retryable,
        )

    def _is_cancel_requested(self, job_id: str) -> bool:
        return self._shutdown_requested() or self._cancelled(
            self._store.get_job(job_id)
        )

    @staticmethod
    def _cancelled(job: JobRecord) -> bool:
        return job.cancel_requested or job.status in {
            JobStatus.CANCELLING,
            JobStatus.CANCELLED,
        }

    def _artifact_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "source-transcript.json"

    def _job_dir(self, job_id: str) -> Path:
        return self._jobs_dir / job_id

    @staticmethod
    def _transcript_source(job: JobRecord) -> str:
        source = job.details.get("transcript_source")
        if source not in {"asr", "subtitle"}:
            raise TranscriptionError(
                "invalid_transcript_source",
                "Nguồn transcript của job không hợp lệ",
                retryable=False,
            )
        return str(source)

    def _asr_model_id(self, job: JobRecord) -> str:
        models = job.spec.get("models")
        selected = models.get("asr") if isinstance(models, dict) else None
        if isinstance(selected, str) and selected.strip():
            return selected.strip()
        return self._default_asr_model_id

    @staticmethod
    def _selected_media(job: JobRecord) -> dict[str, Any]:
        media = job.details.get("selected_media")
        if not isinstance(media, dict):
            raise TranscriptionError(
                "media_checkpoint_missing",
                "Thiếu checkpoint media để tạo transcript",
                retryable=False,
            )
        return media

    def _duration_us(self, job: JobRecord) -> int:
        raw = self._selected_media(job).get("duration_us")
        if isinstance(raw, bool):
            raw = None
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise TranscriptionError(
                "invalid_media_duration",
                "Thời lượng media không hợp lệ",
                retryable=False,
            ) from exc
        if value <= 0:
            raise TranscriptionError(
                "invalid_media_duration",
                "Thời lượng media không hợp lệ",
                retryable=False,
            )
        return value

    @staticmethod
    def _required_path(value: object, message_vi: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise TranscriptionError(
                "source_artifact_missing",
                message_vi,
                retryable=True,
            )
        return Path(value)

    @staticmethod
    def _optional_non_negative_int(value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise TranscriptionError(
                "invalid_audio_stream",
                "Chỉ số luồng âm thanh không hợp lệ",
                retryable=False,
            )
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise TranscriptionError(
                "invalid_audio_stream",
                "Chỉ số luồng âm thanh không hợp lệ",
                retryable=False,
            ) from exc
        if parsed < 0:
            raise TranscriptionError(
                "invalid_audio_stream",
                "Chỉ số luồng âm thanh không hợp lệ",
                retryable=False,
            )
        return parsed

    @staticmethod
    def _subtitle_language(job: JobRecord, selected: dict[str, Any]) -> str:
        candidates = (
            selected.get("language"),
            TranscriptionStage._selected_media(job).get("source_language"),
            job.spec.get("source_language"),
        )
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip().lower() not in {
                "",
                "auto",
                "und",
                "unknown",
            }:
                return candidate.strip()
        raise TranscriptionError(
            "source_language_missing",
            "Không xác định được ngôn ngữ của phụ đề nguồn",
            retryable=False,
        )

    @staticmethod
    def _source_language(job: JobRecord) -> str | None:
        media = TranscriptionStage._selected_media(job)
        candidates = (
            job.details.get("source_language_selected"),
            job.details.get("source_language_override"),
            media.get("source_language"),
            job.spec.get("source_language"),
        )
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            normalized = candidate.strip().lower().replace("_", "-")
            if normalized in {"auto", "und", "unknown", "mul", "zxx"}:
                continue
            return normalized
        return None


__all__ = ["TranscriptionStage"]
