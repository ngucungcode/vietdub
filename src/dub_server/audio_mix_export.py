"""Offline, loudness-aware dubbing mix and atomic MP4 export.

The exporter deliberately maps only the first moving-picture stream from the
source file, excluding attached cover art and thumbnails.  Original source
audio is never mapped.  The replacement audio is made from a dialogue-reduced
accompaniment stem and the Vietnamese narration.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from .media_probe import is_hdr_video_stream


class MediaExportErrorCode(StrEnum):
    SOURCE_VIDEO_MISSING = "source_video_missing"
    SOURCE_VIDEO_CODEC_UNSUPPORTED = "source_video_codec_unsupported"
    SOURCE_VIDEO_HDR_UNSUPPORTED = "source_video_hdr_unsupported"
    ACCOMPANIMENT_MISSING = "accompaniment_missing"
    NARRATION_MISSING = "narration_missing"
    EXPORT_CANCELLED = "media_export_cancelled"
    FFMPEG_UNAVAILABLE = "ffmpeg_unavailable"
    EXPORT_FAILED = "media_export_failed"
    FFPROBE_UNAVAILABLE = "ffprobe_unavailable"
    INVALID_OUTPUT = "invalid_media_output"
    TRACK_LAYOUT_INVALID = "output_track_layout_invalid"
    VIDEO_CODEC_INVALID = "output_video_codec_invalid"
    AUDIO_CODEC_INVALID = "output_audio_codec_invalid"
    DURATION_MISMATCH = "output_duration_mismatch"
    SYNC_MISMATCH = "output_sync_mismatch"
    PUBLISH_FAILED = "media_publish_failed"


class MediaExportError(RuntimeError):
    """Safe, serializable export failure suitable for a job checkpoint."""

    def __init__(
        self,
        code: MediaExportErrorCode,
        message_vi: str,
        *,
        retryable: bool,
    ) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class MixSettings:
    """Loudness and moderate-ducking policy for the final stereo mix."""

    narration_lufs: float = -16.0
    accompaniment_lufs: float = -24.0
    true_peak_db: float = -1.5
    ducking_threshold: float = 0.04
    ducking_ratio: float = 3.0
    ducking_attack_ms: int = 20
    ducking_release_ms: int = 300
    audio_bitrate: str = "192k"

    def __post_init__(self) -> None:
        if not -70.0 <= self.narration_lufs <= -5.0:
            raise ValueError("Mức âm lượng lời thuyết minh không hợp lệ")
        if not -70.0 <= self.accompaniment_lufs <= -5.0:
            raise ValueError("Mức âm lượng nhạc nền không hợp lệ")
        if not -9.0 <= self.true_peak_db <= 0.0:
            raise ValueError("Giới hạn đỉnh âm thanh không hợp lệ")
        if not 0.000001 <= self.ducking_threshold <= 1.0:
            raise ValueError("Ngưỡng ducking không hợp lệ")
        if not 1.0 <= self.ducking_ratio <= 20.0:
            raise ValueError("Tỷ lệ ducking không hợp lệ")
        if not 0 <= self.ducking_attack_ms <= 2_000:
            raise ValueError("Thời gian attack của ducking không hợp lệ")
        if not 1 <= self.ducking_release_ms <= 9_000:
            raise ValueError("Thời gian release của ducking không hợp lệ")
        if not self.audio_bitrate or any(char.isspace() for char in self.audio_bitrate):
            raise ValueError("Bitrate AAC không hợp lệ")


@dataclass(frozen=True, slots=True)
class ExportProgress:
    processed_us: int
    duration_us: int
    fraction: float


@dataclass(frozen=True, slots=True)
class ExportedMedia:
    path: Path
    duration_us: int
    video_start_us: int
    audio_start_us: int
    audio_codec: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _SourceVideoInfo:
    duration_us: int
    codec: str


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...


Cancellation = CancellationToken | Callable[[], bool]
ProgressCallback = Callable[[ExportProgress], None]
CommandRunner = Callable[..., Awaitable[subprocess.CompletedProcess[str]]]


async def _default_command_runner(
    command: Sequence[str],
    *,
    cancellation: Cancellation | None = None,
    on_progress: ProgressCallback | None = None,
    expected_duration_us: int | None = None,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one shell-free command and terminate it promptly on cancellation."""

    process: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[bytes] | None = None
    stdout_chunks: list[bytes] = []
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())

        async def consume_stdout() -> None:
            while True:
                if _is_cancelled(cancellation):
                    raise MediaExportError(
                        MediaExportErrorCode.EXPORT_CANCELLED,
                        "Đã hủy xuất video thuyết minh",
                        retryable=True,
                    )
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=0.1)
                except TimeoutError:
                    continue
                if not line:
                    break
                stdout_chunks.append(line)
                _report_ffmpeg_progress(line, expected_duration_us, on_progress)

        if timeout_seconds is None:
            await consume_stdout()
            returncode = await process.wait()
        else:
            async with asyncio.timeout(timeout_seconds):
                await consume_stdout()
                returncode = await process.wait()
        stderr = await stderr_task
        return subprocess.CompletedProcess(
            list(command),
            returncode,
            b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            await _stop_process(process)
        if stderr_task is not None:
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task
        raise
    except MediaExportError:
        if process is not None and process.returncode is None:
            await _stop_process(process)
        if stderr_task is not None:
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task
        raise
    except TimeoutError as error:
        if process is not None and process.returncode is None:
            await _stop_process(process)
        if stderr_task is not None:
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task
        raise MediaExportError(
            MediaExportErrorCode.EXPORT_FAILED,
            "Xuất video vượt quá thời gian cho phép",
            retryable=True,
        ) from error
    except OSError as error:
        raise MediaExportError(
            MediaExportErrorCode.FFMPEG_UNAVAILABLE,
            "Không thể khởi động FFmpeg/ffprobe",
            retryable=False,
        ) from error


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(Exception):
            await process.wait()


def _report_ffmpeg_progress(
    raw_line: bytes,
    expected_duration_us: int | None,
    callback: ProgressCallback | None,
) -> None:
    if callback is None or expected_duration_us is None or expected_duration_us <= 0:
        return
    try:
        key, value = raw_line.decode("ascii", errors="ignore").strip().split("=", 1)
    except ValueError:
        return
    if key not in {"out_time_us", "out_time_ms"}:
        return
    try:
        processed_us = max(0, min(int(value), expected_duration_us))
    except ValueError:
        return
    _emit_progress(callback, processed_us, expected_duration_us)


@lru_cache(maxsize=8)
def _passthrough_sync_options(ffmpeg_binary: str) -> tuple[str, ...]:
    """Return this ffmpeg's flag for "keep source frame timing".

    ``-vsync`` was deprecated by ``-fps_mode`` in ffmpeg 5.1 and removed
    outright in 8.0, while Ubuntu 22.04 still ships 4.4.2, which only knows
    ``-vsync``.  Both deployment targets are supported, so the flag is probed
    once per binary instead of hard-coded.
    """

    try:
        help_text = subprocess.run(
            [ffmpeg_binary, "-hide_banner", "-h", "full"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ("-vsync", "0")
    if "fps_mode" in help_text:
        return ("-fps_mode:v:0", "passthrough")
    return ("-vsync", "0")


class FfmpegAudioMixExporter:
    """Mix an accompaniment stem with narration and replace all source audio."""

    def __init__(
        self,
        *,
        ffmpeg_binary: str = "ffmpeg",
        ffprobe_binary: str = "ffprobe",
        runner: CommandRunner | None = None,
        settings: MixSettings | None = None,
        duration_tolerance_us: int = 100_000,
        sync_tolerance_us: int = 100_000,
        timeout_seconds: float | None = None,
    ) -> None:
        if duration_tolerance_us < 0 or sync_tolerance_us < 0:
            raise ValueError("Dung sai thời lượng/đồng bộ không được âm")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("Thời gian giới hạn phải lớn hơn 0")
        self._ffmpeg = ffmpeg_binary
        self._ffprobe = ffprobe_binary
        self._runner = runner or _default_command_runner
        self._settings = settings or MixSettings()
        self._duration_tolerance_us = duration_tolerance_us
        self._sync_tolerance_us = sync_tolerance_us
        self._timeout_seconds = timeout_seconds

    async def export(
        self,
        source_video: Path,
        accompaniment_audio: Path,
        narration_audio: Path,
        output_path: Path,
        *,
        expected_duration_us: int,
        cancellation: Cancellation | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> ExportedMedia:
        if expected_duration_us <= 0:
            raise ValueError("Thời lượng video phải lớn hơn 0")
        if output_path.suffix.lower() != ".mp4":
            raise ValueError("File đầu ra phải có phần mở rộng .mp4")

        source = _require_file(
            source_video,
            MediaExportErrorCode.SOURCE_VIDEO_MISSING,
            "Không tìm thấy video nguồn để xuất",
        )
        accompaniment = _require_file(
            accompaniment_audio,
            MediaExportErrorCode.ACCOMPANIMENT_MISSING,
            "Không tìm thấy phần âm thanh nền đã tách lời diễn viên",
        )
        narration = _require_file(
            narration_audio,
            MediaExportErrorCode.NARRATION_MISSING,
            "Không tìm thấy âm thanh thuyết minh",
        )
        destination = output_path.resolve(strict=False)
        if destination in {source, accompaniment, narration}:
            raise ValueError("File đầu ra không được trùng với file đầu vào")
        temporary = destination.with_name(f".{destination.stem}.part{destination.suffix}")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.unlink(missing_ok=True)
        except OSError as error:
            raise MediaExportError(
                MediaExportErrorCode.PUBLISH_FAILED,
                "Không thể chuẩn bị nơi lưu video thuyết minh",
                retryable=True,
            ) from error

        try:
            self._check_cancelled(cancellation)
            source_video_info = await self._source_video_info(
                source,
                cancellation=cancellation,
            )
            visual_duration_us = source_video_info.duration_us
            self._check_cancelled(cancellation)
            command = self._ffmpeg_command(
                source,
                accompaniment,
                narration,
                temporary,
                visual_duration_us,
                source_video_info.codec,
            )
            result = await self._runner(
                command,
                cancellation=cancellation,
                on_progress=on_progress,
                expected_duration_us=visual_duration_us,
                timeout_seconds=self._timeout_seconds,
            )
            self._check_cancelled(cancellation)
            if result.returncode != 0:
                raise MediaExportError(
                    MediaExportErrorCode.EXPORT_FAILED,
                    _ffmpeg_failure_message(result.stderr),
                    retryable=True,
                )
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise MediaExportError(
                    MediaExportErrorCode.INVALID_OUTPUT,
                    "FFmpeg không tạo được file video đầu ra hợp lệ",
                    retryable=True,
                )

            verified = await self._verify(temporary, cancellation)
            self._check_cancelled(cancellation)
            try:
                os.replace(temporary, destination)
            except OSError as error:
                raise MediaExportError(
                    MediaExportErrorCode.PUBLISH_FAILED,
                    "Không thể công bố file video thuyết minh",
                    retryable=True,
                ) from error
            _emit_progress(on_progress, visual_duration_us, visual_duration_us)
            return ExportedMedia(
                path=destination,
                duration_us=verified.duration_us,
                video_start_us=verified.video_start_us,
                audio_start_us=verified.audio_start_us,
                audio_codec=verified.audio_codec,
                size_bytes=destination.stat().st_size,
            )
        except asyncio.CancelledError:
            temporary.unlink(missing_ok=True)
            raise
        except MediaExportError:
            temporary.unlink(missing_ok=True)
            raise
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise MediaExportError(
                MediaExportErrorCode.EXPORT_FAILED,
                "Không thể đọc hoặc ghi file trong khi xuất video",
                retryable=True,
            ) from error

    async def _source_video_info(
        self,
        source: Path,
        *,
        cancellation: Cancellation | None,
    ) -> _SourceVideoInfo:
        """Resolve the visual span and codec before creating the output.

        FFmpeg 4.4 cannot always stop filtered audio at the mapped video EOF
        when an unselected source track extends the container. Prefer the first
        video stream's own duration. Failing closed is important here: using
        the container/checkpoint duration can recreate the mismatch this probe
        is intended to prevent.
        """

        command = (
            self._ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-select_streams",
            "V:0",
            "-show_entries",
            "stream=codec_name,color_transfer,start_time,duration,duration_ts,time_base:"
            "stream_tags=DURATION:stream_side_data_list=side_data_type",
            "-of",
            "json",
            os.fspath(source),
        )
        try:
            result = await self._runner(
                command,
                cancellation=cancellation,
                on_progress=None,
                expected_duration_us=None,
                timeout_seconds=30.0,
            )
        except MediaExportError as error:
            if error.code == MediaExportErrorCode.FFMPEG_UNAVAILABLE:
                raise MediaExportError(
                    MediaExportErrorCode.FFPROBE_UNAVAILABLE,
                    "Không thể khởi động ffprobe để đọc timeline luồng hình",
                    retryable=False,
                ) from error
            if error.code == MediaExportErrorCode.EXPORT_FAILED:
                raise MediaExportError(
                    MediaExportErrorCode.INVALID_OUTPUT,
                    "Không thể đọc thời lượng luồng hình nguồn trong thời gian cho phép",
                    retryable=True,
                ) from error
            raise
        if result.returncode != 0:
            raise MediaExportError(
                MediaExportErrorCode.INVALID_OUTPUT,
                "ffprobe không đọc được timeline luồng hình nguồn",
                retryable=True,
            )
        try:
            payload = json.loads(_as_text(result.stdout))
        except (TypeError, json.JSONDecodeError) as error:
            raise MediaExportError(
                MediaExportErrorCode.INVALID_OUTPUT,
                "ffprobe trả về timeline luồng hình nguồn không hợp lệ",
                retryable=True,
            ) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
            raise MediaExportError(
                MediaExportErrorCode.INVALID_OUTPUT,
                "Video nguồn không có thông tin timeline luồng hình hợp lệ",
                retryable=True,
            )
        video = next(
            (stream for stream in payload["streams"] if isinstance(stream, dict)),
            None,
        )
        if video is None:
            raise MediaExportError(
                MediaExportErrorCode.INVALID_OUTPUT,
                "Video nguồn không có luồng hình để xuất",
                retryable=False,
            )
        duration_us = _stream_duration_us(video)
        if duration_us is None:
            raise MediaExportError(
                MediaExportErrorCode.INVALID_OUTPUT,
                "Không xác định được thời lượng luồng hình nguồn",
                retryable=True,
            )
        codec = str(video.get("codec_name") or "").strip().lower()
        if codec not in {"h264", "hevc"}:
            codec_label = codec.upper() if codec else "không xác định"
            raise MediaExportError(
                MediaExportErrorCode.SOURCE_VIDEO_CODEC_UNSUPPORTED,
                f"Luồng hình chính dùng codec {codec_label}; chỉ hỗ trợ "
                "H.264/AVC hoặc HEVC/H.265",
                retryable=False,
            )
        if codec == "hevc" and is_hdr_video_stream(video):
            raise MediaExportError(
                MediaExportErrorCode.SOURCE_VIDEO_HDR_UNSUPPORTED,
                "Luồng hình HEVC dùng HDR/HLG/Dolby Vision; bản hiện tại chỉ "
                "chuyển mã HEVC SDR để tránh xuất sai màu",
                retryable=False,
            )
        return _SourceVideoInfo(duration_us=duration_us, codec=codec)

    def _ffmpeg_command(
        self,
        source: Path,
        accompaniment: Path,
        narration: Path,
        temporary: Path,
        visual_duration_us: int,
        source_video_codec: str,
    ) -> tuple[str, ...]:
        duration = _seconds_text(visual_duration_us)
        settings = self._settings
        limiter = 10.0 ** (settings.true_peak_db / 20.0)
        graph = (
            "[1:a:0]aresample=48000:async=1:first_pts=0,"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"loudnorm=I={settings.accompaniment_lufs:.1f}:LRA=11.0:TP=-2.0,"
            "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:"
            "channel_layouts=stereo[bed];"
            "[2:a:0]aresample=48000:async=1:first_pts=0,"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"loudnorm=I={settings.narration_lufs:.1f}:LRA=7.0:TP={settings.true_peak_db:.1f},"
            "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:"
            "channel_layouts=stereo,"
            "asplit=2[narr_side][narr_mix];"
            f"[bed][narr_side]sidechaincompress=threshold={settings.ducking_threshold:.6f}:"
            f"ratio={settings.ducking_ratio:.2f}:attack={settings.ducking_attack_ms}:"
            f"release={settings.ducking_release_ms}:makeup=1[ducked];"
            "[ducked][narr_mix]amix=inputs=2:duration=longest:"
            "dropout_transition=0:normalize=0,"
            f"alimiter=limit={limiter:.6f}:attack=5:release=50,"
            f"apad,atrim=duration={duration}[mixed]"
        )
        if source_video_codec == "h264":
            video_options = ("-c:v", "copy")
        elif source_video_codec == "hevc":
            # Use the software encoder so this path also works on cards without
            # a usable NVENC block (for example V100 and CMP 170HX).
            # Reset only the timestamp origin; relative VFR frame intervals are
            # retained by passthrough sync mode.
            video_options = (
                "-filter:v:0",
                "setpts=PTS-STARTPTS,pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-tag:v",
                "avc1",
                *_passthrough_sync_options(self._ffmpeg),
            )
        else:  # Defensive: source probing owns the normal public typed error.
            raise MediaExportError(
                MediaExportErrorCode.SOURCE_VIDEO_CODEC_UNSUPPORTED,
                "Codec luồng hình nguồn không được hỗ trợ",
                retryable=False,
            )
        return (
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
            "-i",
            os.fspath(accompaniment),
            "-i",
            os.fspath(narration),
            "-filter_complex",
            graph,
            "-map",
            "0:V:0",
            "-map",
            "[mixed]",
            "-map_metadata",
            "0",
            "-map_chapters",
            "-1",
            "-sn",
            "-dn",
            *video_options,
            "-disposition:v:0",
            "default",
            "-c:a",
            "aac",
            "-b:a",
            settings.audio_bitrate,
            "-ar",
            "48000",
            "-ac",
            "2",
            "-write_tmcd",
            "0",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            os.fspath(temporary),
        )

    async def _verify(
        self,
        temporary: Path,
        cancellation: Cancellation | None,
    ) -> ExportedMedia:
        command = (
            self._ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-show_entries",
            "stream=index,codec_type,codec_name,codec_tag_string,"
            "start_time,duration:stream_disposition=default,attached_pic,"
            "timed_thumbnails",
            "-of",
            "json",
            os.fspath(temporary),
        )
        try:
            result = await self._runner(
                command,
                cancellation=cancellation,
                on_progress=None,
                expected_duration_us=None,
                timeout_seconds=30.0,
            )
        except MediaExportError as error:
            if error.code == MediaExportErrorCode.FFMPEG_UNAVAILABLE:
                raise MediaExportError(
                    MediaExportErrorCode.FFPROBE_UNAVAILABLE,
                    "Không thể khởi động ffprobe để kiểm tra video đầu ra",
                    retryable=False,
                ) from error
            raise
        if result.returncode != 0:
            raise MediaExportError(
                MediaExportErrorCode.INVALID_OUTPUT,
                "ffprobe không đọc được video thuyết minh vừa xuất",
                retryable=True,
            )
        try:
            payload = json.loads(_as_text(result.stdout))
        except (TypeError, json.JSONDecodeError) as error:
            raise MediaExportError(
                MediaExportErrorCode.INVALID_OUTPUT,
                "ffprobe trả về thông tin video đầu ra không hợp lệ",
                retryable=True,
            ) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
            raise MediaExportError(
                MediaExportErrorCode.INVALID_OUTPUT,
                "Video đầu ra không có thông tin luồng hợp lệ",
                retryable=True,
            )
        streams = [stream for stream in payload["streams"] if isinstance(stream, dict)]
        videos = [stream for stream in streams if stream.get("codec_type") == "video"]
        audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
        attached_videos = [
            stream for stream in videos if _is_visual_attachment(stream)
        ]
        if (
            len(streams) != 2
            or len(videos) != 1
            or len(audios) != 1
            or attached_videos
        ):
            layout_parts: list[str] = []
            for stream in streams[:8]:
                codec = str(stream.get("codec_name", "?"))
                codec_tag = str(stream.get("codec_tag_string", ""))
                if codec_tag and codec_tag != codec:
                    codec = f"{codec}[{codec_tag}]"
                layout_parts.append(
                    f"{stream.get('index', '?')}:{stream.get('codec_type', '?')}/"
                    f"{codec}"
                )
            layout = ", ".join(layout_parts)
            if len(streams) > 8:
                layout = f"{layout}, …"
            cover_note = (
                "; luồng hình đầu ra vẫn bị đánh dấu là ảnh bìa/thumbnail"
                if attached_videos
                else ""
            )
            raise MediaExportError(
                MediaExportErrorCode.TRACK_LAYOUT_INVALID,
                "Video đầu ra phải có đúng một luồng hình và một luồng tiếng "
                f"thuyết minh; ffprobe thấy {len(streams)} luồng "
                f"({layout or 'trống'}){cover_note}",
                retryable=False,
            )
        audio_codec = str(audios[0].get("codec_name", "")).lower()
        video_codec = str(videos[0].get("codec_name", "")).lower()
        if video_codec != "h264":
            raise MediaExportError(
                MediaExportErrorCode.VIDEO_CODEC_INVALID,
                "Luồng hình đầu ra không phải H.264/AVC",
                retryable=False,
            )
        if audio_codec != "aac":
            raise MediaExportError(
                MediaExportErrorCode.AUDIO_CODEC_INVALID,
                "Luồng thuyết minh đầu ra không phải AAC",
                retryable=False,
            )
        video_duration_us = _time_us(videos[0].get("duration"), positive=True)
        audio_duration_us = _time_us(audios[0].get("duration"), positive=True)
        if video_duration_us is None or audio_duration_us is None:
            raise MediaExportError(
                MediaExportErrorCode.INVALID_OUTPUT,
                "Không xác định được thời lượng luồng hình hoặc tiếng đầu ra",
                retryable=True,
            )
        video_start_us = _time_us(videos[0].get("start_time"), positive=False) or 0
        audio_start_us = _time_us(audios[0].get("start_time"), positive=False) or 0
        if abs(video_start_us - audio_start_us) > self._sync_tolerance_us:
            raise MediaExportError(
                MediaExportErrorCode.SYNC_MISMATCH,
                "Luồng hình và lời thuyết minh bị lệch thời điểm bắt đầu",
                retryable=False,
            )
        video_end_us = video_start_us + video_duration_us
        audio_end_us = audio_start_us + audio_duration_us
        if abs(video_end_us - audio_end_us) > self._duration_tolerance_us:
            raise MediaExportError(
                MediaExportErrorCode.DURATION_MISMATCH,
                "Điểm kết thúc luồng hình và tiếng thuyết minh không khớp",
                retryable=False,
            )
        duration_us = video_duration_us
        return ExportedMedia(
            path=temporary,
            duration_us=duration_us,
            video_start_us=video_start_us,
            audio_start_us=audio_start_us,
            audio_codec=audio_codec,
            size_bytes=temporary.stat().st_size,
        )

    @staticmethod
    def _check_cancelled(cancellation: Cancellation | None) -> None:
        if _is_cancelled(cancellation):
            raise MediaExportError(
                MediaExportErrorCode.EXPORT_CANCELLED,
                "Đã hủy xuất video thuyết minh",
                retryable=True,
            )


def _require_file(path: Path, code: MediaExportErrorCode, message: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise MediaExportError(code, message, retryable=True) from error
    if not resolved.is_file():
        raise MediaExportError(code, message, retryable=True)
    return resolved


def _is_cancelled(cancellation: Cancellation | None) -> bool:
    if cancellation is None:
        return False
    try:
        return bool(
            cancellation.is_cancelled()
            if hasattr(cancellation, "is_cancelled")
            else cancellation()
        )
    except Exception:
        return True


def _is_visual_attachment(stream: dict[str, object]) -> bool:
    disposition = stream.get("disposition")
    if not isinstance(disposition, dict):
        return False
    return any(
        disposition.get(name) in {1, "1"}
        for name in ("attached_pic", "timed_thumbnails")
    )


def _emit_progress(
    callback: ProgressCallback | None,
    processed_us: int,
    duration_us: int,
) -> None:
    if callback is None:
        return
    progress = ExportProgress(
        processed_us=max(0, min(processed_us, duration_us)),
        duration_us=duration_us,
        fraction=max(0.0, min(processed_us / duration_us, 1.0)),
    )
    with suppress(Exception):
        callback(progress)


def _seconds_text(duration_us: int) -> str:
    whole, fraction = divmod(duration_us, 1_000_000)
    return f"{whole}.{fraction:06d}"


def _time_us(value: object, *, positive: bool) -> int | None:
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not seconds.is_finite() or (positive and seconds <= 0):
        return None
    return int((seconds * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def _stream_duration_us(stream: dict[str, object]) -> int | None:
    duration_us = _time_us(stream.get("duration"), positive=True)
    if duration_us is not None:
        return duration_us
    try:
        ticks = Decimal(str(stream.get("duration_ts")))
        numerator_text, denominator_text = str(stream.get("time_base")).split("/", 1)
        seconds = ticks * Decimal(numerator_text) / Decimal(denominator_text)
    except (DecimalException, ValueError):
        seconds = Decimal(0)
    if seconds.is_finite() and seconds > 0:
        return int((seconds * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))
    tags = stream.get("tags")
    if not isinstance(tags, dict):
        return None
    raw_duration = tags.get("DURATION") or tags.get("duration")
    try:
        hours_text, minutes_text, seconds_text = str(raw_duration).split(":", 2)
        seconds = (
            Decimal(hours_text) * 3600
            + Decimal(minutes_text) * 60
            + Decimal(seconds_text)
        )
    except (DecimalException, ValueError):
        return None
    if not seconds.is_finite() or seconds <= 0:
        return None
    end_us = int(
        (seconds * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP)
    )
    # Matroska's stream DURATION tag is the absolute end timestamp when the
    # stream starts after zero. FFmpeg normalizes the selected output video's
    # timestamp origin for both stream copy and filtered transcode, so the
    # replacement audio must use the corresponding span rather than that end.
    start_us = _time_us(stream.get("start_time"), positive=False)
    duration_us = end_us - start_us if start_us is not None else end_us
    return duration_us if duration_us > 0 else None


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _ffmpeg_failure_message(stderr: str | bytes | None) -> str:
    diagnostic = _as_text(stderr).lower()
    if "no space left on device" in diagnostic:
        return "Không đủ dung lượng để xuất video thuyết minh"
    if "matches no streams" in diagnostic or "stream specifier" in diagnostic:
        return "Video hoặc file âm thanh đầu vào thiếu luồng cần thiết"
    return "FFmpeg không thể trộn và xuất video thuyết minh"


__all__ = [
    "Cancellation",
    "ExportProgress",
    "ExportedMedia",
    "FfmpegAudioMixExporter",
    "MediaExportError",
    "MediaExportErrorCode",
    "MixSettings",
]
