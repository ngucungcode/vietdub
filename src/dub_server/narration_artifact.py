"""Atomic timing-report and Vietnamese SRT artifacts for narration output."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .narration import Cancellation
from .timing import (
    TIMELINE_SAMPLE_RATE,
    FittedNarrationBlock,
    TimingQuality,
)

NARRATION_ARTIFACT_SCHEMA_VERSION = 1
DEFAULT_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class NarrationArtifactError(RuntimeError):
    """A typed local artifact failure safe for checkpoint persistence."""

    def __init__(self, code: str, message_vi: str, *, retryable: bool) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ArtifactFile:
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class TimingReportBlock:
    ordinal: int
    start_us: int
    end_us: int
    text: str
    source_duration_us: int
    native_speed: float
    atempo_speed: float
    total_speed: float
    padded_frame_count: int
    output_frame_count: int
    quality: TimingQuality

    def __post_init__(self) -> None:
        if self.ordinal < 0 or self.start_us < 0 or self.end_us <= self.start_us:
            raise NarrationArtifactError(
                "timing_report_block_invalid",
                "Khối trong báo cáo timing không hợp lệ",
                retryable=False,
            )
        text = _normalize_text(self.text)
        if self.source_duration_us <= 0:
            raise NarrationArtifactError(
                "timing_report_block_invalid",
                "Thời lượng TTS trong báo cáo không hợp lệ",
                retryable=False,
            )
        if (
            self.native_speed <= 0
            or self.atempo_speed <= 0
            or self.total_speed <= 0
            or self.padded_frame_count < 0
            or self.output_frame_count <= 0
        ):
            raise NarrationArtifactError(
                "timing_report_block_invalid",
                "Thông số tốc độ hoặc sample trong báo cáo không hợp lệ",
                retryable=False,
            )
        object.__setattr__(self, "text", text)


@dataclass(frozen=True, slots=True)
class TimingReport:
    duration_us: int
    timeline_frame_count: int
    tts_model_id: str
    tts_backend: str
    blocks: tuple[TimingReportBlock, ...]
    sample_rate: int = TIMELINE_SAMPLE_RATE
    schema_version: int = NARRATION_ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.duration_us <= 0 or self.sample_rate <= 0:
            raise NarrationArtifactError(
                "timing_report_invalid",
                "Thời lượng hoặc sample rate của báo cáo timing không hợp lệ",
                retryable=False,
            )
        expected_frames = (self.duration_us * self.sample_rate + 500_000) // 1_000_000
        if self.timeline_frame_count != expected_frames:
            raise NarrationArtifactError(
                "timing_report_invalid",
                "Tổng số sample trong báo cáo không khớp thời lượng",
                retryable=False,
            )
        model_id = _normalize_identifier(self.tts_model_id, field="model TTS")
        backend = _normalize_identifier(self.tts_backend, field="backend TTS")
        if not isinstance(self.blocks, tuple) or not self.blocks:
            raise NarrationArtifactError(
                "timing_report_empty",
                "Báo cáo timing không có khối thuyết minh",
                retryable=False,
            )
        previous_end_us = 0
        for expected_ordinal, block in enumerate(self.blocks):
            if not isinstance(block, TimingReportBlock) or block.ordinal != expected_ordinal:
                raise NarrationArtifactError(
                    "timing_report_order_invalid",
                    "Thứ tự khối trong báo cáo timing không liên tục",
                    retryable=False,
                )
            if block.start_us < previous_end_us or block.end_us > self.duration_us:
                raise NarrationArtifactError(
                    "timing_report_timeline_invalid",
                    "Timeline trong báo cáo bị overlap hoặc vượt thời lượng video",
                    retryable=False,
                )
            previous_end_us = block.end_us
        object.__setattr__(self, "tts_model_id", model_id)
        object.__setattr__(self, "tts_backend", backend)


@dataclass(frozen=True, slots=True)
class SrtCue:
    start_us: int
    end_us: int
    text: str

    def __post_init__(self) -> None:
        if self.start_us < 0 or self.end_us <= self.start_us:
            raise NarrationArtifactError(
                "srt_cue_invalid",
                "Mốc thời gian phụ đề không hợp lệ",
                retryable=False,
            )
        object.__setattr__(self, "text", _normalize_text(self.text))


ArtifactProgress = Callable[[int, int], None]


def build_timing_report(
    blocks: Sequence[FittedNarrationBlock],
    *,
    duration_us: int,
    tts_model_id: str,
    tts_backend: str,
) -> TimingReport:
    report_blocks = tuple(
        TimingReportBlock(
            ordinal=ordinal,
            start_us=block.start_us,
            end_us=block.end_us,
            text=block.text,
            source_duration_us=block.source_duration_us,
            native_speed=block.native_speed,
            atempo_speed=block.atempo_speed,
            total_speed=block.total_speed,
            padded_frame_count=block.padded_frame_count,
            output_frame_count=block.output_frame_count,
            quality=block.quality,
        )
        for ordinal, block in enumerate(blocks)
    )
    return TimingReport(
        duration_us=duration_us,
        timeline_frame_count=(duration_us * TIMELINE_SAMPLE_RATE + 500_000) // 1_000_000,
        tts_model_id=tts_model_id,
        tts_backend=tts_backend,
        blocks=report_blocks,
    )


def build_srt_cues(blocks: Sequence[FittedNarrationBlock]) -> tuple[SrtCue, ...]:
    return tuple(
        SrtCue(start_us=block.start_us, end_us=block.end_us, text=block.text)
        for block in blocks
    )


def write_timing_report(
    path: Path,
    report: TimingReport,
    *,
    cancellation: Cancellation | None = None,
    on_progress: ArtifactProgress | None = None,
    max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> ArtifactFile:
    if not isinstance(report, TimingReport):
        raise NarrationArtifactError(
            "timing_report_invalid",
            "Báo cáo timing không hợp lệ",
            retryable=False,
        )
    destination = Path(path)
    if destination.suffix.lower() != ".json":
        raise NarrationArtifactError(
            "timing_report_path_invalid",
            "Báo cáo timing phải có phần mở rộng .json",
            retryable=False,
        )
    block_documents: list[dict[str, Any]] = []
    severity_counts = {value.value: 0 for value in TimingQuality}
    for block in report.blocks:
        _raise_if_cancelled(cancellation)
        severity_counts[block.quality.value] += 1
        block_documents.append(
            {
                "ordinal": block.ordinal,
                "start_us": block.start_us,
                "end_us": block.end_us,
                "slot_duration_us": block.end_us - block.start_us,
                "text": block.text,
                "tts_duration_us": block.source_duration_us,
                "native_speed": _finite_decimal(block.native_speed),
                "atempo_speed": _finite_decimal(block.atempo_speed),
                "total_speed": _finite_decimal(block.total_speed),
                "padded_frame_count": block.padded_frame_count,
                "padded_us": (
                    block.padded_frame_count * 1_000_000 + report.sample_rate // 2
                )
                // report.sample_rate,
                "output_frame_count": block.output_frame_count,
                "quality": block.quality.value,
            }
        )
        _report_progress(on_progress, block.ordinal + 1, len(report.blocks))
    document = {
        "artifact_type": "timing-report",
        "schema_version": report.schema_version,
        "duration_us": report.duration_us,
        "sample_rate": report.sample_rate,
        "timeline_frame_count": report.timeline_frame_count,
        "tts": {"model_id": report.tts_model_id, "backend": report.tts_backend},
        "quality_summary": severity_counts,
        "blocks": block_documents,
    }
    try:
        payload = (
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NarrationArtifactError(
            "timing_report_serialize_failed",
            "Không thể tuần tự hóa báo cáo timing",
            retryable=False,
        ) from exc
    return _atomic_write(
        destination,
        payload,
        cancellation=cancellation,
        max_bytes=max_bytes,
        error_code="timing_report_write_failed",
        message_vi="Không thể ghi báo cáo timing",
    )


def write_srt_artifact(
    path: Path,
    cues: Sequence[SrtCue],
    *,
    cancellation: Cancellation | None = None,
    on_progress: ArtifactProgress | None = None,
    max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> ArtifactFile:
    destination = Path(path)
    if destination.suffix.lower() != ".srt":
        raise NarrationArtifactError(
            "srt_path_invalid",
            "Phụ đề đầu ra phải có phần mở rộng .srt",
            retryable=False,
        )
    if not cues:
        raise NarrationArtifactError(
            "srt_empty",
            "Không có nội dung để tạo phụ đề tiếng Việt",
            retryable=False,
        )
    previous_end_us = 0
    chunks: list[str] = []
    for ordinal, cue in enumerate(cues, 1):
        _raise_if_cancelled(cancellation)
        if not isinstance(cue, SrtCue):
            raise NarrationArtifactError(
                "srt_cue_invalid",
                "Khối phụ đề không hợp lệ",
                retryable=False,
            )
        if cue.start_us < previous_end_us:
            raise NarrationArtifactError(
                "srt_timeline_invalid",
                "Timestamp phụ đề bị overlap",
                retryable=False,
            )
        start_ms = cue.start_us // 1_000
        end_ms = max(start_ms + 1, (cue.end_us + 999) // 1_000)
        chunks.append(
            f"{ordinal}\n{_format_srt_timestamp(start_ms)} --> "
            f"{_format_srt_timestamp(end_ms)}\n{cue.text}\n\n"
        )
        previous_end_us = cue.end_us
        _report_progress(on_progress, ordinal, len(cues))
    payload = "".join(chunks).encode("utf-8")
    return _atomic_write(
        destination,
        payload,
        cancellation=cancellation,
        max_bytes=max_bytes,
        error_code="srt_write_failed",
        message_vi="Không thể ghi phụ đề tiếng Việt",
    )


def _atomic_write(
    destination: Path,
    payload: bytes,
    *,
    cancellation: Cancellation | None,
    max_bytes: int,
    error_code: str,
    message_vi: str,
) -> ArtifactFile:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if len(payload) > max_bytes:
        raise NarrationArtifactError(
            "narration_artifact_too_large",
            "Artifact thuyết minh vượt giới hạn kích thước",
            retryable=False,
        )
    _raise_if_cancelled(cancellation)
    temporary: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _raise_if_cancelled(cancellation)
        os.replace(temporary, destination)
        temporary = None
    except NarrationArtifactError:
        raise
    except OSError as exc:
        raise NarrationArtifactError(error_code, message_vi, retryable=True) from exc
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    return ArtifactFile(
        path=destination,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def _normalize_text(value: object) -> str:
    if not isinstance(value, str) or _CONTROL_PATTERN.search(value):
        raise NarrationArtifactError(
            "narration_text_invalid",
            "Nội dung thuyết minh không hợp lệ",
            retryable=False,
        )
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    if not normalized:
        raise NarrationArtifactError(
            "narration_text_empty",
            "Nội dung thuyết minh không được để trống",
            retryable=False,
        )
    return normalized


def _normalize_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise NarrationArtifactError(
            "timing_report_invalid",
            f"{field} không hợp lệ",
            retryable=False,
        )
    normalized = value.strip()
    if not normalized or _CONTROL_PATTERN.search(normalized):
        raise NarrationArtifactError(
            "timing_report_invalid",
            f"{field} không hợp lệ",
            retryable=False,
        )
    return normalized


def _finite_decimal(value: float) -> float:
    if value != value or value in {float("inf"), float("-inf")}:
        raise NarrationArtifactError(
            "timing_report_invalid",
            "Báo cáo timing chứa số không hữu hạn",
            retryable=False,
        )
    return round(value, 8)


def _format_srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _raise_if_cancelled(cancellation: Cancellation | None) -> None:
    if cancellation is None:
        return
    try:
        cancelled = bool(
            cancellation() if callable(cancellation) else cancellation.is_cancelled()
        )
    except Exception as exc:
        raise NarrationArtifactError(
            "artifact_cancellation_invalid",
            "Không thể kiểm tra trạng thái hủy artifact",
            retryable=False,
        ) from exc
    if cancelled:
        raise NarrationArtifactError(
            "artifact_cancelled",
            "Đã hủy tạo artifact thuyết minh",
            retryable=True,
        )


def _report_progress(
    callback: ArtifactProgress | None, completed: int, total: int
) -> None:
    if callback is None:
        return
    try:
        callback(completed, total)
    except Exception as exc:
        raise NarrationArtifactError(
            "artifact_progress_failed",
            "Không thể cập nhật tiến độ tạo artifact",
            retryable=False,
        ) from exc


__all__ = [
    "DEFAULT_MAX_ARTIFACT_BYTES",
    "NARRATION_ARTIFACT_SCHEMA_VERSION",
    "ArtifactFile",
    "ArtifactProgress",
    "NarrationArtifactError",
    "SrtCue",
    "TimingReport",
    "TimingReportBlock",
    "build_srt_cues",
    "build_timing_report",
    "write_srt_artifact",
    "write_timing_report",
]
