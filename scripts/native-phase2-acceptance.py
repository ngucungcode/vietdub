#!/usr/bin/env python3
"""Deterministic Phase 2 acceptance using only generated local media."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import resource
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from dub_server.model_registry import resolve_verified_model
from dub_server.offline import OfflineNetworkError, install_offline_network_guard
from dub_server.state import JobStage, JobStatus, StateStore
from dub_server.transcription_stage import TranscriptionStage

PHRASE = "this offline fixture tests local speech recognition"
EXPECTED_TOKENS = frozenset(PHRASE.split())
DURATION_US = 8_000_000


def _run(command: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {command[0]}: {result.stderr[-1000:]}"
        )
    return result


def _generate_fixture(path: Path) -> None:
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24:duration=8",
            "-f",
            "lavfi",
            "-i",
            f"flite=text='{PHRASE}':voice=slt",
            "-filter_complex",
            "[1:a]apad=whole_dur=8[a]",
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-t",
            "8",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )


def _make_ready_job(
    root: Path,
    *,
    source: str,
    media_path: Path,
    model_id: str,
    subtitle_path: Path | None = None,
) -> tuple[StateStore, str]:
    store = StateStore(root / "jobs.sqlite3")
    job = store.create_job(
        f"phase2-{source}",
        {
            "rights_confirmed": True,
            "source_language": "auto" if source == "asr" else "en",
            "subtitle_mode": "prefer",
            "models": {"asr": model_id},
        },
    )
    store.update_status(job.id, JobStatus.DOWNLOADING)
    store.update_status(job.id, JobStatus.SUBTITLE_MATCHING, stage=JobStage.SUBTITLE)
    details: dict[str, Any] = {
        "transcript_source": source,
        "source_media_path": str(media_path),
        "source_subtitle_path": str(subtitle_path) if subtitle_path else None,
        "selected_subtitle": (
            {"subtitle_id": "generated", "format": "srt", "language": "en"}
            if subtitle_path
            else None
        ),
        "selected_media": {
            "duration_us": DURATION_US,
            "source_language": "auto" if source == "asr" else "en",
            "audio_stream_index": 1,
        },
    }
    ready = store.update_status(
        job.id,
        JobStatus.READY_OFFLINE,
        stage=JobStage.SUBTITLE,
        progress_permille=250,
        details=details,
    )
    return store, ready.id


def _normalized_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return set(re.findall(r"[a-z]+", normalized))


class _GpuSampler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self.samples: list[dict[str, int]] = []
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def __enter__(self) -> _GpuSampler:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _sample(self) -> None:
        while not self._stop.wait(0.1):
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,used_memory",
                    "--format=csv,noheader,nounits",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                continue
            for line in result.stdout.splitlines():
                fields = [field.strip() for field in line.split(",")]
                if len(fields) != 2 or not all(field.isdigit() for field in fields):
                    continue
                self.samples.append(
                    {"pid": int(fields[0]), "used_memory_mib": int(fields[1])}
                )


def _assert_network_guard() -> dict[str, bool]:
    blocked = {"dns": False, "ipv4_connect": False, "ipv6_connect": False}
    operations = {
        "dns": lambda: socket.getaddrinfo("example.com", 443),
        "ipv4_connect": lambda: socket.socket(
            socket.AF_INET, socket.SOCK_STREAM
        ).connect(("127.0.0.1", 9)),
        "ipv6_connect": lambda: socket.socket(
            socket.AF_INET6, socket.SOCK_STREAM
        ).connect(("::1", 9)),
    }
    for name, operation in operations.items():
        try:
            operation()
        except OfflineNetworkError:
            blocked[name] = True
    if not all(blocked.values()):
        raise AssertionError(f"Offline guard did not block every canary: {blocked}")
    return blocked


def _run_worker_smoke(
    root: Path,
    *,
    media: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    state_root = root / "worker-state"
    store, job_id = _make_ready_job(
        state_root,
        source="asr",
        media_path=media,
        model_id=args.model_id,
    )
    worker_jobs = root / "worker-artifacts"
    worker_output = root / "worker-output"
    worker_log = root / "worker.log"
    environment = dict(os.environ)
    environment.update(
        {
            "DUB_DATABASE_PATH": str(store.database_path),
            "DUB_MODELS_LOCK_PATH": str(args.lock),
            "DUB_MODELS_DIR": str(args.models_dir),
            "DUB_INCOMING_DIR": str(root),
            "DUB_JOBS_DIR": str(worker_jobs),
            "DUB_OUTPUT_DIR": str(worker_output),
            "DUB_GPU_REPORT_PATH": str(root / "worker-gpu.json"),
            "DUB_DEFAULT_ASR_MODEL_ID": args.model_id,
            "DUB_ASR_COMPUTE_TYPE": args.compute_type,
            "DUB_OFFLINE_WORKER_POLL_SECONDS": "0.1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }
    )
    with worker_log.open("w", encoding="utf-8") as log_stream:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "dub_server.worker",
                "--poll-seconds",
                "0.1",
                "--gpu-recheck-seconds",
                "300",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            env=environment,
            text=True,
        )
        deadline = time.monotonic() + 90.0
        final_status = store.get_job(job_id).status
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                final_status = store.get_job(job_id).status
                if final_status in {
                    JobStatus.READY_TRANSLATION,
                    JobStatus.FAILED,
                    JobStatus.NEEDS_LANGUAGE,
                }:
                    break
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    final = store.get_job(job_id)
    log_text = worker_log.read_text(encoding="utf-8", errors="replace")
    if final.status is not JobStatus.READY_TRANSLATION:
        raise AssertionError(
            f"Worker smoke failed with {final.status.value}: "
            f"{final.error_code}: {final.error_message}; log={log_text[-2000:]}"
        )
    if process.returncode != 0:
        raise AssertionError(
            f"Worker did not stop cleanly: returncode={process.returncode}; "
            f"log={log_text[-2000:]}"
        )
    segments = store.list_transcript_segments(job_id)
    if not segments:
        raise AssertionError("Worker smoke committed no transcript segments")
    return {
        "status": final.status.value,
        "exit_code": process.returncode,
        "segment_count": len(segments),
        "transcript": " ".join(segment.text for segment in segments),
        "gpu_heartbeat": (root / "worker-gpu.json").is_file(),
        "artifact_exists": (
            worker_jobs / job_id / "source-transcript.json"
        ).is_file(),
    }


def _run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="dub-phase2-") as temporary:
        root = Path(temporary)
        media = root / "generated-speech.mkv"
        _generate_fixture(media)
        subtitle = root / "generated.srt"
        subtitle.write_text(
            "1\n00:00:00,100 --> 00:00:02,000\nSubtitle route stays offline\n",
            encoding="utf-8",
        )

        # Everything after fixture creation must stay offline. The native
        # container lacks CAP_NET_ADMIN, so this is an application-level guard;
        # Docker network_mode:none remains the kernel-level acceptance path.
        install_offline_network_guard()
        network_canary = _assert_network_guard()

        subtitle_store, subtitle_job = _make_ready_job(
            root / "subtitle-state",
            source="subtitle",
            media_path=media,
            model_id=args.model_id,
            subtitle_path=subtitle,
        )

        def forbidden(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("Subtitle route touched an ASR dependency")

        subtitle_stage = TranscriptionStage(
            models_lock_path=args.lock,
            models_dir=args.models_dir,
            jobs_dir=root / "subtitle-artifacts",
            default_asr_model_id=args.model_id,
            compute_type=args.compute_type,
            store=subtitle_store,
            audio_decoder_factory=forbidden,
            recognizer_factory=forbidden,
            model_resolver=forbidden,
        )
        subtitle_result = asyncio.run(subtitle_stage.run(subtitle_job))
        if subtitle_result.status is not JobStatus.READY_TRANSLATION:
            raise AssertionError(f"Subtitle route failed: {subtitle_result.to_dict()}")

        verify_started = time.monotonic()
        verified = resolve_verified_model(
            args.lock,
            args.models_dir,
            args.model_id,
            "asr",
        )
        verify_seconds = time.monotonic() - verify_started

        asr_store, asr_job = _make_ready_job(
            root / "asr-state",
            source="asr",
            media_path=media,
            model_id=args.model_id,
        )

        def already_verified(
            _lock: Path,
            _models: Path,
            model_id: str,
            stage: str,
        ) -> Any:
            if model_id != args.model_id or stage != "asr":
                raise AssertionError("Stage requested an unexpected model")
            return verified

        asr_stage = TranscriptionStage(
            models_lock_path=args.lock,
            models_dir=args.models_dir,
            jobs_dir=root / "asr-artifacts",
            default_asr_model_id=args.model_id,
            compute_type=args.compute_type,
            store=asr_store,
            model_resolver=already_verified,
        )
        inference_started = time.monotonic()
        with _GpuSampler() as sampler:
            asr_result = asyncio.run(asr_stage.run(asr_job))
        inference_seconds = time.monotonic() - inference_started
        if asr_result.status is not JobStatus.READY_TRANSLATION:
            raise AssertionError(f"ASR route failed: {asr_result.to_dict()}")

        segments = asr_store.list_transcript_segments(asr_job)
        if not segments:
            raise AssertionError("ASR produced no transcript segments")
        previous_end = 0
        for segment in segments:
            if not (previous_end <= segment.start_us < segment.end_us <= DURATION_US):
                raise AssertionError("ASR timestamps are not monotonic and bounded")
            previous_end = segment.end_us
        transcript = " ".join(segment.text for segment in segments)
        observed_tokens = _normalized_tokens(transcript)
        matched_tokens = sorted(EXPECTED_TOKENS & observed_tokens)
        if len(matched_tokens) < 4:
            raise AssertionError(
                f"ASR token recall below gate: transcript={transcript!r}, matched={matched_tokens}"
            )
        own_gpu_samples = [
            sample for sample in sampler.samples if sample["pid"] == os.getpid()
        ]
        if not own_gpu_samples:
            raise AssertionError("nvidia-smi did not observe the ASR process on GPU")

        checkpoint = asr_store.get_checkpoint(asr_job, JobStage.ASR)
        if checkpoint is None or checkpoint.payload.get("completed") is not True:
            raise AssertionError("ASR transcript checkpoint was not committed")
        worker_smoke = _run_worker_smoke(
            root,
            media=media,
            args=args,
        )
        return {
            "schema_version": 1,
            "passed": True,
            "model_id": args.model_id,
            "model_path": str(verified.path),
            "model_tree_sha256": verified.tree_sha256,
            "compute_type": args.compute_type,
            "fixture_duration_us": DURATION_US,
            "subtitle_route": {
                "status": subtitle_result.status.value,
                "asr_invocations": 0,
                "segment_count": len(
                    subtitle_store.list_transcript_segments(subtitle_job)
                ),
            },
            "asr_route": {
                "status": asr_result.status.value,
                "language": asr_result.details.get("source_language_detected"),
                "language_probability": asr_result.details.get(
                    "source_language_probability"
                ),
                "segment_count": len(segments),
                "transcript": transcript,
                "matched_tokens": matched_tokens,
                "token_recall": len(matched_tokens) / len(EXPECTED_TOKENS),
                "first_start_us": segments[0].start_us,
                "last_end_us": segments[-1].end_us,
            },
            "gpu": {
                "observed": True,
                "sample_count": len(own_gpu_samples),
                "peak_process_memory_mib": max(
                    sample["used_memory_mib"] for sample in own_gpu_samples
                ),
                "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                / 1024.0,
            },
            "network_guard": network_canary,
            "worker_smoke": worker_smoke,
            "timing_seconds": {
                "model_verify": round(verify_seconds, 3),
                "asr_stage": round(inference_seconds, 3),
                "total": round(time.monotonic() - started, 3),
                "asr_realtime_factor": round(inference_seconds / 8.0, 3),
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument(
        "--model-id",
        default="asr-faster-whisper-large-v3-turbo",
    )
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    report: dict[str, Any]
    try:
        report = _run_acceptance(args)
    except Exception as error:
        report = {
            "schema_version": 1,
            "passed": False,
            "error_type": type(error).__name__,
            "error": str(error),
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not report.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
