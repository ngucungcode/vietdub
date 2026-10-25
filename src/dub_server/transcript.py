"""Offline subtitle parsing and integrity-checked transcript artifacts.

The public contract in this module deliberately uses integer microseconds and
plain JSON.  It performs no network I/O and does not invoke external tools.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import math
import os
import re
import tempfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dub_server.domain import (
    SubtitleFormat,
    TranscriptionResult,
    TranscriptSegment,
    normalize_speaker_label,
)

TRANSCRIPT_SCHEMA_VERSION = 1
DEFAULT_MAX_SUBTITLE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

_TIMING_LINE = re.compile(
    r"^\s*(?P<start>(?:\d{1,6}:)?\d{1,2}:\d{2}[,.]\d{1,6})"
    r"\s*-->\s*"
    r"(?P<end>(?:\d{1,6}:)?\d{1,2}:\d{2}[,.]\d{1,6})"
    r"(?:\s+.*)?$"
)
_HTML_TAG = re.compile(r"<[^>]*>")
_ASS_OVERRIDE = re.compile(r"\{[^{}]*\}")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
# A speaker label on the first line of a cue, e.g. "ANNA: Xin chào".
# The label must start with a letter so a clock like "12:30" never matches,
# and the spoken remainder must be non-empty so a bare "ANNA:" stays text.
_SPEAKER_LABEL = re.compile(
    r"^(?P<label>[^\W\d_][\w .'\-]{0,39}):\s+(?P<rest>.+)$",
    re.DOTALL,
)
_DIALOGUE_DASH = ("- ", "– ", "— ")
# ASS styles that describe layout, not a character; they must not become a
# speaker just because a file leaves the Name field empty.
_GENERIC_ASS_STYLES = frozenset({"default", "defaulttop", "defaultbottom", "main", "alt"})


def split_speaker_label(text: str) -> tuple[str | None, str]:
    """Split a leading ``NAME:`` speaker label off cue text, if present.

    Returns ``(speaker, spoken_text)``. When no label is found the speaker
    is ``None`` and the text is returned unchanged. A leading dialogue dash
    (``- ANNA: ...``) is tolerated, and ASS override blocks before the
    label are ignored for matching but kept out of the spoken text only
    when a label is actually found.
    """

    body = text.strip()
    for dash in _DIALOGUE_DASH:
        if body.startswith(dash):
            body = body[len(dash):].lstrip()
            break
    first, separator, rest = body.partition("\n")
    # Formatting tags are stripped by _clean_text later anyway, so they are
    # ignored here too: "<i>ANNA: ..." still names its speaker.
    candidate = _HTML_TAG.sub("", _ASS_OVERRIDE.sub("", first)).strip()
    match = _SPEAKER_LABEL.match(candidate)
    if match is None:
        return None, text
    spoken_first = match.group("rest").strip()
    if not spoken_first:
        return None, text
    spoken = spoken_first if not separator else f"{spoken_first}\n{rest}"
    return normalize_speaker_label(match.group("label")), spoken


class TranscriptError(ValueError):
    """A safe, non-retryable transcript parsing or schema error."""


class TranscriptIntegrityError(TranscriptError):
    """Raised when an artifact does not match its expected SHA-256."""


@dataclass(frozen=True, slots=True)
class TranscriptArtifact:
    """A loaded or newly written transcript and its content identity."""

    path: Path
    result: TranscriptionResult
    sha256: str
    size_bytes: int
    schema_version: int = TRANSCRIPT_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class _RawCue:
    start_us: int
    end_us: int
    text: str
    sequence: int
    speaker: str | None = None


def parse_subtitle_file(
    path: Path,
    *,
    language: str,
    duration_us: int,
    subtitle_format: SubtitleFormat | str | None = None,
    language_probability: float = 1.0,
    max_bytes: int = DEFAULT_MAX_SUBTITLE_BYTES,
    allow_legacy_cp1258: bool = True,
) -> TranscriptionResult:
    """Parse a bounded local subtitle file into a normalized transcript."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    source_path = Path(path)
    try:
        size = source_path.stat().st_size
        if size > max_bytes:
            raise TranscriptError("File ph\u1ee5 \u0111\u1ec1 v\u01b0\u1ee3t gi\u1edbi h\u1ea1n k\u00edch th\u01b0\u1edbc")
        with source_path.open("rb") as stream:
            payload = stream.read(max_bytes + 1)
    except TranscriptError:
        raise
    except OSError as exc:
        raise TranscriptError("Kh\u00f4ng th\u1ec3 \u0111\u1ecdc file ph\u1ee5 \u0111\u1ec1") from exc
    if len(payload) > max_bytes:
        raise TranscriptError("File ph\u1ee5 \u0111\u1ec1 v\u01b0\u1ee3t gi\u1edbi h\u1ea1n k\u00edch th\u01b0\u1edbc")
    selected_format = (
        _coerce_subtitle_format(subtitle_format)
        if subtitle_format is not None
        else _format_from_suffix(source_path.suffix)
    )
    return parse_subtitle_bytes(
        payload,
        subtitle_format=selected_format,
        language=language,
        duration_us=duration_us,
        language_probability=language_probability,
        allow_legacy_cp1258=allow_legacy_cp1258,
    )


def parse_subtitle_bytes(
    payload: bytes,
    *,
    subtitle_format: SubtitleFormat | str,
    language: str,
    duration_us: int,
    language_probability: float = 1.0,
    allow_legacy_cp1258: bool = True,
) -> TranscriptionResult:
    """Parse SRT, WebVTT, or ASS bytes without performing network I/O."""

    if duration_us <= 0:
        raise TranscriptError("Th\u1eddi l\u01b0\u1ee3ng media kh\u00f4ng h\u1ee3p l\u1ec7")
    if not isinstance(payload, bytes) or not payload:
        raise TranscriptError("File ph\u1ee5 \u0111\u1ec1 \u0111ang tr\u1ed1ng")
    selected_format = _coerce_subtitle_format(subtitle_format)
    text = _decode_subtitle(payload, allow_legacy_cp1258=allow_legacy_cp1258)
    if selected_format is SubtitleFormat.ASS:
        cues = _parse_ass(text)
    else:
        cues = _parse_timed_text(text, webvtt=selected_format is SubtitleFormat.VTT)
    segments = _normalize_cues(cues, duration_us=duration_us)
    if not segments:
        raise TranscriptError("Ph\u1ee5 \u0111\u1ec1 kh\u00f4ng c\u00f3 l\u1eddi tho\u1ea1i h\u1ee3p l\u1ec7")
    try:
        return TranscriptionResult(
            source="subtitle",
            language=language,
            language_probability=language_probability,
            duration_us=duration_us,
            segments=segments,
            model_id=None,
        )
    except (TypeError, ValueError) as exc:
        raise TranscriptError("Th\u00f4ng tin transcript kh\u00f4ng h\u1ee3p l\u1ec7") from exc


def write_transcript_artifact(path: Path, result: TranscriptionResult) -> TranscriptArtifact:
    """Atomically write canonical schema-v1 JSON and return its SHA-256."""

    _validate_result(result)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _serialize_result(result)
    digest = hashlib.sha256(payload).hexdigest()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    except OSError as exc:
        raise TranscriptError("Kh\u00f4ng th\u1ec3 ghi artifact transcript") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return TranscriptArtifact(
        path=destination,
        result=result,
        sha256=digest,
        size_bytes=len(payload),
    )


def load_transcript_artifact(
    path: Path,
    *,
    expected_sha256: str | None = None,
    max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> TranscriptArtifact:
    """Read, hash, optionally authenticate, and strictly validate an artifact."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    source_path = Path(path)
    try:
        size = source_path.stat().st_size
        if size > max_bytes:
            raise TranscriptError("Artifact transcript v\u01b0\u1ee3t gi\u1edbi h\u1ea1n k\u00edch th\u01b0\u1edbc")
        with source_path.open("rb") as stream:
            payload = stream.read(max_bytes + 1)
    except TranscriptError:
        raise
    except OSError as exc:
        raise TranscriptError("Kh\u00f4ng th\u1ec3 \u0111\u1ecdc artifact transcript") from exc
    if len(payload) > max_bytes:
        raise TranscriptError("Artifact transcript v\u01b0\u1ee3t gi\u1edbi h\u1ea1n k\u00edch th\u01b0\u1edbc")
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
            raise TranscriptIntegrityError("SHA-256 transcript mong \u0111\u1ee3i kh\u00f4ng h\u1ee3p l\u1ec7")
        if not hmac.compare_digest(digest, expected_sha256.lower()):
            raise TranscriptIntegrityError("SHA-256 artifact transcript kh\u00f4ng kh\u1edbp")
    result = _deserialize_result(payload)
    return TranscriptArtifact(
        path=source_path,
        result=result,
        sha256=digest,
        size_bytes=len(payload),
    )


def read_transcript_artifact(
    path: Path,
    *,
    expected_sha256: str | None = None,
    max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> TranscriptionResult:
    """Convenience wrapper returning only the validated transcription result."""

    return load_transcript_artifact(
        path,
        expected_sha256=expected_sha256,
        max_bytes=max_bytes,
    ).result


def transcript_file_sha256(path: Path, *, max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES) -> str:
    """Return a bounded file SHA-256 for checkpoint verification."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    digest = hashlib.sha256()
    total = 0
    try:
        with Path(path).open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise TranscriptError("Artifact transcript v\u01b0\u1ee3t gi\u1edbi h\u1ea1n k\u00edch th\u01b0\u1edbc")
                digest.update(chunk)
    except TranscriptError:
        raise
    except OSError as exc:
        raise TranscriptError("Kh\u00f4ng th\u1ec3 \u0111\u1ecdc artifact transcript") from exc
    return digest.hexdigest()


def _parse_timed_text(text: str, *, webvtt: bool) -> tuple[_RawCue, ...]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[_RawCue] = []
    sequence = 0
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        upper = stripped.upper()
        if webvtt and (
            upper.startswith("WEBVTT")
            or upper == "STYLE"
            or upper == "REGION"
            or upper.startswith("NOTE")
        ):
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        timing_index = index
        timing = _TIMING_LINE.match(lines[timing_index])
        if timing is None and index + 1 < len(lines):
            timing_index = index + 1
            timing = _TIMING_LINE.match(lines[timing_index])
        if timing is None:
            index += 1
            continue
        index = timing_index + 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip():
            body.append(lines[index])
            index += 1
        try:
            start_us = _clock_to_us(timing.group("start"))
            end_us = _clock_to_us(timing.group("end"))
        except ValueError:
            continue
        speaker, spoken = split_speaker_label("\n".join(body))
        cues.append(_RawCue(start_us, end_us, spoken, sequence, speaker))
        sequence += 1
    return tuple(cues)


def _parse_ass(text: str) -> tuple[_RawCue, ...]:
    in_events = False
    fields = (
        "layer",
        "start",
        "end",
        "style",
        "name",
        "marginl",
        "marginr",
        "marginv",
        "effect",
        "text",
    )
    cues: list[_RawCue] = []
    sequence = 0
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.lstrip("\ufeff").strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_events = line.casefold() == "[events]"
            continue
        if not in_events:
            continue
        key, separator, value = line.partition(":")
        if not separator:
            continue
        normalized_key = key.strip().casefold()
        if normalized_key == "format":
            candidate_fields = tuple(part.strip().casefold() for part in value.split(","))
            if {"start", "end", "text"}.issubset(candidate_fields):
                fields = candidate_fields
            continue
        if normalized_key != "dialogue":
            continue
        parts = [part.strip() for part in value.split(",", len(fields) - 1)]
        if len(parts) != len(fields):
            continue
        record = dict(zip(fields, parts, strict=True))
        try:
            start_us = _clock_to_us(record["start"])
            end_us = _clock_to_us(record["end"])
        except (KeyError, ValueError):
            continue
        # The Name field names the character. Only fall back to the Style
        # when it is clearly a character style, never a layout default.
        # A "NAME:" prefix inside the dialogue is stripped from the spoken
        # text whenever present, whatever named the speaker.
        raw_text = record.get("text", "")
        label_speaker, spoken = split_speaker_label(raw_text)
        speaker = normalize_speaker_label(record.get("name"))
        if speaker is None:
            style = normalize_speaker_label(record.get("style"))
            if style is not None and style.casefold() not in _GENERIC_ASS_STYLES:
                speaker = style
        if speaker is None:
            speaker = label_speaker
        cues.append(_RawCue(start_us, end_us, spoken, sequence, speaker))
        sequence += 1
    return tuple(cues)


def _normalize_cues(
    cues: Iterable[_RawCue], *, duration_us: int
) -> tuple[TranscriptSegment, ...]:
    ordered = sorted(cues, key=lambda cue: (cue.start_us, cue.end_us, cue.sequence))
    segments: list[TranscriptSegment] = []
    previous_end_us = 0
    for cue in ordered:
        start_us = min(max(cue.start_us, 0), duration_us)
        end_us = min(max(cue.end_us, 0), duration_us)
        start_us = max(start_us, previous_end_us)
        text = _clean_text(cue.text)
        if not text or end_us <= start_us:
            continue
        segment = TranscriptSegment(
            start_us=start_us,
            end_us=end_us,
            text=text,
            speaker=cue.speaker,
        )
        segments.append(segment)
        previous_end_us = segment.end_us
    return tuple(segments)


def _clean_text(value: str) -> str:
    cleaned = value.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    cleaned = _ASS_OVERRIDE.sub("", cleaned)
    cleaned = _HTML_TAG.sub("", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = _CONTROL_CHARACTERS.sub("", cleaned)
    cleaned = unicodedata.normalize("NFC", cleaned)
    return " ".join(cleaned.split())


def _clock_to_us(value: str) -> int:
    normalized = value.strip().replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = 0
        minute_text, second_text = parts
    elif len(parts) == 3:
        hours = _strict_nonnegative_int(parts[0])
        minute_text, second_text = parts[1:]
    else:
        raise ValueError("invalid clock")
    minutes = _strict_nonnegative_int(minute_text)
    seconds_parts = second_text.split(".")
    if len(seconds_parts) != 2:
        raise ValueError("invalid clock")
    seconds = _strict_nonnegative_int(seconds_parts[0])
    fraction = seconds_parts[1]
    if not fraction.isdigit() or not 1 <= len(fraction) <= 6:
        raise ValueError("invalid clock")
    if minutes >= 60 or seconds >= 60:
        raise ValueError("invalid clock")
    fraction_us = int(fraction.ljust(6, "0"))
    return ((hours * 3600 + minutes * 60 + seconds) * 1_000_000) + fraction_us


def _strict_nonnegative_int(value: str) -> int:
    if not value.isdigit():
        raise ValueError("invalid non-negative integer")
    return int(value)


def _decode_subtitle(payload: bytes, *, allow_legacy_cp1258: bool) -> str:
    candidates: list[str]
    if payload.startswith(b"\xef\xbb\xbf"):
        candidates = ["utf-8-sig"]
    elif payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = ["utf-16"]
    else:
        sample = payload[:4096]
        even_nuls = sample[0::2].count(0)
        odd_nuls = sample[1::2].count(0)
        pairs = max(1, len(sample) // 2)
        if odd_nuls / pairs >= 0.2 and even_nuls / pairs < 0.05:
            candidates = ["utf-16-le", "utf-8"]
        elif even_nuls / pairs >= 0.2 and odd_nuls / pairs < 0.05:
            candidates = ["utf-16-be", "utf-8"]
        else:
            candidates = ["utf-8"]
        if allow_legacy_cp1258:
            candidates.append("cp1258")
    for encoding in candidates:
        try:
            decoded = payload.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
        decoded = decoded.lstrip("\ufeff")
        if _decoded_text_is_safe(decoded):
            return unicodedata.normalize("NFC", decoded)
    raise TranscriptError("Encoding ph\u1ee5 \u0111\u1ec1 kh\u00f4ng \u0111\u01b0\u1ee3c h\u1ed7 tr\u1ee3")


def _decoded_text_is_safe(value: str) -> bool:
    if not value or "\x00" in value or "\ufffd" in value:
        return False
    controls = sum(
        1
        for character in value
        if unicodedata.category(character) == "Cc" and character not in "\r\n\t"
    )
    return controls == 0


def _format_from_suffix(suffix: str) -> SubtitleFormat:
    try:
        return {
            ".srt": SubtitleFormat.SRT,
            ".vtt": SubtitleFormat.VTT,
            ".ass": SubtitleFormat.ASS,
            ".ssa": SubtitleFormat.ASS,
        }[suffix.casefold()]
    except KeyError as exc:
        raise TranscriptError("\u0110\u1ecbnh d\u1ea1ng ph\u1ee5 \u0111\u1ec1 kh\u00f4ng \u0111\u01b0\u1ee3c h\u1ed7 tr\u1ee3") from exc


def _coerce_subtitle_format(value: SubtitleFormat | str) -> SubtitleFormat:
    if isinstance(value, SubtitleFormat):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold().removeprefix(".")
        if normalized == "ssa":
            normalized = "ass"
        try:
            return SubtitleFormat(normalized)
        except ValueError:
            pass
    raise TranscriptError("\u0110\u1ecbnh d\u1ea1ng ph\u1ee5 \u0111\u1ec1 kh\u00f4ng \u0111\u01b0\u1ee3c h\u1ed7 tr\u1ee3")


def _serialize_result(result: TranscriptionResult) -> bytes:
    document: dict[str, Any] = {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "source": result.source,
        "language": result.language,
        "language_probability": result.language_probability,
        "duration_us": result.duration_us,
        "model_id": result.model_id,
        "segments": [
            {
                "start_us": segment.start_us,
                "end_us": segment.end_us,
                "text": segment.text,
                "average_log_probability": segment.average_log_probability,
                "no_speech_probability": segment.no_speech_probability,
                # Absent in artifacts written before speaker labels existed.
                "speaker": segment.speaker,
            }
            for segment in result.segments
        ],
    }
    try:
        text = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TranscriptError("Kh\u00f4ng th\u1ec3 tu\u1ea7n t\u1ef1 h\u00f3a transcript") from exc
    return f"{text}\n".encode()


def _deserialize_result(payload: bytes) -> TranscriptionResult:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TranscriptError("Artifact transcript kh\u00f4ng ph\u1ea3i JSON UTF-8 h\u1ee3p l\u1ec7") from exc
    if not isinstance(document, dict):
        raise TranscriptError("C\u1ea5u tr\u00fac artifact transcript kh\u00f4ng h\u1ee3p l\u1ec7")
    if _strict_int(document.get("schema_version"), "schema_version") != TRANSCRIPT_SCHEMA_VERSION:
        raise TranscriptError("Phi\u00ean b\u1ea3n artifact transcript kh\u00f4ng \u0111\u01b0\u1ee3c h\u1ed7 tr\u1ee3")
    raw_segments = document.get("segments")
    if not isinstance(raw_segments, list):
        raise TranscriptError("Danh s\u00e1ch segment transcript kh\u00f4ng h\u1ee3p l\u1ec7")
    segments: list[TranscriptSegment] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            raise TranscriptError("Segment transcript kh\u00f4ng h\u1ee3p l\u1ec7")
        text = raw.get("text")
        if not isinstance(text, str):
            raise TranscriptError("N\u1ed9i dung segment transcript kh\u00f4ng h\u1ee3p l\u1ec7")
        try:
            segment = TranscriptSegment(
                start_us=_strict_int(raw.get("start_us"), "start_us"),
                end_us=_strict_int(raw.get("end_us"), "end_us"),
                text=text,
                average_log_probability=_optional_finite_number(
                    raw.get("average_log_probability"), "average_log_probability"
                ),
                no_speech_probability=_optional_probability(
                    raw.get("no_speech_probability"), "no_speech_probability"
                ),
                # Old artifacts have no speaker key; that means unknown.
                speaker=normalize_speaker_label(raw.get("speaker")),
            )
        except ValueError as exc:
            raise TranscriptError("Segment transcript kh\u00f4ng h\u1ee3p l\u1ec7") from exc
        segments.append(segment)
    source = document.get("source")
    language = document.get("language")
    model_id = document.get("model_id")
    if not isinstance(source, str) or not isinstance(language, str):
        raise TranscriptError("Metadata transcript kh\u00f4ng h\u1ee3p l\u1ec7")
    if model_id is not None and not isinstance(model_id, str):
        raise TranscriptError("M\u00e3 model transcript kh\u00f4ng h\u1ee3p l\u1ec7")
    try:
        result = TranscriptionResult(
            source=source,
            language=language,
            language_probability=_required_probability(
                document.get("language_probability"), "language_probability"
            ),
            duration_us=_strict_int(document.get("duration_us"), "duration_us"),
            segments=tuple(segments),
            model_id=model_id,
        )
    except ValueError as exc:
        raise TranscriptError("Metadata transcript kh\u00f4ng h\u1ee3p l\u1ec7") from exc
    _validate_result(result)
    return result


def _validate_result(result: TranscriptionResult) -> None:
    if not isinstance(result, TranscriptionResult):
        raise TranscriptError("K\u1ebft qu\u1ea3 transcript kh\u00f4ng h\u1ee3p l\u1ec7")
    if not result.segments:
        raise TranscriptError("Transcript kh\u00f4ng c\u00f3 l\u1eddi tho\u1ea1i")
    if not math.isfinite(result.language_probability):
        raise TranscriptError("\u0110\u1ed9 tin c\u1eady ng\u00f4n ng\u1eef kh\u00f4ng h\u1ee3p l\u1ec7")
    previous_end_us = 0
    for segment in result.segments:
        if segment.start_us < previous_end_us:
            raise TranscriptError("Segment transcript b\u1ecb ch\u1ed3ng l\u1ea5n")
        if segment.end_us > result.duration_us:
            raise TranscriptError("Segment transcript v\u01b0\u1ee3t qu\u00e1 th\u1eddi l\u01b0\u1ee3ng media")
        _optional_finite_number(
            segment.average_log_probability, "average_log_probability"
        )
        _optional_probability(segment.no_speech_probability, "no_speech_probability")
        previous_end_us = segment.end_us


def _strict_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TranscriptError(f"{field} must be an integer")
    return value


def _optional_finite_number(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptError(f"{field} must be a finite number or null")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise TranscriptError(f"{field} must be finite")
    return parsed


def _optional_probability(value: object, field: str) -> float | None:
    if value is None:
        return None
    return _required_probability(value, field)


def _required_probability(value: object, field: str) -> float:
    parsed = _optional_finite_number(value, field)
    if parsed is None or not 0.0 <= parsed <= 1.0:
        raise TranscriptError(f"{field} must be between 0 and 1")
    return parsed
