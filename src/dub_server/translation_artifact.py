"""Durable, integrity-checked artifacts for the offline translation stage.

This module contains data contracts and local file helpers only. Translation
backends and worker orchestration are intentionally kept outside it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import TranscriptionResult

TRANSLATION_ARTIFACT_SCHEMA_VERSION = 1
DEFAULT_MAX_TRANSLATION_ARTIFACT_BYTES = 64 * 1024 * 1024

_ARTIFACT_TYPE = "translation"
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_DISALLOWED_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class TranslationArtifactError(ValueError):
    """A safe translation artifact validation or local I/O error."""


class TranslationArtifactIntegrityError(TranslationArtifactError):
    """Raised when an artifact or its source transcript has the wrong digest."""


@dataclass(frozen=True, slots=True)
class TranslationSegment:
    """One translated block retaining its source text and media timeline."""

    start_us: int
    end_us: int
    source_text: str
    translated_text: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.start_us, bool)
            or isinstance(self.end_us, bool)
            or not isinstance(self.start_us, int)
            or not isinstance(self.end_us, int)
            or self.start_us < 0
            or self.end_us <= self.start_us
        ):
            raise TranslationArtifactError("M\u1ed1c th\u1eddi gian b\u1ea3n d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7")
        source_text = _normalize_text(self.source_text, field_name="source_text")
        translated_text = _normalize_text(
            self.translated_text, field_name="translated_text"
        )
        object.__setattr__(self, "source_text", source_text)
        object.__setattr__(self, "translated_text", translated_text)


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """Serializable output consumed by timing and Vietnamese TTS stages."""

    source_language: str
    target_language: str
    duration_us: int
    source_transcript_sha256: str
    model_id: str
    segments: tuple[TranslationSegment, ...]
    model_revision: str | None = None

    def __post_init__(self) -> None:
        source_language = _normalize_language(
            self.source_language, field_name="source_language"
        )
        target_language = _normalize_language(
            self.target_language, field_name="target_language"
        )
        if (
            isinstance(self.duration_us, bool)
            or not isinstance(self.duration_us, int)
            or self.duration_us <= 0
        ):
            raise TranslationArtifactError("Th\u1eddi l\u01b0\u1ee3ng b\u1ea3n d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7")
        source_digest = _normalize_sha256(
            self.source_transcript_sha256,
            message="SHA-256 transcript ngu\u1ed3n kh\u00f4ng h\u1ee3p l\u1ec7",
        )
        model_id = _normalize_required_string(
            self.model_id,
            message="M\u00e3 model d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7",
        )
        model_revision = self.model_revision
        if model_revision is not None:
            model_revision = _normalize_required_string(
                model_revision,
                message="Revision model d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7",
            )
        if not isinstance(self.segments, tuple) or not self.segments:
            raise TranslationArtifactError("B\u1ea3n d\u1ecbch kh\u00f4ng c\u00f3 segment")
        _validate_timeline(self.segments, duration_us=self.duration_us)
        object.__setattr__(self, "source_language", source_language)
        object.__setattr__(self, "target_language", target_language)
        object.__setattr__(self, "source_transcript_sha256", source_digest)
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "model_revision", model_revision)


@dataclass(frozen=True, slots=True)
class TranslationArtifact:
    """A loaded or newly published translation artifact and its identity."""

    path: Path
    result: TranslationResult
    sha256: str
    size_bytes: int
    schema_version: int = TRANSLATION_ARTIFACT_SCHEMA_VERSION


def build_translation_result(
    source: TranscriptionResult,
    translated_texts: Iterable[str],
    *,
    target_language: str,
    model_id: str,
    source_transcript_sha256: str,
    model_revision: str | None = None,
) -> TranslationResult:
    """Build a one-to-one translation while preserving source timestamps.

    Translation pipelines that merge or split transcript segments can create a
    :class:`TranslationResult` directly with their normalized block timeline.
    """

    if not isinstance(source, TranscriptionResult):
        raise TranslationArtifactError("Transcript ngu\u1ed3n kh\u00f4ng h\u1ee3p l\u1ec7")
    if isinstance(translated_texts, (str, bytes)):
        raise TranslationArtifactError("Danh s\u00e1ch n\u1ed9i dung d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7")
    translations = tuple(translated_texts)
    if len(translations) != len(source.segments):
        raise TranslationArtifactError(
            "S\u1ed1 n\u1ed9i dung d\u1ecbch kh\u00f4ng kh\u1edbp transcript ngu\u1ed3n"
        )
    segments = tuple(
        TranslationSegment(
            start_us=source_segment.start_us,
            end_us=source_segment.end_us,
            source_text=source_segment.text,
            translated_text=translated_text,
        )
        for source_segment, translated_text in zip(
            source.segments, translations, strict=True
        )
    )
    return TranslationResult(
        source_language=source.language,
        target_language=target_language,
        duration_us=source.duration_us,
        source_transcript_sha256=source_transcript_sha256,
        model_id=model_id,
        model_revision=model_revision,
        segments=segments,
    )


def write_translation_artifact(
    path: Path, result: TranslationResult
) -> TranslationArtifact:
    """Atomically publish canonical schema-v1 JSON and return its SHA-256."""

    _validate_result(result)
    destination = Path(path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TranslationArtifactError("Kh\u00f4ng th\u1ec3 t\u1ea1o th\u01b0 m\u1ee5c artifact d\u1ecbch") from exc
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
        raise TranslationArtifactError("Kh\u00f4ng th\u1ec3 ghi artifact d\u1ecbch") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return TranslationArtifact(
        path=destination,
        result=result,
        sha256=digest,
        size_bytes=len(payload),
    )


def load_translation_artifact(
    path: Path,
    *,
    expected_sha256: str | None = None,
    expected_source_transcript_sha256: str | None = None,
    max_bytes: int = DEFAULT_MAX_TRANSLATION_ARTIFACT_BYTES,
) -> TranslationArtifact:
    """Read, hash-authenticate, and strictly validate a translation artifact."""

    payload = _read_bounded_file(
        Path(path),
        max_bytes=max_bytes,
        too_large_message="Artifact d\u1ecbch v\u01b0\u1ee3t gi\u1edbi h\u1ea1n k\u00edch th\u01b0\u1edbc",
        read_error_message="Kh\u00f4ng th\u1ec3 \u0111\u1ecdc artifact d\u1ecbch",
    )
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None:
        expected_digest = _normalize_sha256(
            expected_sha256,
            message="SHA-256 artifact d\u1ecbch mong \u0111\u1ee3i kh\u00f4ng h\u1ee3p l\u1ec7",
            integrity_error=True,
        )
        if not hmac.compare_digest(digest, expected_digest):
            raise TranslationArtifactIntegrityError(
                "SHA-256 artifact d\u1ecbch kh\u00f4ng kh\u1edbp"
            )
    result = _deserialize_result(payload)
    if expected_source_transcript_sha256 is not None:
        expected_source_digest = _normalize_sha256(
            expected_source_transcript_sha256,
            message="SHA-256 transcript ngu\u1ed3n mong \u0111\u1ee3i kh\u00f4ng h\u1ee3p l\u1ec7",
            integrity_error=True,
        )
        if not hmac.compare_digest(
            result.source_transcript_sha256, expected_source_digest
        ):
            raise TranslationArtifactIntegrityError(
                "Artifact d\u1ecbch kh\u00f4ng thu\u1ed9c transcript ngu\u1ed3n mong \u0111\u1ee3i"
            )
    return TranslationArtifact(
        path=Path(path),
        result=result,
        sha256=digest,
        size_bytes=len(payload),
    )


def read_translation_artifact(
    path: Path,
    *,
    expected_sha256: str | None = None,
    expected_source_transcript_sha256: str | None = None,
    max_bytes: int = DEFAULT_MAX_TRANSLATION_ARTIFACT_BYTES,
) -> TranslationResult:
    """Convenience wrapper returning only the validated translation result."""

    return load_translation_artifact(
        path,
        expected_sha256=expected_sha256,
        expected_source_transcript_sha256=expected_source_transcript_sha256,
        max_bytes=max_bytes,
    ).result


def translation_file_sha256(
    path: Path, *, max_bytes: int = DEFAULT_MAX_TRANSLATION_ARTIFACT_BYTES
) -> str:
    """Calculate a bounded local SHA-256 suitable for a stage checkpoint."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    digest = hashlib.sha256()
    total = 0
    try:
        with Path(path).open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise TranslationArtifactError(
                        "Artifact d\u1ecbch v\u01b0\u1ee3t gi\u1edbi h\u1ea1n k\u00edch th\u01b0\u1edbc"
                    )
                digest.update(chunk)
    except TranslationArtifactError:
        raise
    except OSError as exc:
        raise TranslationArtifactError("Kh\u00f4ng th\u1ec3 \u0111\u1ecdc artifact d\u1ecbch") from exc
    return digest.hexdigest()


def _validate_result(result: TranslationResult) -> None:
    if not isinstance(result, TranslationResult):
        raise TranslationArtifactError("K\u1ebft qu\u1ea3 d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7")
    _validate_timeline(result.segments, duration_us=result.duration_us)


def _validate_timeline(
    segments: tuple[TranslationSegment, ...], *, duration_us: int
) -> None:
    previous_end_us = 0
    for segment in segments:
        if not isinstance(segment, TranslationSegment):
            raise TranslationArtifactError("Segment b\u1ea3n d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7")
        if segment.start_us < previous_end_us:
            raise TranslationArtifactError(
                "Timestamp b\u1ea3n d\u1ecbch b\u1ecb overlap ho\u1eb7c kh\u00f4ng \u0111\u01a1n \u0111i\u1ec7u"
            )
        if segment.end_us > duration_us:
            raise TranslationArtifactError(
                "Timestamp b\u1ea3n d\u1ecbch v\u01b0\u1ee3t th\u1eddi l\u01b0\u1ee3ng media"
            )
        previous_end_us = segment.end_us


def _serialize_result(result: TranslationResult) -> bytes:
    document: dict[str, Any] = {
        "artifact_type": _ARTIFACT_TYPE,
        "schema_version": TRANSLATION_ARTIFACT_SCHEMA_VERSION,
        "source": {
            "language": result.source_language,
            "transcript_sha256": result.source_transcript_sha256,
        },
        "target": {"language": result.target_language},
        "model": {
            "id": result.model_id,
            "revision": result.model_revision,
        },
        "duration_us": result.duration_us,
        "segments": [
            {
                "ordinal": ordinal,
                "start_us": segment.start_us,
                "end_us": segment.end_us,
                "source_text": segment.source_text,
                "translated_text": segment.translated_text,
            }
            for ordinal, segment in enumerate(result.segments)
        ],
    }
    try:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TranslationArtifactError("Kh\u00f4ng th\u1ec3 tu\u1ea7n t\u1ef1 h\u00f3a artifact d\u1ecbch") from exc
    return encoded + b"\n"


def _deserialize_result(payload: bytes) -> TranslationResult:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TranslationArtifactError(
            "Artifact d\u1ecbch kh\u00f4ng ph\u1ea3i JSON UTF-8 h\u1ee3p l\u1ec7"
        ) from exc
    if not isinstance(document, dict):
        raise TranslationArtifactError("C\u1ea5u tr\u00fac artifact d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7")
    if document.get("artifact_type") != _ARTIFACT_TYPE:
        raise TranslationArtifactError("Lo\u1ea1i artifact d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7")
    if (
        _strict_int(document.get("schema_version"), "schema_version")
        != TRANSLATION_ARTIFACT_SCHEMA_VERSION
    ):
        raise TranslationArtifactError(
            "Phi\u00ean b\u1ea3n artifact d\u1ecbch kh\u00f4ng \u0111\u01b0\u1ee3c h\u1ed7 tr\u1ee3"
        )
    source = _strict_mapping(document.get("source"), "source")
    target = _strict_mapping(document.get("target"), "target")
    model = _strict_mapping(document.get("model"), "model")
    raw_segments = document.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise TranslationArtifactError("Danh s\u00e1ch segment b\u1ea3n d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7")
    segments: list[TranslationSegment] = []
    for expected_ordinal, raw_segment in enumerate(raw_segments):
        segment_data = _strict_mapping(raw_segment, "segment")
        ordinal = _strict_int(segment_data.get("ordinal"), "ordinal")
        if ordinal != expected_ordinal:
            raise TranslationArtifactError("Th\u1ee9 t\u1ef1 segment b\u1ea3n d\u1ecbch kh\u00f4ng li\u00ean t\u1ee5c")
        source_text = segment_data.get("source_text")
        translated_text = segment_data.get("translated_text")
        if not isinstance(source_text, str) or not isinstance(translated_text, str):
            raise TranslationArtifactError("N\u1ed9i dung segment b\u1ea3n d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7")
        segments.append(
            TranslationSegment(
                start_us=_strict_int(segment_data.get("start_us"), "start_us"),
                end_us=_strict_int(segment_data.get("end_us"), "end_us"),
                source_text=source_text,
                translated_text=translated_text,
            )
        )
    source_language = source.get("language")
    target_language = target.get("language")
    source_digest = source.get("transcript_sha256")
    model_id = model.get("id")
    model_revision = model.get("revision")
    if not isinstance(source_language, str) or not isinstance(target_language, str):
        raise TranslationArtifactError("Ng\u00f4n ng\u1eef artifact d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7")
    if not isinstance(source_digest, str) or not isinstance(model_id, str):
        raise TranslationArtifactError("Ngu\u1ed3n ho\u1eb7c model artifact d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7")
    if model_revision is not None and not isinstance(model_revision, str):
        raise TranslationArtifactError("Revision model artifact d\u1ecbch kh\u00f4ng h\u1ee3p l\u1ec7")
    return TranslationResult(
        source_language=source_language,
        target_language=target_language,
        duration_us=_strict_int(document.get("duration_us"), "duration_us"),
        source_transcript_sha256=source_digest,
        model_id=model_id,
        model_revision=model_revision,
        segments=tuple(segments),
    )


def _read_bounded_file(
    path: Path,
    *,
    max_bytes: int,
    too_large_message: str,
    read_error_message: str,
) -> bytes:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise TranslationArtifactError(too_large_message)
        with path.open("rb") as stream:
            payload = stream.read(max_bytes + 1)
    except TranslationArtifactError:
        raise
    except OSError as exc:
        raise TranslationArtifactError(read_error_message) from exc
    if len(payload) > max_bytes:
        raise TranslationArtifactError(too_large_message)
    return payload


def _normalize_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TranslationArtifactError(f"{field_name} must be text")
    if _DISALLOWED_CONTROL_PATTERN.search(value):
        raise TranslationArtifactError(f"{field_name} contains control characters")
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    if not normalized:
        raise TranslationArtifactError(f"{field_name} must not be empty")
    return normalized


def _normalize_language(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TranslationArtifactError(f"{field_name} must be text")
    normalized = value.strip().lower().replace("_", "-")
    if not normalized or any(character.isspace() for character in normalized):
        raise TranslationArtifactError(f"{field_name} is invalid")
    return normalized


def _normalize_required_string(value: object, *, message: str) -> str:
    if not isinstance(value, str):
        raise TranslationArtifactError(message)
    normalized = value.strip()
    if not normalized or _DISALLOWED_CONTROL_PATTERN.search(normalized):
        raise TranslationArtifactError(message)
    return normalized


def _normalize_sha256(
    value: object,
    *,
    message: str,
    integrity_error: bool = False,
) -> str:
    error_type = (
        TranslationArtifactIntegrityError
        if integrity_error
        else TranslationArtifactError
    )
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise error_type(message)
    return value.lower()


def _strict_mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TranslationArtifactError(f"{field_name} must be an object")
    return value


def _strict_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TranslationArtifactError(f"{field_name} must be an integer")
    return value
