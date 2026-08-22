"""Offline Vietnamese narration synthesis contracts and subprocess adapters.

The adapters in this module only accept verified local model paths.  They do
not import a model runtime and can therefore be used in the API process without
reserving GPU memory.  A process factory is injectable for deterministic unit
tests and for a future sandboxed subprocess launcher.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import sys
import time
import unicodedata
import wave
from array import array
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .process_groups import process_group_spawn_options, signal_process_group

_SPACE_PATTERN = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?…])")
_MISSING_SPACE_AFTER_PUNCTUATION = re.compile(r"([,;:!?])(?=[^\s\d])")
_ZERO_WIDTH = str.maketrans({"\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": ""})
_TYPOGRAPHY = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "…",
    }
)
_COMMON_VIETNAMESE_ABBREVIATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bTP\s*\.\s*HCM\b", re.IGNORECASE), "Thành phố Hồ Chí Minh"),
    (re.compile(r"\bTP\s*\.\s*HN\b", re.IGNORECASE), "Thành phố Hà Nội"),
    (re.compile(r"\bVN\b", re.IGNORECASE), "Việt Nam"),
)

TTS_SILENCE_THRESHOLD_DBFS = -45.0
TTS_SILENCE_PADDING_MS = 30
TTS_SILENCE_TRIM_VERSION = "pcm16-threshold-45dbfs-pad30ms-v1"


class NarrationError(RuntimeError):
    """A typed TTS failure safe to checkpoint and expose to clients."""

    def __init__(self, code: str, message_vi: str, *, retryable: bool) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SynthesizedNarration:
    """Metadata for one atomically published PCM WAV narration block."""

    path: Path
    text: str
    sample_rate: int
    channels: int
    sample_width_bytes: int
    frame_count: int
    duration_us: int
    native_speed: float
    backend: str


def trim_synthesized_narration_silence(
    narration: SynthesizedNarration,
) -> SynthesizedNarration:
    """Atomically trim low-level PCM silence while retaining natural margins.

    TTS adapters already guarantee mono 16-bit PCM WAV.  Samples below
    ``-45 dBFS`` at the leading and trailing edges are removed, except for a
    30 ms margin immediately surrounding the detected speech.  The original
    WAV remains untouched until a complete replacement has been written.
    """

    path = Path(narration.path).resolve(strict=False)
    metadata = _inspect_pcm_wav(path)
    try:
        with wave.open(os.fspath(path), "rb") as stream:
            pcm_bytes = stream.readframes(metadata.frame_count)
    except (OSError, EOFError, wave.Error) as exc:
        raise NarrationError(
            "tts_output_invalid",
            "Không thể đọc âm thanh TTS để loại bỏ khoảng lặng",
            retryable=True,
        ) from exc

    expected_bytes = metadata.frame_count * metadata.sample_width_bytes
    if len(pcm_bytes) != expected_bytes:
        raise NarrationError(
            "tts_output_invalid",
            "Dữ liệu PCM của âm thanh TTS không đầy đủ",
            retryable=True,
        )

    samples = array("h")
    samples.frombytes(pcm_bytes)
    if sys.byteorder != "little":  # WAV PCM is always little-endian.
        samples.byteswap()
    threshold = math.ceil(
        ((1 << 15) - 1) * math.pow(10.0, TTS_SILENCE_THRESHOLD_DBFS / 20.0)
    )
    first_audible = next(
        (index for index, sample in enumerate(samples) if abs(sample) >= threshold),
        None,
    )
    if first_audible is None:
        raise NarrationError(
            "tts_output_silent",
            "Bộ tổng hợp giọng nói chỉ tạo ra khoảng lặng",
            retryable=True,
        )
    last_audible = next(
        index
        for index in range(len(samples) - 1, first_audible - 1, -1)
        if abs(samples[index]) >= threshold
    )
    padding_frames = (
        metadata.sample_rate * TTS_SILENCE_PADDING_MS + 999
    ) // 1_000
    start_frame = max(0, first_audible - padding_frames)
    end_frame = min(metadata.frame_count, last_audible + 1 + padding_frames)
    trimmed_frame_count = end_frame - start_frame

    if start_frame != 0 or end_frame != metadata.frame_count:
        temporary = path.with_name(f".{path.stem}.silence-trim.part.wav")
        start_byte = start_frame * metadata.sample_width_bytes
        end_byte = end_frame * metadata.sample_width_bytes
        try:
            temporary.unlink(missing_ok=True)
            with wave.open(os.fspath(temporary), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(metadata.sample_rate)
                stream.writeframes(pcm_bytes[start_byte:end_byte])
            os.replace(temporary, path)
        except (OSError, wave.Error) as exc:
            raise NarrationError(
                "tts_output_unavailable",
                "Không thể lưu âm thanh TTS sau khi loại bỏ khoảng lặng",
                retryable=True,
            ) from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    duration_us = (
        trimmed_frame_count * 1_000_000 + metadata.sample_rate // 2
    ) // metadata.sample_rate
    return SynthesizedNarration(
        path=path,
        text=narration.text,
        sample_rate=metadata.sample_rate,
        channels=metadata.channels,
        sample_width_bytes=metadata.sample_width_bytes,
        frame_count=trimmed_frame_count,
        duration_us=duration_us,
        native_speed=narration.native_speed,
        backend=narration.backend,
    )


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...


Cancellation = CancellationToken | Callable[[], bool]
NarrationProgress = Callable[[int, int], None]


@runtime_checkable
class NarrationSynthesizer(Protocol):
    """Synthesize one Vietnamese block with a local model only."""

    async def synthesize(
        self,
        text: str,
        output_path: Path,
        *,
        speed: float = 1.0,
        cancellation: Cancellation | None = None,
        on_progress: NarrationProgress | None = None,
    ) -> SynthesizedNarration: ...

    async def close(self) -> None: ...


class ProcessHandle(Protocol):
    returncode: int | None

    async def communicate(
        self, input: bytes | None = None
    ) -> tuple[bytes | None, bytes | None]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., Awaitable[ProcessHandle]]


async def _default_process_factory(*command: str, **kwargs: object) -> ProcessHandle:
    return await asyncio.create_subprocess_exec(*command, **kwargs)


class AsyncLineReader(Protocol):
    async def readline(self) -> bytes: ...

    async def read(self, size: int = -1) -> bytes: ...


class AsyncLineWriter(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class PersistentProcessHandle(Protocol):
    """Subset of ``asyncio.subprocess.Process`` used by the VieNeu server."""

    returncode: int | None
    stdin: AsyncLineWriter | None
    stdout: AsyncLineReader | None
    stderr: AsyncLineReader | None

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


PersistentProcessFactory = Callable[..., Awaitable[PersistentProcessHandle]]


async def _default_persistent_process_factory(
    *command: str, **kwargs: object
) -> PersistentProcessHandle:
    return await asyncio.create_subprocess_exec(*command, **kwargs)


def normalize_vietnamese_for_tts(text: str) -> str:
    """Apply conservative Vietnamese text cleanup without changing meaning."""

    if not isinstance(text, str):
        raise NarrationError(
            "tts_text_invalid",
            "Nội dung thuyết minh không hợp lệ",
            retryable=False,
        )
    normalized = unicodedata.normalize("NFC", text.translate(_ZERO_WIDTH).translate(_TYPOGRAPHY))
    normalized = "".join(
        character
        for character in normalized
        if character in "\n\t" or not unicodedata.category(character).startswith("C")
    )
    for pattern, replacement in _COMMON_VIETNAMESE_ABBREVIATIONS:
        normalized = pattern.sub(replacement, normalized)
    normalized = normalized.replace("%", " phần trăm ").replace("&", " và ")
    normalized = _SPACE_PATTERN.sub(" ", normalized).strip()
    normalized = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", normalized)
    normalized = _MISSING_SPACE_AFTER_PUNCTUATION.sub(r"\1 ", normalized)
    if not normalized:
        raise NarrationError(
            "tts_text_empty",
            "Nội dung thuyết minh không được để trống",
            retryable=False,
        )
    return normalized


class _SubprocessNarrationSynthesizer:
    backend = "subprocess"

    def __init__(
        self,
        *,
        process_factory: ProcessFactory | None,
        poll_interval_seconds: float,
        stop_grace_seconds: float,
        timeout_seconds: float | None,
        environment: Mapping[str, str] | None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if stop_grace_seconds < 0:
            raise ValueError("stop_grace_seconds must not be negative")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._process_factory = process_factory or _default_process_factory
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_grace_seconds = stop_grace_seconds
        self._timeout_seconds = timeout_seconds
        self._environment = dict(environment or {})

    async def synthesize(
        self,
        text: str,
        output_path: Path,
        *,
        speed: float = 1.0,
        cancellation: Cancellation | None = None,
        on_progress: NarrationProgress | None = None,
    ) -> SynthesizedNarration:
        normalized_text = normalize_vietnamese_for_tts(text)
        if not 0.5 <= speed <= 2.0:
            raise NarrationError(
                "tts_speed_invalid",
                "Tốc độ tổng hợp giọng nói không hợp lệ",
                retryable=False,
            )
        destination = Path(output_path).resolve(strict=False)
        if destination.suffix.lower() != ".wav":
            raise NarrationError(
                "tts_output_invalid",
                "File TTS đầu ra phải có phần mở rộng .wav",
                retryable=False,
            )
        temporary = destination.with_name(f".{destination.stem}.tts.part.wav")
        text_file = destination.with_name(f".{destination.stem}.tts-input.part.txt")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.unlink(missing_ok=True)
            text_file.unlink(missing_ok=True)
        except OSError as exc:
            raise NarrationError(
                "tts_output_unavailable",
                "Không thể chuẩn bị nơi lưu âm thanh thuyết minh",
                retryable=True,
            ) from exc

        if is_cancelled(cancellation):
            raise NarrationError(
                "tts_cancelled",
                "Đã hủy tổng hợp giọng nói",
                retryable=True,
            )

        process: ProcessHandle | None = None
        communication: asyncio.Task[tuple[bytes | None, bytes | None]] | None = None
        started = time.monotonic()
        try:
            command, standard_input = self._build_invocation(
                normalized_text,
                speed=speed,
                output_path=temporary,
                text_file=text_file,
            )
            _validate_local_command(command)
            _report_progress(on_progress, 0, 1)
            try:
                process = await self._process_factory(
                    *command,
                    stdin=(
                        asyncio.subprocess.PIPE
                        if standard_input is not None
                        else asyncio.subprocess.DEVNULL
                    ),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=_offline_environment(self._environment),
                    **process_group_spawn_options(),
                )
            except OSError as exc:
                raise NarrationError(
                    "tts_runtime_unavailable",
                    "Không thể khởi động bộ tổng hợp giọng nói cục bộ",
                    retryable=False,
                ) from exc

            communication = asyncio.create_task(process.communicate(standard_input))
            while not communication.done():
                if is_cancelled(cancellation):
                    await self._stop_process(process, communication)
                    raise NarrationError(
                        "tts_cancelled",
                        "Đã hủy tổng hợp giọng nói",
                        retryable=True,
                    )
                if (
                    self._timeout_seconds is not None
                    and time.monotonic() - started >= self._timeout_seconds
                ):
                    await self._stop_process(process, communication)
                    raise NarrationError(
                        "tts_timeout",
                        "Tổng hợp giọng nói vượt quá thời gian cho phép",
                        retryable=True,
                    )
                await asyncio.wait({communication}, timeout=self._poll_interval_seconds)

            _, stderr = await communication
            if process.returncode != 0:
                diagnostic = (stderr or b"").decode("utf-8", errors="replace").casefold()
                missing_model = any(
                    marker in diagnostic
                    for marker in ("no such file", "not found", "model path", "model_path")
                )
                raise NarrationError(
                    "tts_model_missing" if missing_model else "tts_failed",
                    (
                        "Không tìm thấy model TTS đã cài đặt"
                        if missing_model
                        else "Tổng hợp giọng nói tiếng Việt thất bại"
                    ),
                    retryable=not missing_model,
                )

            metadata = _inspect_pcm_wav(temporary)
            try:
                os.replace(temporary, destination)
            except OSError as exc:
                raise NarrationError(
                    "tts_output_unavailable",
                    "Không thể lưu âm thanh thuyết minh",
                    retryable=True,
                ) from exc
            _report_progress(on_progress, 1, 1)
            return SynthesizedNarration(
                path=destination,
                text=normalized_text,
                sample_rate=metadata.sample_rate,
                channels=metadata.channels,
                sample_width_bytes=metadata.sample_width_bytes,
                frame_count=metadata.frame_count,
                duration_us=metadata.duration_us,
                native_speed=speed,
                backend=self.backend,
            )
        except asyncio.CancelledError:
            if process is not None and communication is not None:
                await self._stop_process(process, communication)
            raise
        except NarrationError:
            raise
        except (OSError, ValueError, wave.Error) as exc:
            raise NarrationError(
                "tts_failed",
                "Tổng hợp giọng nói tiếng Việt thất bại",
                retryable=True,
            ) from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            with suppress(OSError):
                text_file.unlink(missing_ok=True)

    async def close(self) -> None:
        """Subprocess adapters hold no model or process between calls."""

    def _build_invocation(
        self,
        text: str,
        *,
        speed: float,
        output_path: Path,
        text_file: Path,
    ) -> tuple[tuple[str, ...], bytes | None]:
        raise NotImplementedError

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
                asyncio.shield(communication), timeout=self._stop_grace_seconds
            )
            return
        except TimeoutError:
            pass
        signal_process_group(process, force=True)
        with suppress(Exception):
            await communication


class PiperNarrationSynthesizer(_SubprocessNarrationSynthesizer):
    """Production adapter for the local Piper CLI and a local ONNX voice."""

    backend = "piper"

    def __init__(
        self,
        model_path: Path,
        *,
        binary: str | Path = "piper",
        config_path: Path | None = None,
        speaker_id: int | None = None,
        process_factory: ProcessFactory | None = None,
        poll_interval_seconds: float = 0.05,
        stop_grace_seconds: float = 1.0,
        timeout_seconds: float | None = 300.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            process_factory=process_factory,
            poll_interval_seconds=poll_interval_seconds,
            stop_grace_seconds=stop_grace_seconds,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
        if speaker_id is not None and speaker_id < 0:
            raise ValueError("speaker_id must not be negative")
        self._model_path = Path(model_path)
        self._config_path = None if config_path is None else Path(config_path)
        self._binary = binary
        self._speaker_id = speaker_id

    def _build_invocation(
        self,
        text: str,
        *,
        speed: float,
        output_path: Path,
        text_file: Path,
    ) -> tuple[tuple[str, ...], bytes | None]:
        executable = _resolve_executable(self._binary)
        model = _require_local_path(self._model_path, file_only=True)
        command = [
            executable,
            "--model",
            os.fspath(model),
            "--output_file",
            os.fspath(output_path),
            "--length_scale",
            f"{1.0 / speed:.8f}",
        ]
        if self._config_path is not None:
            command.extend(
                ["--config", os.fspath(_require_local_path(self._config_path, file_only=True))]
            )
        if self._speaker_id is not None:
            command.extend(["--speaker", str(self._speaker_id)])
        return tuple(command), f"{text}\n".encode()


VieNeuArgumentBuilder = Callable[
    [Path, Path, Path, Path, Path | None, float], Sequence[str]
]


def _default_vieneu_arguments(
    entrypoint: Path,
    model_path: Path,
    text_file: Path,
    output_path: Path,
    reference_audio: Path | None,
    speed: float,
) -> Sequence[str]:
    arguments = [
        os.fspath(entrypoint),
        "--model-path",
        os.fspath(model_path),
        "--text-file",
        os.fspath(text_file),
        "--output-file",
        os.fspath(output_path),
        "--speed",
        f"{speed:.8f}",
        "--local-files-only",
    ]
    if reference_audio is not None:
        arguments.extend(["--reference-audio", os.fspath(reference_audio)])
    return arguments


class VieNeuNarrationSynthesizer(_SubprocessNarrationSynthesizer):
    """Production adapter for a pinned local VieNeu inference entrypoint.

    VieNeu releases do not currently expose one stable CLI.  ``argument_builder``
    keeps the adapter production-usable with a pinned wrapper while preserving
    the local-path and offline-environment guardrails.
    """

    backend = "vieneu"

    def __init__(
        self,
        model_path: Path,
        entrypoint: Path,
        *,
        python_binary: str | Path,
        reference_audio: Path | None = None,
        argument_builder: VieNeuArgumentBuilder | None = None,
        process_factory: ProcessFactory | None = None,
        poll_interval_seconds: float = 0.05,
        stop_grace_seconds: float = 1.0,
        timeout_seconds: float | None = 600.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            process_factory=process_factory,
            poll_interval_seconds=poll_interval_seconds,
            stop_grace_seconds=stop_grace_seconds,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
        self._model_path = Path(model_path)
        self._entrypoint = Path(entrypoint)
        self._python_binary = python_binary
        self._reference_audio = (
            None if reference_audio is None else Path(reference_audio)
        )
        self._argument_builder = argument_builder or _default_vieneu_arguments

    def _build_invocation(
        self,
        text: str,
        *,
        speed: float,
        output_path: Path,
        text_file: Path,
    ) -> tuple[tuple[str, ...], bytes | None]:
        executable = _resolve_executable(self._python_binary)
        entrypoint = _require_local_path(self._entrypoint, file_only=True)
        model = _require_local_path(self._model_path, file_only=False)
        reference = (
            None
            if self._reference_audio is None
            else _require_local_path(self._reference_audio, file_only=True)
        )
        try:
            text_file.write_text(f"{text}\n", encoding="utf-8")
        except OSError as exc:
            raise NarrationError(
                "tts_input_unavailable",
                "Không thể chuẩn bị nội dung cho bộ tổng hợp giọng nói",
                retryable=True,
            ) from exc
        arguments = tuple(
            str(value)
            for value in self._argument_builder(
                entrypoint,
                model,
                text_file,
                output_path,
                reference,
                speed,
            )
        )
        if not arguments:
            raise NarrationError(
                "tts_runtime_invalid",
                "Cấu hình bộ tổng hợp VieNeu không hợp lệ",
                retryable=False,
            )
        return (executable, *arguments), None


class PersistentVieNeuNarrationSynthesizer:
    """Persistent local-only VieNeu adapter using a JSON-lines stdio protocol.

    The child process loads the GPU model and ONNX codec exactly once, then
    serves sequential synthesis requests.  No port or socket is opened.  A
    cancelled, timed-out, crashed, or protocol-invalid child is discarded so
    the next call starts a clean runtime instead of reusing uncertain CUDA
    state.
    """

    backend = "vieneu"
    _PROTOCOL_VERSION = 1
    _MAX_PROTOCOL_LINE_BYTES = 64 * 1024
    _MAX_STDERR_BYTES = 32 * 1024

    def __init__(
        self,
        model_path: Path,
        entrypoint: Path,
        *,
        python_binary: str | Path,
        process_factory: PersistentProcessFactory | None = None,
        poll_interval_seconds: float = 0.05,
        stop_grace_seconds: float = 2.0,
        startup_timeout_seconds: float | None = 600.0,
        timeout_seconds: float | None = 600.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if stop_grace_seconds < 0:
            raise ValueError("stop_grace_seconds must not be negative")
        if startup_timeout_seconds is not None and startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._model_path = Path(model_path)
        self._entrypoint = Path(entrypoint)
        self._python_binary = python_binary
        self._process_factory = (
            process_factory or _default_persistent_process_factory
        )
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_grace_seconds = stop_grace_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._timeout_seconds = timeout_seconds
        self._environment = dict(environment or {})
        self._operation_lock = asyncio.Lock()
        self._process: PersistentProcessHandle | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_buffer = bytearray()
        self._next_request_id = 1
        self._closed = False

    async def synthesize(
        self,
        text: str,
        output_path: Path,
        *,
        speed: float = 1.0,
        cancellation: Cancellation | None = None,
        on_progress: NarrationProgress | None = None,
    ) -> SynthesizedNarration:
        normalized_text = normalize_vietnamese_for_tts(text)
        if not 0.5 <= speed <= 2.0:
            raise NarrationError(
                "tts_speed_invalid",
                "Tốc độ tổng hợp giọng nói không hợp lệ",
                retryable=False,
            )
        destination = Path(output_path).resolve(strict=False)
        if destination.suffix.lower() != ".wav":
            raise NarrationError(
                "tts_output_invalid",
                "File TTS đầu ra phải có phần mở rộng .wav",
                retryable=False,
            )

        async with self._operation_lock:
            if self._closed:
                raise NarrationError(
                    "tts_runtime_closed",
                    "Bộ tổng hợp giọng nói đã được đóng",
                    retryable=False,
                )
            temporary = destination.with_name(f".{destination.stem}.tts.part.wav")
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise NarrationError(
                    "tts_output_unavailable",
                    "Không thể chuẩn bị nơi lưu âm thanh thuyết minh",
                    retryable=True,
                ) from exc
            if is_cancelled(cancellation):
                raise NarrationError(
                    "tts_cancelled",
                    "Đã hủy tổng hợp giọng nói",
                    retryable=True,
                )

            _report_progress(on_progress, 0, 1)
            request_started = False
            try:
                await self._ensure_process(cancellation)
                request_id = self._next_request_id
                self._next_request_id += 1
                request_started = True
                await self._send_message(
                    {
                        "type": "synthesize",
                        "protocol": self._PROTOCOL_VERSION,
                        "id": request_id,
                        "text": normalized_text,
                        "output_file": os.fspath(temporary),
                        "speed": speed,
                    }
                )
                response = await self._read_message(
                    cancellation=cancellation,
                    timeout_seconds=self._timeout_seconds,
                )
                self._validate_result(response, request_id)
                metadata = _inspect_pcm_wav(temporary)
                try:
                    os.replace(temporary, destination)
                except OSError as exc:
                    raise NarrationError(
                        "tts_output_unavailable",
                        "Không thể lưu âm thanh thuyết minh",
                        retryable=True,
                    ) from exc
                _report_progress(on_progress, 1, 1)
                return SynthesizedNarration(
                    path=destination,
                    text=normalized_text,
                    sample_rate=metadata.sample_rate,
                    channels=metadata.channels,
                    sample_width_bytes=metadata.sample_width_bytes,
                    frame_count=metadata.frame_count,
                    duration_us=metadata.duration_us,
                    native_speed=speed,
                    backend=self.backend,
                )
            except asyncio.CancelledError:
                await self._discard_process()
                raise
            except NarrationError:
                # Once a request has been written, any failure can leave the
                # synchronous CUDA engine mid-inference.  Restart it before a
                # retry.  Startup failures are discarded by _ensure_process.
                if request_started:
                    await self._discard_process()
                raise
            except (OSError, ValueError, wave.Error) as exc:
                await self._discard_process()
                raise NarrationError(
                    "tts_failed",
                    "Tổng hợp giọng nói tiếng Việt thất bại",
                    retryable=True,
                ) from exc
            finally:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    async def close(self) -> None:
        """Close the resident engine and release its GPU memory."""

        async with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            if process is None:
                return
            try:
                await self._send_message(
                    {"type": "close", "protocol": self._PROTOCOL_VERSION}
                )
                response = await self._read_message(
                    cancellation=None,
                    timeout_seconds=max(self._stop_grace_seconds, 0.1),
                )
                if response != {
                    "type": "closed",
                    "protocol": self._PROTOCOL_VERSION,
                }:
                    raise NarrationError(
                        "tts_protocol_error",
                        "Bộ tổng hợp giọng nói trả về giao thức không hợp lệ",
                        retryable=True,
                    )
                await asyncio.wait_for(
                    process.wait(), timeout=max(self._stop_grace_seconds, 0.1)
                )
                self._process = None
                await self._finish_stderr_task()
                self._close_stdin(process)
            except asyncio.CancelledError:
                await asyncio.shield(self._discard_process())
                raise
            except (NarrationError, OSError, TimeoutError, ValueError):
                await self._discard_process()

    async def _ensure_process(self, cancellation: Cancellation | None) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        await self._discard_process()
        executable = _resolve_executable(self._python_binary)
        entrypoint = _require_local_path(self._entrypoint, file_only=True)
        model = _require_local_path(self._model_path, file_only=False)
        command = (
            executable,
            os.fspath(entrypoint),
            "--model-path",
            os.fspath(model),
            "--server",
            "--local-files-only",
        )
        _validate_local_command(command)
        self._stderr_buffer.clear()
        try:
            process = await self._process_factory(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_offline_environment(self._environment),
                **process_group_spawn_options(),
            )
        except OSError as exc:
            raise NarrationError(
                "tts_runtime_unavailable",
                "Không thể khởi động bộ tổng hợp giọng nói cục bộ",
                retryable=False,
            ) from exc
        if process.stdin is None or process.stdout is None:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(Exception):
                await process.wait()
            raise NarrationError(
                "tts_runtime_invalid",
                "Bộ tổng hợp giọng nói không hỗ trợ giao thức cục bộ",
                retryable=False,
            )
        self._process = process
        if process.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._capture_stderr(process.stderr)
            )
        try:
            ready = await self._read_message(
                cancellation=cancellation,
                timeout_seconds=self._startup_timeout_seconds,
            )
            if ready != {
                "type": "ready",
                "protocol": self._PROTOCOL_VERSION,
            }:
                raise NarrationError(
                    "tts_protocol_error",
                    "Bộ tổng hợp giọng nói trả về giao thức không hợp lệ",
                    retryable=True,
                )
        except NarrationError:
            await self._discard_process()
            raise

    async def _send_message(self, message: Mapping[str, object]) -> None:
        process = self._process
        writer = None if process is None else process.stdin
        if process is None or process.returncode is not None or writer is None:
            raise NarrationError(
                "tts_runtime_exited",
                "Bộ tổng hợp giọng nói đã dừng ngoài dự kiến",
                retryable=True,
            )
        try:
            payload = (
                json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            if len(payload) > self._MAX_PROTOCOL_LINE_BYTES:
                raise NarrationError(
                    "tts_text_too_long",
                    "Nội dung thuyết minh vượt quá giới hạn của bộ tổng hợp",
                    retryable=False,
                )
            writer.write(payload)
            await writer.drain()
        except NarrationError:
            raise
        except (BrokenPipeError, ConnectionError, OSError, RuntimeError) as exc:
            raise NarrationError(
                "tts_runtime_exited",
                "Bộ tổng hợp giọng nói đã dừng ngoài dự kiến",
                retryable=True,
            ) from exc

    async def _read_message(
        self,
        *,
        cancellation: Cancellation | None,
        timeout_seconds: float | None,
    ) -> dict[str, object]:
        process = self._process
        reader = None if process is None else process.stdout
        if process is None or reader is None:
            raise NarrationError(
                "tts_runtime_exited",
                "Bộ tổng hợp giọng nói đã dừng ngoài dự kiến",
                retryable=True,
            )
        started = time.monotonic()
        read_task = asyncio.create_task(reader.readline())
        try:
            while not read_task.done():
                if is_cancelled(cancellation):
                    raise NarrationError(
                        "tts_cancelled",
                        "Đã hủy tổng hợp giọng nói",
                        retryable=True,
                    )
                if (
                    timeout_seconds is not None
                    and time.monotonic() - started >= timeout_seconds
                ):
                    raise NarrationError(
                        "tts_timeout",
                        "Tổng hợp giọng nói vượt quá thời gian cho phép",
                        retryable=True,
                    )
                await asyncio.wait(
                    {read_task}, timeout=self._poll_interval_seconds
                )
            raw = await read_task
        except BaseException:
            if not read_task.done():
                read_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await read_task
            raise
        if not raw:
            raise NarrationError(
                "tts_runtime_exited",
                "Bộ tổng hợp giọng nói đã dừng ngoài dự kiến",
                retryable=True,
            )
        if len(raw) > self._MAX_PROTOCOL_LINE_BYTES:
            raise NarrationError(
                "tts_protocol_error",
                "Bộ tổng hợp giọng nói trả về dữ liệu không hợp lệ",
                retryable=True,
            )
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NarrationError(
                "tts_protocol_error",
                "Bộ tổng hợp giọng nói trả về dữ liệu không hợp lệ",
                retryable=True,
            ) from exc
        if not isinstance(decoded, dict) or any(
            not isinstance(key, str) for key in decoded
        ):
            raise NarrationError(
                "tts_protocol_error",
                "Bộ tổng hợp giọng nói trả về dữ liệu không hợp lệ",
                retryable=True,
            )
        return decoded

    def _validate_result(self, response: Mapping[str, object], request_id: int) -> None:
        if (
            response.get("type") != "result"
            or response.get("protocol") != self._PROTOCOL_VERSION
            or response.get("id") != request_id
            or not isinstance(response.get("ok"), bool)
        ):
            raise NarrationError(
                "tts_protocol_error",
                "Bộ tổng hợp giọng nói trả về giao thức không hợp lệ",
                retryable=True,
            )
        if response["ok"] is not True:
            raise NarrationError(
                "tts_failed",
                "Tổng hợp giọng nói tiếng Việt thất bại",
                retryable=True,
            )

    async def _capture_stderr(self, reader: AsyncLineReader) -> None:
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                self._stderr_buffer.extend(chunk)
                overflow = len(self._stderr_buffer) - self._MAX_STDERR_BYTES
                if overflow > 0:
                    del self._stderr_buffer[:overflow]
        except (OSError, RuntimeError):
            return

    async def _discard_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            await self._finish_stderr_task()
            return
        self._close_stdin(process)
        if process.returncode is None:
            signal_process_group(process, force=False)
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=max(self._stop_grace_seconds, 0.001)
                )
            except (TimeoutError, RuntimeError):
                signal_process_group(process, force=True)
                with suppress(Exception):
                    await process.wait()
        else:
            with suppress(Exception):
                await process.wait()
        await self._finish_stderr_task()

    async def _finish_stderr_task(self) -> None:
        task = self._stderr_task
        self._stderr_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    @staticmethod
    def _close_stdin(process: PersistentProcessHandle) -> None:
        if process.stdin is None:
            return
        with suppress(BrokenPipeError, ConnectionError, OSError, RuntimeError):
            process.stdin.close()


@dataclass(frozen=True, slots=True)
class _WavMetadata:
    sample_rate: int
    channels: int
    sample_width_bytes: int
    frame_count: int
    duration_us: int


def _inspect_pcm_wav(path: Path) -> _WavMetadata:
    try:
        with wave.open(os.fspath(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_rate = stream.getframerate()
            sample_width = stream.getsampwidth()
            frame_count = stream.getnframes()
            compression = stream.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise NarrationError(
            "tts_output_invalid",
            "Bộ tổng hợp tạo file WAV không hợp lệ",
            retryable=True,
        ) from exc
    if (
        channels != 1
        or sample_width != 2
        or not 8_000 <= sample_rate <= 192_000
        or frame_count <= 0
        or compression != "NONE"
    ):
        raise NarrationError(
            "tts_output_invalid",
            "Âm thanh TTS phải là PCM 16-bit mono hợp lệ",
            retryable=True,
        )
    duration_us = (frame_count * 1_000_000 + sample_rate // 2) // sample_rate
    return _WavMetadata(
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        frame_count=frame_count,
        duration_us=duration_us,
    )


def _resolve_executable(value: str | Path) -> str:
    candidate = os.fspath(value)
    if not candidate.strip() or "://" in candidate:
        raise NarrationError(
            "tts_runtime_unavailable",
            "Đường dẫn bộ tổng hợp giọng nói không hợp lệ",
            retryable=False,
        )
    has_separator = os.sep in candidate or (os.altsep is not None and os.altsep in candidate)
    if Path(candidate).is_absolute() or has_separator:
        # Preserve interpreter symlinks such as ``.venv/bin/python``.  Resolving
        # that symlink to ``/usr/bin/python`` silently drops the virtualenv and
        # makes native dependencies (for example sea_g2p) disappear.
        absolute = Path(os.path.abspath(candidate))
        if not absolute.is_file():
            raise NarrationError(
                "tts_runtime_unavailable",
                "Bộ tổng hợp giọng nói cục bộ không hợp lệ",
                retryable=False,
            )
        return os.fspath(absolute)
    resolved_executable = shutil.which(candidate)
    if resolved_executable is None:
        raise NarrationError(
            "tts_runtime_unavailable",
            "Không tìm thấy bộ tổng hợp giọng nói cục bộ",
            retryable=False,
        )
    return resolved_executable


def _require_local_path(path: Path, *, file_only: bool) -> Path:
    raw = os.fspath(path)
    if "://" in raw:
        raise NarrationError(
            "tts_model_invalid",
            "Model TTS phải là tài nguyên cục bộ",
            retryable=False,
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise NarrationError(
            "tts_model_missing",
            "Không tìm thấy model TTS đã cài đặt",
            retryable=False,
        ) from exc
    if file_only and not resolved.is_file():
        raise NarrationError(
            "tts_model_invalid",
            "File model TTS không hợp lệ",
            retryable=False,
        )
    if not file_only and not (resolved.is_file() or resolved.is_dir()):
        raise NarrationError(
            "tts_model_invalid",
            "Thư mục model TTS không hợp lệ",
            retryable=False,
        )
    return resolved


def _validate_local_command(command: Sequence[str]) -> None:
    if not command or any(not isinstance(value, str) or not value for value in command):
        raise NarrationError(
            "tts_runtime_invalid",
            "Lệnh tổng hợp giọng nói không hợp lệ",
            retryable=False,
        )
    if any("://" in value for value in command):
        raise NarrationError(
            "tts_network_forbidden",
            "Bộ tổng hợp giọng nói không được dùng tài nguyên mạng",
            retryable=False,
        )


def _offline_environment(extra: Mapping[str, str]) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(extra)
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "DO_NOT_TRACK": "1",
        }
    )
    return environment


def is_cancelled(cancellation: Cancellation | None) -> bool:
    if cancellation is None:
        return False
    try:
        return bool(cancellation() if callable(cancellation) else cancellation.is_cancelled())
    except Exception as exc:
        raise NarrationError(
            "tts_cancellation_invalid",
            "Không thể kiểm tra trạng thái hủy TTS",
            retryable=False,
        ) from exc


def _report_progress(
    callback: NarrationProgress | None, completed: int, total: int
) -> None:
    if callback is None:
        return
    try:
        callback(completed, total)
    except Exception as exc:
        raise NarrationError(
            "tts_progress_failed",
            "Không thể cập nhật tiến độ tổng hợp giọng nói",
            retryable=False,
        ) from exc


__all__ = [
    "TTS_SILENCE_PADDING_MS",
    "TTS_SILENCE_THRESHOLD_DBFS",
    "TTS_SILENCE_TRIM_VERSION",
    "Cancellation",
    "CancellationToken",
    "NarrationError",
    "NarrationProgress",
    "NarrationSynthesizer",
    "PersistentProcessFactory",
    "PersistentVieNeuNarrationSynthesizer",
    "PiperNarrationSynthesizer",
    "ProcessFactory",
    "SynthesizedNarration",
    "VieNeuArgumentBuilder",
    "VieNeuNarrationSynthesizer",
    "is_cancelled",
    "normalize_vietnamese_for_tts",
    "trim_synthesized_narration_silence",
]
