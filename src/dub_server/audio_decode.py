"""Offline audio extraction to deterministic mono 16 kHz PCM WAV files."""

from __future__ import annotations

import asyncio
import os
import time
import wave
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class AudioDecodeError(RuntimeError):
    """A safe, serializable audio-decode failure."""

    def __init__(self, code: str, message_vi: str, *, retryable: bool) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DecodedAudio:
    """Metadata for a validated PCM WAV artifact."""

    path: Path
    sample_rate: int
    channels: int
    sample_width_bytes: int
    frame_count: int
    duration_us: int


class CancellationToken(Protocol):
    """Minimal cancellation contract accepted by the decoder."""

    def is_cancelled(self) -> bool: ...


Cancellation = CancellationToken | Callable[[], bool]


class ProcessHandle(Protocol):
    returncode: int | None

    async def communicate(self) -> tuple[bytes | None, bytes | None]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., Awaitable[ProcessHandle]]


async def _default_process_factory(
    *command: str,
    **kwargs: object,
) -> ProcessHandle:
    return await asyncio.create_subprocess_exec(*command, **kwargs)


class FfmpegAudioDecoder:
    """Decode one local media audio stream without enabling network protocols.

    ``audio_stream_index`` is the absolute ffprobe stream index. When omitted,
    FFmpeg selects the first audio stream. The caller supplies the expected
    media duration so a truncated or unexpectedly padded decode fails closed.
    """

    def __init__(
        self,
        *,
        ffmpeg_binary: str = "ffmpeg",
        process_factory: ProcessFactory | None = None,
        duration_tolerance_us: int = 20_000,
        poll_interval_seconds: float = 0.05,
        stop_grace_seconds: float = 1.0,
        timeout_seconds: float | None = None,
    ) -> None:
        if duration_tolerance_us < 0:
            raise ValueError("Dung sai thời lượng không được âm")
        if poll_interval_seconds <= 0:
            raise ValueError("Chu kỳ kiểm tra hủy phải lớn hơn 0")
        if stop_grace_seconds < 0:
            raise ValueError("Thời gian chờ dừng FFmpeg không được âm")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("Thời gian giới hạn phải lớn hơn 0")
        self._ffmpeg = ffmpeg_binary
        self._process_factory = process_factory or _default_process_factory
        self._duration_tolerance_us = duration_tolerance_us
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_grace_seconds = stop_grace_seconds
        self._timeout_seconds = timeout_seconds

    async def decode(
        self,
        media_path: Path,
        output_path: Path,
        *,
        expected_duration_us: int,
        audio_stream_index: int | None = None,
        cancellation: Cancellation | None = None,
    ) -> DecodedAudio:
        """Decode, validate, and atomically publish a WAV artifact."""

        if expected_duration_us <= 0:
            raise ValueError("Thời lượng media phải lớn hơn 0")
        if audio_stream_index is not None and audio_stream_index < 0:
            raise ValueError("Chỉ số luồng âm thanh không được âm")
        if output_path.suffix.lower() != ".wav":
            raise ValueError("Đường dẫn đầu ra phải có phần mở rộng .wav")

        try:
            source = media_path.resolve(strict=True)
        except OSError as exc:
            raise AudioDecodeError(
                "source_media_missing",
                "Không tìm thấy file media để tách âm thanh",
                retryable=True,
            ) from exc
        if not source.is_file():
            raise AudioDecodeError(
                "source_media_missing",
                "Không tìm thấy file media để tách âm thanh",
                retryable=True,
            )

        destination = output_path.resolve(strict=False)
        if destination == source:
            raise ValueError("File đầu ra không được trùng file media nguồn")
        temporary = destination.with_name(f".{destination.name}.part")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise AudioDecodeError(
                "audio_output_unavailable",
                "Không thể chuẩn bị nơi lưu âm thanh đã tách",
                retryable=True,
            ) from exc

        if _is_cancelled(cancellation):
            raise AudioDecodeError(
                "audio_decode_cancelled",
                "Đã hủy tách âm thanh",
                retryable=True,
            )

        stream_selector = (
            f"0:{audio_stream_index}"
            if audio_stream_index is not None
            else "0:a:0"
        )
        command = (
            self._ffmpeg,
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
            stream_selector,
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            os.fspath(temporary),
        )

        process: ProcessHandle | None = None
        communication: asyncio.Task[tuple[bytes | None, bytes | None]] | None = None
        started = time.monotonic()
        try:
            try:
                process = await self._process_factory(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise AudioDecodeError(
                    "audio_decoder_unavailable",
                    "Không thể khởi động FFmpeg để tách âm thanh",
                    retryable=False,
                ) from exc

            communication = asyncio.create_task(process.communicate())
            while not communication.done():
                if _is_cancelled(cancellation):
                    await self._stop_process(process, communication)
                    raise AudioDecodeError(
                        "audio_decode_cancelled",
                        "Đã hủy tách âm thanh",
                        retryable=True,
                    )
                if (
                    self._timeout_seconds is not None
                    and time.monotonic() - started >= self._timeout_seconds
                ):
                    await self._stop_process(process, communication)
                    raise AudioDecodeError(
                        "audio_decode_timeout",
                        "Tách âm thanh vượt quá thời gian cho phép",
                        retryable=True,
                    )
                await asyncio.wait(
                    {communication},
                    timeout=self._poll_interval_seconds,
                )

            _, stderr = await communication
            if process.returncode != 0:
                diagnostic = (stderr or b"").decode("utf-8", errors="replace")
                if _missing_audio_stream(diagnostic):
                    raise AudioDecodeError(
                        "no_audio_stream",
                        "File media không chứa luồng âm thanh đã chọn",
                        retryable=False,
                    )
                raise AudioDecodeError(
                    "audio_decode_failed",
                    "FFmpeg không thể tách âm thanh từ file media",
                    retryable=False,
                )

            decoded = _inspect_wav(
                temporary,
                expected_duration_us=expected_duration_us,
                tolerance_us=self._duration_tolerance_us,
            )
            try:
                os.replace(temporary, destination)
            except OSError as exc:
                raise AudioDecodeError(
                    "audio_output_unavailable",
                    "Không thể lưu file âm thanh đã tách",
                    retryable=True,
                ) from exc
            return DecodedAudio(
                path=destination,
                sample_rate=decoded.sample_rate,
                channels=decoded.channels,
                sample_width_bytes=decoded.sample_width_bytes,
                frame_count=decoded.frame_count,
                duration_us=decoded.duration_us,
            )
        except asyncio.CancelledError:
            if process is not None and communication is not None:
                await self._stop_process(process, communication)
            raise
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    async def _stop_process(
        self,
        process: ProcessHandle,
        communication: asyncio.Task[tuple[bytes | None, bytes | None]],
    ) -> None:
        if communication.done() or process.returncode is not None:
            with suppress(Exception):
                await communication
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=self._stop_grace_seconds,
            )
            return
        except TimeoutError:
            pass
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(Exception):
            await communication


def _is_cancelled(cancellation: Cancellation | None) -> bool:
    if cancellation is None:
        return False
    if callable(cancellation):
        return bool(cancellation())
    return bool(cancellation.is_cancelled())


def _missing_audio_stream(diagnostic: str) -> bool:
    normalized = diagnostic.casefold()
    return any(
        marker in normalized
        for marker in (
            "matches no streams",
            "does not contain any stream",
            "invalid stream specifier",
        )
    )


def _inspect_wav(
    path: Path,
    *,
    expected_duration_us: int,
    tolerance_us: int,
) -> DecodedAudio:
    try:
        with wave.open(os.fspath(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_rate = stream.getframerate()
            sample_width = stream.getsampwidth()
            frame_count = stream.getnframes()
            compression = stream.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise AudioDecodeError(
            "invalid_decoded_audio",
            "FFmpeg tạo file âm thanh không hợp lệ",
            retryable=False,
        ) from exc

    if (
        channels != 1
        or sample_rate != 16_000
        or sample_width != 2
        or compression != "NONE"
        or frame_count <= 0
    ):
        raise AudioDecodeError(
            "invalid_decoded_audio",
            "Âm thanh đã tách không đúng chuẩn PCM mono 16 kHz",
            retryable=False,
        )
    duration_us = (frame_count * 1_000_000 + sample_rate // 2) // sample_rate
    if abs(duration_us - expected_duration_us) > tolerance_us:
        raise AudioDecodeError(
            "decoded_audio_duration_mismatch",
            "Thời lượng âm thanh đã tách không khớp thời lượng media",
            retryable=False,
        )
    return DecodedAudio(
        path=path,
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        frame_count=frame_count,
        duration_us=duration_us,
    )


__all__ = [
    "AudioDecodeError",
    "CancellationToken",
    "DecodedAudio",
    "FfmpegAudioDecoder",
]
