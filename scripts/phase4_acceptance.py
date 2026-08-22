#!/usr/bin/env python3
"""Offline Phase 4 acceptance for separation, TTS, timing, and MP4 export.

The script intentionally accepts only local media and already installed model
directories verified by ``models.lock.json``.  It never invokes a model
manager or a Hugging Face download API.  ``--quick`` creates a short local
synthetic movie before the process-level network guard is installed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dub_server.audio_mix_export import FfmpegAudioMixExporter, MixSettings
from dub_server.audio_separation import (
    CinematicAudioSeparator,
    TigerDnrSubprocessRunner,
)
from dub_server.model_registry import VerifiedModel, resolve_verified_model
from dub_server.narration import (
    NarrationSynthesizer,
)
from dub_server.narration_artifact import (
    build_srt_cues,
    build_timing_report,
    write_srt_artifact,
    write_timing_report,
)
from dub_server.offline import OfflineNetworkError, install_offline_network_guard
from dub_server.phase4_stage import build_narration_synthesizer
from dub_server.timing import FfmpegTimingFitter, build_timeline_wav

_OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "WANDB_DISABLED": "true",
    "WANDB_MODE": "offline",
}
_LOUDNESS_PATTERN = re.compile(r"\bI:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+LUFS")
_TRUE_PEAK_PATTERN = re.compile(r"\bPeak:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+dBFS")


@dataclass(frozen=True, slots=True)
class ProbeContract:
    duration_us: int
    video_duration_us: int
    audio_duration_us: int
    video_start_us: int
    audio_start_us: int
    duration_error_us: int
    av_duration_error_us: int
    sync_error_us: int
    video_codec: str
    audio_codec: str


def _run(
    command: Sequence[str | os.PathLike[str]],
    *,
    timeout: float = 120.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    normalized = [os.fspath(value) for value in command]
    if not normalized or any("\x00" in value for value in normalized):
        raise ValueError("Lệnh cục bộ không hợp lệ")
    try:
        completed = subprocess.run(
            normalized,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except OSError as error:
        raise RuntimeError(f"Không thể chạy công cụ cục bộ: {normalized[0]}") from error
    if check and completed.returncode != 0:
        diagnostic = completed.stderr.strip()[-2_000:]
        raise RuntimeError(
            f"Lệnh cục bộ thất bại ({completed.returncode}): "
            f"{Path(normalized[0]).name}: {diagnostic}"
        )
    return completed


def _fixture_command(ffmpeg: str, output: Path, duration_seconds: float) -> tuple[str, ...]:
    """Return a shell-free, network-free synthetic cinematic fixture command."""

    if not 2.0 <= duration_seconds <= 1_800.0:
        raise ValueError("Fixture nhanh phải dài từ 2 giây đến 30 phút")
    duration = f"{duration_seconds:.3f}"
    graph = (
        "[1:a]volume=0.10,pan=stereo|c0=c0|c1=c0[music];"
        "[2:a]volume=0.12,pan=stereo|c0=c0|c1=c0[dialogue_like];"
        "[3:a]volume=0.025,highpass=f=1200,"
        "pan=stereo|c0=c0|c1=c0[effects];"
        "[music][dialogue_like][effects]amix=inputs=3:duration=longest:"
        "normalize=0,alimiter=limit=0.8[mix]"
    )
    return (
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x180:rate=24:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=220:sample_rate=44100:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=190:sample_rate=44100:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=color=pink:sample_rate=44100:duration={duration}",
        "-filter_complex",
        graph,
        "-map",
        "0:v:0",
        "-map",
        "[mix]",
        "-t",
        duration,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        os.fspath(output),
    )


def _generate_fixture(
    output: Path,
    *,
    ffmpeg: str,
    duration_seconds: float,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(_fixture_command(ffmpeg, output, duration_seconds), timeout=120.0)


def _require_local_media(path: Path) -> Path:
    raw = os.fspath(path)
    if "://" in raw or "\x00" in raw:
        raise ValueError("Phase 4 chỉ chấp nhận file media cục bộ")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValueError("Không tìm thấy file media cục bộ") from error
    if not resolved.is_file():
        raise ValueError("Đầu vào Phase 4 không phải file media")
    return resolved


def _create_acceptance_clip(
    source: Path,
    output: Path,
    *,
    ffmpeg: str,
    duration_seconds: float,
) -> None:
    """Create a bounded local H.264/AAC clip so acceptance cannot run for hours."""

    if duration_seconds <= 0:
        raise ValueError("Thời lượng clip nghiệm thu phải lớn hơn 0")
    local_source = _require_local_media(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = f"{duration_seconds:.3f}"
    _run(
        (
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            os.fspath(local_source),
            "-t",
            duration,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-sn",
            "-dn",
            "-vf",
            "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            os.fspath(output),
        ),
        timeout=max(120.0, duration_seconds * 10.0),
    )


def _probe_json(path: Path, *, ffprobe: str) -> dict[str, Any]:
    local = _require_local_media(path)
    completed = _run(
        (
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,start_time,duration:"
            "stream_disposition=attached_pic,timed_thumbnails",
            "-of",
            "json",
            os.fspath(local),
        ),
        timeout=30.0,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError("ffprobe trả về JSON không hợp lệ") from error
    if not isinstance(payload, dict):
        raise AssertionError("ffprobe không trả về object JSON")
    return payload


def _seconds_to_us(value: object, *, positive: bool) -> int | None:
    try:
        seconds = float(str(value))
    except (TypeError, ValueError):
        return None
    if not seconds == seconds or seconds in {float("inf"), float("-inf")}:
        return None
    if positive and seconds <= 0:
        return None
    return round(seconds * 1_000_000)


def _validate_probe_contract(
    payload: Mapping[str, Any],
    *,
    expected_duration_us: int | None,
    tolerance_us: int = 100_000,
) -> ProbeContract:
    """Enforce exactly one video and one AAC track plus the 100 ms A/V gate."""

    if tolerance_us < 0:
        raise ValueError("Dung sai không được âm")
    raw_streams = payload.get("streams")
    if not isinstance(raw_streams, list):
        raise AssertionError("Không có danh sách track ffprobe hợp lệ")
    streams = [item for item in raw_streams if isinstance(item, Mapping)]
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    if len(streams) != 2 or len(videos) != 1 or len(audios) != 1:
        raise AssertionError("Output phải có đúng một video track và một audio track")
    disposition = videos[0].get("disposition")
    if isinstance(disposition, Mapping) and any(
        disposition.get(name) in {1, "1"}
        for name in ("attached_pic", "timed_thumbnails")
    ):
        raise AssertionError("Video track đầu ra không được là ảnh bìa/thumbnail")
    audio_codec = str(audios[0].get("codec_name", "")).casefold()
    if audio_codec != "aac":
        raise AssertionError("Audio track đầu ra không phải AAC")
    video_duration = _seconds_to_us(videos[0].get("duration"), positive=True)
    audio_duration = _seconds_to_us(audios[0].get("duration"), positive=True)
    if video_duration is None or audio_duration is None:
        raise AssertionError("Không xác định được thời lượng video/audio output")
    duration = video_duration
    video_start = _seconds_to_us(videos[0].get("start_time"), positive=False) or 0
    audio_start = _seconds_to_us(audios[0].get("start_time"), positive=False) or 0
    duration_error = (
        0 if expected_duration_us is None else abs(duration - expected_duration_us)
    )
    av_duration_error = abs(
        (video_start + video_duration) - (audio_start + audio_duration)
    )
    sync_error = abs(video_start - audio_start)
    if duration_error > tolerance_us:
        raise AssertionError("Thời lượng output lệch quá 100 ms so với timeline")
    if av_duration_error > tolerance_us:
        raise AssertionError("Thời lượng video/audio lệch quá 100 ms")
    if sync_error > tolerance_us:
        raise AssertionError("Điểm bắt đầu video/audio lệch quá 100 ms")
    return ProbeContract(
        duration_us=duration,
        video_duration_us=video_duration,
        audio_duration_us=audio_duration,
        video_start_us=video_start,
        audio_start_us=audio_start,
        duration_error_us=duration_error,
        av_duration_error_us=av_duration_error,
        sync_error_us=sync_error,
        video_codec=str(videos[0].get("codec_name", "")),
        audio_codec=audio_codec,
    )


def _build_synthesizer(
    tts_model: VerifiedModel,
    *,
    support_model: VerifiedModel | None,
    piper_binary: str,
    vieneu_entrypoint: Path,
    python_binary: str,
) -> NarrationSynthesizer:
    """Delegate to the production factory so acceptance cannot drift from it."""

    return build_narration_synthesizer(
        tts_model,
        support_model,
        vieneu_entrypoint=vieneu_entrypoint,
        python_executable=python_binary,
        piper_binary=piper_binary,
    )


def _assert_network_guard() -> dict[str, bool]:
    operations = {
        "dns": lambda: socket.getaddrinfo("example.com", 443),
        "ipv4_connect": lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(
            ("192.0.2.1", 443)
        ),
        "ipv6_connect": lambda: socket.socket(socket.AF_INET6, socket.SOCK_STREAM).connect(
            ("2001:db8::1", 443)
        ),
    }
    result: dict[str, bool] = {}
    for name, operation in operations.items():
        try:
            operation()
        except OfflineNetworkError:
            result[name] = True
        else:
            result[name] = False
    if not all(result.values()):
        raise AssertionError(f"Offline network guard không chặn đủ canary: {result}")
    return result


def _parse_gpu_rows(output: str) -> tuple[dict[str, int], ...]:
    rows: list[dict[str, int]] = []
    for line in output.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) == 2 and all(field.isdecimal() for field in fields):
            rows.append({"pid": int(fields[0]), "used_memory_mib": int(fields[1])})
    return tuple(rows)


class _GpuSampler:
    def __init__(self, binary: str = "nvidia-smi", interval_seconds: float = 0.1) -> None:
        self._binary = binary
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self.available = shutil.which(binary) is not None
        self.samples: list[tuple[dict[str, int], ...]] = []
        self.baseline_pids: set[int] = set()

    def __enter__(self) -> _GpuSampler:
        if self.available:
            self._sample_once()
            if self.samples:
                self.baseline_pids = {row["pid"] for row in self.samples[0]}
            self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        if not self.available:
            return
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._sample_once()

    def _sample_loop(self) -> None:
        while not self._stop.wait(self._interval):
            self._sample_once()

    def _sample_once(self) -> None:
        try:
            completed = subprocess.run(
                (
                    self._binary,
                    "--query-compute-apps=pid,used_memory",
                    "--format=csv,noheader,nounits",
                ),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.SubprocessError):
            return
        if completed.returncode == 0:
            self.samples.append(_parse_gpu_rows(completed.stdout))

    def report(self) -> dict[str, Any]:
        rows = [row for sample in self.samples for row in sample]
        totals = [sum(row["used_memory_mib"] for row in sample) for sample in self.samples]
        observed_pids = {row["pid"] for row in rows}
        inference_pids = observed_pids - self.baseline_pids
        return {
            "available": self.available,
            "sample_count": len(self.samples),
            "observed_compute_process": bool(rows),
            "observed_new_inference_process": bool(inference_pids),
            "peak_single_process_mib": max(
                (row["used_memory_mib"] for row in rows), default=0
            ),
            "peak_total_compute_mib": max(totals, default=0),
            "baseline_pids": sorted(self.baseline_pids),
            "observed_pids": sorted(observed_pids),
            "inference_pids": sorted(inference_pids),
        }


def _peak_rss_mib(kind: str) -> float | None:
    try:
        import resource
    except ImportError:
        return None
    selector = resource.RUSAGE_SELF if kind == "self" else resource.RUSAGE_CHILDREN
    raw = float(resource.getrusage(selector).ru_maxrss)
    # Linux reports KiB. This acceptance runs on Ubuntu; retain a portable
    # fallback for developers importing the helpers on macOS.
    divisor = 1024.0 if sys.platform != "darwin" else 1024.0 * 1024.0
    return raw / divisor


def _wav_metadata(path: Path) -> dict[str, int]:
    try:
        with wave.open(os.fspath(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_rate = stream.getframerate()
            sample_width = stream.getsampwidth()
            frames = stream.getnframes()
    except (OSError, EOFError, wave.Error) as error:
        raise AssertionError(f"WAV nghiệm thu không hợp lệ: {path.name}") from error
    return {
        "channels": channels,
        "sample_rate": sample_rate,
        "sample_width_bytes": sample_width,
        "frame_count": frames,
        "duration_us": (frames * 1_000_000 + sample_rate // 2) // sample_rate,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _measure_loudness(path: Path, *, ffmpeg: str) -> dict[str, float | None]:
    completed = _run(
        (
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-nostats",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            os.fspath(_require_local_media(path)),
            "-map",
            "0:a:0",
            "-filter:a",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ),
        check=False,
        timeout=120.0,
    )
    matches_i = _LOUDNESS_PATTERN.findall(completed.stderr)
    matches_peak = _TRUE_PEAK_PATTERN.findall(completed.stderr)

    def last_finite(values: list[str]) -> float | None:
        if not values or values[-1] in {"inf", "-inf"}:
            return None
        return float(values[-1])

    return {
        "integrated_lufs": last_finite(matches_i),
        "true_peak_dbfs": last_finite(matches_peak),
    }


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".part", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _verified_summary(model: VerifiedModel) -> dict[str, Any]:
    return {
        "id": model.model_id,
        "stage": model.stage,
        "backend": model.entry.get("backend"),
        "repository": model.entry.get("repository"),
        "revision": model.entry.get("revision"),
        "tree_sha256": model.tree_sha256,
        "path": os.fspath(model.path),
    }


async def _run_pipeline(
    args: argparse.Namespace,
    *,
    work_dir: Path,
    source: Path,
    duration_us: int,
    separation_model: VerifiedModel,
    tts_model: VerifiedModel,
    support_model: VerifiedModel | None,
) -> dict[str, Any]:
    stage_seconds: dict[str, float] = {}
    accompaniment = work_dir / "music-effects.wav"
    separation_progress: list[int] = []
    separation_runner = TigerDnrSubprocessRunner(
        model_path=separation_model.path,
        model_id=separation_model.model_id,
        model_tree_sha256=separation_model.tree_sha256,
        python_executable=sys.executable,
        timeout_seconds=args.separation_timeout_seconds,
        source_dir=args.tiger_source_dir,
        chunk_seconds=args.separation_chunk_seconds,
        context_seconds=args.separation_context_seconds,
        batch_size=args.separation_batch_size,
    )
    separator = CinematicAudioSeparator(separation_runner)
    started = time.monotonic()
    separated = await separator.separate(
        source,
        accompaniment,
        on_progress=lambda event: separation_progress.append(event.completed_permille),
    )
    stage_seconds["separation"] = time.monotonic() - started
    accompaniment_meta = _wav_metadata(accompaniment)
    if abs(accompaniment_meta["duration_us"] - duration_us) > 100_000:
        raise AssertionError("Stem nhạc+hiệu ứng lệch quá 100 ms so với video")
    if separated.backend_name != "tiger-dnr":
        raise AssertionError("Nghiệm thu không chạy backend TIGER-DnR")

    synthesizer = _build_synthesizer(
        tts_model,
        support_model=support_model,
        piper_binary=args.piper_binary,
        vieneu_entrypoint=args.vieneu_entrypoint,
        python_binary=sys.executable,
    )
    raw_first = work_dir / "tts-speed-1.wav"
    raw_fitted_speed = work_dir / "tts-native-speed.wav"
    fitted_path = work_dir / "tts-fitted.wav"
    slot_start_us = min(250_000, duration_us // 10)
    slot_end_us = duration_us - slot_start_us
    if slot_end_us - slot_start_us < 1_000_000:
        raise AssertionError("Fixture Phase 4 phải có slot TTS tối thiểu một giây")
    slot_duration_us = slot_end_us - slot_start_us
    tts_progress: list[tuple[int, int]] = []
    started = time.monotonic()
    try:
        first = await synthesizer.synthesize(
            args.text,
            raw_first,
            speed=1.0,
            on_progress=lambda done, total: tts_progress.append((done, total)),
        )
        native_speed = max(0.90, min(first.duration_us / slot_duration_us, 1.20))
        second = await synthesizer.synthesize(
            args.text,
            raw_fitted_speed,
            speed=native_speed,
            on_progress=lambda done, total: tts_progress.append((done, total)),
        )
    finally:
        await synthesizer.close()
    stage_seconds["tts"] = time.monotonic() - started

    timing_progress: list[tuple[int, int]] = []
    started = time.monotonic()
    fitted = await FfmpegTimingFitter(ffmpeg_binary=args.ffmpeg).fit(
        second.path,
        fitted_path,
        start_us=slot_start_us,
        end_us=slot_end_us,
        text=second.text,
        native_speed=native_speed,
        on_progress=lambda done, total: timing_progress.append((done, total)),
    )
    timeline = build_timeline_wav(
        (fitted,),
        work_dir / "narration-timeline.wav",
        duration_us=duration_us,
    )
    timeline_meta = _wav_metadata(timeline.path)
    expected_frames = round(duration_us * 48_000 / 1_000_000)
    if timeline_meta["sample_rate"] != 48_000 or timeline_meta["frame_count"] != expected_frames:
        raise AssertionError("Timeline narration không đúng chính xác sample 48 kHz")
    report_file = write_timing_report(
        work_dir / "timing-report.json",
        build_timing_report(
            (fitted,),
            duration_us=duration_us,
            tts_model_id=tts_model.model_id,
            tts_backend=second.backend,
        ),
    )
    srt_file = write_srt_artifact(
        work_dir / "vietnamese.srt",
        build_srt_cues((fitted,)),
    )
    stage_seconds["timing"] = time.monotonic() - started

    output = work_dir / "dubbed.mp4"
    export_progress: list[float] = []
    started = time.monotonic()
    exported = await FfmpegAudioMixExporter(
        ffmpeg_binary=args.ffmpeg,
        ffprobe_binary=args.ffprobe,
        settings=MixSettings(
            narration_lufs=args.narration_lufs,
            accompaniment_lufs=args.accompaniment_lufs,
        ),
        duration_tolerance_us=100_000,
        sync_tolerance_us=100_000,
        timeout_seconds=args.export_timeout_seconds,
    ).export(
        source,
        accompaniment,
        timeline.path,
        output,
        expected_duration_us=duration_us,
        on_progress=lambda event: export_progress.append(event.fraction),
    )
    stage_seconds["mix_export"] = time.monotonic() - started
    contract = _validate_probe_contract(
        _probe_json(output, ffprobe=args.ffprobe),
        expected_duration_us=duration_us,
        tolerance_us=100_000,
    )

    return {
        "separation": {
            "backend": separated.backend_name,
            "published_stem": "music+effects",
            "dialogue_stem_published": False,
            "sha256": separated.checksum_sha256,
            "progress_monotonic": separation_progress == sorted(separation_progress),
            "metrics": asdict(separated.metrics),
            "wav": accompaniment_meta,
        },
        "tts": {
            "backend": second.backend,
            "text": second.text,
            "base_duration_us": first.duration_us,
            "native_speed": native_speed,
            "second_duration_us": second.duration_us,
            "progress_events": tts_progress,
        },
        "timing": {
            "slot_start_us": fitted.start_us,
            "slot_end_us": fitted.end_us,
            "target_frame_count": fitted.target_frame_count,
            "output_frame_count": fitted.output_frame_count,
            "total_speed": fitted.total_speed,
            "quality": fitted.quality.value,
            "padded_frame_count": fitted.padded_frame_count,
            "timeline": timeline_meta,
            "progress_events": timing_progress,
            "timing_report_sha256": report_file.sha256,
            "srt_sha256": srt_file.sha256,
        },
        "export": {
            "path": os.fspath(exported.path),
            "sha256": _sha256(exported.path),
            "size_bytes": exported.size_bytes,
            "progress_monotonic": export_progress == sorted(export_progress),
            "probe": asdict(contract),
            "loudness": _measure_loudness(exported.path, ffmpeg=args.ffmpeg),
        },
        "stage_seconds": {name: round(value, 3) for name, value in stage_seconds.items()},
    }


def _run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    if not args.quick and args.input is None:
        raise ValueError("Hãy truyền --input hoặc dùng --quick")
    if args.quick and args.input is not None:
        raise ValueError("--quick không được dùng đồng thời với --input")
    args.artifact_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(
        tempfile.mkdtemp(prefix="phase4-", dir=os.fspath(args.artifact_root))
    ).resolve()

    source = work_dir / "source.mp4"
    fixture_started = time.monotonic()
    if args.quick:
        _generate_fixture(
            source,
            ffmpeg=args.ffmpeg,
            duration_seconds=args.quick_duration_seconds,
        )
        fixture_kind = "synthetic"
    else:
        assert args.input is not None
        _create_acceptance_clip(
            args.input,
            source,
            ffmpeg=args.ffmpeg,
            duration_seconds=args.clip_duration_seconds,
        )
        fixture_kind = "local-clip"
    fixture_seconds = time.monotonic() - fixture_started
    source_contract = _validate_probe_contract(
        _probe_json(source, ffprobe=args.ffprobe),
        expected_duration_us=None,
        tolerance_us=100_000,
    )
    duration_us = source_contract.duration_us

    verify_started = time.monotonic()
    separation_model = resolve_verified_model(
        args.lock,
        args.models_dir,
        args.separation_model_id,
        "separation",
    )
    tts_model = resolve_verified_model(
        args.lock,
        args.models_dir,
        args.tts_model_id,
        "tts",
    )
    support_model: VerifiedModel | None = None
    if str(tts_model.entry.get("backend", "")).casefold() == "vieneu":
        support_id = tts_model.entry.get("support_model_id") or args.tts_support_model_id
        if not isinstance(support_id, str) or not support_id:
            raise ValueError("Manifest VieNeu thiếu support_model_id")
        support_model = resolve_verified_model(
            args.lock,
            args.models_dir,
            support_id,
            "tts-support",
        )
    verify_seconds = time.monotonic() - verify_started

    os.environ.update(_OFFLINE_ENVIRONMENT)
    install_offline_network_guard()
    network_guard = _assert_network_guard()
    with _GpuSampler(args.nvidia_smi) as gpu_sampler:
        pipeline = asyncio.run(
            _run_pipeline(
                args,
                work_dir=work_dir,
                source=source,
                duration_us=duration_us,
                separation_model=separation_model,
                tts_model=tts_model,
                support_model=support_model,
            )
        )
    gpu = gpu_sampler.report()
    if not args.allow_missing_gpu_metrics and not gpu["observed_new_inference_process"]:
        raise AssertionError("nvidia-smi không quan sát thấy tiến trình inference trên GPU")

    return {
        "schema_version": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "passed": True,
        "mode": "quick" if args.quick else "local-input",
        "offline": {
            "runtime_downloads_allowed": False,
            "environment": dict(_OFFLINE_ENVIRONMENT),
            "network_guard": network_guard,
        },
        "models": {
            "separation": _verified_summary(separation_model),
            "tts": _verified_summary(tts_model),
            "tts_support": (
                None if support_model is None else _verified_summary(support_model)
            ),
        },
        "fixture": {
            "kind": fixture_kind,
            "path": os.fspath(source),
            "duration_us": duration_us,
            "probe": asdict(source_contract),
        },
        "pipeline": pipeline,
        "resources": {
            "gpu": gpu,
            "peak_rss_self_mib": _peak_rss_mib("self"),
            "peak_rss_children_mib": _peak_rss_mib("children"),
        },
        "timing_seconds": {
            "fixture": round(fixture_seconds, 3),
            "model_verification": round(verify_seconds, 3),
            "total": round(time.monotonic() - started, 3),
            "total_realtime_factor": round(
                (time.monotonic() - started) / (duration_us / 1_000_000), 3
            ),
        },
        "artifacts_dir": os.fspath(work_dir),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Nghiệm thu Phase 4 hoàn toàn offline trên Ubuntu GPU"
    )
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--quick-duration-seconds", type=float, default=4.0)
    parser.add_argument("--clip-duration-seconds", type=float, default=12.0)
    parser.add_argument("--separation-model-id", default="separation-tiger-dnr")
    parser.add_argument("--tts-model-id", default="tts-vieneu-v2")
    parser.add_argument("--tts-support-model-id", default="tts-neucodec-onnx-int8")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--nvidia-smi", default="nvidia-smi")
    parser.add_argument("--piper-binary", default="piper")
    parser.add_argument("--tiger-source-dir", type=Path, default=Path("/opt/tiger"))
    parser.add_argument(
        "--vieneu-entrypoint",
        type=Path,
        default=Path("/opt/vieneu/vieneu-offline.py"),
    )
    parser.add_argument(
        "--text",
        default="Xin chào, đây là bản thuyết minh tiếng Việt chạy hoàn toàn ngoại tuyến.",
    )
    parser.add_argument("--narration-lufs", type=float, default=-18.0)
    parser.add_argument("--accompaniment-lufs", type=float, default=-24.0)
    parser.add_argument("--separation-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--separation-chunk-seconds", type=float, default=120.0)
    parser.add_argument("--separation-context-seconds", type=float, default=4.0)
    parser.add_argument("--separation-batch-size", type=int, default=1)
    parser.add_argument("--export-timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--allow-missing-gpu-metrics",
        action="store_true",
        help="Chỉ dành cho test adapter; nghiệm thu GPU thật không dùng cờ này",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    report: dict[str, Any]
    try:
        report = _run_acceptance(args)
    except Exception as error:
        report = {
            "schema_version": 1,
            "checked_at": datetime.now(UTC).isoformat(),
            "passed": False,
            "error_type": type(error).__name__,
            "error": str(error),
        }
    _atomic_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2), flush=True)
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
