"""Checkpointed Phase 4 cinematic dubbing pipeline.

This stage consumes the hash-authenticated Phase 3 translation artifact and a
local source movie.  It then removes the original dialogue with TIGER-DnR,
synthesizes Vietnamese speech, builds an exact 48 kHz timeline, and publishes
an MP4 containing one copied video stream and one AAC mix.  Every inference
model is resolved through the immutable local model registry before use.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, Protocol

from .audio_mix_export import (
    ExportedMedia,
    ExportProgress,
    FfmpegAudioMixExporter,
    MediaExportError,
    MediaExportErrorCode,
    MixSettings,
)
from .audio_separation import (
    AudioSeparationCancelled,
    AudioSeparationError,
    AudioSeparationMetrics,
    AudioSeparationResult,
    CinematicAudioSeparator,
    SeparationProgress,
    TigerDnrSubprocessRunner,
)
from .lip_sync import (
    ExternalLipSyncProcessor,
    LipSyncBackendPaths,
    LipSyncError,
    LipSyncMode,
    LipSyncResult,
)
from .llama_translation import LlamaServerTranslator, LlamaTranslationError
from .model_registry import (
    ModelNotFoundError,
    ModelRegistryError,
    ModelVerificationError,
    VerifiedModel,
    resolve_verified_model,
)
from .narration import (
    TTS_SILENCE_TRIM_VERSION,
    NarrationError,
    NarrationSynthesizer,
    PersistentVieNeuNarrationSynthesizer,
    PiperNarrationSynthesizer,
    SynthesizedNarration,
    trim_synthesized_narration_silence,
)
from .narration_artifact import (
    NarrationArtifactError,
    build_srt_cues,
    build_timing_report,
    write_srt_artifact,
    write_timing_report,
)
from .state import InvalidTransition, JobRecord, JobStage, JobStatus, StateStore
from .timing import (
    NATURAL_MAX_SILENT_BORROW_US,
    NATURAL_MAX_TOTAL_SPEED,
    NATURAL_SILENT_GAP_GUARD_US,
    FfmpegTimingFitter,
    FittedNarrationBlock,
    NarrationTimingInput,
    TimingError,
    TimingProfile,
    TimingQuality,
    build_timeline_wav,
    plan_narration_slots,
)
from .translation_artifact import (
    TranslationArtifact,
    TranslationArtifactError,
    load_translation_artifact,
)

ModelResolver = Callable[[Path, Path, str, str], VerifiedModel]
SeparatorFactory = Callable[[VerifiedModel], CinematicAudioSeparator]
SynthesizerFactory = Callable[
    [VerifiedModel, VerifiedModel | None], NarrationSynthesizer
]
TimingFitterFactory = Callable[[], FfmpegTimingFitter]
ExporterFactory = Callable[[], FfmpegAudioMixExporter]


class TimingTextRewriter(Protocol):
    """Local model contract used only after measured TTS exceeds its window."""

    def start(self) -> None: ...

    def translate_batch_for_durations(
        self,
        texts: Sequence[str],
        target_durations_us: Sequence[int],
        *,
        source_language: str,
        target_language: str = "vi",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Sequence[str]: ...

    def close(self) -> None: ...

    def abort(self) -> None: ...

    def rewrite_for_duration(
        self,
        source_text: str,
        prior_target_text: str,
        observed_duration_us: int,
        target_duration_us: int,
        max_output_words: int,
        *,
        source_language: str,
        target_language: str = "vi",
        canonical_vi: str | None = None,
        adaptive_attempt: int = 1,
        semantic_recovery: bool = False,
        context_before_vi: str | None = None,
        context_after_vi: str | None = None,
        rejected_candidate_hashes: Sequence[str] = (),
        on_progress: Callable[[int, int], None] | None = None,
    ) -> str: ...


TimingRewriterFactory = Callable[[VerifiedModel], TimingTextRewriter]

_PHASE4_STATUSES = frozenset(
    {
        JobStatus.READY_TTS,
        JobStatus.SEPARATING,
        JobStatus.SYNTHESIZING,
        JobStatus.TIMING,
        JobStatus.MIXING,
        JobStatus.MUXING,
        JobStatus.VERIFYING,
    }
)
_SHA256 = frozenset("0123456789abcdef")
_TIMING_REWRITE_TARGET_FACTORS = (0.90, 0.75, 0.62)
_TIMING_REWRITE_MIN_TARGET_US = 120_000
_TIMING_REWRITE_CANCEL_POLL_SECONDS = 0.20
_TIMING_REWRITE_PROMPT_V1 = "timing-rewrite-v1"
_TIMING_REWRITE_PROMPT_V2 = "timing-rewrite-v2"
_TIMING_REWRITE_PROMPT_V3 = "timing-rewrite-v3"
_TIMING_SEMANTIC_RECOVERY_MAX_ATTEMPTS = 3
_TIMING_SEMANTIC_CONTEXT_WINDOW_US = 2_000_000
_TIMING_REWRITE_RAW_SPEED_MARGIN = 0.97
_TIMING_REWRITE_RAW_DURATION_RESERVE_US = 20_000
_TIMING_REWRITE_ADAPTIVE_DECAY = 0.85
_NATURAL_BASE_PLANNER_POLICY = "natural-base-v1"
_NATURAL_SILENT_SLACK_PLANNER_POLICY = "natural-silent-slack-v2"
_STRICT_PLANNER_POLICY = "strict-v1"
_TIMING_REWRITE_FAILURE_OWNER_STRATEGY = "failure-owner-v1"
_TIMING_REWRITE_GROUP_NEIGHBOR_STRATEGY = "critical-group-neighbor-v1"
_TIMING_REWRITE_SINGLE_SEMANTIC_STRATEGY = "single-block-semantic-v1"


class _StageCancelled(Exception):
    pass


def _next_block_progress(
    *,
    completed: int,
    total: int,
    range_start: int,
    range_size: int,
    last_persisted: int,
) -> int | None:
    """Coalesce per-block database events to visible progress changes."""

    if total <= 0:
        return None
    mapped = range_start + round(min(max(completed, 0), total) * range_size / total)
    return mapped if mapped > last_persisted else None


def build_audio_separator(
    model: VerifiedModel,
    *,
    tiger_source_dir: Path = Path("/opt/tiger"),
    python_executable: str | Path = sys.executable,
    chunk_seconds: float = 120.0,
    context_seconds: float = 4.0,
    batch_size: int = 1,
    device: str = "cuda",
) -> CinematicAudioSeparator:
    """Build production TIGER-DnR from a verified local model directory."""

    backend = str(model.entry.get("backend", "")).strip().casefold()
    if backend not in {"tiger", "tiger-dnr"}:
        raise ModelRegistryError("Backend tách âm thanh không được hỗ trợ")
    source_dir = Path(tiger_source_dir).resolve(strict=False)
    if not source_dir.is_dir() or not (source_dir / "look2hear").is_dir():
        raise ModelRegistryError("Không tìm thấy mã nguồn TIGER-DnR đã khóa")
    return CinematicAudioSeparator(
        TigerDnrSubprocessRunner(
            model_path=model.path,
            model_id=model.model_id,
            model_tree_sha256=model.tree_sha256,
            python_executable=python_executable,
            source_dir=source_dir,
            chunk_seconds=chunk_seconds,
            context_seconds=context_seconds,
            batch_size=batch_size,
            device=device,
        )
    )


def build_narration_synthesizer(
    model: VerifiedModel,
    support_model: VerifiedModel | None = None,
    *,
    vieneu_entrypoint: Path = Path("/opt/vieneu/vieneu-offline.py"),
    python_executable: str | Path = sys.executable,
    piper_binary: str | Path = "piper",
) -> NarrationSynthesizer:
    """Build VieNeu or Piper using verified local artifacts only."""

    backend = str(model.entry.get("backend", "")).strip().casefold()
    if backend in {"vieneu", "vieneu-tts"}:
        if support_model is None:
            raise ModelRegistryError("VieNeu thiếu codec cục bộ đã xác minh")
        raw_entrypoint = model.entry.get("runtime_entrypoint")
        entrypoint = (
            Path(raw_entrypoint)
            if isinstance(raw_entrypoint, str) and raw_entrypoint.strip()
            else Path(vieneu_entrypoint)
        )
        if "://" in str(entrypoint) or "\x00" in str(entrypoint):
            raise ModelRegistryError("Entrypoint VieNeu không phải file cục bộ")
        return PersistentVieNeuNarrationSynthesizer(
            model.path,
            entrypoint,
            python_binary=python_executable,
            environment={"VIENEU_CODEC_PATH": str(support_model.path)},
        )
    if backend == "piper":
        model_file = _verified_entry_file(model, "model_file", suffix=".onnx")
        raw_config = model.entry.get("config_file")
        config_file = (
            _verified_entry_file(model, "config_file", suffix=".json")
            if isinstance(raw_config, str) and raw_config
            else None
        )
        selected_piper_binary: str | Path = piper_binary
        if os.fspath(piper_binary) == "piper":
            executable_name = "piper.exe" if os.name == "nt" else "piper"
            venv_piper = Path(python_executable).absolute().parent / executable_name
            if venv_piper.is_file():
                selected_piper_binary = venv_piper
        return PiperNarrationSynthesizer(
            model_file,
            binary=selected_piper_binary,
            config_path=config_file,
        )
    raise ModelRegistryError("Backend TTS không được hỗ trợ")


def build_timing_text_rewriter(
    model: VerifiedModel,
    *,
    llama_server_binary: Path,
    port: int,
    context_size: int,
    max_output_tokens: int,
    startup_timeout_seconds: float,
    request_timeout_seconds: float,
    gpu_layers: int = -1,
) -> TimingTextRewriter:
    """Build the local duration-aware rewriter used by natural timing."""

    return LlamaServerTranslator(
        llama_server_binary=llama_server_binary,
        model_path=_verified_translation_model_file(model),
        model_id=model.model_id,
        port=port,
        context_size=context_size,
        max_output_tokens=max_output_tokens,
        startup_timeout_seconds=startup_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
        gpu_layers=gpu_layers,
    )


def _verified_translation_model_file(model: VerifiedModel) -> Path:
    raw = model.entry.get("model_file")
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ModelRegistryError("Model dịch không khai báo model_file an toàn")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ModelRegistryError("Model dịch khai báo model_file không an toàn")
    try:
        root = model.path.resolve(strict=True)
        candidate = model.path.joinpath(*relative.parts).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as error:
        raise ModelRegistryError("Không tìm thấy file GGUF đã khóa") from error
    if not candidate.is_file() or candidate.suffix.casefold() != ".gguf":
        raise ModelRegistryError("File model dịch không phải GGUF")
    return candidate


def _verified_entry_file(
    model: VerifiedModel,
    field: str,
    *,
    suffix: str,
) -> Path:
    raw = model.entry.get(field)
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ModelRegistryError(f"Model TTS thiếu {field} an toàn")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ModelRegistryError(f"Model TTS khai báo {field} không an toàn")
    root = model.path.resolve(strict=True)
    candidate = model.path.joinpath(*relative.parts).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ModelRegistryError(f"Model TTS khai báo {field} không an toàn") from error
    if not candidate.is_file() or candidate.suffix.casefold() != suffix:
        raise ModelRegistryError(f"File {field} của model TTS không hợp lệ")
    return candidate


class Phase4Stage:
    """Run separation, Vietnamese TTS, timing, mixing, muxing, and verify."""

    def __init__(
        self,
        *,
        models_lock_path: Path,
        models_dir: Path,
        jobs_dir: Path,
        output_dir: Path,
        default_separation_model_id: str,
        default_tts_model_id: str,
        default_translation_model_id: str,
        tts_support_model_id: str | None,
        store: StateStore,
        tiger_source_dir: Path = Path("/opt/tiger"),
        vieneu_entrypoint: Path = Path("/opt/vieneu/vieneu-offline.py"),
        python_executable: str | Path = sys.executable,
        ffmpeg_binary: str = "ffmpeg",
        ffprobe_binary: str = "ffprobe",
        narration_target_lufs: float = -18.0,
        accompaniment_target_lufs: float = -24.0,
        separation_chunk_seconds: float = 120.0,
        separation_context_seconds: float = 4.0,
        separation_batch_size: int = 1,
        separation_device: str = "cuda",
        separator_factory: SeparatorFactory | None = None,
        synthesizer_factory: SynthesizerFactory | None = None,
        timing_rewriter_factory: TimingRewriterFactory | None = None,
        timing_rewrite_max_attempts: int = 3,
        timing_fitter_factory: TimingFitterFactory | None = None,
        exporter_factory: ExporterFactory | None = None,
        lip_sync_processor: ExternalLipSyncProcessor | None = None,
        model_resolver: ModelResolver = resolve_verified_model,
        shutdown_requested: Callable[[], bool] | None = None,
    ) -> None:
        if (
            not default_separation_model_id.strip()
            or not default_tts_model_id.strip()
            or not default_translation_model_id.strip()
        ):
            raise ValueError("ID model Phase 4 mặc định không được để trống")
        if not 0 <= timing_rewrite_max_attempts <= len(
            _TIMING_REWRITE_TARGET_FACTORS
        ):
            raise ValueError("Số lần tự rút gọn phải từ 0 đến 3")
        self._models_lock_path = Path(models_lock_path)
        self._models_dir = Path(models_dir)
        self._jobs_dir = Path(jobs_dir)
        self._output_dir = Path(output_dir)
        self._default_separation_model_id = default_separation_model_id.strip()
        self._default_tts_model_id = default_tts_model_id.strip()
        self._default_translation_model_id = default_translation_model_id.strip()
        self._tts_support_model_id = (
            tts_support_model_id.strip()
            if isinstance(tts_support_model_id, str) and tts_support_model_id.strip()
            else None
        )
        self._store = store
        self._model_resolver = model_resolver
        self._shutdown_requested = shutdown_requested or (lambda: False)
        self._tiger_source_dir = Path(tiger_source_dir)
        self._vieneu_entrypoint = Path(vieneu_entrypoint)
        self._python_executable = python_executable
        self._ffmpeg_binary = ffmpeg_binary
        self._ffprobe_binary = ffprobe_binary
        self._separation_chunk_seconds = separation_chunk_seconds
        self._separation_context_seconds = separation_context_seconds
        self._separation_batch_size = separation_batch_size
        self._separation_device = separation_device

        self._separator_factory = separator_factory or self._build_separator
        self._synthesizer_factory = synthesizer_factory or self._build_synthesizer
        self._timing_rewriter_factory = timing_rewriter_factory
        self._timing_rewrite_max_attempts = timing_rewrite_max_attempts
        self._timing_fitter_factory = timing_fitter_factory or (
            lambda: FfmpegTimingFitter(ffmpeg_binary=self._ffmpeg_binary)
        )
        self._exporter_factory = exporter_factory or (
            lambda: FfmpegAudioMixExporter(
                ffmpeg_binary=self._ffmpeg_binary,
                ffprobe_binary=self._ffprobe_binary,
                settings=MixSettings(
                    narration_lufs=narration_target_lufs,
                    accompaniment_lufs=accompaniment_target_lufs,
                ),
            )
        )
        self._lip_sync_processor = lip_sync_processor

    async def run(self, job_id: str) -> JobRecord:
        job = self._store.get_job(job_id)
        if job.status is JobStatus.COMPLETED or self._cancelled(job):
            return job
        if job.status not in _PHASE4_STATUSES:
            raise InvalidTransition(
                f"Job {job.id} không ở trạng thái có thể chạy Phase 4"
            )

        try:
            translation = await self._load_translation(job)
            source_media = self._source_media(job)
            source_identity = await asyncio.to_thread(self._source_identity, source_media)
            separation_model = await self._resolve_model(
                self._separation_model_id(job), "separation"
            )
            tts_model = await self._resolve_model(self._tts_model_id(job), "tts")
            tts_support = await self._resolve_tts_support(tts_model)
            timing_profile = self._timing_profile(job)
            self._raise_if_cancelled(job.id)

            separation = await self._ensure_separation(
                job.id,
                source_media=source_media,
                source_identity=source_identity,
                model=separation_model,
            )
            separation_delta_us = abs(
                separation.metrics.duration_us - translation.result.duration_us
            )
            separation_tolerance_us = max(
                2_000_000,
                round(translation.result.duration_us * 0.02),
            )
            if separation_delta_us > separation_tolerance_us:
                raise AudioSeparationError(
                    "audio_separation_duration_mismatch",
                    "Âm thanh nền đã tách không khớp thời lượng video nguồn",
                    retryable=False,
                )
            if separation_delta_us > 100_000:
                warnings = self._store.get_job(job.id).details.get("warnings", [])
                if not any(
                    isinstance(item, Mapping)
                    and item.get("code") == "audio_separation_duration_adjusted"
                    for item in (warnings if isinstance(warnings, list) else [])
                ):
                    self._store.append_warning(
                        job.id,
                        "audio_separation_duration_adjusted",
                        (
                            "Âm thanh nền lệch nhẹ so với container và sẽ được tự căn "
                            "theo timeline video; cảnh báo này không chặn xuất file"
                        ),
                    )
            raw_blocks = await self._ensure_narration_blocks(
                job.id,
                translation=translation,
                model=tts_model,
                support_model=tts_support,
                timing_profile=timing_profile,
            )
            fitted_blocks = await self._ensure_timing_with_rewrites(
                job.id,
                translation=translation,
                model=tts_model,
                support_model=tts_support,
                raw_blocks=raw_blocks,
                timing_profile=timing_profile,
            )
            return await self._export(
                job.id,
                translation=translation,
                separation=separation,
                tts_model=tts_model,
                fitted_blocks=fitted_blocks,
                source_media=source_media,
            )
        except _StageCancelled:
            return self._store.get_job(job_id)
        except AudioSeparationError as error:
            if isinstance(error, AudioSeparationCancelled):
                return self._store.get_job(job_id)
            return self._fail(job_id, error.code, error.message_vi, error.retryable)
        except NarrationError as error:
            if error.code == "tts_cancelled":
                return self._store.get_job(job_id)
            return self._fail(job_id, error.code, error.message_vi, error.retryable)
        except TimingError as error:
            if error.code == "timing_cancelled":
                return self._store.get_job(job_id)
            if error.details:
                self._update_progress(
                    job_id,
                    self._store.get_job(job_id).progress_permille,
                    {"timing_failure": error.details},
                )
            return self._fail(job_id, error.code, error.message_vi, error.retryable)
        except NarrationArtifactError as error:
            if error.code == "artifact_cancelled":
                return self._store.get_job(job_id)
            return self._fail(job_id, error.code, error.message_vi, error.retryable)
        except MediaExportError as error:
            if error.code is MediaExportErrorCode.EXPORT_CANCELLED:
                return self._store.get_job(job_id)
            return self._fail(
                job_id,
                error.code.value,
                error.message_vi,
                error.retryable,
            )
        except LipSyncError as error:
            if error.code == "lip_sync_cancelled":
                return self._store.get_job(job_id)
            return self._fail(job_id, error.code, error.message_vi, error.retryable)
        except ModelNotFoundError:
            return self._fail(
                job_id,
                "phase4_model_not_found",
                (
                    "Model tách âm thanh, TTS hoặc dịch/rút gọn đã chọn không có "
                    "trong danh mục cục bộ"
                ),
                False,
            )
        except (ModelVerificationError, ModelRegistryError):
            return self._fail(
                job_id,
                "phase4_model_verification_failed",
                (
                    "Model Phase 4 hoặc model dịch/rút gọn bị thiếu hay không vượt "
                    "qua kiểm tra toàn vẹn"
                ),
                True,
            )
        except TranslationArtifactError:
            return self._fail(
                job_id,
                "translation_artifact_invalid",
                "Artifact bản dịch đầu vào Phase 4 không hợp lệ",
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
                "phase4_failed",
                "Không thể hoàn tất tách lời, thuyết minh và xuất video",
                True,
            )

    async def process(self, job_id: str) -> JobRecord:
        return await self.run(job_id)

    async def _load_translation(self, job: JobRecord) -> TranslationArtifact:
        raw_digest = job.details.get("translated_transcript_sha256")
        source_digest = job.details.get("source_transcript_sha256")
        if not isinstance(raw_digest, str) or not isinstance(source_digest, str):
            raise TranslationArtifactError("Thiếu SHA-256 artifact bản dịch")
        artifact = await asyncio.to_thread(
            load_translation_artifact,
            self._job_dir(job.id) / "translated-transcript.json",
            expected_sha256=raw_digest,
            expected_source_transcript_sha256=source_digest,
        )
        if artifact.result.target_language.strip().lower() != "vi":
            raise TranslationArtifactError("Phase 4 chỉ chấp nhận bản dịch tiếng Việt")
        return artifact

    async def _resolve_model(self, model_id: str, stage: str) -> VerifiedModel:
        return await asyncio.to_thread(
            self._model_resolver,
            self._models_lock_path,
            self._models_dir,
            model_id,
            stage,
        )

    async def _resolve_tts_support(
        self, tts_model: VerifiedModel
    ) -> VerifiedModel | None:
        backend = str(tts_model.entry.get("backend", "")).strip().casefold()
        if backend not in {"vieneu", "vieneu-tts"}:
            return None
        raw_id = tts_model.entry.get("support_model_id") or self._tts_support_model_id
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise ModelRegistryError("Model VieNeu thiếu model codec hỗ trợ")
        return await self._resolve_model(raw_id.strip(), "tts-support")

    async def _ensure_separation(
        self,
        job_id: str,
        *,
        source_media: Path,
        source_identity: Mapping[str, Any],
        model: VerifiedModel,
    ) -> AudioSeparationResult:
        checkpoint = self._store.get_checkpoint(job_id, JobStage.SEPARATION)
        resumed = await self._load_separation_checkpoint(
            job_id,
            checkpoint.payload if checkpoint is not None else None,
            source_identity=source_identity,
            model=model,
        )
        if resumed is not None:
            return resumed

        self._set_status(
            job_id,
            JobStatus.SEPARATING,
            JobStage.SEPARATION,
            650,
            force_reset=True,
            detail_updates={
                "separation_model_id": model.model_id,
                "phase4_step": "separation",
            },
        )
        separator = self._separator_factory(model)

        def progress(value: SeparationProgress) -> None:
            if self._is_cancel_requested(job_id):
                return
            mapped = 650 + round(value.completed_permille * 85 / 1000)
            self._update_progress(
                job_id,
                mapped,
                {
                    "phase4_step": "separation",
                    "phase4_message": value.message_vi,
                    "separation_progress_permille": value.completed_permille,
                },
            )

        result = await separator.separate(
            source_media,
            self._job_dir(job_id) / "accompaniment.wav",
            cancellation=lambda: self._is_cancel_requested(job_id),
            on_progress=progress,
        )
        self._raise_if_cancelled(job_id)
        self._store.save_checkpoint(
            job_id,
            JobStage.SEPARATION,
            {
                "schema_version": 1,
                "completed": True,
                "source": dict(source_identity),
                "model_id": result.model_id,
                "model_tree_sha256": result.model_tree_sha256,
                "backend": result.backend_name,
                "artifact_path": str(result.path),
                "artifact_sha256": result.sha256,
                "duration_us": result.metrics.duration_us,
                "sample_rate": result.metrics.sample_rate,
                "channels": result.metrics.channels,
                "sample_width_bytes": result.metrics.sample_width_bytes,
                "frame_count": result.metrics.frame_count,
                "metrics": {
                    "elapsed_ms": result.metrics.elapsed_ms,
                    "real_time_factor": result.metrics.real_time_factor,
                    "source_bytes": result.metrics.source_bytes,
                    "output_bytes": result.metrics.output_bytes,
                    "backend": dict(result.metrics.backend),
                },
            },
        )
        self._set_status(
            job_id,
            JobStatus.SYNTHESIZING,
            JobStage.TTS,
            735,
            detail_updates={
                "phase4_step": "tts",
                "accompaniment_path": str(result.path),
                "accompaniment_sha256": result.sha256,
            },
        )
        return result

    async def _load_separation_checkpoint(
        self,
        job_id: str,
        payload: Mapping[str, Any] | None,
        *,
        source_identity: Mapping[str, Any],
        model: VerifiedModel,
    ) -> AudioSeparationResult | None:
        if not payload or payload.get("completed") is not True:
            return None
        if (
            payload.get("source") != dict(source_identity)
            or payload.get("model_id") != model.model_id
            or payload.get("model_tree_sha256") != model.tree_sha256
        ):
            return None
        raw_path = payload.get("artifact_path")
        raw_digest = payload.get("artifact_sha256")
        if not isinstance(raw_path, str) or not self._valid_sha(raw_digest):
            return None
        path = Path(raw_path).resolve(strict=False)
        if path != (self._job_dir(job_id) / "accompaniment.wav"):
            return None
        try:
            digest = await asyncio.to_thread(self._file_sha256, path)
            values = {
                key: int(payload[key])
                for key in (
                    "duration_us",
                    "sample_rate",
                    "channels",
                    "sample_width_bytes",
                    "frame_count",
                )
            }
            stat = path.stat()
        except (OSError, KeyError, TypeError, ValueError):
            return None
        if digest != raw_digest or stat.st_size <= 0:
            return None
        raw_metrics = payload.get("metrics")
        metrics = dict(raw_metrics) if isinstance(raw_metrics, dict) else {}
        return AudioSeparationResult(
            accompaniment_path=path,
            checksum_sha256=digest,
            model_id=model.model_id,
            model_tree_sha256=model.tree_sha256,
            backend_name=str(payload.get("backend") or "tiger-dnr"),
            metrics=AudioSeparationMetrics(
                elapsed_ms=int(metrics.get("elapsed_ms", 0)),
                duration_us=values["duration_us"],
                real_time_factor=float(metrics.get("real_time_factor", 0.0)),
                source_bytes=int(metrics.get("source_bytes", 0)),
                output_bytes=stat.st_size,
                sample_rate=values["sample_rate"],
                channels=values["channels"],
                sample_width_bytes=values["sample_width_bytes"],
                frame_count=values["frame_count"],
                backend=(
                    dict(metrics.get("backend"))
                    if isinstance(metrics.get("backend"), dict)
                    else {}
                ),
            ),
        )

    async def _ensure_narration_blocks(
        self,
        job_id: str,
        *,
        translation: TranslationArtifact,
        model: VerifiedModel,
        support_model: VerifiedModel | None,
        timing_profile: TimingProfile,
    ) -> list[dict[str, Any]]:
        checkpoint = self._store.get_checkpoint(job_id, JobStage.TTS)
        payload = self._valid_tts_checkpoint(
            checkpoint.payload if checkpoint is not None else None,
            translation=translation,
            model=model,
            support_model=support_model,
            timing_profile=timing_profile,
        )
        if payload is None:
            payload = {
                "schema_version": 2,
                "completed": False,
                "translation_sha256": translation.sha256,
                "model_id": model.model_id,
                "model_tree_sha256": model.tree_sha256,
                "backend": str(model.entry.get("backend") or "unknown"),
                "support_model_id": (
                    support_model.model_id if support_model is not None else None
                ),
                "support_model_tree_sha256": (
                    support_model.tree_sha256 if support_model is not None else None
                ),
                "timing_profile": timing_profile.value,
                "silence_trim_version": TTS_SILENCE_TRIM_VERSION,
                "block_count": len(translation.result.segments),
                "timing_rewrites": [],
                "blocks": [],
            }
            self._store.save_checkpoint(job_id, JobStage.TTS, payload)
        elif "timing_profile" not in payload or "timing_rewrites" not in payload:
            payload.setdefault("timing_profile", timing_profile.value)
            payload.setdefault("timing_rewrites", [])
            self._store.save_checkpoint(job_id, JobStage.TTS, payload)

        rewrites_by_ordinal = self._timing_rewrites_by_ordinal(
            payload,
            translation=translation,
        )

        running = self._set_status(
            job_id,
            JobStatus.SYNTHESIZING,
            JobStage.TTS,
            735,
            force_reset=True,
            detail_updates={
                "phase4_step": "tts",
                "tts_model_id": model.model_id,
                "timing_profile": timing_profile.value,
                "tts_completed_blocks": len(payload["blocks"]),
                "tts_block_count": len(translation.result.segments),
            },
        )
        last_persisted_progress = running.progress_permille
        blocks_by_ordinal = {
            int(item["ordinal"]): dict(item)
            for item in payload["blocks"]
            if (
                isinstance(item, dict)
                and isinstance(item.get("ordinal"), int)
                and 0 <= int(item["ordinal"]) < len(translation.result.segments)
            )
        }
        synthesizer: NarrationSynthesizer | None = None
        try:
            if len(blocks_by_ordinal) < len(translation.result.segments):
                synthesizer = self._synthesizer_factory(model, support_model)
            for ordinal, segment in enumerate(translation.result.segments):
                self._raise_if_cancelled(job_id)
                rewrite = rewrites_by_ordinal.get(ordinal)
                narration_text = (
                    str(rewrite["text"])
                    if rewrite is not None
                    else segment.translated_text
                )
                existing = blocks_by_ordinal.get(ordinal)
                if existing is not None and await self._valid_raw_block(
                    existing,
                    job_id=job_id,
                    ordinal=ordinal,
                    text=narration_text,
                    start_us=segment.start_us,
                    end_us=segment.end_us,
                ):
                    continue
                if synthesizer is None:
                    synthesizer = self._synthesizer_factory(model, support_model)
                record = await self._synthesize_block(
                    synthesizer,
                    job_id=job_id,
                    ordinal=ordinal,
                    text=narration_text,
                    start_us=segment.start_us,
                    end_us=segment.end_us,
                    timing_profile=timing_profile,
                )
                blocks_by_ordinal[ordinal] = record
                payload["blocks"] = [
                    blocks_by_ordinal[index] for index in sorted(blocks_by_ordinal)
                ]
                payload["completed"] = len(blocks_by_ordinal) == len(
                    translation.result.segments
                )
                self._store.save_checkpoint(job_id, JobStage.TTS, payload)
                mapped = _next_block_progress(
                    completed=len(blocks_by_ordinal),
                    total=len(translation.result.segments),
                    range_start=735,
                    range_size=115,
                    last_persisted=last_persisted_progress,
                )
                if mapped is not None:
                    self._update_progress(
                        job_id,
                        mapped,
                        {
                            "phase4_step": "tts",
                            "tts_completed_blocks": len(blocks_by_ordinal),
                            "tts_block_count": len(translation.result.segments),
                        },
                    )
                    last_persisted_progress = mapped
        finally:
            if synthesizer is not None:
                await synthesizer.close()
        if len(blocks_by_ordinal) != len(translation.result.segments):
            raise NarrationError(
                "tts_checkpoint_incomplete",
                "Checkpoint TTS chưa hoàn tất tất cả khối thuyết minh",
                retryable=True,
            )
        payload["completed"] = True
        self._store.save_checkpoint(job_id, JobStage.TTS, payload)
        self._raise_if_cancelled(job_id)
        self._set_status(
            job_id,
            JobStatus.TIMING,
            JobStage.TIMING,
            850,
            detail_updates={
                "phase4_step": "timing",
                "tts_completed_blocks": len(blocks_by_ordinal),
            },
        )
        return [
            blocks_by_ordinal[index]
            for index in range(len(translation.result.segments))
        ]

    async def _synthesize_block(
        self,
        synthesizer: NarrationSynthesizer,
        *,
        job_id: str,
        ordinal: int,
        text: str,
        start_us: int,
        end_us: int,
        timing_profile: TimingProfile,
    ) -> dict[str, Any]:
        block_dir = self._job_dir(job_id) / "tts"
        probe_path = block_dir / f"block-{ordinal:05d}.probe.wav"
        final_path = block_dir / f"block-{ordinal:05d}.wav"
        with suppress(OSError):
            probe_path.unlink(missing_ok=True)
        probe = await synthesizer.synthesize(
            text,
            probe_path,
            speed=1.0,
            cancellation=lambda: self._is_cancel_requested(job_id),
        )
        self._raise_if_cancelled(job_id)
        probe = await asyncio.to_thread(trim_synthesized_narration_silence, probe)
        target_us = end_us - start_us
        native_speed = (
            min(1.20, max(0.90, probe.duration_us / target_us))
            if timing_profile is TimingProfile.STRICT
            else 1.0
        )
        final: SynthesizedNarration
        if abs(native_speed - 1.0) > 0.001:
            final = await synthesizer.synthesize(
                text,
                final_path,
                speed=native_speed,
                cancellation=lambda: self._is_cancel_requested(job_id),
            )
            self._raise_if_cancelled(job_id)
            final = await asyncio.to_thread(trim_synthesized_narration_silence, final)
            with suppress(OSError):
                probe_path.unlink(missing_ok=True)
        else:
            try:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(probe.path, final_path)
            except OSError as error:
                raise NarrationError(
                    "tts_output_unavailable",
                    "Không thể lưu khối âm thanh TTS",
                    retryable=True,
                ) from error
            final = SynthesizedNarration(
                path=final_path,
                text=probe.text,
                sample_rate=probe.sample_rate,
                channels=probe.channels,
                sample_width_bytes=probe.sample_width_bytes,
                frame_count=probe.frame_count,
                duration_us=probe.duration_us,
                native_speed=1.0,
                backend=probe.backend,
            )
        self._raise_if_cancelled(job_id)
        return {
            "ordinal": ordinal,
            "start_us": start_us,
            "end_us": end_us,
            # Keep the effective narration script stable across TTS backends.
            # Synthesizers may normalize numbers or punctuation internally;
            # that spoken form belongs to the WAV metadata, not the SRT text.
            "text": text,
            "tts_normalized_text": final.text,
            "text_sha256": self._text_sha256(text),
            "path": str(final.path),
            "sha256": await asyncio.to_thread(self._file_sha256, final.path),
            "duration_us": final.duration_us,
            "sample_rate": final.sample_rate,
            "channels": final.channels,
            "sample_width_bytes": final.sample_width_bytes,
            "frame_count": final.frame_count,
            "native_speed": native_speed,
            "backend": final.backend,
        }

    def _valid_tts_checkpoint(
        self,
        payload: Mapping[str, Any] | None,
        *,
        translation: TranslationArtifact,
        model: VerifiedModel,
        support_model: VerifiedModel | None,
        timing_profile: TimingProfile,
    ) -> dict[str, Any] | None:
        if not isinstance(payload, Mapping):
            return None
        expected_support_id = (
            support_model.model_id if support_model is not None else None
        )
        expected_support_sha = (
            support_model.tree_sha256 if support_model is not None else None
        )
        if (
            payload.get("translation_sha256") != translation.sha256
            or payload.get("model_id") != model.model_id
            or payload.get("model_tree_sha256") != model.tree_sha256
            or payload.get("support_model_id") != expected_support_id
            or payload.get("support_model_tree_sha256") != expected_support_sha
            or payload.get("block_count") != len(translation.result.segments)
            or not isinstance(payload.get("blocks"), list)
            or not self._valid_timing_rewrites(payload, translation=translation)
            or not self._checkpoint_profile_matches(payload, timing_profile)
            or not self._checkpoint_trim_matches(payload, timing_profile)
        ):
            return None
        return dict(payload)

    def _valid_timing_rewrites(
        self,
        payload: Mapping[str, Any],
        *,
        translation: TranslationArtifact,
    ) -> bool:
        raw_rewrites = payload.get("timing_rewrites", [])
        if not isinstance(raw_rewrites, list):
            return False
        seen: set[int] = set()
        for item in raw_rewrites:
            if not isinstance(item, Mapping):
                return False
            ordinal = item.get("ordinal")
            if (
                isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or ordinal in seen
                or not 0 <= ordinal < len(translation.result.segments)
            ):
                return False
            segment = translation.result.segments[ordinal]
            text = item.get("text")
            attempt = item.get("attempt")
            available_us = item.get("available_duration_us")
            target_us = item.get("target_duration_us")
            prompt_version = item.get("prompt_version")
            if (
                not isinstance(text, str)
                or not text.strip()
                or " ".join(text.split()) != text
                or item.get("text_sha256") != self._text_sha256(text)
                or item.get("source_text_sha256")
                != self._text_sha256(segment.source_text)
                or item.get("original_translation_sha256")
                != self._text_sha256(segment.translated_text)
                or isinstance(attempt, bool)
                or not isinstance(attempt, int)
                or not 1 <= attempt <= len(_TIMING_REWRITE_TARGET_FACTORS)
                or isinstance(available_us, bool)
                or not isinstance(available_us, int)
                or available_us <= 0
                or isinstance(target_us, bool)
                or not isinstance(target_us, int)
                or target_us <= 0
                or not isinstance(item.get("model_id"), str)
                or not str(item["model_id"]).strip()
                or not self._valid_sha(item.get("model_tree_sha256"))
                or prompt_version
                not in {
                    _TIMING_REWRITE_PROMPT_V1,
                    _TIMING_REWRITE_PROMPT_V2,
                    _TIMING_REWRITE_PROMPT_V3,
                }
            ):
                return False
            if prompt_version == _TIMING_REWRITE_PROMPT_V2:
                legacy_attempt_count = item.get("legacy_attempt_count")
                adaptive_attempt = item.get("adaptive_attempt")
                max_words = item.get("max_words")
                previous_target_us = item.get("previous_target_duration_us")
                previous_observed_us = item.get("previous_observed_duration_us")
                observed_us = item.get("observed_duration_us")
                if (
                    isinstance(legacy_attempt_count, bool)
                    or not isinstance(legacy_attempt_count, int)
                    or not 0 <= legacy_attempt_count <= len(
                        _TIMING_REWRITE_TARGET_FACTORS
                    )
                    or adaptive_attempt != attempt
                    or isinstance(max_words, bool)
                    or not isinstance(max_words, int)
                    or max_words < 2
                    or not self._valid_sha(item.get("previous_text_sha256"))
                    or isinstance(previous_target_us, bool)
                    or not isinstance(previous_target_us, int)
                    or previous_target_us <= 0
                    or isinstance(previous_observed_us, bool)
                    or not isinstance(previous_observed_us, int)
                    or previous_observed_us <= 0
                    or (
                        observed_us is not None
                        and (
                            isinstance(observed_us, bool)
                            or not isinstance(observed_us, int)
                            or observed_us <= 0
                        )
                    )
                    or not isinstance(item.get("accepted"), bool)
                    or not isinstance(item.get("history"), list)
                    or len(item["history"]) > 6
                ):
                    return False
            elif prompt_version == _TIMING_REWRITE_PROMPT_V3:
                semantic_attempt = item.get("semantic_attempt")
                max_words = item.get("max_words")
                previous_target_us = item.get("previous_target_duration_us")
                previous_observed_us = item.get("previous_observed_duration_us")
                observed_us = item.get("observed_duration_us")
                rejected_hashes = item.get("rejected_candidate_hashes")
                history = item.get("history")
                if (
                    isinstance(semantic_attempt, bool)
                    or not isinstance(semantic_attempt, int)
                    or semantic_attempt != attempt
                    or not 1
                    <= semantic_attempt
                    <= _TIMING_SEMANTIC_RECOVERY_MAX_ATTEMPTS
                    or item.get("semantic_recovery") is not True
                    or isinstance(max_words, bool)
                    or not isinstance(max_words, int)
                    or max_words < 1
                    or not self._valid_sha(item.get("previous_text_sha256"))
                    or isinstance(previous_target_us, bool)
                    or not isinstance(previous_target_us, int)
                    or previous_target_us <= 0
                    or isinstance(previous_observed_us, bool)
                    or not isinstance(previous_observed_us, int)
                    or previous_observed_us <= 0
                    or (
                        observed_us is not None
                        and (
                            isinstance(observed_us, bool)
                            or not isinstance(observed_us, int)
                            or observed_us <= 0
                        )
                    )
                    or not isinstance(item.get("accepted"), bool)
                    or not isinstance(history, list)
                    or len(history) > 12
                    or not isinstance(rejected_hashes, list)
                    or not rejected_hashes
                    or len(rejected_hashes) > 16
                    or any(not self._valid_sha(value) for value in rejected_hashes)
                    or len(set(rejected_hashes)) != len(rejected_hashes)
                    or (
                        item["text_sha256"] in rejected_hashes
                        and item.get("semantic_candidate_pending") is not True
                    )
                    or not isinstance(item.get("semantic_candidate_pending"), bool)
                    or (item.get("context_before_vi") is not None
                    and not isinstance(item.get("context_before_vi"), str))
                    or (item.get("context_after_vi") is not None
                    and not isinstance(item.get("context_after_vi"), str))
                ):
                    return False
            recovery_metadata = (
                "strategy",
                "failure_ordinal",
                "critical_group_start_ordinal",
                "critical_group_end_ordinal",
                "schedule_deficit_us",
            )
            if any(key in item for key in recovery_metadata):
                strategy = item.get("strategy")
                failure_ordinal = item.get("failure_ordinal")
                group_start = item.get("critical_group_start_ordinal")
                group_end = item.get("critical_group_end_ordinal")
                deficit_us = item.get("schedule_deficit_us")
                if (
                    strategy
                    not in {
                        _TIMING_REWRITE_FAILURE_OWNER_STRATEGY,
                        _TIMING_REWRITE_GROUP_NEIGHBOR_STRATEGY,
                        _TIMING_REWRITE_SINGLE_SEMANTIC_STRATEGY,
                    }
                    or isinstance(failure_ordinal, bool)
                    or not isinstance(failure_ordinal, int)
                    or isinstance(group_start, bool)
                    or not isinstance(group_start, int)
                    or isinstance(group_end, bool)
                    or not isinstance(group_end, int)
                    or isinstance(deficit_us, bool)
                    or not isinstance(deficit_us, int)
                    or deficit_us <= 0
                    or not 0 <= group_start <= failure_ordinal <= group_end
                    or not group_start <= ordinal <= group_end
                    or group_end >= len(translation.result.segments)
                    or (
                        strategy == _TIMING_REWRITE_FAILURE_OWNER_STRATEGY
                        and ordinal != failure_ordinal
                    )
                    or (
                        strategy == _TIMING_REWRITE_GROUP_NEIGHBOR_STRATEGY
                        and ordinal == failure_ordinal
                    )
                    or (
                        strategy == _TIMING_REWRITE_SINGLE_SEMANTIC_STRATEGY
                        and (
                            ordinal != failure_ordinal
                            or group_start != failure_ordinal
                            or group_end != failure_ordinal
                        )
                    )
                ):
                    return False
            seen.add(ordinal)
        return True

    def _timing_rewrites_by_ordinal(
        self,
        payload: Mapping[str, Any],
        *,
        translation: TranslationArtifact,
    ) -> dict[int, dict[str, Any]]:
        if not self._valid_timing_rewrites(payload, translation=translation):
            return {}
        return {
            int(item["ordinal"]): dict(item)
            for item in payload.get("timing_rewrites", [])
            if isinstance(item, Mapping)
        }

    async def _valid_raw_block(
        self,
        record: Mapping[str, Any],
        *,
        job_id: str,
        ordinal: int,
        text: str,
        start_us: int,
        end_us: int,
    ) -> bool:
        if (
            record.get("ordinal") != ordinal
            or record.get("start_us") != start_us
            or record.get("end_us") != end_us
            or (
                record.get("text") is not None
                and record.get("text") != text
            )
            or record.get("text_sha256") != self._text_sha256(text)
            or not self._valid_sha(record.get("sha256"))
            or not isinstance(record.get("path"), str)
        ):
            return False
        path = Path(str(record["path"])).resolve(strict=False)
        expected = self._job_dir(job_id) / "tts" / f"block-{ordinal:05d}.wav"
        if path != expected:
            return False
        try:
            return await asyncio.to_thread(self._file_sha256, path) == record["sha256"]
        except OSError:
            return False

    async def _ensure_timing_with_rewrites(
        self,
        job_id: str,
        *,
        translation: TranslationArtifact,
        model: VerifiedModel,
        support_model: VerifiedModel | None,
        raw_blocks: Sequence[Mapping[str, Any]],
        timing_profile: TimingProfile,
    ) -> list[FittedNarrationBlock]:
        current_blocks = list(raw_blocks)
        rewrite_model: VerifiedModel | None = None
        exhausted_candidate_budgets: dict[int, int] = {}
        while True:
            try:
                fitted = await self._ensure_timing(
                    job_id,
                    translation=translation,
                    model=model,
                    raw_blocks=current_blocks,
                    timing_profile=timing_profile,
                )
                self._accept_timing_rewrites(
                    job_id,
                    translation=translation,
                )
                return fitted
            except TimingError as error:
                if (
                    error.code != "timing_rewrite_required"
                    or timing_profile is not TimingProfile.NATURAL
                    or self._timing_rewriter_factory is None
                    or self._timing_rewrite_max_attempts == 0
                ):
                    raise
                ordinal, rewrite_error = self._select_timing_rewrite_candidate(
                    job_id,
                    error,
                    translation=translation,
                    exhausted_candidate_budgets=exhausted_candidate_budgets,
                )
                if rewrite_model is None:
                    job = self._store.get_job(job_id)
                    rewrite_model = await self._resolve_model(
                        self._translation_model_id(job),
                        "mt",
                    )
                try:
                    await self._persist_timing_rewrite(
                        job_id,
                        translation=translation,
                        timing_error=rewrite_error,
                        ordinal=ordinal,
                        tts_model=model,
                        tts_support_model=support_model,
                        rewrite_model=rewrite_model,
                        timing_profile=timing_profile,
                    )
                except TimingError as rewrite_failure:
                    if rewrite_failure.code != "timing_semantic_budget_impossible":
                        raise
                    if self._valid_group_rewrite_candidates(
                        error,
                        block_count=len(translation.result.segments),
                    ) is None:
                        raise
                    available_us = rewrite_error.details.get(
                        "available_duration_us"
                    )
                    if (
                        isinstance(available_us, bool)
                        or not isinstance(available_us, int)
                        or available_us <= 0
                    ):
                        raise
                    # Semantic/minimum-duration exhaustion applies only to this
                    # contributor at this budget. Try the next block in the
                    # same congestion group instead of failing the whole job.
                    exhausted_candidate_budgets[ordinal] = max(
                        available_us,
                        exhausted_candidate_budgets.get(ordinal, 0),
                    )
                    continue
                current_blocks = await self._ensure_narration_blocks(
                    job_id,
                    translation=translation,
                    model=model,
                    support_model=support_model,
                    timing_profile=timing_profile,
                )
                self._record_timing_rewrite_observation(
                    job_id,
                    translation=translation,
                    ordinal=ordinal,
                    observed_duration_us=int(current_blocks[ordinal]["duration_us"]),
                )

    @staticmethod
    def _timing_failure_ordinal(
        error: TimingError,
        *,
        block_count: int,
    ) -> int:
        ordinal = error.details.get("ordinal")
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or not 0 <= ordinal < block_count
        ):
            raise TimingError(
                "timing_rewrite_context_invalid",
                "Không xác định được khối thuyết minh cần rút gọn",
                retryable=True,
            ) from error
        return ordinal

    def _select_timing_rewrite_candidate(
        self,
        job_id: str,
        error: TimingError,
        *,
        translation: TranslationArtifact,
        exhausted_candidate_budgets: Mapping[int, int] | None = None,
    ) -> tuple[int, TimingError]:
        """Choose one bounded rewrite from an elastic planner failure group.

        Older planners only reported one ordinal.  Malformed or legacy detail
        payloads deliberately retain that behaviour instead of guessing at a
        neighbouring subtitle.  A valid congestion group, however, can move
        recovery away from an already-exhausted v2 rewrite and shorten one of
        the other blocks that consumes the same local silence budget.
        """

        block_count = len(translation.result.segments)
        owner = self._timing_failure_ordinal(error, block_count=block_count)
        candidates = self._valid_group_rewrite_candidates(
            error,
            block_count=block_count,
        )
        if candidates is None:
            return owner, error

        checkpoint = self._store.get_checkpoint(job_id, JobStage.TTS)
        payload = checkpoint.payload if checkpoint is not None else {}
        rewrites = self._timing_rewrites_by_ordinal(
            payload,
            translation=translation,
        )
        exhausted_budgets = exhausted_candidate_budgets or {}

        def budget_is_eligible(candidate: Mapping[str, int]) -> bool:
            exhausted_at_us = exhausted_budgets.get(candidate["ordinal"])
            return (
                exhausted_at_us is None
                or candidate["target_available_duration_us"] > exhausted_at_us
            )

        def eligible_v2(candidate: Mapping[str, int]) -> bool:
            previous = rewrites.get(candidate["ordinal"])
            return (
                budget_is_eligible(candidate)
                and previous is not None
                and previous.get("prompt_version") == _TIMING_REWRITE_PROMPT_V2
                and isinstance(previous.get("attempt"), int)
                and not isinstance(previous.get("attempt"), bool)
                and int(previous["attempt"]) < self._timing_rewrite_max_attempts
            )

        def eligible_fresh_or_legacy(candidate: Mapping[str, int]) -> bool:
            previous = rewrites.get(candidate["ordinal"])
            return budget_is_eligible(candidate) and (
                previous is None
                or previous.get("prompt_version") == _TIMING_REWRITE_PROMPT_V1
            )

        selected = next((item for item in candidates if eligible_v2(item)), None)
        if selected is None:
            selected = next(
                (item for item in candidates if eligible_fresh_or_legacy(item)),
                None,
            )
        semantic_recovery = False
        previous_owner = rewrites.get(owner)
        if (
            selected is None
            and self._valid_single_block_semantic_recovery(
                error,
                owner=owner,
                candidates=candidates,
            )
            and previous_owner is not None
            and (
                (
                    previous_owner.get("prompt_version")
                    == _TIMING_REWRITE_PROMPT_V2
                    and previous_owner.get("attempt")
                    == _TIMING_SEMANTIC_RECOVERY_MAX_ATTEMPTS
                )
                or previous_owner.get("prompt_version")
                == _TIMING_REWRITE_PROMPT_V3
            )
        ):
            previous_semantic_attempt = (
                int(previous_owner["semantic_attempt"])
                if previous_owner.get("prompt_version")
                == _TIMING_REWRITE_PROMPT_V3
                else 0
            )
            if previous_semantic_attempt >= _TIMING_SEMANTIC_RECOVERY_MAX_ATTEMPTS:
                self._raise_single_block_semantic_exhausted(error, owner=owner)
            selected = candidates[0]
            semantic_recovery = True
        if selected is None:
            raise TimingError(
                "timing_group_budget_impossible",
                (
                    f"Nhóm thuyết minh {int(error.details['critical_group_start_ordinal']) + 1}–"
                    f"{int(error.details['critical_group_end_ordinal']) + 1} vẫn quá dài sau khi "
                    "đã thử rút gọn tối đa các khối phù hợp"
                ),
                retryable=False,
                details=dict(error.details),
            ) from error

        selected_ordinal = selected["ordinal"]
        details = dict(error.details)
        details.update(
            {
                "ordinal": selected_ordinal,
                "required_duration_us": selected["required_duration_us"],
                "available_duration_us": selected[
                    "target_available_duration_us"
                ],
                "strategy": (
                    _TIMING_REWRITE_SINGLE_SEMANTIC_STRATEGY
                    if semantic_recovery
                    else _TIMING_REWRITE_FAILURE_OWNER_STRATEGY
                    if selected_ordinal == owner
                    else _TIMING_REWRITE_GROUP_NEIGHBOR_STRATEGY
                ),
                "semantic_recovery": semantic_recovery,
            }
        )
        return selected_ordinal, TimingError(
            "timing_rewrite_required",
            error.message_vi,
            retryable=False,
            details=details,
        )

    @staticmethod
    def _valid_single_block_semantic_recovery(
        error: TimingError,
        *,
        owner: int,
        candidates: Sequence[Mapping[str, int]],
    ) -> bool:
        """Allow contextual rewriting only for a true intrinsic one-block overflow."""

        return (
            error.details.get("failure_kind")
            in {"single_window_capacity", "elastic_postvalidation"}
            and error.details.get("critical_group_start_ordinal") == owner
            and error.details.get("critical_group_end_ordinal") == owner
            and len(candidates) == 1
            and candidates[0].get("ordinal") == owner
        )

    @staticmethod
    def _raise_single_block_semantic_exhausted(
        error: TimingError,
        *,
        owner: int,
    ) -> NoReturn:
        raise TimingError(
            "timing_single_block_budget_impossible",
            (
                f"Khối thuyết minh {owner + 1} vẫn quá dài sau khi đã thử "
                "rút gọn ngữ nghĩa theo ngữ cảnh tối đa"
            ),
            retryable=False,
            details={
                **error.details,
                "ordinal": owner,
                "semantic_recovery_attempts": (
                    _TIMING_SEMANTIC_RECOVERY_MAX_ATTEMPTS
                ),
            },
        ) from error

    @staticmethod
    def _valid_group_rewrite_candidates(
        error: TimingError,
        *,
        block_count: int,
    ) -> tuple[dict[str, int], ...] | None:
        """Validate the planner's structured group contract defensively."""

        details = error.details
        failure_ordinal = details.get("failure_ordinal")
        group_start = details.get("critical_group_start_ordinal")
        group_end = details.get("critical_group_end_ordinal")
        deficit_us = details.get("schedule_deficit_us")
        raw_candidates = details.get("rewrite_candidates")
        if (
            isinstance(failure_ordinal, bool)
            or not isinstance(failure_ordinal, int)
            or isinstance(group_start, bool)
            or not isinstance(group_start, int)
            or isinstance(group_end, bool)
            or not isinstance(group_end, int)
            or isinstance(deficit_us, bool)
            or not isinstance(deficit_us, int)
            or deficit_us <= 0
            or not 0 <= group_start <= failure_ordinal <= group_end < block_count
            or details.get("ordinal") != failure_ordinal
            or not isinstance(raw_candidates, list)
            or not raw_candidates
        ):
            return None
        parsed: list[dict[str, int]] = []
        seen: set[int] = set()
        # One congestion failure may rewrite at most three candidates.  The
        # planner already orders them by impact and proximity deterministically.
        for raw in raw_candidates[:3]:
            if not isinstance(raw, Mapping):
                return None
            ordinal = raw.get("ordinal")
            required_us = raw.get("required_duration_us")
            target_us = raw.get("target_available_duration_us")
            work_us = raw.get("work_duration_us")
            if (
                isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or ordinal in seen
                or not group_start <= ordinal <= group_end
                or isinstance(required_us, bool)
                or not isinstance(required_us, int)
                or required_us <= 0
                or isinstance(target_us, bool)
                or not isinstance(target_us, int)
                or target_us != max(1, required_us - deficit_us)
                or isinstance(work_us, bool)
                or not isinstance(work_us, int)
                or work_us <= 0
            ):
                return None
            parsed.append(
                {
                    "ordinal": ordinal,
                    "required_duration_us": required_us,
                    "target_available_duration_us": target_us,
                    "work_duration_us": work_us,
                }
            )
            seen.add(ordinal)
        return tuple(parsed)

    @classmethod
    def _timing_rewrite_seed(
        cls,
        payload: Mapping[str, Any],
        *,
        segment_text: str,
        ordinal: int,
        previous: Mapping[str, Any] | None,
    ) -> tuple[str, int]:
        previous_text = (
            str(previous["text"])
            if previous is not None
            else segment_text
        )
        # The block checkpoint is the latest measured WAV. It may have been
        # regenerated after a missing/corrupt artifact, so prefer it over the
        # rewrite entry's older observation.
        observed_us: int | None = None
        for record in payload.get("blocks", []):
            if not isinstance(record, Mapping) or record.get("ordinal") != ordinal:
                continue
            if cls._raw_narration_text(
                record,
                fallback=segment_text,
            ) != previous_text:
                continue
            candidate = record.get("duration_us")
            if (
                not isinstance(candidate, bool)
                and isinstance(candidate, int)
                and candidate > 0
            ):
                observed_us = candidate
                break
        if observed_us is None:
            candidate = previous.get("observed_duration_us") if previous else None
            if (
                not isinstance(candidate, bool)
                and isinstance(candidate, int)
                and candidate > 0
            ):
                observed_us = candidate
        if observed_us is None:
            raise TimingError(
                "timing_rewrite_measurement_missing",
                "Không tìm thấy thời lượng TTS đã đo để rút gọn thích ứng",
                retryable=True,
            )
        return previous_text, observed_us

    @staticmethod
    def _adaptive_timing_rewrite_plan(
        *,
        available_us: int,
        maximum_total_speed: object,
        previous_text: str,
        previous_target_us: int,
        previous_observed_us: int,
        adaptive_attempt: int,
    ) -> tuple[int, int]:
        if previous_observed_us <= _TIMING_REWRITE_MIN_TARGET_US:
            raise TimingError(
                "timing_semantic_budget_impossible",
                "Lời thuyết minh đã ở thời lượng tối thiểu nên không thể rút gọn thêm",
                retryable=False,
                details={
                    "previous_observed_duration_us": previous_observed_us,
                    "minimum_target_duration_us": _TIMING_REWRITE_MIN_TARGET_US,
                },
            )
        if (
            isinstance(maximum_total_speed, bool)
            or not isinstance(maximum_total_speed, (int, float))
            or not math.isfinite(float(maximum_total_speed))
            or not 1.0 <= float(maximum_total_speed) <= 2.0
        ):
            maximum_total_speed = NATURAL_MAX_TOTAL_SPEED
        raw_budget_us = max(
            _TIMING_REWRITE_MIN_TARGET_US,
            math.floor(
                available_us
                * float(maximum_total_speed)
                * _TIMING_REWRITE_RAW_SPEED_MARGIN
            )
            - _TIMING_REWRITE_RAW_DURATION_RESERVE_US,
        )
        gain = min(
            0.90,
            0.90 * raw_budget_us / previous_observed_us,
        ) * (_TIMING_REWRITE_ADAPTIVE_DECAY ** (adaptive_attempt - 1))
        gain = min(0.90, max(0.05, gain))
        target_us = max(
            _TIMING_REWRITE_MIN_TARGET_US,
            min(
                raw_budget_us,
                math.floor(previous_target_us * 0.90),
                math.floor(previous_target_us * gain),
            ),
        )
        target_us = min(target_us, previous_observed_us - 1)
        previous_word_count = max(1, len(previous_text.split()))
        max_words = max(2, math.floor(previous_word_count * gain))
        if previous_word_count > 2:
            max_words = min(max_words, previous_word_count - 1)
        return target_us, max_words

    @staticmethod
    def _semantic_timing_rewrite_plan(
        *,
        available_us: int,
        maximum_total_speed: object,
        previous_text: str,
        previous_target_us: int,
        previous_observed_us: int,
        semantic_attempt: int,
    ) -> tuple[int, int]:
        """Build a measured one-block budget that may reach one spoken word."""

        if (
            isinstance(semantic_attempt, bool)
            or not 1 <= semantic_attempt <= _TIMING_SEMANTIC_RECOVERY_MAX_ATTEMPTS
        ):
            raise ValueError("semantic_attempt must be between 1 and 3")
        if (
            isinstance(maximum_total_speed, bool)
            or not isinstance(maximum_total_speed, (int, float))
            or not math.isfinite(float(maximum_total_speed))
            or not 1.0 <= float(maximum_total_speed) <= 2.0
        ):
            maximum_total_speed = NATURAL_MAX_TOTAL_SPEED
        raw_budget_us = max(
            _TIMING_REWRITE_MIN_TARGET_US,
            math.floor(
                available_us
                * float(maximum_total_speed)
                * _TIMING_REWRITE_RAW_SPEED_MARGIN
            )
            - _TIMING_REWRITE_RAW_DURATION_RESERVE_US,
        )
        attempt_factor = (0.92, 0.84, 0.76)[semantic_attempt - 1]
        target_us = max(
            _TIMING_REWRITE_MIN_TARGET_US,
            min(
                raw_budget_us,
                previous_observed_us - 1,
                math.floor(previous_observed_us * 0.90),
                math.floor(previous_target_us * attempt_factor),
            ),
        )
        if target_us >= previous_observed_us:
            raise TimingError(
                "timing_single_block_budget_impossible",
                "Lời thuyết minh một khối đã ở thời lượng tối thiểu có thể đo",
                retryable=False,
            )
        previous_word_count = max(1, len(previous_text.split()))
        duration_ratio = min(0.95, raw_budget_us / previous_observed_us)
        max_words = max(
            1,
            math.floor(
                previous_word_count
                * duration_ratio
                * (0.92 ** (semantic_attempt - 1))
            ),
        )
        if previous_word_count > 1:
            max_words = min(max_words, previous_word_count - 1)
        return target_us, max_words

    @classmethod
    def _semantic_rewrite_context(
        cls,
        translation: TranslationArtifact,
        rewrites: Mapping[int, Mapping[str, Any]],
        *,
        ordinal: int,
    ) -> tuple[str | None, str | None]:
        """Return only the nearest Vietnamese neighbour within two seconds."""

        segments = translation.result.segments

        def narration_text(index: int) -> str:
            rewrite = rewrites.get(index)
            return (
                str(rewrite["text"])
                if rewrite is not None and isinstance(rewrite.get("text"), str)
                else segments[index].translated_text
            )

        before: str | None = None
        if ordinal > 0:
            previous = segments[ordinal - 1]
            current = segments[ordinal]
            if current.start_us - previous.end_us <= _TIMING_SEMANTIC_CONTEXT_WINDOW_US:
                before = narration_text(ordinal - 1)
        after: str | None = None
        if ordinal + 1 < len(segments):
            current = segments[ordinal]
            following = segments[ordinal + 1]
            if (
                following.start_us - current.end_us
                <= _TIMING_SEMANTIC_CONTEXT_WINDOW_US
            ):
                after = narration_text(ordinal + 1)
        return before, after

    @classmethod
    def _semantic_rejected_hashes(
        cls,
        previous: Mapping[str, Any],
        *,
        original_text: str,
    ) -> list[str]:
        """Collect every prior candidate hash so semantic recovery cannot cycle."""

        candidates: list[object] = [
            cls._text_sha256(original_text),
            previous.get("text_sha256"),
            previous.get("previous_text_sha256"),
        ]
        raw_rejected = previous.get("rejected_candidate_hashes", [])
        if isinstance(raw_rejected, list):
            candidates.extend(raw_rejected)
        raw_history = previous.get("history", [])
        if isinstance(raw_history, list):
            candidates.extend(
                item.get("text_sha256")
                for item in raw_history
                if isinstance(item, Mapping)
            )
        unique: list[str] = []
        for value in candidates:
            if (
                isinstance(value, str)
                and cls._valid_sha(value)
                and value not in unique
            ):
                unique.append(value)
        return unique[-16:]

    async def _persist_single_block_semantic_rewrite(
        self,
        job_id: str,
        *,
        translation: TranslationArtifact,
        timing_error: TimingError,
        ordinal: int,
        payload: dict[str, Any],
        rewrites: dict[int, dict[str, Any]],
        previous: Mapping[str, Any] | None,
        rewrite_model: VerifiedModel,
    ) -> None:
        retry_payload = copy.deepcopy(payload)
        if (
            previous is None
            or timing_error.details.get("strategy")
            != _TIMING_REWRITE_SINGLE_SEMANTIC_STRATEGY
        ):
            raise TimingError(
                "timing_rewrite_context_invalid",
                "Checkpoint rút gọn ngữ nghĩa một khối không hợp lệ",
                retryable=True,
            )
        available_us = timing_error.details.get("available_duration_us")
        if (
            isinstance(available_us, bool)
            or not isinstance(available_us, int)
            or available_us <= 0
        ):
            raise TimingError(
                "timing_rewrite_context_invalid",
                "Cửa sổ thời lượng rút gọn ngữ nghĩa không hợp lệ",
                retryable=True,
            )
        segment = translation.result.segments[ordinal]
        previous_text, previous_observed_us = self._timing_rewrite_seed(
            payload,
            segment_text=segment.translated_text,
            ordinal=ordinal,
            previous=previous,
        )
        previous_target_us = int(previous["target_duration_us"])
        semantic_attempt = (
            int(previous["semantic_attempt"]) + 1
            if previous.get("prompt_version") == _TIMING_REWRITE_PROMPT_V3
            else 1
        )
        rejected_hashes = self._semantic_rejected_hashes(
            previous,
            original_text=segment.translated_text,
        )
        context_before, context_after = self._semantic_rewrite_context(
            translation,
            rewrites,
            ordinal=ordinal,
        )
        inherited_history = (
            list(previous.get("history", []))
            if isinstance(previous.get("history"), list)
            else []
        )

        def persist_entry(
            *,
            text: str,
            attempt: int,
            target_duration_us: int,
            max_words: int,
            pending: bool,
            history: list[object],
        ) -> None:
            entry = {
                "ordinal": ordinal,
                "text": text,
                "text_sha256": self._text_sha256(text),
                "source_text_sha256": self._text_sha256(segment.source_text),
                "original_translation_sha256": self._text_sha256(
                    segment.translated_text
                ),
                "attempt": attempt,
                "semantic_attempt": attempt,
                "semantic_recovery": True,
                "semantic_candidate_pending": pending,
                "available_duration_us": available_us,
                # A pending ledger still represents the last measured text.
                # Keep its accepted target stable across process death and
                # store the next requested target separately.
                "target_duration_us": (
                    previous_target_us if pending else target_duration_us
                ),
                "semantic_requested_target_duration_us": target_duration_us,
                "max_words": max_words,
                "previous_text_sha256": self._text_sha256(previous_text),
                "previous_target_duration_us": previous_target_us,
                "previous_observed_duration_us": previous_observed_us,
                "observed_duration_us": previous_observed_us if pending else None,
                "model_id": rewrite_model.model_id,
                "model_tree_sha256": rewrite_model.tree_sha256,
                "prompt_version": _TIMING_REWRITE_PROMPT_V3,
                "accepted": False,
                "history": history[-12:],
                "rejected_candidate_hashes": list(rejected_hashes),
                "context_before_vi": context_before,
                "context_after_vi": context_after,
                "strategy": _TIMING_REWRITE_SINGLE_SEMANTIC_STRATEGY,
                "failure_ordinal": timing_error.details["failure_ordinal"],
                "critical_group_start_ordinal": timing_error.details[
                    "critical_group_start_ordinal"
                ],
                "critical_group_end_ordinal": timing_error.details[
                    "critical_group_end_ordinal"
                ],
                "schedule_deficit_us": timing_error.details[
                    "schedule_deficit_us"
                ],
            }
            rewrites[ordinal] = entry
            payload["schema_version"] = 4
            payload["timing_rewrites"] = [
                rewrites[index] for index in sorted(rewrites)
            ]
            payload["completed"] = False
            self._store.save_checkpoint(job_id, JobStage.TTS, payload)

        rewritten: str | None = None
        target_us = 0
        max_words = 0
        while semantic_attempt <= _TIMING_SEMANTIC_RECOVERY_MAX_ATTEMPTS:
            target_us, max_words = self._semantic_timing_rewrite_plan(
                available_us=available_us,
                maximum_total_speed=timing_error.details.get(
                    "maximum_total_speed",
                    NATURAL_MAX_TOTAL_SPEED,
                ),
                previous_text=previous_text,
                previous_target_us=previous_target_us,
                previous_observed_us=previous_observed_us,
                semantic_attempt=semantic_attempt,
            )
            # Persist before invoking llama.cpp. A killed process resumes at
            # the next bounded strategy instead of repeating an inference.
            persist_entry(
                text=previous_text,
                attempt=semantic_attempt,
                target_duration_us=target_us,
                max_words=max_words,
                pending=True,
                history=inherited_history,
            )
            self._update_progress(
                job_id,
                self._store.get_job(job_id).progress_permille,
                {
                    "phase4_step": "timing_rewrite",
                    "timing_rewrite_mode": "semantic_recovery",
                    "phase4_message": (
                        f"Đang rút gọn ngữ nghĩa khối {ordinal + 1} theo ngữ cảnh "
                        f"(chiến lược {semantic_attempt}/"
                        f"{_TIMING_SEMANTIC_RECOVERY_MAX_ATTEMPTS})"
                    ),
                    "timing_rewrite_ordinal": ordinal,
                    "timing_semantic_attempt": semantic_attempt,
                    "timing_rewrite_target_us": target_us,
                    "timing_rewrite_max_words": max_words,
                    "timing_rewrite_previous_duration_us": previous_observed_us,
                },
            )
            try:
                rewritten = await self._rewrite_timing_text(
                    rewrite_model,
                    source_text=segment.source_text,
                    canonical_vi=segment.translated_text,
                    previous_vi=previous_text,
                    source_language=translation.result.source_language,
                    target_duration_us=target_us,
                    observed_duration_us=previous_observed_us,
                    max_words=max_words,
                    adaptive_attempt=semantic_attempt,
                    semantic_recovery=True,
                    context_before_vi=context_before,
                    context_after_vi=context_after,
                    rejected_candidate_hashes=tuple(rejected_hashes),
                    job_id=job_id,
                )
                break
            except TimingError as error:
                if error.code not in {
                    "timing_rewrite_output_empty",
                    "timing_rewrite_output_invalid",
                }:
                    # A handled runtime/network failure produced no semantic
                    # candidate. Restore the exact durable checkpoint so a
                    # normal retry may use the same strategy. An actual
                    # process death cannot reach this rollback and therefore
                    # remains bounded by the pre-call ledger above.
                    self._store.save_checkpoint(
                        job_id,
                        JobStage.TTS,
                        retry_payload,
                    )
                    raise
                candidate_hash = error.details.get("candidate_sha256")
                if (
                    isinstance(candidate_hash, str)
                    and self._valid_sha(candidate_hash)
                    and candidate_hash not in rejected_hashes
                ):
                    rejected_hashes.append(candidate_hash)
                    del rejected_hashes[:-16]
                    persist_entry(
                        text=previous_text,
                        attempt=semantic_attempt,
                        target_duration_us=target_us,
                        max_words=max_words,
                        pending=True,
                        history=inherited_history,
                    )
                # A rejected semantic candidate is a consumed strategy. If a
                # later strategy hits a transient runtime failure, roll back
                # only that later call, never the durable rejected attempt.
                retry_payload = copy.deepcopy(payload)
                semantic_attempt += 1
        if rewritten is None:
            self._raise_single_block_semantic_exhausted(
                timing_error,
                owner=ordinal,
            )

        history = inherited_history
        history.append(
            {
                "text_sha256": self._text_sha256(previous_text),
                "target_duration_us": previous_target_us,
                "observed_duration_us": previous_observed_us,
                "prompt_version": str(previous.get("prompt_version") or ""),
            }
        )
        persist_entry(
            text=rewritten,
            attempt=semantic_attempt,
            target_duration_us=target_us,
            max_words=max_words,
            pending=False,
            history=history,
        )
        self._store.save_checkpoint(job_id, JobStage.TIMING, {})

    async def _persist_timing_rewrite(
        self,
        job_id: str,
        *,
        translation: TranslationArtifact,
        timing_error: TimingError,
        ordinal: int,
        tts_model: VerifiedModel,
        tts_support_model: VerifiedModel | None,
        rewrite_model: VerifiedModel,
        timing_profile: TimingProfile,
    ) -> None:
        checkpoint = self._store.get_checkpoint(job_id, JobStage.TTS)
        payload = self._valid_tts_checkpoint(
            checkpoint.payload if checkpoint is not None else None,
            translation=translation,
            model=tts_model,
            support_model=tts_support_model,
            timing_profile=timing_profile,
        )
        if payload is None:
            raise TimingError(
                "timing_rewrite_checkpoint_invalid",
                "Checkpoint TTS không hợp lệ để tự rút gọn lời thuyết minh",
                retryable=True,
            )
        rewrites = self._timing_rewrites_by_ordinal(
            payload,
            translation=translation,
        )
        previous = rewrites.get(ordinal)
        if timing_error.details.get("semantic_recovery") is True:
            await self._persist_single_block_semantic_rewrite(
                job_id,
                translation=translation,
                timing_error=timing_error,
                ordinal=ordinal,
                payload=payload,
                rewrites=rewrites,
                previous=previous,
                rewrite_model=rewrite_model,
            )
            return
        available_us = timing_error.details.get("available_duration_us")
        if (
            isinstance(available_us, bool)
            or not isinstance(available_us, int)
            or available_us <= 0
        ):
            raise TimingError(
                "timing_rewrite_context_invalid",
                (
                    "Không còn cửa sổ thời lượng hợp lệ gần cảnh để đặt lời thuyết minh; "
                    "hãy chỉnh phụ đề hoặc chọn chế độ thời gian cân bằng"
                ),
                retryable=False,
            )
        segment = translation.result.segments[ordinal]
        previous_text, previous_observed_us = self._timing_rewrite_seed(
            payload,
            segment_text=segment.translated_text,
            ordinal=ordinal,
            previous=previous,
        )
        previous_target_us = (
            int(previous["target_duration_us"])
            if previous is not None
            else previous_observed_us
        )
        previous_prompt = (
            str(previous.get("prompt_version")) if previous is not None else None
        )
        legacy_attempt_count = (
            int(previous["attempt"])
            if previous_prompt == _TIMING_REWRITE_PROMPT_V1
            else int(previous.get("legacy_attempt_count", 0))
            if previous is not None
            else 0
        )
        attempt = (
            int(previous["attempt"]) + 1
            if previous_prompt == _TIMING_REWRITE_PROMPT_V2
            else 1
        )
        while True:
            if attempt > self._timing_rewrite_max_attempts:
                raise TimingError(
                    "timing_semantic_budget_impossible",
                    (
                        f"Khối thuyết minh {ordinal + 1} vẫn quá dài sau "
                        f"{self._timing_rewrite_max_attempts} lần rút gọn thích ứng"
                    ),
                    retryable=False,
                    details={
                        **timing_error.details,
                        "ordinal": ordinal,
                        "legacy_rewrite_attempts": legacy_attempt_count,
                        "adaptive_rewrite_attempts": self._timing_rewrite_max_attempts,
                        "previous_observed_duration_us": previous_observed_us,
                    },
                )
            try:
                target_us, max_words = self._adaptive_timing_rewrite_plan(
                    available_us=available_us,
                    maximum_total_speed=timing_error.details.get(
                        "maximum_total_speed",
                        NATURAL_MAX_TOTAL_SPEED,
                    ),
                    previous_text=previous_text,
                    previous_target_us=previous_target_us,
                    previous_observed_us=previous_observed_us,
                    adaptive_attempt=attempt,
                )
            except TimingError as error:
                if error.code != "timing_semantic_budget_impossible":
                    raise
                raise TimingError(
                    error.code,
                    (
                        f"Khối thuyết minh {ordinal + 1} đã ở thời lượng tối thiểu "
                        "nên không thể rút gọn thêm"
                    ),
                    retryable=False,
                    details={
                        **timing_error.details,
                        **error.details,
                        "ordinal": ordinal,
                    },
                ) from error
            self._update_progress(
                job_id,
                self._store.get_job(job_id).progress_permille,
                {
                    "phase4_step": "timing_rewrite",
                    "phase4_message": (
                        f"Đang rút gọn thích ứng khối {ordinal + 1} còn tối đa "
                        f"{max_words} từ (lần {attempt}/"
                        f"{self._timing_rewrite_max_attempts})"
                    ),
                    "timing_rewrite_ordinal": ordinal,
                    "timing_rewrite_attempt": attempt,
                    "timing_rewrite_target_us": target_us,
                    "timing_rewrite_max_words": max_words,
                    "timing_rewrite_previous_duration_us": previous_observed_us,
                },
            )
            try:
                rewritten = await self._rewrite_timing_text(
                    rewrite_model,
                    source_text=segment.source_text,
                    canonical_vi=segment.translated_text,
                    previous_vi=previous_text,
                    source_language=translation.result.source_language,
                    target_duration_us=target_us,
                    observed_duration_us=previous_observed_us,
                    max_words=max_words,
                    adaptive_attempt=attempt,
                    job_id=job_id,
                )
                break
            except TimingError as error:
                if error.code not in {
                    "timing_rewrite_output_empty",
                    "timing_rewrite_output_invalid",
                }:
                    raise
                attempt += 1
        previous_history = (
            list(previous.get("history", []))
            if previous_prompt == _TIMING_REWRITE_PROMPT_V2
            and isinstance(previous, Mapping)
            and isinstance(previous.get("history"), list)
            else []
        )
        previous_history.append(
            {
                "text_sha256": self._text_sha256(previous_text),
                "target_duration_us": previous_target_us,
                "observed_duration_us": previous_observed_us,
            }
        )
        entry = {
            "ordinal": ordinal,
            "text": rewritten,
            "text_sha256": self._text_sha256(rewritten),
            "source_text_sha256": self._text_sha256(segment.source_text),
            "original_translation_sha256": self._text_sha256(
                segment.translated_text
            ),
            "attempt": attempt,
            "adaptive_attempt": attempt,
            "legacy_attempt_count": legacy_attempt_count,
            "available_duration_us": available_us,
            "target_duration_us": target_us,
            "max_words": max_words,
            "previous_text_sha256": self._text_sha256(previous_text),
            "previous_target_duration_us": previous_target_us,
            "previous_observed_duration_us": previous_observed_us,
            "model_id": rewrite_model.model_id,
            "model_tree_sha256": rewrite_model.tree_sha256,
            "prompt_version": _TIMING_REWRITE_PROMPT_V2,
            "accepted": False,
            "history": previous_history[-6:],
        }
        if timing_error.details.get("strategy") in {
            _TIMING_REWRITE_FAILURE_OWNER_STRATEGY,
            _TIMING_REWRITE_GROUP_NEIGHBOR_STRATEGY,
        }:
            entry.update(
                {
                    "strategy": timing_error.details["strategy"],
                    "failure_ordinal": timing_error.details["failure_ordinal"],
                    "critical_group_start_ordinal": timing_error.details[
                        "critical_group_start_ordinal"
                    ],
                    "critical_group_end_ordinal": timing_error.details[
                        "critical_group_end_ordinal"
                    ],
                    "schedule_deficit_us": timing_error.details[
                        "schedule_deficit_us"
                    ],
                }
            )
        rewrites[ordinal] = entry
        payload["schema_version"] = 3
        payload["timing_rewrites"] = [
            rewrites[index] for index in sorted(rewrites)
        ]
        payload["completed"] = False
        self._store.save_checkpoint(job_id, JobStage.TTS, payload)
        # A shorter block can move neighbouring natural slots. Refitting WAV
        # blocks is cheap and avoids reusing a stale aggregate timeline.
        self._store.save_checkpoint(job_id, JobStage.TIMING, {})

    async def _rewrite_timing_text(
        self,
        model: VerifiedModel,
        *,
        source_text: str,
        canonical_vi: str,
        previous_vi: str,
        source_language: str,
        target_duration_us: int,
        observed_duration_us: int,
        max_words: int,
        adaptive_attempt: int,
        semantic_recovery: bool = False,
        context_before_vi: str | None = None,
        context_after_vi: str | None = None,
        rejected_candidate_hashes: Sequence[str] = (),
        job_id: str,
    ) -> str:
        if self._timing_rewriter_factory is None:  # pragma: no cover - caller guard
            raise TimingError(
                "timing_rewriter_unavailable",
                "Không có bộ rút gọn lời thuyết minh cục bộ",
                retryable=False,
            )
        try:
            rewriter = self._timing_rewriter_factory(model)
        except (OSError, ValueError) as error:
            raise TimingError(
                "timing_rewriter_runtime_invalid",
                "Runtime rút gọn cục bộ bị thiếu hoặc có cấu hình không hợp lệ",
                retryable=True,
            ) from error

        def progress(_completed: int, _total: int) -> None:
            if self._is_cancel_requested(job_id):
                raise _StageCancelled()

        try:
            await self._run_cancellable_rewriter_call(
                rewriter,
                rewriter.start,
                job_id=job_id,
            )
            self._raise_if_cancelled(job_id)
            output = await self._run_cancellable_rewriter_call(
                rewriter,
                lambda: rewriter.rewrite_for_duration(
                    source_text=source_text,
                    prior_target_text=previous_vi,
                    observed_duration_us=observed_duration_us,
                    target_duration_us=target_duration_us,
                    max_output_words=max_words,
                    canonical_vi=canonical_vi,
                    source_language=source_language,
                    adaptive_attempt=adaptive_attempt,
                    semantic_recovery=semantic_recovery,
                    context_before_vi=context_before_vi,
                    context_after_vi=context_after_vi,
                    rejected_candidate_hashes=rejected_candidate_hashes,
                    target_language="vi",
                    on_progress=progress,
                ),
                job_id=job_id,
            )
        except LlamaTranslationError as error:
            if error.code in {
                "critical_fact_missing",
                "duration_constraint_violated",
                "invalid_output",
                "rewrite_unchanged",
                "translation_truncated",
            }:
                raise TimingError(
                    "timing_rewrite_output_invalid",
                    "Model rút gọn trả về lời thuyết minh không hợp lệ hoặc bị cắt",
                    retryable=True,
                ) from error
            raise TimingError(
                "timing_rewrite_translation_failed",
                "Không thể tự rút gọn khối thuyết minh bằng model cục bộ",
                retryable=error.retryable,
            ) from error
        finally:
            pending_error = sys.exception()
            cancellation_cleanup = isinstance(
                pending_error,
                (_StageCancelled, asyncio.CancelledError),
            ) or self._is_cancel_requested(job_id)
            if cancellation_cleanup:
                with suppress(Exception):
                    await asyncio.to_thread(rewriter.abort)
            try:
                await asyncio.to_thread(rewriter.close)
            except Exception as error:
                # A durable/user cancellation has already called ``abort``.
                # Never replace that control-flow signal with a cleanup error,
                # otherwise the worker can report a failed job instead of a
                # promptly cancelled one.
                if not cancellation_cleanup:
                    raise TimingError(
                        "timing_rewriter_cleanup_failed",
                        "Không thể giải phóng model rút gọn trước khi mở lại TTS",
                        retryable=True,
                    ) from error
        self._raise_if_cancelled(job_id)
        if not isinstance(output, str):
            raise TimingError(
                "timing_rewrite_output_invalid",
                "Model rút gọn trả về dữ liệu không hợp lệ",
                retryable=True,
            )
        rewritten = " ".join(output.split())
        if not rewritten:
            raise TimingError(
                "timing_rewrite_output_empty",
                "Model rút gọn trả về lời thuyết minh rỗng",
                retryable=True,
            )
        rewritten_sha256 = self._text_sha256(rewritten)
        if (
            rewritten == " ".join(previous_vi.split())
            or len(rewritten.split()) > max_words
            or rewritten_sha256 in rejected_candidate_hashes
        ):
            raise TimingError(
                "timing_rewrite_output_invalid",
                "Model chưa rút gọn đúng giới hạn số từ đã đo",
                retryable=True,
                details={"candidate_sha256": rewritten_sha256},
            )
        return rewritten

    async def _run_cancellable_rewriter_call(
        self,
        rewriter: TimingTextRewriter,
        operation: Callable[[], Any],
        *,
        job_id: str,
    ) -> Any:
        """Run a blocking llama.cpp call while polling durable cancellation."""

        operation_task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {operation_task},
                    timeout=_TIMING_REWRITE_CANCEL_POLL_SECONDS,
                )
                if operation_task in done:
                    result = await operation_task
                    if self._is_cancel_requested(job_id):
                        with suppress(Exception):
                            await asyncio.to_thread(rewriter.abort)
                        raise _StageCancelled()
                    return result
                if not await asyncio.to_thread(
                    self._is_cancel_requested,
                    job_id,
                ):
                    continue
                # Abort bypasses the normal request lock and kills the local
                # server, interrupting startup/tokenization/generation instead
                # of waiting for their 180-300 second timeouts.
                with suppress(Exception):
                    await asyncio.to_thread(rewriter.abort)
                operation_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await operation_task
                raise _StageCancelled()
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.to_thread(rewriter.abort)
            operation_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await operation_task
            raise

    def _record_timing_rewrite_observation(
        self,
        job_id: str,
        *,
        translation: TranslationArtifact,
        ordinal: int,
        observed_duration_us: int,
    ) -> None:
        checkpoint = self._store.get_checkpoint(job_id, JobStage.TTS)
        if checkpoint is None:
            return
        payload = dict(checkpoint.payload)
        rewrites = self._timing_rewrites_by_ordinal(
            payload,
            translation=translation,
        )
        entry = rewrites.get(ordinal)
        if entry is None or entry.get("observed_duration_us") == observed_duration_us:
            return
        entry["observed_duration_us"] = observed_duration_us
        payload["timing_rewrites"] = [
            rewrites[index] for index in sorted(rewrites)
        ]
        self._store.save_checkpoint(job_id, JobStage.TTS, payload)

    def _accept_timing_rewrites(
        self,
        job_id: str,
        *,
        translation: TranslationArtifact,
    ) -> None:
        checkpoint = self._store.get_checkpoint(job_id, JobStage.TTS)
        if checkpoint is None:
            return
        payload = dict(checkpoint.payload)
        rewrites = self._timing_rewrites_by_ordinal(
            payload,
            translation=translation,
        )
        pending: list[tuple[dict[str, Any], str]] = []
        for entry in rewrites.values():
            if (
                entry.get("prompt_version")
                not in {_TIMING_REWRITE_PROMPT_V2, _TIMING_REWRITE_PROMPT_V3}
                or entry.get("semantic_candidate_pending") is True
                or entry.get("accepted") is True
            ):
                continue
            ordinal = int(entry["ordinal"])
            message = (
                f"Khối thuyết minh {ordinal + 1} đã được rút gọn thích ứng "
                f"còn tối đa {int(entry['max_words'])} từ và đã khớp cửa sổ "
                f"{int(entry['available_duration_us']) / 1_000_000:.2f} giây"
            )
            pending.append((entry, message))
        if not pending:
            return
        job = self._store.get_job(job_id)
        raw_warnings = job.details.get("warnings", [])
        if isinstance(raw_warnings, list):
            existing_warnings = {
                (item.get("code"), item.get("message"))
                for item in raw_warnings
                if isinstance(item, Mapping)
            }
        else:
            existing_warnings = set()
        for _entry, message in pending:
            warning_key = (
                "timing_translation_rewritten_semantic"
                if _entry.get("prompt_version") == _TIMING_REWRITE_PROMPT_V3
                else "timing_translation_rewritten_adaptive",
                (
                    message
                    if _entry.get("prompt_version") != _TIMING_REWRITE_PROMPT_V3
                    else (
                        f"Khối thuyết minh {int(_entry['ordinal']) + 1} đã được "
                        "rút gọn ngữ nghĩa theo ngữ cảnh và đo lại bằng TTS"
                    )
                ),
            )
            if warning_key not in existing_warnings:
                self._store.append_warning(job_id, *warning_key)
                existing_warnings.add(warning_key)
        # Warnings are written idempotently first. If checkpoint persistence
        # fails, resume can safely retry without losing or duplicating them.
        for entry, _message in pending:
            entry["accepted"] = True
        payload["timing_rewrites"] = [
            rewrites[index] for index in sorted(rewrites)
        ]
        self._store.save_checkpoint(job_id, JobStage.TTS, payload)

    async def _ensure_timing(
        self,
        job_id: str,
        *,
        translation: TranslationArtifact,
        model: VerifiedModel,
        raw_blocks: Sequence[Mapping[str, Any]],
        timing_profile: TimingProfile,
    ) -> list[FittedNarrationBlock]:
        narration_texts = tuple(
            self._raw_narration_text(raw, fallback=segment.translated_text)
            for segment, raw in zip(
                translation.result.segments,
                raw_blocks,
                strict=True,
            )
        )
        planning_inputs = tuple(
            NarrationTimingInput(
                start_us=segment.start_us,
                end_us=segment.end_us,
                source_duration_us=int(raw["duration_us"]),
                native_speed=float(raw["native_speed"]),
                source_frame_count=int(raw["frame_count"]),
                source_sample_rate=int(raw["sample_rate"]),
            )
            for segment, raw in zip(
                translation.result.segments, raw_blocks, strict=True
            )
        )
        planner_policy = (
            _NATURAL_BASE_PLANNER_POLICY
            if timing_profile is TimingProfile.NATURAL
            else _STRICT_PLANNER_POLICY
        )
        try:
            planned_slots = await asyncio.to_thread(
                plan_narration_slots,
                planning_inputs,
                duration_us=translation.result.duration_us,
                profile=timing_profile,
            )
        except TimingError as error:
            if (
                timing_profile is not TimingProfile.NATURAL
                or error.code != "timing_rewrite_required"
            ):
                raise
            planned_slots = await asyncio.to_thread(
                plan_narration_slots,
                planning_inputs,
                duration_us=translation.result.duration_us,
                profile=timing_profile,
                maximum_silent_borrow_us=NATURAL_MAX_SILENT_BORROW_US,
                silence_guard_us=NATURAL_SILENT_GAP_GUARD_US,
            )
            planner_policy = _NATURAL_SILENT_SLACK_PLANNER_POLICY
        checkpoint = self._store.get_checkpoint(job_id, JobStage.TIMING)
        payload = self._valid_timing_checkpoint(
            checkpoint.payload if checkpoint is not None else None,
            translation=translation,
            model=model,
            block_count=len(raw_blocks),
            timing_profile=timing_profile,
            planner_policy=planner_policy,
        )
        if payload is None:
            payload = {
                "schema_version": 1,
                "completed": False,
                "translation_sha256": translation.sha256,
                "tts_model_id": model.model_id,
                "tts_model_tree_sha256": model.tree_sha256,
                "timing_profile": timing_profile.value,
                "planner_policy": planner_policy,
                "silence_trim_version": TTS_SILENCE_TRIM_VERSION,
                "block_count": len(raw_blocks),
                "blocks": [],
            }
        elif "timing_profile" not in payload or "planner_policy" not in payload:
            payload.setdefault("timing_profile", timing_profile.value)
            payload.setdefault("planner_policy", planner_policy)
            self._store.save_checkpoint(job_id, JobStage.TIMING, payload)
        running = self._set_status(
            job_id,
            JobStatus.TIMING,
            JobStage.TIMING,
            850,
            force_reset=True,
            detail_updates={"phase4_step": "timing"},
        )
        last_persisted_progress = running.progress_permille
        fitted_by_ordinal: dict[int, FittedNarrationBlock] = {}
        records_by_ordinal: dict[int, dict[str, Any]] = {}
        for raw in payload.get("blocks", []):
            if not isinstance(raw, dict) or not isinstance(raw.get("ordinal"), int):
                continue
            ordinal = int(raw["ordinal"])
            if not 0 <= ordinal < len(translation.result.segments):
                continue
            block = await self._load_fitted_block(
                raw,
                job_id=job_id,
                ordinal=ordinal,
            )
            if block is not None:
                narration_text = narration_texts[ordinal]
                planned = planned_slots[ordinal]
                if (
                    block.start_us != planned.start_us
                    or block.end_us != planned.end_us
                    or block.text != narration_text
                ):
                    continue
                fitted_by_ordinal[ordinal] = block
                records_by_ordinal[ordinal] = dict(raw)

        fitter = self._timing_fitter_factory()
        for ordinal, (narration_text, raw, planned) in enumerate(
            zip(
                narration_texts,
                raw_blocks,
                planned_slots,
                strict=True,
            )
        ):
            self._raise_if_cancelled(job_id)
            if ordinal in fitted_by_ordinal:
                continue
            try:
                fitted = await fitter.fit(
                    Path(str(raw["path"])),
                    self._job_dir(job_id) / "timing" / f"block-{ordinal:05d}.wav",
                    start_us=planned.start_us,
                    end_us=planned.end_us,
                    text=narration_text,
                    native_speed=float(raw["native_speed"]),
                    maximum_total_speed=(
                        NATURAL_MAX_TOTAL_SPEED
                        if timing_profile is TimingProfile.NATURAL
                        else None
                    ),
                    cancellation=lambda: self._is_cancel_requested(job_id),
                )
            except TimingError as error:
                if error.code == "timing_rewrite_required":
                    error.details.setdefault("ordinal", ordinal)
                raise
            fitted_by_ordinal[ordinal] = fitted
            records_by_ordinal[ordinal] = await self._fitted_record(ordinal, fitted)
            payload["blocks"] = [
                records_by_ordinal[index] for index in sorted(records_by_ordinal)
            ]
            # Any rebuilt block invalidates the previously sealed aggregate
            # timeline/SRT/report, even when those files still match their old
            # hashes. Rebuild all aggregate artifacts from the current blocks.
            payload["completed"] = False
            for key in (
                "timeline_path",
                "timeline_sha256",
                "timeline_frame_count",
                "timing_report_path",
                "timing_report_sha256",
                "srt_path",
                "srt_sha256",
            ):
                payload.pop(key, None)
            self._store.save_checkpoint(job_id, JobStage.TIMING, payload)
            mapped = _next_block_progress(
                completed=len(fitted_by_ordinal),
                total=len(raw_blocks),
                range_start=850,
                range_size=50,
                last_persisted=last_persisted_progress,
            )
            if mapped is not None:
                self._update_progress(
                    job_id,
                    mapped,
                    {
                        "phase4_step": "timing",
                        "timing_completed_blocks": len(fitted_by_ordinal),
                        "timing_block_count": len(raw_blocks),
                        "timing_profile": timing_profile.value,
                    },
                )
                last_persisted_progress = mapped

        fitted_blocks = [fitted_by_ordinal[index] for index in range(len(raw_blocks))]
        artifacts_valid = await self._valid_timeline_artifacts(payload, job_id=job_id)
        if not artifacts_valid:
            timeline = await asyncio.to_thread(
                build_timeline_wav,
                fitted_blocks,
                self._job_dir(job_id) / "narration-48k.wav",
                duration_us=translation.result.duration_us,
                cancellation=lambda: self._is_cancel_requested(job_id),
            )
            report = build_timing_report(
                fitted_blocks,
                duration_us=translation.result.duration_us,
                tts_model_id=model.model_id,
                tts_backend=str(model.entry.get("backend") or "unknown"),
            )
            timing_artifact = await asyncio.to_thread(
                write_timing_report,
                self._job_dir(job_id) / "timing-report.json",
                report,
                cancellation=lambda: self._is_cancel_requested(job_id),
            )
            srt_artifact = await asyncio.to_thread(
                write_srt_artifact,
                self._output_dir / f"{job_id}.vi.srt",
                build_srt_cues(fitted_blocks),
                cancellation=lambda: self._is_cancel_requested(job_id),
            )
            payload.update(
                {
                    "completed": True,
                    "timeline_path": str(timeline.path),
                    "timeline_sha256": await asyncio.to_thread(
                        self._file_sha256, timeline.path
                    ),
                    "timeline_frame_count": timeline.frame_count,
                    "timing_report_path": str(timing_artifact.path),
                    "timing_report_sha256": timing_artifact.sha256,
                    "srt_path": str(srt_artifact.path),
                    "srt_sha256": srt_artifact.sha256,
                }
            )
            self._store.save_checkpoint(job_id, JobStage.TIMING, payload)
        self._raise_if_cancelled(job_id)
        if (
            payload.get("completed") is True
            and planner_policy == _NATURAL_SILENT_SLACK_PLANNER_POLICY
        ):
            self._warn_timing_silent_slack_used(job_id)
        quality_counts = {
            value.value: sum(block.quality is value for block in fitted_blocks)
            for value in TimingQuality
        }
        self._set_status(
            job_id,
            JobStatus.MIXING,
            JobStage.MIX,
            900,
            detail_updates={
                "phase4_step": "mix",
                "narration_timeline_path": payload["timeline_path"],
                "timing_report_path": payload["timing_report_path"],
                "vietnamese_srt_path": payload["srt_path"],
                "timing_profile": timing_profile.value,
                "timing_quality": quality_counts,
            },
        )
        return fitted_blocks

    def _valid_timing_checkpoint(
        self,
        payload: Mapping[str, Any] | None,
        *,
        translation: TranslationArtifact,
        model: VerifiedModel,
        block_count: int,
        timing_profile: TimingProfile,
        planner_policy: str,
    ) -> dict[str, Any] | None:
        if not isinstance(payload, Mapping):
            return None
        if (
            payload.get("translation_sha256") != translation.sha256
            or payload.get("tts_model_id") != model.model_id
            or payload.get("tts_model_tree_sha256") != model.tree_sha256
            or payload.get("block_count") != block_count
            or not isinstance(payload.get("blocks"), list)
            or not self._checkpoint_profile_matches(payload, timing_profile)
            or not self._checkpoint_trim_matches(payload, timing_profile)
            or (
                timing_profile is TimingProfile.NATURAL
                and payload.get("planner_policy") != planner_policy
            )
            or (
                timing_profile is TimingProfile.STRICT
                and payload.get("planner_policy")
                not in {None, _STRICT_PLANNER_POLICY}
            )
        ):
            return None
        return dict(payload)

    def _warn_timing_silent_slack_used(self, job_id: str) -> None:
        checkpoint = self._store.get_checkpoint(job_id, JobStage.TIMING)
        if (
            checkpoint is None
            or checkpoint.payload.get("completed") is not True
            or checkpoint.payload.get("planner_policy")
            != _NATURAL_SILENT_SLACK_PLANNER_POLICY
        ):
            return
        job = self._store.get_job(job_id)
        warnings = job.details.get("warnings", [])
        if isinstance(warnings, list) and any(
            isinstance(item, Mapping)
            and item.get("code") == "timing_silent_slack_used"
            for item in warnings
        ):
            return
        self._store.append_warning(
            job_id,
            "timing_silent_slack_used",
            (
                "Timeline đã mượn thêm khoảng lặng an toàn giữa các cảnh để giữ "
                "giọng thuyết minh tự nhiên trong giới hạn tốc độ 1,20×"
            ),
        )

    async def _load_fitted_block(
        self,
        record: Mapping[str, Any],
        *,
        job_id: str,
        ordinal: int,
    ) -> FittedNarrationBlock | None:
        raw_path = record.get("path")
        if not isinstance(raw_path, str) or not self._valid_sha(record.get("sha256")):
            return None
        path = Path(raw_path).resolve(strict=False)
        if path != self._job_dir(job_id) / "timing" / f"block-{ordinal:05d}.wav":
            return None
        try:
            if await asyncio.to_thread(self._file_sha256, path) != record["sha256"]:
                return None
            return FittedNarrationBlock(
                path=path,
                start_us=int(record["start_us"]),
                end_us=int(record["end_us"]),
                text=str(record["text"]),
                source_duration_us=int(record["source_duration_us"]),
                target_frame_count=int(record["target_frame_count"]),
                output_frame_count=int(record["output_frame_count"]),
                native_speed=float(record["native_speed"]),
                atempo_speed=float(record["atempo_speed"]),
                total_speed=float(record["total_speed"]),
                padded_frame_count=int(record["padded_frame_count"]),
                quality=TimingQuality(str(record["quality"])),
            )
        except (OSError, KeyError, TypeError, ValueError, TimingError):
            return None

    async def _fitted_record(
        self, ordinal: int, block: FittedNarrationBlock
    ) -> dict[str, Any]:
        return {
            "ordinal": ordinal,
            "path": str(block.path),
            "sha256": await asyncio.to_thread(self._file_sha256, block.path),
            "start_us": block.start_us,
            "end_us": block.end_us,
            "text": block.text,
            "source_duration_us": block.source_duration_us,
            "target_frame_count": block.target_frame_count,
            "output_frame_count": block.output_frame_count,
            "native_speed": block.native_speed,
            "atempo_speed": block.atempo_speed,
            "total_speed": block.total_speed,
            "padded_frame_count": block.padded_frame_count,
            "quality": block.quality.value,
        }

    async def _valid_timeline_artifacts(
        self,
        payload: Mapping[str, Any],
        *,
        job_id: str,
    ) -> bool:
        fields = (
            (
                "timeline_path",
                "timeline_sha256",
                self._job_dir(job_id) / "narration-48k.wav",
            ),
            (
                "timing_report_path",
                "timing_report_sha256",
                self._job_dir(job_id) / "timing-report.json",
            ),
            (
                "srt_path",
                "srt_sha256",
                self._output_dir.resolve(strict=False) / f"{job_id}.vi.srt",
            ),
        )
        if payload.get("completed") is not True:
            return False
        for path_key, digest_key, expected_path in fields:
            raw_path = payload.get(path_key)
            raw_digest = payload.get(digest_key)
            if not isinstance(raw_path, str) or not self._valid_sha(raw_digest):
                return False
            try:
                path = Path(raw_path).resolve(strict=False)
                if path != expected_path or (
                    await asyncio.to_thread(self._file_sha256, path) != raw_digest
                ):
                    return False
            except OSError:
                return False
        return True

    async def _load_export_checkpoint(
        self,
        payload: Mapping[str, Any],
        *,
        job_id: str,
        expected_duration_us: int,
        translation_sha256: str,
        separation_sha256: str,
        timeline_sha256: str,
        tts_model_tree_sha256: str,
        lip_sync_mode: LipSyncMode,
    ) -> ExportedMedia | None:
        """Reuse only an unchanged MP4 previously verified by the exporter."""

        if payload.get("completed") is not True:
            return None
        bindings = {
            "expected_duration_us": expected_duration_us,
            "translation_sha256": translation_sha256,
            "separation_sha256": separation_sha256,
            "timeline_sha256": timeline_sha256,
            "tts_model_tree_sha256": tts_model_tree_sha256,
            "lip_sync_mode": lip_sync_mode.value,
            "video_track_count": 1,
            "audio_track_count": 1,
            "audio_codec": "aac",
        }
        if any(payload.get(key) != value for key, value in bindings.items()):
            return None
        raw_path = payload.get("output_path")
        raw_digest = payload.get("output_sha256")
        expected_path = (
            self._output_dir.resolve(strict=False) / f"{job_id}.mp4"
        ).absolute()
        if (
            not isinstance(raw_path, str)
            or Path(raw_path).absolute() != expected_path
            or not self._valid_sha(raw_digest)
            or expected_path.is_symlink()
        ):
            return None
        try:
            size_bytes = int(payload["size_bytes"])
            duration_us = int(payload["duration_us"])
            video_start_us = int(payload["video_start_us"])
            audio_start_us = int(payload["audio_start_us"])
            if (
                size_bytes <= 0
                or duration_us <= 0
                or video_start_us < 0
                or audio_start_us < 0
                or expected_path.stat().st_size != size_bytes
                or await asyncio.to_thread(self._file_sha256, expected_path)
                != raw_digest
                or expected_path.is_symlink()
            ):
                return None
        except (OSError, KeyError, TypeError, ValueError):
            return None
        return ExportedMedia(
            path=expected_path,
            duration_us=duration_us,
            video_start_us=video_start_us,
            audio_start_us=audio_start_us,
            audio_codec="aac",
            size_bytes=size_bytes,
        )

    async def _export(
        self,
        job_id: str,
        *,
        translation: TranslationArtifact,
        separation: AudioSeparationResult,
        tts_model: VerifiedModel,
        fitted_blocks: Sequence[FittedNarrationBlock],
        source_media: Path,
    ) -> JobRecord:
        timing = self._store.get_checkpoint(job_id, JobStage.TIMING)
        if timing is None or timing.payload.get("completed") is not True:
            raise TimingError(
                "timing_checkpoint_incomplete",
                "Checkpoint timeline chưa hoàn tất",
                retryable=True,
            )
        lip_sync_mode = self._lip_sync_mode(self._store.get_job(job_id))
        checkpoint = self._store.get_checkpoint(job_id, JobStage.EXPORT)
        exported = (
            None
            if checkpoint is None
            else await self._load_export_checkpoint(
                checkpoint.payload,
                job_id=job_id,
                expected_duration_us=translation.result.duration_us,
                translation_sha256=translation.sha256,
                separation_sha256=separation.checksum_sha256,
                timeline_sha256=str(timing.payload["timeline_sha256"]),
                tts_model_tree_sha256=tts_model.tree_sha256,
                lip_sync_mode=lip_sync_mode,
            )
        )

        def progress(value: ExportProgress) -> None:
            if self._is_cancel_requested(job_id):
                return
            self._update_progress(
                job_id,
                910 + round(value.fraction * 70),
                {
                    "phase4_step": "export",
                    "export_processed_us": value.processed_us,
                    "export_duration_us": value.duration_us,
                },
            )

        if exported is None:
            current = self._store.get_job(job_id)
            self._set_status(
                job_id,
                JobStatus.MUXING,
                JobStage.EXPORT,
                910,
                detail_updates={"phase4_step": "export"},
                force_reset=current.status is JobStatus.VERIFYING,
            )
            exporter = self._exporter_factory()
            output_path = self._output_dir / f"{job_id}.mp4"
            exported = await exporter.export(
                source_media,
                separation.path,
                Path(str(timing.payload["timeline_path"])),
                output_path,
                expected_duration_us=translation.result.duration_us,
                cancellation=lambda: self._is_cancel_requested(job_id),
                on_progress=progress,
            )
            self._raise_if_cancelled(job_id)
            if abs(
                exported.duration_us - translation.result.duration_us
            ) > 100_000:
                warnings = self._store.get_job(job_id).details.get("warnings", [])
                if not any(
                    isinstance(item, Mapping)
                    and item.get("code") == "output_duration_adjusted_to_video"
                    for item in (warnings if isinstance(warnings, list) else [])
                ):
                    self._store.append_warning(
                        job_id,
                        "output_duration_adjusted_to_video",
                        "Timeline đầu ra đã được căn theo luồng hình; phần tiếng thiếu "
                        "được đệm im lặng hoặc phần vượt quá hình được loại bỏ",
                    )
            lip_sync = await self._apply_lip_sync(
                job_id,
                mode=lip_sync_mode,
                exported=exported,
                narration_audio=Path(str(timing.payload["timeline_path"])),
            )
            exported = ExportedMedia(
                path=lip_sync.path,
                duration_us=exported.duration_us,
                video_start_us=exported.video_start_us,
                audio_start_us=exported.audio_start_us,
                audio_codec=exported.audio_codec,
                size_bytes=lip_sync.path.stat().st_size,
            )
        else:
            lip_sync = LipSyncResult(
                path=exported.path,
                mode=lip_sync_mode,
                backend=(
                    str(checkpoint.payload.get("lip_sync_backend"))
                    if checkpoint is not None
                    and isinstance(checkpoint.payload.get("lip_sync_backend"), str)
                    else None
                ),
                applied=bool(
                    checkpoint is not None
                    and checkpoint.payload.get("lip_sync_applied") is True
                ),
                fallback_reason=(
                    str(checkpoint.payload.get("lip_sync_fallback_reason"))
                    if checkpoint is not None
                    and isinstance(
                        checkpoint.payload.get("lip_sync_fallback_reason"), str
                    )
                    else None
                ),
            )
        self._set_status(
            job_id,
            JobStatus.VERIFYING,
            JobStage.LIP_SYNC if lip_sync.applied else JobStage.VERIFY,
            985,
            detail_updates={"phase4_step": "verify"},
            force_reset=True,
        )
        result = await self._result_payload(
            job_id,
            exported=exported,
            translation=translation,
            separation=separation,
            tts_model=tts_model,
            fitted_blocks=fitted_blocks,
            timing_payload=timing.payload,
            lip_sync=lip_sync,
        )
        self._store.save_checkpoint(
            job_id,
            JobStage.EXPORT,
            {
                "schema_version": 1,
                "completed": True,
                "output_path": result["video_path"],
                "output_sha256": result["video_sha256"],
                "duration_us": result["duration_us"],
                "expected_duration_us": translation.result.duration_us,
                "size_bytes": result["size_bytes"],
                "video_start_us": result["video_start_us"],
                "audio_start_us": result["audio_start_us"],
                "video_track_count": 1,
                "audio_track_count": 1,
                "audio_codec": "aac",
                "translation_sha256": translation.sha256,
                "separation_sha256": separation.checksum_sha256,
                "timeline_sha256": str(timing.payload["timeline_sha256"]),
                "tts_model_tree_sha256": tts_model.tree_sha256,
                "lip_sync_mode": lip_sync.mode.value,
                "lip_sync_backend": lip_sync.backend,
                "lip_sync_applied": lip_sync.applied,
                "lip_sync_fallback_reason": lip_sync.fallback_reason,
            },
        )
        current = self._store.get_job(job_id)
        details = {
            **current.details,
            "phase4_step": "completed",
            "output_path": result["video_path"],
            "output_sha256": result["video_sha256"],
        }
        return self._store.update_status(
            job_id,
            JobStatus.COMPLETED,
            expected_status=JobStatus.VERIFYING,
            stage=JobStage.DONE,
            progress_permille=1000,
            details=details,
            result=result,
        )

    async def _apply_lip_sync(
        self,
        job_id: str,
        *,
        mode: LipSyncMode,
        exported: ExportedMedia,
        narration_audio: Path,
    ) -> LipSyncResult:
        if mode is LipSyncMode.OFF:
            return LipSyncResult(
                path=exported.path,
                mode=mode,
                backend=None,
                applied=False,
            )
        if self._lip_sync_processor is None:
            raise LipSyncError(
                "lip_sync_not_configured",
                "Backend tạo khẩu hình chưa được cấu hình",
                retryable=False,
            )
        self._set_status(
            job_id,
            JobStatus.VERIFYING,
            JobStage.LIP_SYNC,
            980,
            detail_updates={
                "phase4_step": "lip_sync",
                "lip_sync_mode": mode.value,
            },
        )
        source_sha256 = await asyncio.to_thread(self._file_sha256, exported.path)
        result = await self._lip_sync_processor.render(
            mode=mode,
            dubbed_video=exported.path,
            narration_audio=narration_audio,
            output_path=exported.path,
            work_dir=self._job_dir(job_id) / "lip-sync",
            cancellation=lambda: self._is_cancel_requested(job_id),
        )
        self._raise_if_cancelled(job_id)
        output_sha256 = await asyncio.to_thread(self._file_sha256, result.path)
        self._store.save_checkpoint(
            job_id,
            JobStage.LIP_SYNC,
            {
                "schema_version": 1,
                "completed": True,
                "mode": result.mode.value,
                "backend": result.backend,
                "applied": result.applied,
                "fallback_reason": result.fallback_reason,
                "source_dubbed_sha256": source_sha256,
                "output_path": str(result.path),
                "output_sha256": output_sha256,
            },
        )
        return result

    async def _result_payload(
        self,
        job_id: str,
        *,
        exported: ExportedMedia,
        translation: TranslationArtifact,
        separation: AudioSeparationResult,
        tts_model: VerifiedModel,
        fitted_blocks: Sequence[FittedNarrationBlock],
        timing_payload: Mapping[str, Any],
        lip_sync: LipSyncResult,
    ) -> dict[str, Any]:
        quality_counts = {
            value.value: sum(block.quality is value for block in fitted_blocks)
            for value in TimingQuality
        }
        return {
            "video_path": str(exported.path),
            "video_sha256": await asyncio.to_thread(
                self._file_sha256, exported.path
            ),
            "size_bytes": exported.size_bytes,
            "duration_us": exported.duration_us,
            "video_track_count": 1,
            "audio_track_count": 1,
            "audio_codec": exported.audio_codec,
            "audio_start_us": exported.audio_start_us,
            "video_start_us": exported.video_start_us,
            "srt_path": str(timing_payload["srt_path"]),
            "srt_sha256": str(timing_payload["srt_sha256"]),
            "timing_report_path": str(timing_payload["timing_report_path"]),
            "timing_report_sha256": str(timing_payload["timing_report_sha256"]),
            "translation_sha256": translation.sha256,
            "separation_model_id": separation.model_id,
            "separation_model_tree_sha256": separation.model_tree_sha256,
            "tts_model_id": tts_model.model_id,
            "tts_model_tree_sha256": tts_model.tree_sha256,
            "timing_profile": str(timing_payload["timing_profile"]),
            "timing_quality": quality_counts,
            "original_dialogue_removed": True,
            "music_and_effects_preserved": True,
            "lip_sync_mode": lip_sync.mode.value,
            "lip_sync_backend": lip_sync.backend,
            "lip_sync_applied": lip_sync.applied,
            "lip_sync_fallback_reason": lip_sync.fallback_reason,
            "job_id": job_id,
        }

    def _set_status(
        self,
        job_id: str,
        status: JobStatus,
        stage: JobStage,
        minimum_progress: int,
        *,
        detail_updates: Mapping[str, Any] | None = None,
        force_reset: bool = False,
    ) -> JobRecord:
        current = self._store.get_job(job_id)
        if self._cancelled(current):
            raise _StageCancelled()
        details = {**current.details, **dict(detail_updates or {})}
        if current.status is status:
            return self._store.update_progress(
                job_id,
                max(current.progress_permille, minimum_progress),
                details=details,
            )
        return self._store.update_status(
            job_id,
            status,
            expected_status=current.status,
            stage=stage,
            progress_permille=max(current.progress_permille, minimum_progress),
            details=details,
            force=force_reset,
        )

    def _update_progress(
        self,
        job_id: str,
        value: int,
        detail_updates: Mapping[str, Any],
    ) -> None:
        current = self._store.get_job(job_id)
        if self._cancelled(current):
            return
        self._store.update_progress(
            job_id,
            max(current.progress_permille, min(value, 999)),
            details={**current.details, **dict(detail_updates)},
        )

    def _fail(
        self,
        job_id: str,
        code: str,
        message_vi: str,
        retryable: bool,
    ) -> JobRecord:
        current = self._store.get_job(job_id)
        if self._cancelled(current) or current.status is JobStatus.COMPLETED:
            return current
        return self._store.update_status(
            job_id,
            JobStatus.FAILED,
            expected_status=current.status,
            stage=current.stage,
            details=current.details,
            error_code=code,
            error_message=message_vi,
            retryable=retryable,
        )

    def _build_separator(self, model: VerifiedModel) -> CinematicAudioSeparator:
        return build_audio_separator(
            model,
            tiger_source_dir=self._tiger_source_dir,
            python_executable=self._python_executable,
            chunk_seconds=self._separation_chunk_seconds,
            context_seconds=self._separation_context_seconds,
            batch_size=self._separation_batch_size,
            device=self._separation_device,
        )

    def _build_synthesizer(
        self,
        model: VerifiedModel,
        support_model: VerifiedModel | None,
    ) -> NarrationSynthesizer:
        return build_narration_synthesizer(
            model,
            support_model,
            vieneu_entrypoint=self._vieneu_entrypoint,
            python_executable=self._python_executable,
        )

    def _source_media(self, job: JobRecord) -> Path:
        raw = job.details.get("source_media_path")
        if not isinstance(raw, str) or "://" in raw or "\x00" in raw:
            raise TranslationArtifactError("Thiếu file media nguồn cục bộ")
        try:
            path = Path(raw).resolve(strict=True)
        except OSError as error:
            raise TranslationArtifactError("Không tìm thấy file media nguồn") from error
        if not path.is_file():
            raise TranslationArtifactError("Không tìm thấy file media nguồn")
        return path

    @staticmethod
    def _source_identity(path: Path) -> dict[str, Any]:
        stat = path.stat()
        return {
            "path": str(path),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

    def _separation_model_id(self, job: JobRecord) -> str:
        return self._selected_model(job, "separation", self._default_separation_model_id)

    def _tts_model_id(self, job: JobRecord) -> str:
        return self._selected_model(job, "tts", self._default_tts_model_id)

    def _translation_model_id(self, job: JobRecord) -> str:
        return self._selected_model(
            job,
            "translation",
            self._default_translation_model_id,
        )

    @staticmethod
    def _timing_profile(job: JobRecord) -> TimingProfile:
        raw = job.spec.get("timing_profile")
        # Jobs created before natural timing existed had strict per-subtitle
        # semantics.  Treating a missing field as strict preserves their sealed
        # checkpoints and makes upgrades safe.
        if raw is None:
            return TimingProfile.STRICT
        try:
            return TimingProfile(raw)
        except (TypeError, ValueError) as error:
            raise TimingError(
                "timing_profile_invalid",
                "Chế độ khớp thời lượng của job không hợp lệ",
                retryable=False,
            ) from error

    @staticmethod
    def _lip_sync_mode(job: JobRecord) -> LipSyncMode:
        raw = job.spec.get("lip_sync_mode", LipSyncMode.OFF.value)
        try:
            return LipSyncMode(raw)
        except (TypeError, ValueError) as error:
            raise LipSyncError(
                "lip_sync_mode_invalid",
                "Chế độ khớp khẩu hình của job không hợp lệ",
                retryable=False,
            ) from error

    @staticmethod
    def _checkpoint_profile_matches(
        payload: Mapping[str, Any], timing_profile: TimingProfile
    ) -> bool:
        stored = payload.get("timing_profile")
        return stored == timing_profile.value or (
            stored is None and timing_profile is TimingProfile.STRICT
        )

    @staticmethod
    def _checkpoint_trim_matches(
        payload: Mapping[str, Any], timing_profile: TimingProfile
    ) -> bool:
        stored = payload.get("silence_trim_version")
        return stored == TTS_SILENCE_TRIM_VERSION or (
            stored is None and timing_profile is TimingProfile.STRICT
        )

    @staticmethod
    def _selected_model(job: JobRecord, stage: str, default: str) -> str:
        models = job.spec.get("models")
        selected = models.get(stage) if isinstance(models, dict) else None
        if isinstance(selected, str) and selected.strip():
            return selected.strip()
        return default

    def _is_cancel_requested(self, job_id: str) -> bool:
        return self._shutdown_requested() or self._cancelled(
            self._store.get_job(job_id)
        )

    def _raise_if_cancelled(self, job_id: str) -> None:
        if self._is_cancel_requested(job_id):
            raise _StageCancelled()

    @staticmethod
    def _cancelled(job: JobRecord) -> bool:
        return job.cancel_requested or job.status in {
            JobStatus.CANCELLING,
            JobStatus.CANCELLED,
        }

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _text_sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def _raw_narration_text(
        cls,
        record: Mapping[str, Any],
        *,
        fallback: str,
    ) -> str:
        raw_text = record.get("text")
        if (
            isinstance(raw_text, str)
            and raw_text.strip()
            and record.get("text_sha256") == cls._text_sha256(raw_text)
        ):
            return raw_text
        return fallback

    @staticmethod
    def _valid_sha(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in _SHA256 for character in value)
        )

    def _job_dir(self, job_id: str) -> Path:
        return (self._jobs_dir / job_id).resolve(strict=False)


def build_phase4_stage(
    settings: Any,
    store: StateStore,
    shutdown: Any,
) -> Phase4Stage:
    """Build the production Phase 4 stage from the shared Settings object."""

    def timing_rewriter_factory(model: VerifiedModel) -> TimingTextRewriter:
        return build_timing_text_rewriter(
            model,
            llama_server_binary=settings.llama_server_binary,
            port=settings.llama_server_port,
            context_size=settings.llama_context_size,
            max_output_tokens=settings.llama_max_output_tokens,
            startup_timeout_seconds=settings.llama_startup_timeout_seconds,
            request_timeout_seconds=settings.llama_request_timeout_seconds,
            gpu_layers=settings.llama_gpu_layers,
        )

    return Phase4Stage(
        models_lock_path=settings.models_lock_path,
        models_dir=settings.models_dir,
        jobs_dir=settings.jobs_dir,
        output_dir=settings.output_dir,
        default_separation_model_id=settings.default_separation_model_id,
        default_tts_model_id=settings.default_tts_model_id,
        default_translation_model_id=settings.default_translation_model_id,
        tts_support_model_id=settings.tts_support_model_id,
        store=store,
        tiger_source_dir=settings.tiger_source_dir,
        vieneu_entrypoint=getattr(
            settings,
            "vieneu_entrypoint",
            Path("/opt/vieneu/vieneu-offline.py"),
        ),
        separation_chunk_seconds=settings.separation_chunk_seconds,
        separation_context_seconds=settings.separation_context_seconds,
        separation_batch_size=settings.separation_batch_size,
        separation_device=settings.compute_mode,
        timing_rewriter_factory=timing_rewriter_factory,
        timing_rewrite_max_attempts=settings.timing_rewrite_max_attempts,
        narration_target_lufs=settings.narration_target_lufs,
        accompaniment_target_lufs=-24.0,
        lip_sync_processor=ExternalLipSyncProcessor(
            paths=LipSyncBackendPaths(
                latentsync_python=settings.latentsync_python,
                latentsync_source=settings.latentsync_source_dir,
                latentsync_checkpoint=settings.latentsync_checkpoint_path,
                latentsync_config=settings.latentsync_config_path,
                musetalk_python=settings.musetalk_python,
                musetalk_source=settings.musetalk_source_dir,
                ffmpeg_binary=Path("ffmpeg"),
            ),
            timeout_seconds=settings.lip_sync_timeout_seconds,
            face_preflight=True,
        ),
        shutdown_requested=shutdown.is_set,
    )


__all__ = [
    "Phase4Stage",
    "build_audio_separator",
    "build_narration_synthesizer",
    "build_phase4_stage",
    "build_timing_text_rewriter",
]
