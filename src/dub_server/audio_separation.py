"""Offline cinematic audio separation with atomic accompaniment artifacts.

The public separator deliberately knows nothing about job state.  It accepts one
local media file and publishes a PCM WAV containing only the music and effects
stems.  Dialogue is never copied into the published artifact.

The expensive implementation is injected through :class:`SeparationBackendRunner`.
Production uses :class:`TigerDnrSubprocessRunner`, which starts a short-lived
TIGER-DnR runtime with an already verified local model.  Tests can use a small
runner that writes a deterministic WAV without importing CUDA dependencies.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import wave
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .process_groups import process_group_spawn_options, signal_process_group

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "WANDB_DISABLED": "true",
    "WANDB_MODE": "offline",
}


class AudioSeparationError(RuntimeError):
    """A safe, serializable cinematic-separation failure."""

    def __init__(self, code: str, message_vi: str, *, retryable: bool) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable


class AudioSeparationCancelled(AudioSeparationError):
    """Cooperative cancellation requested by the caller."""

    def __init__(self) -> None:
        super().__init__(
            "audio_separation_cancelled",
            "Đã hủy tách lời thoại khỏi âm thanh",
            retryable=True,
        )


@dataclass(frozen=True, slots=True)
class SeparationProgress:
    """One monotonic progress event emitted by the public separator."""

    stage: str
    completed_permille: int
    message_vi: str

    def __post_init__(self) -> None:
        if not 0 <= self.completed_permille <= 1000:
            raise ValueError("Tiến độ tách âm thanh phải nằm trong khoảng 0..1000")

    @property
    def fraction(self) -> float:
        return self.completed_permille / 1000.0


@dataclass(frozen=True, slots=True)
class SeparationBackendRequest:
    """Paths supplied to a backend; every output must remain in ``work_dir``."""

    source_path: Path
    work_dir: Path
    accompaniment_path: Path


@dataclass(frozen=True, slots=True)
class BackendSeparationResult:
    """Untrusted backend output validated by :class:`CinematicAudioSeparator`."""

    accompaniment_path: Path
    backend_name: str
    model_id: str
    model_tree_sha256: str
    metrics: Mapping[str, int | float | str | bool | None]


@dataclass(frozen=True, slots=True)
class AudioSeparationMetrics:
    """Stable measurements for reports and phase acceptance tests."""

    elapsed_ms: int
    duration_us: int
    real_time_factor: float
    source_bytes: int
    output_bytes: int
    sample_rate: int
    channels: int
    sample_width_bytes: int
    frame_count: int
    backend: Mapping[str, int | float | str | bool | None]


@dataclass(frozen=True, slots=True)
class AudioSeparationResult:
    """A fully validated and atomically published accompaniment WAV."""

    accompaniment_path: Path
    checksum_sha256: str
    model_id: str
    model_tree_sha256: str
    backend_name: str
    metrics: AudioSeparationMetrics

    @property
    def path(self) -> Path:
        """Compatibility alias for generic artifact consumers."""

        return self.accompaniment_path

    @property
    def sha256(self) -> str:
        """Short compatibility alias for checksum consumers."""

        return self.checksum_sha256


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...


Cancellation = CancellationToken | Callable[[], bool]
ProgressCallback = Callable[[SeparationProgress], None]
BackendProgressCallback = Callable[[float], None]


class SeparationBackendRunner(Protocol):
    """Injectable boundary around a CUDA/runtime implementation."""

    async def run(
        self,
        request: SeparationBackendRequest,
        *,
        cancellation: Cancellation | None = None,
        on_progress: BackendProgressCallback | None = None,
    ) -> BackendSeparationResult: ...


class ProcessHandle(Protocol):
    returncode: int | None

    async def communicate(self) -> tuple[bytes | None, bytes | None]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., Awaitable[ProcessHandle]]


async def _default_process_factory(*command: str, **kwargs: object) -> ProcessHandle:
    return await asyncio.create_subprocess_exec(*command, **kwargs)


class CinematicAudioSeparator:
    """Create a music+effects WAV while excluding the dialogue stem."""

    def __init__(self, runner: SeparationBackendRunner) -> None:
        self._runner = runner

    async def separate(
        self,
        source_path: Path,
        output_path: Path,
        *,
        cancellation: Cancellation | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AudioSeparationResult:
        """Separate, validate, checksum, and atomically publish one artifact."""

        started = time.monotonic()
        source = _local_source(source_path)
        destination = output_path.resolve(strict=False)
        if destination.suffix.casefold() != ".wav":
            raise ValueError("File âm thanh nền đầu ra phải có phần mở rộng .wav")
        if destination == source:
            raise ValueError("File âm thanh nền không được trùng với file nguồn")

        _raise_if_cancelled(cancellation)
        _emit(on_progress, "preparing", 0, "Đang chuẩn bị tách lời thoại")

        work_dir = destination.parent / (
            f".{destination.name}.{uuid.uuid4().hex}.separation-part"
        )
        candidate = work_dir / "accompaniment.wav"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            work_dir.mkdir(parents=False, exist_ok=False)
        except OSError as error:
            raise AudioSeparationError(
                "audio_separation_output_unavailable",
                "Không thể chuẩn bị nơi lưu âm thanh nền",
                retryable=True,
            ) from error

        last_backend_progress = 0.0

        def backend_progress(fraction: float) -> None:
            nonlocal last_backend_progress
            try:
                normalized = float(fraction)
            except (TypeError, ValueError) as error:
                raise AudioSeparationError(
                    "invalid_audio_separation_progress",
                    "Backend tách âm thanh báo tiến độ không hợp lệ",
                    retryable=False,
                ) from error
            if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
                raise AudioSeparationError(
                    "invalid_audio_separation_progress",
                    "Backend tách âm thanh báo tiến độ không hợp lệ",
                    retryable=False,
                )
            normalized = max(last_backend_progress, normalized)
            last_backend_progress = normalized
            completed = 50 + round(normalized * 800)
            _emit(
                on_progress,
                "separating",
                completed,
                "Đang tách lời thoại, nhạc và hiệu ứng",
            )

        request = SeparationBackendRequest(
            source_path=source,
            work_dir=work_dir,
            accompaniment_path=candidate,
        )
        try:
            _emit(
                on_progress,
                "separating",
                50,
                "Đang tách lời thoại, nhạc và hiệu ứng",
            )
            backend_result = await self._runner.run(
                request,
                cancellation=cancellation,
                on_progress=backend_progress,
            )
            _raise_if_cancelled(cancellation)
            _emit(
                on_progress,
                "validating",
                875,
                "Đang kiểm tra âm thanh nền",
            )
            backend_path = _safe_backend_artifact(
                backend_result.accompaniment_path,
                work_dir,
            )
            wav = _inspect_pcm_wav(backend_path)
            checksum = _sha256_file(backend_path)
            output_bytes = backend_path.stat().st_size
            _raise_if_cancelled(cancellation)

            _emit(
                on_progress,
                "publishing",
                950,
                "Đang lưu âm thanh nền",
            )
            try:
                _fsync_file(backend_path)
                os.replace(backend_path, destination)
                _fsync_directory(destination.parent)
            except OSError as error:
                raise AudioSeparationError(
                    "audio_separation_output_unavailable",
                    "Không thể lưu âm thanh nền đã tách",
                    retryable=True,
                ) from error

            elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
            elapsed_seconds = elapsed_ms / 1000.0
            duration_seconds = wav.duration_us / 1_000_000.0
            metrics = AudioSeparationMetrics(
                elapsed_ms=elapsed_ms,
                duration_us=wav.duration_us,
                real_time_factor=(
                    elapsed_seconds / duration_seconds if duration_seconds else 0.0
                ),
                source_bytes=source.stat().st_size,
                output_bytes=output_bytes,
                sample_rate=wav.sample_rate,
                channels=wav.channels,
                sample_width_bytes=wav.sample_width_bytes,
                frame_count=wav.frame_count,
                backend=dict(backend_result.metrics),
            )
            _emit(on_progress, "complete", 1000, "Đã tách xong âm thanh nền")
            return AudioSeparationResult(
                accompaniment_path=destination,
                checksum_sha256=checksum,
                model_id=backend_result.model_id,
                model_tree_sha256=backend_result.model_tree_sha256,
                backend_name=backend_result.backend_name,
                metrics=metrics,
            )
        except (AudioSeparationError, asyncio.CancelledError):
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise AudioSeparationError(
                "audio_separation_failed",
                "Không thể tách lời thoại khỏi âm thanh nguồn",
                retryable=True,
            ) from error
        finally:
            with suppress(OSError):
                shutil.rmtree(work_dir)


class TigerDnrSubprocessRunner:
    """Run TIGER-DnR in a short-lived, offline-only Python subprocess.

    ``model_path`` and ``model_tree_sha256`` must come from the repository's
    offline model resolver.  This class never accepts a repository ID or URL.
    The subprocess installs the Python socket audit guard before importing the
    model runtime, and all common Hugging Face download paths are forced into
    offline mode through its environment.
    """

    def __init__(
        self,
        *,
        model_path: Path,
        model_id: str,
        model_tree_sha256: str,
        python_executable: Path | str = sys.executable,
        process_factory: ProcessFactory | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.1,
        stop_grace_seconds: float = 3.0,
        cuda_device: int = 0,
        device: str = "cuda",
        source_dir: Path | None = None,
        chunk_seconds: float = 120.0,
        context_seconds: float = 4.0,
        batch_size: int = 1,
    ) -> None:
        local_model = model_path.resolve(strict=False)
        if not local_model.is_dir():
            raise AudioSeparationError(
                "model_missing",
                "Không tìm thấy model TIGER-DnR đã xác minh trên máy",
                retryable=True,
            )
        if not model_id.strip():
            raise ValueError("ID model TIGER-DnR không được để trống")
        normalized_hash = model_tree_sha256.strip().casefold()
        if _SHA256_PATTERN.fullmatch(normalized_hash) is None:
            raise ValueError("SHA-256 cây model TIGER-DnR không hợp lệ")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("Thời gian giới hạn phải lớn hơn 0")
        if poll_interval_seconds <= 0 or stop_grace_seconds < 0:
            raise ValueError("Cấu hình theo dõi tiến trình TIGER-DnR không hợp lệ")
        if device not in {"cuda", "cpu"}:
            raise ValueError("Thiết bị TIGER-DnR phải là cuda hoặc cpu")
        if device == "cuda" and cuda_device != 0:
            raise ValueError(
                "TIGER-DnR chỉ chấp nhận GPU logical 0; "
                "hãy chọn GPU vật lý ở installer/container runtime"
            )
        local_source_dir: Path | None = None
        if source_dir is not None:
            local_source_dir = Path(source_dir).resolve(strict=False)
            if not local_source_dir.is_dir() or not (
                local_source_dir / "look2hear"
            ).is_dir():
                raise ValueError("Mã nguồn TIGER-DnR cục bộ không hợp lệ")
        if chunk_seconds < 12.0:
            raise ValueError("Mỗi chunk TIGER-DnR phải dài ít nhất 12 giây")
        if context_seconds < 0.0 or context_seconds > chunk_seconds / 2.0:
            raise ValueError("Phần context TIGER-DnR không hợp lệ")
        if batch_size < 1:
            raise ValueError("Batch size TIGER-DnR phải lớn hơn 0")

        self._model_path = local_model
        self._model_id = model_id.strip()
        self._model_tree_sha256 = normalized_hash
        self._python = os.fspath(python_executable)
        self._process_factory = process_factory or _default_process_factory
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_grace_seconds = stop_grace_seconds
        self._cuda_device = cuda_device
        self._device = device
        self._source_dir = local_source_dir
        self._chunk_seconds = chunk_seconds
        self._context_seconds = context_seconds
        self._batch_size = batch_size

    async def run(
        self,
        request: SeparationBackendRequest,
        *,
        cancellation: Cancellation | None = None,
        on_progress: BackendProgressCallback | None = None,
    ) -> BackendSeparationResult:
        source = _local_source(request.source_path)
        work_dir = request.work_dir.resolve(strict=True)
        output = request.accompaniment_path.resolve(strict=False)
        progress_path = work_dir / "tiger-progress.txt"
        _ensure_inside(output, work_dir)
        _raise_if_cancelled(cancellation)

        command = (
            self._python,
            "-m",
            "dub_server.audio_separation",
            "_tiger-runtime",
            "--input",
            os.fspath(source),
            "--output",
            os.fspath(output),
            "--model-path",
            os.fspath(self._model_path),
            "--device",
            self._device,
            "--chunk-seconds",
            f"{self._chunk_seconds:.6f}",
            "--context-seconds",
            f"{self._context_seconds:.6f}",
            "--batch-size",
            str(self._batch_size),
            "--progress-file",
            os.fspath(progress_path),
        )
        environment = os.environ.copy()
        environment.update(_OFFLINE_ENVIRONMENT)
        # Keep the GPU visibility contract established by the parent worker.
        # Native installs pin a physical GPU by UUID, while the NVIDIA
        # container runtime exposes the selected physical GPU as logical 0.
        # Replacing an inherited UUID with "0" here would select host GPU 0
        # when this short-lived subprocess starts outside a container.
        if self._device == "cuda":
            environment.setdefault("CUDA_VISIBLE_DEVICES", str(self._cuda_device))
        environment["TIGER_DNR_MODEL_PATH"] = os.fspath(self._model_path)
        if self._source_dir is not None:
            existing_python_path = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(
                value
                for value in (os.fspath(self._source_dir), existing_python_path)
                if value
            )

        process: ProcessHandle | None = None
        communication: asyncio.Task[tuple[bytes | None, bytes | None]] | None = None
        started = time.monotonic()
        reported_progress = 0.0
        if on_progress is not None:
            on_progress(0.0)
        try:
            try:
                process = await self._process_factory(
                    *command,
                    cwd=os.fspath(work_dir),
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **process_group_spawn_options(),
                )
            except OSError as error:
                raise AudioSeparationError(
                    "audio_separator_unavailable",
                    "Không thể khởi động TIGER-DnR",
                    retryable=False,
                ) from error

            communication = asyncio.create_task(process.communicate())
            while not communication.done():
                progress_value = _read_runtime_progress(progress_path)
                if (
                    progress_value is not None
                    and progress_value > reported_progress
                    and on_progress is not None
                ):
                    reported_progress = progress_value
                    on_progress(progress_value)
                if _is_cancelled(cancellation):
                    await self._stop_process(process, communication)
                    raise AudioSeparationCancelled()
                if (
                    self._timeout_seconds is not None
                    and time.monotonic() - started >= self._timeout_seconds
                ):
                    await self._stop_process(process, communication)
                    raise AudioSeparationError(
                        "audio_separation_timeout",
                        "Tách lời thoại vượt quá thời gian cho phép",
                        retryable=True,
                    )
                await asyncio.wait(
                    {communication},
                    timeout=self._poll_interval_seconds,
                )

            stdout, stderr = await communication
            if process.returncode != 0:
                diagnostic = b"\n".join((stdout or b"", stderr or b"")).decode(
                    "utf-8", errors="replace"
                )
                raise _backend_failure(diagnostic)
            if on_progress is not None:
                on_progress(1.0)
            return BackendSeparationResult(
                accompaniment_path=output,
                backend_name="tiger-dnr",
                model_id=self._model_id,
                model_tree_sha256=self._model_tree_sha256,
                metrics={
                    "device": self._device,
                    "cuda_device": (
                        self._cuda_device if self._device == "cuda" else None
                    ),
                    "runtime_elapsed_ms": max(
                        0, round((time.monotonic() - started) * 1000)
                    ),
                    "offline": True,
                },
            )
        except asyncio.CancelledError:
            if process is not None and communication is not None:
                await self._stop_process(process, communication)
            raise

    async def _stop_process(
        self,
        process: ProcessHandle,
        communication: asyncio.Task[tuple[bytes | None, bytes | None]],
    ) -> None:
        if communication.done() or process.returncode is not None:
            with suppress(Exception):
                await communication
            return
        signal_process_group(process, force=False)
        try:
            await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=self._stop_grace_seconds,
            )
            return
        except TimeoutError:
            pass
        signal_process_group(process, force=True)
        with suppress(Exception):
            await communication


@dataclass(frozen=True, slots=True)
class _WavInfo:
    sample_rate: int
    channels: int
    sample_width_bytes: int
    frame_count: int
    duration_us: int


def _local_source(path: Path) -> Path:
    raw = os.fspath(path)
    if "://" in raw or "\x00" in raw:
        raise AudioSeparationError(
            "non_local_audio_source",
            "Tách lời thoại chỉ chấp nhận file media cục bộ",
            retryable=False,
        )
    try:
        source = path.resolve(strict=True)
    except OSError as error:
        raise AudioSeparationError(
            "source_media_missing",
            "Không tìm thấy file media để tách lời thoại",
            retryable=True,
        ) from error
    if not source.is_file():
        raise AudioSeparationError(
            "source_media_missing",
            "Không tìm thấy file media để tách lời thoại",
            retryable=True,
        )
    return source


def _safe_backend_artifact(path: Path, work_dir: Path) -> Path:
    try:
        candidate = path.resolve(strict=True)
        root = work_dir.resolve(strict=True)
        _ensure_inside(candidate, root)
        if candidate.is_symlink() or not candidate.is_file():
            raise OSError("backend artifact is not a regular file")
    except (OSError, ValueError) as error:
        raise AudioSeparationError(
            "invalid_audio_separation_output",
            "TIGER-DnR không tạo được file âm thanh nền hợp lệ",
            retryable=False,
        ) from error
    return candidate


def _ensure_inside(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise AudioSeparationError(
            "unsafe_audio_separation_output",
            "Backend tách âm thanh trả về đường dẫn không an toàn",
            retryable=False,
        ) from error


def _inspect_pcm_wav(path: Path) -> _WavInfo:
    try:
        with wave.open(os.fspath(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_rate = stream.getframerate()
            sample_width = stream.getsampwidth()
            frame_count = stream.getnframes()
            compression = stream.getcomptype()
    except (OSError, EOFError, wave.Error) as error:
        raise AudioSeparationError(
            "invalid_audio_separation_output",
            "TIGER-DnR tạo file âm thanh nền không hợp lệ",
            retryable=False,
        ) from error
    if (
        compression != "NONE"
        or channels not in {1, 2}
        or sample_width != 2
        or not 8_000 <= sample_rate <= 192_000
        or frame_count <= 0
    ):
        raise AudioSeparationError(
            "invalid_audio_separation_output",
            "Âm thanh nền phải là PCM 16-bit mono hoặc stereo hợp lệ",
            retryable=False,
        )
    duration_us = (frame_count * 1_000_000 + sample_rate // 2) // sample_rate
    return _WavInfo(
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        frame_count=frame_count,
        duration_us=duration_us,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise AudioSeparationError(
            "invalid_audio_separation_output",
            "Không thể kiểm tra toàn vẹn âm thanh nền",
            retryable=True,
        ) from error
    return digest.hexdigest()


def _read_runtime_progress(path: Path) -> float | None:
    try:
        value = float(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return max(0.0, min(value, 1.0))


def _write_runtime_progress(path: Path | None, value: float) -> None:
    if path is None:
        return
    bounded = max(0.0, min(float(value), 1.0))
    temporary = path.with_name(f".{path.name}.part")
    temporary.write_text(f"{bounded:.9f}\n", encoding="ascii")
    os.replace(temporary, path)


def _is_cancelled(cancellation: Cancellation | None) -> bool:
    if cancellation is None:
        return False
    if callable(cancellation):
        return bool(cancellation())
    return bool(cancellation.is_cancelled())


def _raise_if_cancelled(cancellation: Cancellation | None) -> None:
    if _is_cancelled(cancellation):
        raise AudioSeparationCancelled()


def _emit(
    callback: ProgressCallback | None,
    stage: str,
    completed_permille: int,
    message_vi: str,
) -> None:
    if callback is not None:
        callback(
            SeparationProgress(
                stage=stage,
                completed_permille=completed_permille,
                message_vi=message_vi,
            )
        )


def _backend_failure(diagnostic: str) -> AudioSeparationError:
    normalized = diagnostic.casefold()
    if "out of memory" in normalized or "cuda oom" in normalized:
        return AudioSeparationError(
            "native_oom",
            "GPU không đủ bộ nhớ để tách lời thoại",
            retryable=True,
        )
    if "no module named" in normalized or "modulenotfounderror" in normalized:
        return AudioSeparationError(
            "audio_separator_unavailable",
            "Runtime TIGER-DnR chưa được cài đặt đầy đủ",
            retryable=False,
        )
    return AudioSeparationError(
        "audio_separation_failed",
        "TIGER-DnR không thể tách lời thoại khỏi âm thanh nguồn",
        retryable=True,
    )


def _fsync_file(path: Path) -> None:
    # Windows rejects ``FlushFileBuffers`` on a read-only handle.  Opening the
    # already validated candidate read/write does not alter its bytes and keeps
    # the durability step portable.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _runtime_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-path", required=True, type=Path)
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--chunk-seconds", type=float, default=120.0)
    parser.add_argument("--context-seconds", type=float, default=4.0)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--progress-file", type=Path)
    return parser


def _run_tiger_runtime(arguments: Sequence[str]) -> int:
    """Internal subprocess entry point; never called by the API process."""

    parsed = _runtime_parser().parse_args(arguments)
    source = _local_source(parsed.input)
    model_path = parsed.model_path.resolve(strict=True)
    output = parsed.output.resolve(strict=False)
    progress_path = (
        parsed.progress_file.resolve(strict=False)
        if parsed.progress_file is not None
        else None
    )
    if not model_path.is_dir():
        raise RuntimeError("TIGER-DnR model path is not a local directory")
    if parsed.chunk_seconds < 12.0:
        raise RuntimeError("TIGER-DnR chunk length must be at least 12 seconds")
    if parsed.context_seconds < 0.0 or parsed.context_seconds > parsed.chunk_seconds / 2.0:
        raise RuntimeError("TIGER-DnR context length is invalid")
    if parsed.batch_size < 1:
        raise RuntimeError("TIGER-DnR batch size must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)
    if progress_path is not None:
        _ensure_inside(progress_path, output.parent)
        _write_runtime_progress(progress_path, 0.0)
    for key, value in _OFFLINE_ENVIRONMENT.items():
        os.environ[key] = value

    # Install this before importing the upstream model loader.  Even if that
    # loader accidentally receives a repository ID in a future refactor, DNS
    # resolution and Internet sockets fail closed.
    from .offline import install_offline_network_guard

    install_offline_network_guard()

    decoded = output.parent / ".tiger-input.wav"
    ffmpeg = os.environ.get("DUB_FFMPEG_BINARY", "ffmpeg")
    torch_runtime: Any | None = None
    model: Any | None = None
    try:
        subprocess.run(
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
                os.fspath(source),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                os.fspath(decoded),
            ),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        _write_runtime_progress(progress_path, 0.03)
        import numpy as np
        import torch
        from look2hear.models import TIGERDNR

        torch_runtime = torch
        device = torch.device(parsed.device)
        model = TIGERDNR.from_pretrained(
            os.fspath(model_path),
            cache_dir=os.fspath(model_path),
        )
        model.to(device)
        model.eval()
        _write_runtime_progress(progress_path, 0.05)

        # Stream bounded chunks so a feature-length movie is never resident in
        # RAM or VRAM. Context is retained around each core chunk and cropped
        # after separation. The defaults align with TIGER's four-second hop,
        # avoiding boundary seams without accumulating timestamp drift.
        with wave.open(os.fspath(decoded), "rb") as reader:
            if (
                reader.getframerate() != 44_100
                or reader.getnchannels() != 2
                or reader.getsampwidth() != 2
            ):
                raise RuntimeError("Unexpected TIGER-DnR input WAV format")
            total_frames = reader.getnframes()
            chunk_frames = max(1, round(parsed.chunk_seconds * 44_100))
            context_frames = max(0, round(parsed.context_seconds * 44_100))
            with wave.open(os.fspath(output), "wb") as writer:
                writer.setnchannels(2)
                writer.setsampwidth(2)
                writer.setframerate(44_100)
                for core_start in range(0, total_frames, chunk_frames):
                    core_end = min(total_frames, core_start + chunk_frames)
                    read_start = max(0, core_start - context_frames)
                    read_end = min(total_frames, core_end + context_frames)
                    reader.setpos(read_start)
                    raw = reader.readframes(read_end - read_start)
                    samples = np.frombuffer(raw, dtype="<i2")
                    expected_values = (read_end - read_start) * 2
                    if samples.size != expected_values:
                        raise RuntimeError("Truncated TIGER-DnR decoded input")
                    waveform = torch.from_numpy(
                        samples.copy().reshape(-1, 2).T
                    ).to(dtype=torch.float32).div_(32768.0)
                    mixture = waveform.unsqueeze(0).to(device)
                    with torch.inference_mode():
                        # The dialogue network is intentionally skipped: only
                        # music and effects are retained in the final movie.
                        effects = model.wav_chunk_inference(
                            model.effect,
                            mixture,
                            batch_size=parsed.batch_size,
                        )[1]
                        music = model.wav_chunk_inference(
                            model.music,
                            mixture,
                            batch_size=parsed.batch_size,
                        )[0]
                    effects = _runtime_waveform(effects)
                    music = _runtime_waveform(music)
                    crop_start = core_start - read_start
                    crop_length = core_end - core_start
                    accompaniment = (
                        effects[:2, crop_start : crop_start + crop_length]
                        + music[:2, crop_start : crop_start + crop_length]
                    ).clamp(-1.0, 1.0)
                    if accompaniment.shape != (2, crop_length):
                        raise RuntimeError("TIGER-DnR returned a truncated stem")
                    pcm = (
                        accompaniment.transpose(0, 1)
                        .mul(32767.0)
                        .round()
                        .to(dtype=torch.int16)
                        .cpu()
                        .numpy()
                        .astype("<i2", copy=False)
                    )
                    writer.writeframesraw(pcm.tobytes())
                    del waveform, mixture, effects, music, accompaniment, pcm
                    _write_runtime_progress(
                        progress_path,
                        0.05 + 0.95 * (core_end / max(total_frames, 1)),
                    )
                writer.writeframes(b"")
    finally:
        # Releasing the allocator after every chunk forces synchronization and
        # defeats PyTorch's caching allocator. Drop the model and empty unused
        # blocks once when this short-lived runtime finishes instead.
        model = None
        if torch_runtime is not None:
            with suppress(Exception):
                torch_runtime.cuda.empty_cache()
        with suppress(OSError):
            decoded.unlink(missing_ok=True)
    return 0


def _runtime_waveform(value: Any) -> Any:
    while value.ndim > 2 and value.shape[0] == 1:
        value = value.squeeze(0)
    if value.ndim == 1:
        value = value.unsqueeze(0)
    if value.ndim != 2:
        raise RuntimeError("TIGER-DnR returned an unsupported stem shape")
    return value


def _main(arguments: Sequence[str] | None = None) -> int:
    values = tuple(sys.argv[1:] if arguments is None else arguments)
    if not values or values[0] != "_tiger-runtime":
        raise SystemExit("This module only exposes its internal TIGER runtime")
    return _run_tiger_runtime(values[1:])


if __name__ == "__main__":  # pragma: no cover - exercised on the GPU host
    raise SystemExit(_main())


__all__ = [
    "AudioSeparationCancelled",
    "AudioSeparationError",
    "AudioSeparationMetrics",
    "AudioSeparationResult",
    "BackendSeparationResult",
    "CancellationToken",
    "CinematicAudioSeparator",
    "SeparationBackendRequest",
    "SeparationBackendRunner",
    "SeparationProgress",
    "TigerDnrSubprocessRunner",
]
