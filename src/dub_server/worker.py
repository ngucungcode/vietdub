"""Offline GPU worker for checkpointed ASR, translation, and dubbing stages."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import threading
import time
from contextlib import suppress
from dataclasses import replace
from typing import Any

from .asr import FasterWhisperRecognizer
from .config import Settings, get_settings, load_model_catalog
from .gpu import GpuPreflightError, GpuPreflightReport, inspect_gpu, write_gpu_report
from .offline import install_offline_network_guard
from .phase4_stage import Phase4Stage, build_phase4_stage
from .state import InvalidTransition, JobRecord, JobStatus, StateStore
from .transcription_stage import TranscriptionStage
from .translation_stage import TranslationStage

_TRANSCRIPT_QUEUE = (
    JobStatus.READY_OFFLINE,
    JobStatus.TRANSCRIBING,
    JobStatus.SUBTITLE_SELECTED,
    JobStatus.CANCELLING,
)

_TRANSLATION_QUEUE = (
    JobStatus.READY_TRANSLATION,
    JobStatus.TRANSLATING,
)

_PHASE4_QUEUE = (
    JobStatus.READY_TTS,
    JobStatus.SEPARATING,
    JobStatus.SYNTHESIZING,
    JobStatus.TIMING,
    JobStatus.MIXING,
    JobStatus.MUXING,
    JobStatus.VERIFYING,
)


def _inspect_configured_gpu(settings: Settings) -> GpuPreflightReport:
    """Inspect CUDA strictly for GPU mode and diagnostically for CPU mode."""

    report = inspect_gpu(
        require_gpu=settings.compute_mode == "gpu",
        expected_gpu_uuid=(
            settings.selected_gpu_uuid if settings.compute_mode == "gpu" else None
        ),
        expected_cuda_architecture=(
            settings.selected_cuda_architecture
            if settings.compute_mode == "gpu"
            else None
        ),
        expected_cuda_toolkit_version=(
            settings.selected_cuda_toolkit_version
            if settings.compute_mode == "gpu"
            else None
        ),
    )
    if settings.compute_mode == "cpu":
        return replace(
            report,
            ready=True,
            enforced=False,
            selected_gpu_uuid=None,
            gpus=(),
            errors=(),
        )
    return report


def _selected_model_id(
    record: JobRecord,
    key: str,
    default_model_id: str,
) -> str:
    models = record.spec.get("models")
    selected = models.get(key) if isinstance(models, dict) else None
    if isinstance(selected, str) and selected.strip():
        return selected.strip()
    return default_model_id


def _ensure_selected_models_fit_vram(
    store: StateStore,
    record: JobRecord,
    *,
    settings: Settings | None,
    report: GpuPreflightReport | None,
    stages: tuple[str, ...],
) -> bool:
    """Fail one queued job before model load when its locked VRAM floor cannot fit."""

    if (
        not stages
        or settings is None
        or settings.compute_mode == "cpu"
        or report is None
        or not report.gpus
    ):
        return True
    available_vram_mib = report.gpus[0].memory_total_mib
    try:
        catalog = load_model_catalog(settings.models_lock_path, settings.models_dir)
    except (OSError, ValueError) as error:
        current = store.get_job(record.id)
        try:
            store.update_status(
                current.id,
                JobStatus.FAILED,
                expected_status=current.status,
                stage=current.stage,
                details=current.details,
                error_code="model_catalog_invalid",
                error_message="Danh mục model cục bộ bị thiếu hoặc không hợp lệ",
                retryable=True,
            )
        except InvalidTransition:
            # Cancellation can win after the guard reads the queued record.
            # Leave the newer state untouched and let the normal cancellation
            # reconciliation path handle it on the next worker iteration.
            _print_event(
                "job.model_guard_stale_state",
                job_id=current.id,
                observed_status=current.status.value,
            )
            return False
        _print_event(
            "job.rejected_model_catalog",
            job_id=current.id,
            error_type=type(error).__name__,
        )
        return False
    entries = {entry.id: entry for entry in catalog.models}
    defaults = {
        "asr": ("asr", settings.default_asr_model_id),
        "mt": ("translation", settings.default_translation_model_id),
        "separation": ("separation", settings.default_separation_model_id),
        "tts": ("tts", settings.default_tts_model_id),
    }
    for stage in stages:
        spec_key, default_model_id = defaults[stage]
        model_id = _selected_model_id(record, spec_key, default_model_id)
        entry = entries.get(model_id)
        minimum_vram_mib = entry.minimum_vram_mib if entry is not None else None
        if minimum_vram_mib is None or minimum_vram_mib <= available_vram_mib:
            continue
        current = store.get_job(record.id)
        try:
            store.update_status(
                current.id,
                JobStatus.FAILED,
                expected_status=current.status,
                stage=current.stage,
                details={
                    **current.details,
                    "required_model_id": model_id,
                    "required_vram_mib": minimum_vram_mib,
                    "available_vram_mib": available_vram_mib,
                },
                error_code="model_vram_insufficient",
                error_message=(
                    f"Model {model_id} cần tối thiểu {minimum_vram_mib} MiB VRAM "
                    f"nhưng GPU logical 0 chỉ có {available_vram_mib} MiB"
                ),
                retryable=False,
            )
        except InvalidTransition:
            _print_event(
                "job.model_guard_stale_state",
                job_id=current.id,
                observed_status=current.status.value,
            )
            return False
        _print_event(
            "job.rejected_vram",
            job_id=current.id,
            model_id=model_id,
            required_vram_mib=minimum_vram_mib,
            available_vram_mib=available_vram_mib,
        )
        return False
    return True


def _print_event(event: str, **payload: Any) -> None:
    print(
        json.dumps(
            {"event": event, **payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


def build_transcription_stage(
    settings: Settings,
    store: StateStore,
    shutdown: threading.Event,
) -> TranscriptionStage:
    """Build the production stage without any network-enabled dependency."""

    return TranscriptionStage(
        models_lock_path=settings.models_lock_path,
        models_dir=settings.models_dir,
        jobs_dir=settings.jobs_dir,
        default_asr_model_id=settings.default_asr_model_id,
        compute_type=settings.asr_compute_type,
        store=store,
        recognizer_factory=lambda: FasterWhisperRecognizer(
            language_confidence_threshold=(
                settings.asr_language_confidence_threshold
            ),
            device=settings.compute_mode,
        ),
        shutdown_requested=shutdown.is_set,
        diarization_pipeline_path=settings.diarization_pipeline_path,
        diarization_device="cuda" if settings.compute_mode == "gpu" else "cpu",
    )


def build_translation_stage(
    settings: Settings,
    store: StateStore,
    shutdown: threading.Event,
) -> TranslationStage:
    """Build the local llama.cpp stage with no model provisioning path."""

    return TranslationStage(
        models_lock_path=settings.models_lock_path,
        models_dir=settings.models_dir,
        jobs_dir=settings.jobs_dir,
        default_translation_model_id=settings.default_translation_model_id,
        store=store,
        llama_server_binary=settings.llama_server_binary,
        llama_server_port=settings.llama_server_port,
        llama_context_size=settings.llama_context_size,
        llama_max_output_tokens=settings.llama_max_output_tokens,
        llama_startup_timeout_seconds=settings.llama_startup_timeout_seconds,
        llama_request_timeout_seconds=settings.llama_request_timeout_seconds,
        llama_gpu_layers=settings.llama_gpu_layers,
        shutdown_requested=shutdown.is_set,
    )


async def process_next_transcript_job(
    store: StateStore,
    stage: TranscriptionStage,
    *,
    shutdown: threading.Event,
    settings: Settings | None = None,
    gpu_report: GpuPreflightReport | None = None,
) -> bool:
    """Process at most one job and return whether queue work was observed."""

    records = store.list_jobs(_TRANSCRIPT_QUEUE, limit=100)
    records = [
        item
        for item in records
        if item.status is not JobStatus.CANCELLING
        or bool(item.details.get("offline_cancel_pending"))
    ]
    if not records:
        return False
    record = records[0]
    if shutdown.is_set():
        return False
    if record.status is JobStatus.CANCELLING:
        # A restarted worker has no live native handle. If the API marked this
        # as an offline cancellation, finalizing is now safe and idempotent.
        if record.details.get("offline_cancel_pending"):
            store.finalize_cancel(record.id)
            _print_event("job.cancelled", job_id=record.id)
        return True

    transcript_source = record.details.get("transcript_source")
    subtitle_can_fallback = (
        record.status is JobStatus.READY_OFFLINE
        and str(record.spec.get("subtitle_mode", "prefer")) == "prefer"
    )
    transcript_stages = (
        ("asr",)
        if transcript_source == "asr" or subtitle_can_fallback
        else ()
    )
    if not _ensure_selected_models_fit_vram(
        store,
        record,
        settings=settings,
        report=gpu_report,
        stages=transcript_stages,
    ):
        return True

    _print_event(
        "transcription.started",
        job_id=record.id,
        status=record.status.value,
        transcript_source=record.details.get("transcript_source"),
    )
    await stage.run(record.id)
    current = store.get_job(record.id)
    if current.status is JobStatus.CANCELLING:
        # stage.run has returned, therefore decoder/model cleanup and the
        # recognizer's finally block have completed before this transition.
        current = store.finalize_cancel(record.id)
    _print_event(
        "transcription.finished",
        job_id=current.id,
        status=current.status.value,
        error_code=current.error_code,
    )
    return True


async def process_next_translation_job(
    store: StateStore,
    stage: TranslationStage,
    *,
    shutdown: threading.Event,
    settings: Settings | None = None,
    gpu_report: GpuPreflightReport | None = None,
    translation_backend: str | None = None,
) -> bool:
    """Process at most one ready/resumable translation job.

    ``translation_backend`` routes jobs by their spec: the offline worker
    passes ``"local"`` (it must never claim an OpenAI job, whose API call
    its network guard would block), while the standalone OpenAI runner
    passes ``"openai"``. ``None`` keeps the old unfiltered behaviour.
    """

    records = store.list_jobs(_TRANSLATION_QUEUE, limit=100)
    if not records or shutdown.is_set():
        return False
    record = None
    for candidate in records:
        backend = candidate.spec.get("translation_backend") or "local"
        if translation_backend == "local" and backend == "openai":
            continue
        if translation_backend == "openai" and backend != "openai":
            continue
        record = candidate
        break
    if record is None:
        return False
    if not _ensure_selected_models_fit_vram(
        store,
        record,
        settings=settings,
        report=gpu_report,
        stages=("mt",),
    ):
        return True
    _print_event(
        "translation.started",
        job_id=record.id,
        status=record.status.value,
        model_id=(record.spec.get("models") or {}).get("translation"),
    )
    await stage.run(record.id)
    current = store.get_job(record.id)
    if current.status is JobStatus.CANCELLING:
        # TranslationStage has closed llama-server in its finally block.
        current = store.finalize_cancel(record.id)
    _print_event(
        "translation.finished",
        job_id=current.id,
        status=current.status.value,
        error_code=current.error_code,
    )
    return True


async def process_next_phase4_job(
    store: StateStore,
    stage: Phase4Stage,
    *,
    shutdown: threading.Event,
    settings: Settings | None = None,
    gpu_report: GpuPreflightReport | None = None,
) -> bool:
    """Process at most one ready or checkpointed Phase 4 job."""

    records = store.list_jobs(_PHASE4_QUEUE, limit=100)
    if not records or shutdown.is_set():
        return False
    record = records[0]
    phase4_model_stages: tuple[str, ...]
    timing_rewrite_stages = (
        ("mt",)
        if record.spec.get("timing_profile") == "natural"
        and (
            settings is None
            or settings.timing_rewrite_max_attempts > 0
        )
        else ()
    )
    if record.status in {JobStatus.MIXING, JobStatus.MUXING, JobStatus.VERIFYING}:
        phase4_model_stages = ()
    elif record.status in {JobStatus.SYNTHESIZING, JobStatus.TIMING}:
        phase4_model_stages = ("tts", *timing_rewrite_stages)
    else:
        phase4_model_stages = ("separation", "tts", *timing_rewrite_stages)
    if not _ensure_selected_models_fit_vram(
        store,
        record,
        settings=settings,
        report=gpu_report,
        stages=phase4_model_stages,
    ):
        return True
    _print_event(
        "phase4.started",
        job_id=record.id,
        status=record.status.value,
        separation_model_id=(record.spec.get("models") or {}).get("separation"),
        tts_model_id=(record.spec.get("models") or {}).get("tts"),
    )
    await stage.run(record.id)
    current = store.get_job(record.id)
    if current.status is JobStatus.CANCELLING:
        # All native/subprocess handles are released by Phase4Stage finally
        # blocks before run() returns.
        current = store.finalize_cancel(record.id)
    _print_event(
        "phase4.finished",
        job_id=current.id,
        status=current.status.value,
        error_code=current.error_code,
    )
    return True


async def _heartbeat_loop(
    settings: Settings,
    report: GpuPreflightReport,
    shutdown: threading.Event,
) -> None:
    interval = min(max(settings.gpu_report_max_age_seconds / 3.0, 1.0), 10.0)
    while not shutdown.is_set():
        write_gpu_report(settings.gpu_report_path, report)
        await asyncio.sleep(interval)


async def _worker_loop(
    settings: Settings,
    report: GpuPreflightReport,
    *,
    shutdown: threading.Event,
    poll_seconds: float,
    gpu_recheck_seconds: float,
) -> None:
    store = StateStore(settings.database_path)
    transcription_stage = build_transcription_stage(settings, store, shutdown)
    translation_stage = build_translation_stage(settings, store, shutdown)
    phase4_stage = build_phase4_stage(settings, store, shutdown)
    heartbeat = asyncio.create_task(
        _heartbeat_loop(settings, report, shutdown),
        name="gpu-heartbeat",
    )
    next_gpu_check = time.monotonic() + max(gpu_recheck_seconds, 5.0)
    try:
        while not shutdown.is_set():
            worked = await process_next_transcript_job(
                store,
                transcription_stage,
                shutdown=shutdown,
                settings=settings,
                gpu_report=report,
            )
            if not worked:
                worked = await process_next_translation_job(
                    store,
                    translation_stage,
                    shutdown=shutdown,
                    settings=settings,
                    gpu_report=report,
                    translation_backend="local",
                )
            if not worked:
                worked = await process_next_phase4_job(
                    store,
                    phase4_stage,
                    shutdown=shutdown,
                    settings=settings,
                    gpu_report=report,
                )
            if shutdown.is_set():
                break
            if settings.compute_mode == "gpu" and time.monotonic() >= next_gpu_check:
                try:
                    report = await asyncio.to_thread(_inspect_configured_gpu, settings)
                except GpuPreflightError as error:
                    write_gpu_report(settings.gpu_report_path, error.report)
                    _print_event(
                        "gpu.failed",
                        report=error.report.model_dump(mode="json"),
                    )
                    raise
                write_gpu_report(settings.gpu_report_path, report)
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
                heartbeat = asyncio.create_task(
                    _heartbeat_loop(settings, report, shutdown),
                    name="gpu-heartbeat",
                )
                next_gpu_check = time.monotonic() + max(
                    gpu_recheck_seconds,
                    5.0,
                )
            if not worked:
                await asyncio.sleep(max(poll_seconds, 0.1))
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        write_gpu_report(settings.gpu_report_path, report)


def _install_signal_handlers(shutdown: threading.Event) -> None:
    def stop(_signum: int, _frame: object) -> None:
        shutdown.set()

    for signal_name in (signal.SIGTERM, signal.SIGINT):
        with suppress(ValueError, OSError):
            signal.signal(signal_name, stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline dubbing worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run only the configured compute startup check and exit",
    )
    parser.add_argument("--poll-seconds", type=float, default=None)
    parser.add_argument("--gpu-recheck-seconds", type=float, default=30.0)
    args = parser.parse_args()

    settings = get_settings()
    try:
        report = _inspect_configured_gpu(settings)
    except GpuPreflightError as error:
        report = error.report
        write_gpu_report(settings.gpu_report_path, report)
        _print_event("gpu.failed", report=report.model_dump(mode="json"))
        raise SystemExit(2) from error
    write_gpu_report(settings.gpu_report_path, report)
    _print_event(
        "compute.ready",
        compute_mode=settings.compute_mode,
        report=report.model_dump(mode="json"),
    )
    if args.once:
        return

    shutdown = threading.Event()
    _install_signal_handlers(shutdown)
    poll_seconds = (
        args.poll_seconds
        if args.poll_seconds is not None
        else settings.offline_worker_poll_seconds
    )
    # Windows' ProactorEventLoop creates a private loopback socketpair during
    # initialization. Build that OS-internal wake-up channel before installing
    # the fail-closed network audit hook; every subsequent Python DNS/TCP call
    # remains blocked except the locked local llama-server port.
    runner = asyncio.Runner()
    try:
        runner.get_loop()
        install_offline_network_guard(
            allowed_loopback_ports={settings.llama_server_port}
        )
        runner.run(
            _worker_loop(
                settings,
                report,
                shutdown=shutdown,
                poll_seconds=max(poll_seconds, 0.1),
                gpu_recheck_seconds=max(args.gpu_recheck_seconds, 5.0),
            )
        )
    except GpuPreflightError as error:
        raise SystemExit(2) from error
    finally:
        runner.close()


if __name__ == "__main__":
    main()
