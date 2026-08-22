"""Offline GPU speech recognition backed by a verified local Whisper model."""

from __future__ import annotations

import contextlib
import importlib
import math
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .domain import (
    TranscriptionProgress,
    TranscriptionResult,
    TranscriptSegment,
)

_LANGUAGE_ALIASES = {
    "ara": "ar",
    "deu": "de",
    "ger": "de",
    "eng": "en",
    "spa": "es",
    "fra": "fr",
    "fre": "fr",
    "ind": "id",
    "jpn": "ja",
    "kor": "ko",
    "rus": "ru",
    "tha": "th",
    "vie": "vi",
    "chi": "zh",
    "zho": "zh",
}
_AUTO_LANGUAGES = {"", "auto", "und", "unknown", "mul", "zxx"}
_COMPUTE_TYPES = {"float16", "int8_float16", "bfloat16", "int8"}


class TranscriptionError(RuntimeError):
    def __init__(self, code: str, message_vi: str, *, retryable: bool) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable


class LanguageDetectionRequired(TranscriptionError):
    def __init__(
        self,
        detected_language: str,
        probability: float,
        alternatives: tuple[tuple[str, float], ...],
    ) -> None:
        super().__init__(
            "language_uncertain",
            "Không thể xác định chắc chắn ngôn ngữ lời thoại",
            retryable=False,
        )
        self.detected_language = detected_language
        self.probability = probability
        self.alternatives = alternatives


class NoSpeechError(TranscriptionError):
    def __init__(self) -> None:
        super().__init__(
            "no_speech",
            "Không phát hiện lời thoại trong video",
            retryable=False,
        )


ModelFactory = Callable[..., Any]


class FasterWhisperRecognizer:
    """Load one model for one stage and release it before later GPU stages."""

    def __init__(
        self,
        *,
        model_factory: ModelFactory | None = None,
        language_confidence_threshold: float = 0.5,
        device: str = "cuda",
    ) -> None:
        if not 0.0 <= language_confidence_threshold <= 1.0:
            raise ValueError("Ngưỡng nhận diện ngôn ngữ không hợp lệ")
        self._model_factory = model_factory
        self._language_threshold = language_confidence_threshold
        if device not in {"cuda", "cpu"}:
            raise ValueError("Thiết bị ASR phải là cuda hoặc cpu")
        self._device = device

    def transcribe(
        self,
        media_path: Path,
        *,
        model_path: Path,
        model_id: str,
        compute_type: str,
        language: str | None,
        duration_us: int,
        on_progress: TranscriptionProgress | None = None,
    ) -> TranscriptionResult:
        source = media_path.resolve(strict=False)
        local_model = model_path.resolve(strict=False)
        if not source.is_file():
            raise TranscriptionError(
                "source_media_missing",
                "Không tìm thấy file video để nhận dạng lời nói",
                retryable=True,
            )
        if not local_model.is_dir():
            raise TranscriptionError(
                "model_missing",
                "Không tìm thấy model ASR đã cài đặt",
                retryable=True,
            )
        if duration_us <= 0:
            raise TranscriptionError(
                "invalid_media_duration",
                "Thời lượng video không hợp lệ",
                retryable=False,
            )
        if compute_type not in _COMPUTE_TYPES:
            raise TranscriptionError(
                "unsupported_compute_type",
                "Kiểu tính toán ASR không được hỗ trợ",
                retryable=False,
            )

        requested_language = normalize_whisper_language(language)
        factory = self._model_factory or _load_whisper_model
        model: Any | None = None
        try:
            model = factory(
                str(local_model),
                device=self._device,
                compute_type=compute_type,
                local_files_only=True,
            )
            raw_segments, info = model.transcribe(
                str(source),
                language=requested_language,
                task="transcribe",
                beam_size=5,
                temperature=0.0,
                condition_on_previous_text=True,
                vad_filter=True,
                word_timestamps=False,
            )
            detected_language = str(
                getattr(info, "language", None) or requested_language or "und"
            ).strip().lower()
            probability = _probability(
                getattr(info, "language_probability", None),
                default=1.0 if requested_language is not None else 0.0,
            )
            alternatives = _language_alternatives(
                getattr(info, "all_language_probs", None)
            )
            if (
                requested_language is None
                and probability < self._language_threshold
            ):
                raise LanguageDetectionRequired(
                    detected_language,
                    probability,
                    alternatives,
                )
            normalized = normalize_segments(
                raw_segments,
                duration_us=duration_us,
                on_progress=on_progress,
            )
            if not normalized:
                raise NoSpeechError()
            return TranscriptionResult(
                source="asr",
                language=detected_language,
                language_probability=probability,
                duration_us=duration_us,
                segments=normalized,
                model_id=model_id,
            )
        except TranscriptionError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise TranscriptionError(
                "asr_failed",
                "Nhận dạng lời nói cục bộ thất bại",
                retryable=True,
            ) from error
        finally:
            if model is not None:
                del model
            _release_cuda_cache()


def normalize_whisper_language(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().replace("_", "-").split("-", 1)[0]
    if normalized in _AUTO_LANGUAGES:
        return None
    normalized = _LANGUAGE_ALIASES.get(normalized, normalized)
    if len(normalized) != 2 or not normalized.isalpha():
        raise TranscriptionError(
            "unsupported_language",
            "Mã ngôn ngữ nguồn không được Whisper hỗ trợ",
            retryable=False,
        )
    return normalized


def normalize_segments(
    segments: Iterable[Any],
    *,
    duration_us: int,
    on_progress: TranscriptionProgress | None = None,
) -> tuple[TranscriptSegment, ...]:
    normalized: list[TranscriptSegment] = []
    previous_end_us = 0
    for raw in segments:
        text = " ".join(str(getattr(raw, "text", "")).split())
        start_us = _seconds_to_us(getattr(raw, "start", None), duration_us)
        end_us = _seconds_to_us(getattr(raw, "end", None), duration_us)
        if not text or start_us is None or end_us is None:
            continue
        start_us = max(start_us, previous_end_us)
        if end_us <= start_us:
            continue
        segment = TranscriptSegment(
            start_us=start_us,
            end_us=end_us,
            text=text,
            average_log_probability=_optional_finite_float(
                getattr(raw, "avg_logprob", None)
            ),
            no_speech_probability=_optional_probability(
                getattr(raw, "no_speech_prob", None)
            ),
        )
        normalized.append(segment)
        previous_end_us = segment.end_us
        if on_progress is not None:
            on_progress(segment.end_us, len(normalized))
    return tuple(normalized)


def _load_whisper_model(*args: Any, **kwargs: Any) -> Any:
    module = importlib.import_module("faster_whisper")
    return module.WhisperModel(*args, **kwargs)


def _seconds_to_us(value: object, duration_us: int) -> int | None:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    return min(duration_us, max(0, round(seconds * 1_000_000)))


def _probability(value: object, *, default: float) -> float:
    parsed = _optional_probability(value)
    return default if parsed is None else parsed


def _optional_probability(value: object) -> float | None:
    parsed = _optional_finite_float(value)
    if parsed is None:
        return None
    return min(1.0, max(0.0, parsed))


def _optional_finite_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _language_alternatives(value: object) -> tuple[tuple[str, float], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    parsed: list[tuple[str, float]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        language = str(item[0]).strip().lower()
        probability = _optional_probability(item[1])
        if language and probability is not None:
            parsed.append((language, probability))
    parsed.sort(key=lambda item: item[1], reverse=True)
    return tuple(parsed[:5])


def _release_cuda_cache() -> None:
    # Best effort: a missing/broken torch install must never fail a finished job.
    with contextlib.suppress(Exception):
        torch = importlib.import_module("torch")
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


__all__ = [
    "FasterWhisperRecognizer",
    "LanguageDetectionRequired",
    "NoSpeechError",
    "TranscriptionError",
    "normalize_segments",
    "normalize_whisper_language",
]
