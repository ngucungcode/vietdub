"""Media metadata probing through a shell-free ffprobe subprocess."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

from .domain import MediaAsset, MediaKind


class MediaProbeError(RuntimeError):
    """Safe, typed media-probe failure that can be persisted on a job."""

    def __init__(self, code: str, message_vi: str, *, retryable: bool) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable


class MediaProbe(Protocol):
    async def probe(
        self,
        path: Path,
        *,
        source_language: str,
        title: str | None = None,
        media_kind: MediaKind = MediaKind.MOVIE,
        year: int | None = None,
        require_h264_passthrough: bool = False,
        allow_hevc_transcode: bool = False,
    ) -> MediaAsset: ...


FfprobeRunner = Callable[
    [Sequence[str]], Awaitable[subprocess.CompletedProcess[str]]
]


async def _default_runner(
    command: Sequence[str],
    *,
    timeout_seconds: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(timeout_seconds):
            stdout, stderr = await process.communicate()
    except TimeoutError as error:
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(Exception):
                await process.communicate()
        raise MediaProbeError(
            "media_probe_unavailable",
            "Không thể đọc thông tin file video",
            retryable=True,
        ) from error
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(Exception):
                await process.communicate()
        raise
    except OSError as error:
        raise MediaProbeError(
            "media_probe_unavailable",
            "Không thể đọc thông tin file video",
            retryable=True,
        ) from error
    return subprocess.CompletedProcess(
        list(command),
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


class FfprobeMediaProbe:
    def __init__(
        self,
        *,
        ffprobe_binary: str = "ffprobe",
        runner: FfprobeRunner | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._ffprobe = ffprobe_binary
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    async def probe(
        self,
        path: Path,
        *,
        source_language: str,
        title: str | None = None,
        media_kind: MediaKind = MediaKind.MOVIE,
        year: int | None = None,
        require_h264_passthrough: bool = False,
        allow_hevc_transcode: bool = False,
    ) -> MediaAsset:
        media_path = path.resolve(strict=False)
        if not media_path.is_file():
            raise MediaProbeError(
                "source_media_missing",
                "Không tìm thấy file video đã tải",
                retryable=True,
            )
        command = (
            self._ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-show_entries",
            "format=duration:format_tags=title:stream=index,codec_name,codec_type,start_time,duration,avg_frame_rate,r_frame_rate,color_transfer:stream_tags=language:stream_disposition=default,attached_pic,timed_thumbnails:stream_side_data_list=side_data_type",
            "-of",
            "json",
            os.fspath(media_path),
        )
        try:
            result = (
                await self._runner(command)
                if self._runner is not None
                else await _default_runner(command, timeout_seconds=self._timeout_seconds)
            )
        except MediaProbeError:
            raise
        except (OSError, TimeoutError) as error:
            raise MediaProbeError(
                "media_probe_unavailable",
                "Không thể đọc thông tin file video",
                retryable=True,
            ) from error
        if result.returncode != 0:
            raise MediaProbeError(
                "unsupported_media",
                "ffprobe không thể đọc file video đã tải",
                retryable=False,
            )
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as error:
            raise MediaProbeError(
                "invalid_media_metadata",
                "ffprobe trả về thông tin video không hợp lệ",
                retryable=False,
            ) from error
        if not isinstance(payload, dict):
            raise MediaProbeError(
                "invalid_media_metadata",
                "ffprobe trả về thông tin video không hợp lệ",
                retryable=False,
            )

        streams = payload.get("streams")
        if not isinstance(streams, list):
            streams = []
        video_streams = [
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ]
        if not video_streams:
            raise MediaProbeError(
                "unsupported_media",
                "File đã tải không chứa luồng video",
                retryable=False,
            )
        content_video_streams = [
            stream for stream in video_streams if not _is_visual_attachment(stream)
        ]
        if not content_video_streams:
            raise MediaProbeError(
                "unsupported_media",
                "File chỉ chứa ảnh bìa hoặc thumbnail, không có luồng hình của video",
                retryable=False,
            )
        selected_video = content_video_streams[0]
        video_codec = str(selected_video.get("codec_name") or "").strip().lower()
        if (
            require_h264_passthrough
            and allow_hevc_transcode
            and video_codec == "hevc"
            and is_hdr_video_stream(selected_video)
        ):
            raise MediaProbeError(
                "unsupported_media",
                "Luồng hình HEVC dùng HDR/HLG/Dolby Vision; bản hiện tại chỉ "
                "chuyển mã HEVC SDR để tránh xuất sai màu",
                retryable=False,
            )
        export_compatible = video_codec == "h264" or (
            allow_hevc_transcode and video_codec == "hevc"
        )
        if require_h264_passthrough and not export_compatible:
            codec_label = video_codec.upper() if video_codec else "không xác định"
            requirement = (
                "cần H.264/AVC hoặc HEVC/H.265 để xuất MP4"
                if allow_hevc_transcode
                else "cần H.264/AVC để xuất MP4 không mã hóa lại"
            )
            raise MediaProbeError(
                "unsupported_media",
                f"Luồng hình chính dùng codec {codec_label}; {requirement}",
                retryable=False,
            )
        audio_streams = [
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        ]
        if not audio_streams:
            raise MediaProbeError(
                "no_audio_stream",
                "File đã tải không chứa luồng âm thanh để nhận dạng lời nói",
                retryable=False,
            )

        format_data = payload.get("format")
        if not isinstance(format_data, dict):
            format_data = {}
        duration_us = _duration_us(format_data.get("duration"))
        if duration_us is None:
            duration_us = next(
                (
                    parsed
                    for stream in content_video_streams
                    if (parsed := _duration_us(stream.get("duration"))) is not None
                ),
                None,
            )
        if duration_us is None:
            raise MediaProbeError(
                "invalid_media_metadata",
                "Không xác định được thời lượng video",
                retryable=False,
            )

        requested_language = _normalize_language(source_language)
        selected_audio = _select_audio_stream(audio_streams, requested_language)
        audio_stream_index = _stream_index(selected_audio.get("index"))
        if audio_stream_index is None:
            raise MediaProbeError(
                "invalid_media_metadata",
                "Không xác định được chỉ số luồng âm thanh",
                retryable=False,
            )
        if requested_language == "auto":
            tags = selected_audio.get("tags")
            requested_language = _normalize_language(
                tags.get("language") if isinstance(tags, dict) else None
            )
        format_tags = format_data.get("tags")
        embedded_title = (
            format_tags.get("title")
            if isinstance(format_tags, dict) and isinstance(format_tags.get("title"), str)
            else None
        )
        return MediaAsset(
            path=media_path,
            title=(title or embedded_title or media_path.stem).strip(),
            duration_us=duration_us,
            source_language=requested_language,
            media_kind=media_kind,
            year=year,
            fps=_fps(selected_video),
            audio_stream_index=audio_stream_index,
            audio_start_us=_signed_time_us(selected_audio.get("start_time")),
            video_stream_index=_stream_index(selected_video.get("index")),
            video_codec=video_codec or None,
        )


def _normalize_language(value: object) -> str:
    if not isinstance(value, str):
        return "auto"
    normalized = value.strip().lower().replace("_", "-")
    return normalized or "auto"


def _is_visual_attachment(stream: dict[str, Any]) -> bool:
    """Return whether a video-typed stream is cover art or a thumbnail.

    FFmpeg exposes attached pictures as video streams.  They must not become
    the visual timeline even when the attachment happens to precede the movie
    stream in container order.
    """

    disposition = stream.get("disposition")
    if not isinstance(disposition, dict):
        return False
    return any(
        disposition.get(name) in {1, "1"}
        for name in ("attached_pic", "timed_thumbnails")
    )


def is_hdr_video_stream(stream: dict[str, Any]) -> bool:
    transfer = str(stream.get("color_transfer") or "").strip().lower()
    if transfer in {"smpte2084", "arib-std-b67"}:
        return True
    side_data = stream.get("side_data_list")
    if not isinstance(side_data, list):
        return False
    for item in side_data:
        if not isinstance(item, dict):
            continue
        label = str(item.get("side_data_type") or "").lower()
        if "dovi" in label or "dolby vision" in label:
            return True
    return False


def _duration_us(value: object) -> int | None:
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not seconds.is_finite() or seconds <= 0:
        return None
    duration = int((seconds * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))
    return duration if duration > 0 else None


def _signed_time_us(value: object) -> int:
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 0
    if not seconds.is_finite():
        return 0
    return int((seconds * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def _stream_index(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _select_audio_stream(
    audio_streams: list[dict[str, Any]],
    requested_language: str,
) -> dict[str, Any]:
    def is_default(stream: dict[str, Any]) -> bool:
        disposition = stream.get("disposition")
        return isinstance(disposition, dict) and disposition.get("default") == 1

    candidates = audio_streams
    if requested_language != "auto":
        requested = _canonical_language(requested_language)
        matching = []
        for stream in audio_streams:
            tags = stream.get("tags")
            language = _normalize_language(
                tags.get("language") if isinstance(tags, dict) else None
            )
            if _canonical_language(language) == requested:
                matching.append(stream)
        if matching:
            candidates = matching
    return next((stream for stream in candidates if is_default(stream)), candidates[0])


def _canonical_language(value: str) -> str:
    primary = value.split("-", 1)[0]
    aliases = {
        "ara": "ar",
        "eng": "en",
        "jpn": "ja",
        "kor": "ko",
        "tha": "th",
        "vie": "vi",
        "zho": "zh",
        "chi": "zh",
    }
    return aliases.get(primary, primary)


def _fps(stream: dict[str, Any]) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if not isinstance(raw, str) or not raw or raw == "0/0":
            continue
        try:
            value = float(Fraction(raw))
        except (ValueError, ZeroDivisionError):
            continue
        if 0 < value <= 1000:
            return value
    return None


__all__ = [
    "FfprobeMediaProbe",
    "MediaProbe",
    "MediaProbeError",
    "is_hdr_video_stream",
]
