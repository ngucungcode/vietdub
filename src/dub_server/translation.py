"""Pure contracts and timeline normalization for offline translation.

The helpers in this module never load a model and never access the network.
Backends receive short, deterministic blocks and return Vietnamese text only.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

_SPACE_PATTERN = re.compile(r"\s+")
_PUNCTUATION = frozenset(".!?…;:,。！？；：،؟")


class TranslationError(RuntimeError):
    """Typed failure safe to persist and expose through the Vietnamese API."""

    def __init__(self, code: str, message_vi: str, *, retryable: bool) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class TranslationBlock:
    """One normalized source block and its fixed media time slot."""

    start_us: int
    end_us: int
    source_text: str
    translated_text: str = ""
    source_ordinals: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.start_us < 0 or self.end_us <= self.start_us:
            raise TranslationError(
                "translation_timeline_invalid",
                "Mốc thời gian của khối dịch không hợp lệ",
                retryable=False,
            )
        source_text = _normalize_text(self.source_text)
        if not source_text:
            raise TranslationError(
                "translation_source_empty",
                "Khối dịch không có nội dung nguồn",
                retryable=False,
            )
        translated_text = _normalize_text(self.translated_text)
        if any(value < 0 for value in self.source_ordinals):
            raise TranslationError(
                "translation_ordinal_invalid",
                "Chỉ số segment nguồn không hợp lệ",
                retryable=False,
            )
        object.__setattr__(self, "source_text", source_text)
        object.__setattr__(self, "translated_text", translated_text)


TranslationProgress = Callable[[int, int], None]
TokenCounter = Callable[[str], int]


@runtime_checkable
class Translator(Protocol):
    """A loaded local model that can translate a sequence of text blocks."""

    def count_tokens(self, text: str) -> int: ...

    def translate_batch(
        self,
        texts: Sequence[str],
        *,
        source_language: str,
        target_language: str = "vi",
        on_progress: TranslationProgress | None = None,
    ) -> list[str]: ...

    def close(self) -> None: ...


def build_translation_blocks(
    segments: Iterable[Any],
    *,
    duration_us: int,
    token_counter: TokenCounter,
    max_duration_us: int = 12_000_000,
    max_tokens: int = 128,
    mandatory_merge_us: int = 250_000,
    short_merge_us: int = 1_000_000,
    max_merge_gap_us: int = 250_000,
) -> tuple[TranslationBlock, ...]:
    """Clamp, de-overlap, split and merge source transcript segments.

    Long blocks are split near punctuation and receive proportional integer
    microsecond slots. Very short blocks are merged only when the resulting
    block still respects the duration and model-token limits.
    """

    if duration_us <= 0 or max_duration_us <= 0 or max_tokens <= 0:
        raise ValueError("Translation block limits must be positive")

    normalized: list[TranslationBlock] = []
    previous_end_us = 0
    for fallback_ordinal, segment in enumerate(segments):
        try:
            raw_start = int(segment.start_us)
            raw_end = int(segment.end_us)
            raw_text_value = getattr(segment, "text", None)
            if raw_text_value is None:
                raw_text_value = segment.source_text
            raw_text = str(raw_text_value)
            raw_ordinal = int(getattr(segment, "ordinal", fallback_ordinal))
        except (AttributeError, TypeError, ValueError) as exc:
            raise TranslationError(
                "translation_segment_invalid",
                "Segment nguồn cho bước dịch không hợp lệ",
                retryable=False,
            ) from exc

        start_us = max(previous_end_us, min(max(raw_start, 0), duration_us))
        end_us = min(max(raw_end, 0), duration_us)
        text = _normalize_text(raw_text)
        if not text:
            continue
        if end_us <= start_us:
            if normalized:
                previous = normalized[-1]
                normalized[-1] = TranslationBlock(
                    previous.start_us,
                    previous.end_us,
                    f"{previous.source_text} {text}",
                    source_ordinals=(*previous.source_ordinals, raw_ordinal),
                )
                continue
            raise TranslationError(
                "translation_timeline_invalid",
                "Timestamp transcript không còn hợp lệ sau khi loại overlap",
                retryable=False,
            )
        block = TranslationBlock(
            start_us,
            end_us,
            text,
            source_ordinals=(raw_ordinal,),
        )
        normalized.extend(
            _split_to_limits(
                block,
                token_counter=token_counter,
                max_duration_us=max_duration_us,
                max_tokens=max_tokens,
            )
        )
        previous_end_us = end_us

    if not normalized:
        raise TranslationError(
            "translation_source_empty",
            "Transcript nguồn không có nội dung để dịch",
            retryable=False,
        )

    blocks = _merge_mandatory_short_blocks(
        normalized,
        token_counter=token_counter,
        max_duration_us=max_duration_us,
        max_tokens=max_tokens,
        mandatory_merge_us=mandatory_merge_us,
    )
    blocks = _merge_optional_short_blocks(
        blocks,
        token_counter=token_counter,
        max_duration_us=max_duration_us,
        max_tokens=max_tokens,
        short_merge_us=short_merge_us,
        max_merge_gap_us=max_merge_gap_us,
    )
    _validate_block_sequence(
        blocks,
        duration_us=duration_us,
        token_counter=token_counter,
        max_duration_us=max_duration_us,
        max_tokens=max_tokens,
    )
    return tuple(blocks)


def translate_blocks(
    translator: Translator,
    blocks: Sequence[TranslationBlock],
    *,
    source_language: str,
    target_language: str = "vi",
    on_progress: TranslationProgress | None = None,
) -> tuple[TranslationBlock, ...]:
    """Translate blocks and retry an empty result once after splitting it."""

    if not blocks:
        raise TranslationError(
            "translation_source_empty",
            "Không có khối nội dung để dịch",
            retryable=False,
        )
    outputs = translator.translate_batch(
        [block.source_text for block in blocks],
        source_language=source_language,
        target_language=target_language,
        on_progress=on_progress,
    )
    if len(outputs) != len(blocks):
        raise TranslationError(
            "translation_output_mismatch",
            "Số kết quả dịch không khớp số khối nguồn",
            retryable=True,
        )

    translated: list[TranslationBlock] = []
    for block, output in zip(blocks, outputs, strict=True):
        normalized_output = _normalize_text(output)
        if normalized_output:
            translated.append(_with_translation(block, normalized_output))
            continue

        retry_blocks = _split_for_empty_retry(block)
        if retry_blocks is None:
            raise TranslationError(
                "translation_output_empty",
                "Model dịch trả về nội dung rỗng",
                retryable=True,
            )
        retry_outputs = translator.translate_batch(
            [item.source_text for item in retry_blocks],
            source_language=source_language,
            target_language=target_language,
            on_progress=None,
        )
        if len(retry_outputs) != 2 or any(
            not _normalize_text(item) for item in retry_outputs
        ):
            raise TranslationError(
                "translation_output_empty",
                "Model dịch vẫn trả về nội dung rỗng sau khi chia khối",
                retryable=True,
            )
        translated.extend(
            _with_translation(item, _normalize_text(output_text))
            for item, output_text in zip(retry_blocks, retry_outputs, strict=True)
        )
    return tuple(translated)


def bypass_vietnamese_translation(
    blocks: Sequence[TranslationBlock],
) -> tuple[TranslationBlock, ...]:
    """Preserve Vietnamese source text without loading any MT model."""

    return tuple(_with_translation(block, block.source_text) for block in blocks)


def _normalize_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _SPACE_PATTERN.sub(" ", unicodedata.normalize("NFC", value)).strip()


def _token_count(token_counter: TokenCounter, text: str) -> int:
    count = token_counter(text)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise TranslationError(
            "translation_tokenizer_failed",
            "Không thể đếm token cho khối dịch",
            retryable=True,
        )
    return count


def _split_to_limits(
    block: TranslationBlock,
    *,
    token_counter: TokenCounter,
    max_duration_us: int,
    max_tokens: int,
) -> list[TranslationBlock]:
    duration = block.end_us - block.start_us
    if duration <= max_duration_us and _token_count(token_counter, block.source_text) <= max_tokens:
        return [block]
    split = _split_block_once(block)
    if split is None:
        raise TranslationError(
            "translation_block_too_long",
            "Không thể chia khối dịch để đáp ứng giới hạn model",
            retryable=False,
        )
    result: list[TranslationBlock] = []
    for child in split:
        result.extend(
            _split_to_limits(
                child,
                token_counter=token_counter,
                max_duration_us=max_duration_us,
                max_tokens=max_tokens,
            )
        )
    return result


def _split_block_once(
    block: TranslationBlock,
) -> tuple[TranslationBlock, TranslationBlock] | None:
    text = block.source_text
    if len(text) < 2 or block.end_us - block.start_us < 2:
        return None
    cut = _preferred_cut(text)
    left_text = _normalize_text(text[:cut])
    right_text = _normalize_text(text[cut:])
    if not left_text or not right_text:
        return None
    total_characters = len(left_text) + len(right_text)
    left_duration = round(
        (block.end_us - block.start_us) * len(left_text) / total_characters
    )
    split_us = min(
        block.end_us - 1,
        max(block.start_us + 1, block.start_us + left_duration),
    )
    return (
        TranslationBlock(
            block.start_us,
            split_us,
            left_text,
            source_ordinals=block.source_ordinals,
        ),
        TranslationBlock(
            split_us,
            block.end_us,
            right_text,
            source_ordinals=block.source_ordinals,
        ),
    )


def _preferred_cut(text: str) -> int:
    midpoint = len(text) / 2
    punctuation = [index + 1 for index, character in enumerate(text[:-1]) if character in _PUNCTUATION]
    whitespace = [index for index, character in enumerate(text[1:-1], 1) if character.isspace()]
    candidates = punctuation or whitespace
    if candidates:
        return min(candidates, key=lambda index: abs(index - midpoint))
    return max(1, min(len(text) - 1, math.floor(midpoint)))


def _merge_blocks(left: TranslationBlock, right: TranslationBlock) -> TranslationBlock:
    return TranslationBlock(
        left.start_us,
        right.end_us,
        f"{left.source_text} {right.source_text}",
        source_ordinals=left.source_ordinals + right.source_ordinals,
    )


def _can_merge(
    left: TranslationBlock,
    right: TranslationBlock,
    *,
    token_counter: TokenCounter,
    max_duration_us: int,
    max_tokens: int,
) -> bool:
    if right.start_us < left.end_us:
        return False
    merged_text = f"{left.source_text} {right.source_text}"
    return (
        right.end_us - left.start_us <= max_duration_us
        and _token_count(token_counter, merged_text) <= max_tokens
    )


def _merge_mandatory_short_blocks(
    source: list[TranslationBlock],
    *,
    token_counter: TokenCounter,
    max_duration_us: int,
    max_tokens: int,
    mandatory_merge_us: int,
) -> list[TranslationBlock]:
    blocks = list(source)
    index = 0
    while index < len(blocks):
        current = blocks[index]
        if current.end_us - current.start_us >= mandatory_merge_us:
            index += 1
            continue
        candidates: list[tuple[int, str]] = []
        if index > 0 and _can_merge(
            blocks[index - 1], current,
            token_counter=token_counter,
            max_duration_us=max_duration_us,
            max_tokens=max_tokens,
        ):
            candidates.append((current.start_us - blocks[index - 1].end_us, "left"))
        if index + 1 < len(blocks) and _can_merge(
            current,
            blocks[index + 1],
            token_counter=token_counter,
            max_duration_us=max_duration_us,
            max_tokens=max_tokens,
        ):
            candidates.append((blocks[index + 1].start_us - current.end_us, "right"))
        if not candidates:
            raise TranslationError(
                "translation_short_block_unmergeable",
                "Không thể ghép segment quá ngắn vào khối lân cận",
                retryable=False,
            )
        side = min(candidates, key=lambda item: item[0])[1]
        if side == "left":
            blocks[index - 1 : index + 1] = [
                _merge_blocks(blocks[index - 1], current)
            ]
            index = max(index - 1, 0)
        else:
            blocks[index : index + 2] = [
                _merge_blocks(current, blocks[index + 1])
            ]
    return blocks


def _merge_optional_short_blocks(
    source: list[TranslationBlock],
    *,
    token_counter: TokenCounter,
    max_duration_us: int,
    max_tokens: int,
    short_merge_us: int,
    max_merge_gap_us: int,
) -> list[TranslationBlock]:
    blocks = list(source)
    index = 0
    while index < len(blocks):
        current = blocks[index]
        if current.end_us - current.start_us >= short_merge_us:
            index += 1
            continue
        merged = False
        if index + 1 < len(blocks):
            right = blocks[index + 1]
            if (
                right.start_us - current.end_us <= max_merge_gap_us
                and _can_merge(
                    current,
                    right,
                    token_counter=token_counter,
                    max_duration_us=max_duration_us,
                    max_tokens=max_tokens,
                )
            ):
                blocks[index : index + 2] = [_merge_blocks(current, right)]
                merged = True
        if not merged and index > 0:
            left = blocks[index - 1]
            if (
                current.start_us - left.end_us <= max_merge_gap_us
                and _can_merge(
                    left,
                    current,
                    token_counter=token_counter,
                    max_duration_us=max_duration_us,
                    max_tokens=max_tokens,
                )
            ):
                blocks[index - 1 : index + 1] = [_merge_blocks(left, current)]
                index = max(index - 1, 0)
                merged = True
        if not merged:
            index += 1
    return blocks


def _validate_block_sequence(
    blocks: Sequence[TranslationBlock],
    *,
    duration_us: int,
    token_counter: TokenCounter,
    max_duration_us: int,
    max_tokens: int,
) -> None:
    previous_end_us = 0
    for block in blocks:
        if block.start_us < previous_end_us or block.end_us > duration_us:
            raise TranslationError(
                "translation_timeline_invalid",
                "Timeline khối dịch bị overlap hoặc vượt thời lượng media",
                retryable=False,
            )
        if block.end_us - block.start_us > max_duration_us:
            raise TranslationError(
                "translation_block_too_long",
                "Khối dịch vượt giới hạn thời lượng",
                retryable=False,
            )
        if _token_count(token_counter, block.source_text) > max_tokens:
            raise TranslationError(
                "translation_block_too_long",
                "Khối dịch vượt giới hạn token",
                retryable=False,
            )
        previous_end_us = block.end_us


def _split_for_empty_retry(
    block: TranslationBlock,
) -> tuple[TranslationBlock, TranslationBlock] | None:
    return _split_block_once(block)


def _with_translation(block: TranslationBlock, text: str) -> TranslationBlock:
    normalized = _normalize_text(text)
    if not normalized:
        raise TranslationError(
            "translation_output_empty",
            "Model dịch trả về nội dung rỗng",
            retryable=True,
        )
    return TranslationBlock(
        block.start_us,
        block.end_us,
        block.source_text,
        normalized,
        block.source_ordinals,
    )


__all__ = [
    "TokenCounter",
    "TranslationBlock",
    "TranslationError",
    "TranslationProgress",
    "Translator",
    "build_translation_blocks",
    "bypass_vietnamese_translation",
    "translate_blocks",
]
