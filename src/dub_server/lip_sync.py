"""Optional offline visual lip-sync through isolated local backends."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .process_groups import process_group_spawn_options, signal_process_group


class LipSyncMode(StrEnum):
    OFF = "off"
    DRAFT = "draft"
    FINAL = "final"


class LipSyncError(RuntimeError):
    def __init__(self, code: str, message_vi: str, *, retryable: bool) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class LipSyncBackendPaths:
    latentsync_python: Path
    latentsync_source: Path
    latentsync_checkpoint: Path
    latentsync_config: Path
    musetalk_python: Path
    musetalk_source: Path
    ffmpeg_binary: Path


@dataclass(frozen=True, slots=True)
class LipSyncResult:
    path: Path
    mode: LipSyncMode
    backend: str | None
    applied: bool
    fallback_reason: str | None = None


Cancellation = Callable[[], bool]
CommandRunner = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]
LATENTSYNC_16_CHECKPOINT_SHA256 = (
    "0a478e89eb660f82da4c35dbdde8a5adfb27f99d1b4e50edd03729e1e98316d3"
)


async def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    cancellation: Cancellation | None,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    process: asyncio.subprocess.Process | None = None
    communication: asyncio.Task[tuple[bytes, bytes]] | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=os.fspath(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "DIFFUSERS_OFFLINE": "1",
            },
            **process_group_spawn_options(),
        )
        communication = asyncio.create_task(process.communicate())
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while not communication.done():
            if cancellation is not None and cancellation():
                raise LipSyncError(
                    "lip_sync_cancelled",
                    "Đã hủy tạo khẩu hình",
                    retryable=True,
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise LipSyncError(
                    "lip_sync_timeout",
                    "Tạo khẩu hình vượt quá thời gian cho phép",
                    retryable=True,
                )
            await asyncio.sleep(0.1)
        stdout, stderr = await communication
        return subprocess.CompletedProcess(
            tuple(command),
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            await _stop_process(process)
        await _cancel_communication(communication)
        raise
    except LipSyncError:
        if process is not None and process.returncode is None:
            await _stop_process(process)
        await _cancel_communication(communication)
        raise
    except OSError as error:
        raise LipSyncError(
            "lip_sync_runtime_unavailable",
            "Không thể khởi động backend tạo khẩu hình",
            retryable=False,
        ) from error


async def _cancel_communication(
    task: asyncio.Task[tuple[bytes, bytes]] | None,
) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    signal_process_group(process, force=False)
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        signal_process_group(process, force=True)
        with suppress(Exception):
            await process.wait()


class ExternalLipSyncProcessor:
    """Run pinned MuseTalk/LatentSync source trees without shell interpolation."""

    def __init__(
        self,
        *,
        paths: LipSyncBackendPaths,
        runner: CommandRunner = _run_command,
        timeout_seconds: float = 6 * 60 * 60,
        latentsync_checkpoint_sha256: str = LATENTSYNC_16_CHECKPOINT_SHA256,
        face_preflight: bool = False,
    ) -> None:
        self._paths = paths
        self._runner = runner
        self._timeout_seconds = timeout_seconds
        self._latentsync_checkpoint_sha256 = latentsync_checkpoint_sha256
        self._face_preflight_enabled = face_preflight

    async def render(
        self,
        *,
        mode: LipSyncMode,
        dubbed_video: Path,
        narration_audio: Path,
        output_path: Path,
        work_dir: Path,
        cancellation: Cancellation | None = None,
    ) -> LipSyncResult:
        if mode is LipSyncMode.OFF:
            return LipSyncResult(
                path=dubbed_video,
                mode=mode,
                backend=None,
                applied=False,
            )
        source = dubbed_video.resolve(strict=True)
        narration = narration_audio.resolve(strict=True)
        destination = output_path.resolve(strict=False)
        work = work_dir.resolve(strict=False)
        work.mkdir(parents=True, exist_ok=True)
        generated = work / "generated.mp4"
        generated.unlink(missing_ok=True)
        preflight_decision = await self._face_preflight(
            mode=mode,
            source=source,
            narration=narration,
            work=work,
            cancellation=cancellation,
        )
        if preflight_decision != "safe":
            fallback = {
                "no_face": "no_face_detected",
                "ambiguous_faces": "ambiguous_faces",
                "face_too_small": "face_too_small",
                "no_speech": "no_active_speech",
            }.get(preflight_decision, "face_preflight_unavailable")
            return LipSyncResult(
                path=source,
                mode=mode,
                backend=None,
                applied=False,
                fallback_reason=fallback,
            )
        try:
            backend = await self._render_visuals(
                mode,
                source=source,
                narration=narration,
                generated=generated,
                work=work,
                cancellation=cancellation,
            )
        except LipSyncError as error:
            if error.code != "lip_sync_no_face":
                raise
            return LipSyncResult(
                path=source,
                mode=mode,
                backend=None,
                applied=False,
                fallback_reason="no_face_detected",
            )
        if not generated.is_file() or generated.stat().st_size <= 0:
            raise LipSyncError(
                "lip_sync_output_missing",
                "Backend không tạo được video khẩu hình",
                retryable=True,
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.stem}.part.mp4")
        temporary.unlink(missing_ok=True)
        remux = (
            os.fspath(self._paths.ffmpeg_binary),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            os.fspath(generated),
            "-i",
            os.fspath(source),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-shortest",
            os.fspath(temporary),
        )
        result = await self._runner(
            remux,
            cwd=work,
            cancellation=cancellation,
            timeout_seconds=self._timeout_seconds,
        )
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
            temporary.unlink(missing_ok=True)
            raise LipSyncError(
                "lip_sync_remux_failed",
                "Không thể ghép lại audio thuyết minh sau tạo khẩu hình",
                retryable=True,
            )
        os.replace(temporary, destination)
        return LipSyncResult(
            path=destination,
            mode=mode,
            backend=backend,
            applied=True,
        )

    async def _face_preflight(
        self,
        *,
        mode: LipSyncMode,
        source: Path,
        narration: Path,
        work: Path,
        cancellation: Cancellation | None,
    ) -> str:
        if not self._face_preflight_enabled:
            return "safe"
        cache_path = work / "face-preflight.json"
        identity = {
            "schema_version": 1,
            "preflight_version": "haar-active-speech-v1",
            "video_sha256": await asyncio.to_thread(self._sha256, source),
            "narration_sha256": await asyncio.to_thread(self._sha256, narration),
            "sample_count_requested": 16,
            "minimum_face_ratio": 0.015,
        }
        cached = self._read_preflight_cache(cache_path, identity)
        if cached is not None:
            return cached
        python = (
            self._paths.latentsync_python
            if mode is LipSyncMode.FINAL
            else self._paths.musetalk_python
        )
        command = (
            os.fspath(python),
            os.fspath(Path(__file__).with_name("lip_sync_preflight.py")),
            "--video",
            os.fspath(source),
            "--audio",
            os.fspath(narration),
            "--sample-count",
            "16",
            "--minimum-face-ratio",
            "0.015",
        )
        result = await self._runner(
            command,
            cwd=work,
            cancellation=cancellation,
            timeout_seconds=min(self._timeout_seconds, 300.0),
        )
        decision = "preflight_unavailable"
        report: dict[str, object] = {}
        if result.returncode == 0:
            try:
                raw = json.loads(result.stdout)
            except json.JSONDecodeError:
                raw = None
            if isinstance(raw, dict) and raw.get("schema_version") == 1:
                value = raw.get("decision")
                if value in {
                    "safe",
                    "no_face",
                    "ambiguous_faces",
                    "face_too_small",
                    "no_speech",
                }:
                    decision = str(value)
                    report = raw
        payload = {**identity, **report, "decision": decision}
        temporary = cache_path.with_suffix(".part.json")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, cache_path)
        return decision

    @staticmethod
    def _read_preflight_cache(
        path: Path,
        identity: dict[str, object],
    ) -> str | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or any(
            payload.get(key) != value for key, value in identity.items()
        ):
            return None
        decision = payload.get("decision")
        return (
            str(decision)
            if decision in {
                "safe",
                "no_face",
                "ambiguous_faces",
                "face_too_small",
                "no_speech",
            }
            else None
        )

    async def _render_visuals(
        self,
        mode: LipSyncMode,
        *,
        source: Path,
        narration: Path,
        generated: Path,
        work: Path,
        cancellation: Cancellation | None,
    ) -> str:
        if mode is LipSyncMode.FINAL:
            command, cwd, backend = self._latentsync_command(
                source=source,
                narration=narration,
                generated=generated,
                work=work,
            )
            checkpoint_sha256 = await asyncio.to_thread(
                self._sha256,
                self._paths.latentsync_checkpoint,
            )
            if checkpoint_sha256 != self._latentsync_checkpoint_sha256:
                raise LipSyncError(
                    "latentsync_verification_failed",
                    "Checkpoint LatentSync không khớp SHA-256 đã khóa",
                    retryable=False,
                )
        else:
            command, cwd, generated_output, backend = self._musetalk_command(
                source=source,
                narration=narration,
                work=work,
            )
        result = await self._runner(
            command,
            cwd=cwd,
            cancellation=cancellation,
            timeout_seconds=self._timeout_seconds,
        )
        if result.returncode == 3 and "DUB_LIP_SYNC_NO_FACE" in result.stderr:
            raise LipSyncError(
                "lip_sync_no_face",
                "Không phát hiện khuôn mặt phù hợp; giữ nguyên video",
                retryable=False,
            )
        if result.returncode != 0:
            raise LipSyncError(
                "lip_sync_backend_failed",
                "Backend tạo khẩu hình kết thúc với lỗi",
                retryable=True,
            )
        if mode is LipSyncMode.DRAFT:
            if generated_output.is_file():
                shutil.move(generated_output, generated)
        return backend

    def _latentsync_command(
        self,
        *,
        source: Path,
        narration: Path,
        generated: Path,
        work: Path,
    ) -> tuple[tuple[str, ...], Path, str]:
        paths = self._paths
        required = (
            paths.latentsync_source,
            paths.latentsync_checkpoint,
            paths.latentsync_config,
        )
        if not required[0].is_dir() or not all(path.is_file() for path in required[1:]):
            raise LipSyncError(
                "latentsync_not_installed",
                "LatentSync chưa được cài và xác minh đầy đủ",
                retryable=False,
            )
        return (
            (
                os.fspath(paths.latentsync_python),
                "-m",
                "scripts.inference",
                "--unet_config_path",
                os.fspath(paths.latentsync_config.resolve()),
                "--inference_ckpt_path",
                os.fspath(paths.latentsync_checkpoint.resolve()),
                "--inference_steps",
                "20",
                "--guidance_scale",
                "1.5",
                "--enable_deepcache",
                "--video_path",
                os.fspath(source),
                "--audio_path",
                os.fspath(narration),
                "--video_out_path",
                os.fspath(generated),
                "--temp_dir",
                os.fspath(work / "latentsync-temp"),
            ),
            paths.latentsync_source.resolve(),
            "latentsync-1.6",
        )

    def _musetalk_command(
        self,
        *,
        source: Path,
        narration: Path,
        work: Path,
    ) -> tuple[tuple[str, ...], Path, Path, str]:
        paths = self._paths
        required = (
            paths.musetalk_source / "models/musetalkV15/unet.pth",
            paths.musetalk_source / "models/musetalkV15/musetalk.json",
            paths.musetalk_source / "models/sd-vae/config.json",
            paths.musetalk_source / "models/whisper/config.json",
            paths.musetalk_source / "models/dwpose/dw-ll_ucoco_384.pth",
            paths.musetalk_source / "models/syncnet/latentsync_syncnet.pt",
            paths.musetalk_source / "models/face-parse-bisent/79999_iter.pth",
            paths.musetalk_source / "models/face-parse-bisent/resnet18-5c106cde.pth",
        )
        if not paths.musetalk_source.is_dir() or not all(
            path.is_file() for path in required
        ):
            raise LipSyncError(
                "musetalk_not_installed",
                "MuseTalk chưa được cài và xác minh đầy đủ",
                retryable=False,
            )
        result_dir = work / "musetalk-results"
        config = work / "musetalk-inference.yaml"
        config.write_text(
            "task_0:\n"
            f"  video_path: {self._yaml_string(source)}\n"
            f"  audio_path: {self._yaml_string(narration)}\n"
            "  result_name: generated.mp4\n",
            encoding="utf-8",
            newline="\n",
        )
        command = (
            os.fspath(paths.musetalk_python),
            "-m",
            "scripts.inference",
            "--inference_config",
            os.fspath(config),
            "--result_dir",
            os.fspath(result_dir),
            "--unet_model_path",
            os.fspath(paths.musetalk_source / "models/musetalkV15/unet.pth"),
            "--unet_config",
            os.fspath(paths.musetalk_source / "models/musetalkV15/musetalk.json"),
            "--version",
            "v15",
            "--use_float16",
        )
        return (
            command,
            paths.musetalk_source.resolve(),
            result_dir / "v15" / "generated.mp4",
            "musetalk-1.5",
        )

    @staticmethod
    def _yaml_string(path: Path) -> str:
        value = os.fspath(path).replace("\\", "/").replace("'", "''")
        return f"'{value}'"

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


__all__ = [
    "ExternalLipSyncProcessor",
    "LipSyncBackendPaths",
    "LipSyncError",
    "LipSyncMode",
    "LipSyncResult",
]
