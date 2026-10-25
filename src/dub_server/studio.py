"""Dubbing-studio helpers: cast, script, retakes, and mix.

A finished pipeline run is not the end of a dub: a director renames
characters, revoices a cast member, rewrites a line, re-records one
line, or rebalances the mix, then re-runs only what changed. This
module holds the pure, testable half of that workflow:

- which jobs may be edited (completed, or translated but not yet
  narrated — never a job the worker is currently moving),
- validation for line overrides, display names, and mix settings,
- read views (cast roster, script) built from a translation artifact
  plus the job spec and the TTS checkpoint.

The state surgery itself (spec updates, checkpoint invalidation,
requeueing) lives in the API handlers, because it needs the store.
One deliberate asymmetry is documented here so it is not mistaken for
a bug: changing a character's *voice* rebuilds every narration block
(the TTS checkpoint fingerprint covers the whole voice map), while
editing or re-recording individual *lines* rebuilds only those lines
(block validity is per ordinal, keyed on text and file hash).
"""

from __future__ import annotations

from typing import Any, Mapping

from dub_server.domain import normalize_speaker_label
from dub_server.state import JobStatus

# Studio edits are only safe when the worker is not mid-pipeline.
# COMPLETED: the whole run finished, so edits requeue Phase 4.
# READY_TTS: translation is done but narration has not started, so
# edits simply land in the spec before the first narration run.
STUDIO_EDITABLE_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.READY_TTS}
)

MAX_LINE_TEXT_CHARS = 2000

# (min, max) for every mix field the studio exposes. These mirror the
# MixSettings bounds in audio_mix_export.py; the API validates here so
# a bad value fails fast instead of failing a GPU run.
MIX_FIELD_BOUNDS: dict[str, tuple[float, float]] = {
    "narration_lufs": (-70.0, -5.0),
    "accompaniment_lufs": (-70.0, -5.0),
    "true_peak_db": (-9.0, 0.0),
    "ducking_threshold": (0.000001, 1.0),
    "ducking_ratio": (1.0, 20.0),
    "ducking_attack_ms": (0.0, 2000.0),
    "ducking_release_ms": (1.0, 9000.0),
}


class StudioError(ValueError):
    """Invalid studio input; the API maps this to a 400 response."""

    def __init__(self, code: str, message_vi: str) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi


def parse_line_overrides(
    raw: Any, *, block_count: int
) -> dict[int, str]:
    """Validate a spec ``studio_line_overrides`` mapping.

    Keys are ordinals (as ints or decimal strings); values are the
    replacement Vietnamese lines. Empty or whitespace-only lines are
    rejected: deleting a line is not a studio edit, it is a script cut
    that belongs in the subtitle file, not in a dub override.
    """

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise StudioError(
            "studio_overrides_invalid",
            "studio_line_overrides phải là một object",
        )
    overrides: dict[int, str] = {}
    for key, value in raw.items():
        if isinstance(key, bool):
            raise StudioError(
                "studio_overrides_invalid",
                "Số thứ tự câu thoại không hợp lệ",
            )
        if isinstance(key, int):
            ordinal = key
        elif isinstance(key, str) and key.strip().isdigit():
            ordinal = int(key.strip())
        else:
            raise StudioError(
                "studio_overrides_invalid",
                f"Số thứ tự câu thoại không hợp lệ: {key!r}",
            )
        if not 0 <= ordinal < block_count:
            raise StudioError(
                "studio_overrides_invalid",
                f"Câu thoại {ordinal} nằm ngoài kịch bản ({block_count} câu)",
            )
        text = _clean_line_text(value)
        overrides[ordinal] = text
    return overrides


def _clean_line_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StudioError(
            "studio_line_invalid",
            "Câu thoại sửa phải là chuỗi không rỗng",
        )
    text = " ".join(value.split())
    if len(text) > MAX_LINE_TEXT_CHARS:
        raise StudioError(
            "studio_line_invalid",
            f"Câu thoại sửa vượt quá {MAX_LINE_TEXT_CHARS} ký tự",
        )
    return text


def parse_speaker_names(raw: Any) -> dict[str, str]:
    """Validate a spec ``speaker_names`` mapping (raw label → display name)."""

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise StudioError(
            "studio_speaker_names_invalid",
            "speaker_names phải là một object",
        )
    names: dict[str, str] = {}
    for label, display in raw.items():
        normalized_label = normalize_speaker_label(label)
        if normalized_label is None:
            raise StudioError(
                "studio_speaker_names_invalid",
                "Nhãn nhân vật không hợp lệ",
            )
        cleaned = normalize_speaker_label(display)
        if cleaned is None:
            raise StudioError(
                "studio_speaker_names_invalid",
                "Tên hiển thị nhân vật không được để trống",
            )
        names[normalized_label] = cleaned
    return names


def parse_studio_mix(raw: Any) -> dict[str, float]:
    """Validate a ``studio_mix`` mapping; unknown fields are rejected."""

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise StudioError(
            "studio_mix_invalid",
            "studio_mix phải là một object",
        )
    mix: dict[str, float] = {}
    for key, value in raw.items():
        if key not in MIX_FIELD_BOUNDS:
            raise StudioError(
                "studio_mix_invalid",
                f"Thông số mix không hỗ trợ: {key}",
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StudioError(
                "studio_mix_invalid",
                f"Thông số mix {key} phải là số",
            )
        low, high = MIX_FIELD_BOUNDS[key]
        if not low <= float(value) <= high:
            raise StudioError(
                "studio_mix_invalid",
                f"Thông số mix {key} phải nằm trong [{low}, {high}]",
            )
        mix[str(key)] = float(value)
    return mix


def build_cast_view(
    segments: Any,
    spec: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """One roster row per distinct speaker, in first-appearance order."""

    names = parse_speaker_names(spec.get("speaker_names"))
    voices = spec.get("speaker_voices")
    voice_map: dict[str, dict[str, Any]] = {}
    if isinstance(voices, Mapping):
        for label, entry in voices.items():
            normalized = normalize_speaker_label(label)
            if normalized is not None and isinstance(entry, Mapping):
                voice_map[normalized] = entry
    rows: dict[str, dict[str, Any]] = {}
    for segment in segments:
        speaker = getattr(segment, "speaker", None)
        if not isinstance(speaker, str) or not speaker:
            continue
        label = normalize_speaker_label(speaker) or speaker
        row = rows.get(label)
        if row is None:
            entry = voice_map.get(label, {})
            row = {
                "speaker": label,
                "display_name": names.get(label, label),
                "line_count": 0,
                "first_start_us": int(segment.start_us),
                "voice_id": entry.get("voice_id"),
                "reference_path": entry.get("reference_path"),
            }
            rows[label] = row
        row["line_count"] = int(row["line_count"]) + 1
    return list(rows.values())


def build_script_view(
    segments: Any,
    spec: Mapping[str, Any],
    tts_checkpoint: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """One row per translation segment, with overrides and synth state."""

    names = parse_speaker_names(spec.get("speaker_names"))
    overrides = parse_line_overrides(
        spec.get("studio_line_overrides"), block_count=len(segments)
    )
    voices = spec.get("speaker_voices")
    voice_map: dict[str, dict[str, Any]] = {}
    if isinstance(voices, Mapping):
        for label, entry in voices.items():
            normalized = normalize_speaker_label(label)
            if normalized is not None and isinstance(entry, Mapping):
                voice_map[normalized] = entry
    synthesized: set[int] = set()
    if isinstance(tts_checkpoint, Mapping):
        for item in tts_checkpoint.get("blocks", []):
            if isinstance(item, Mapping) and isinstance(item.get("ordinal"), int):
                synthesized.add(int(item["ordinal"]))
    lines: list[dict[str, Any]] = []
    for ordinal, segment in enumerate(segments):
        speaker = getattr(segment, "speaker", None)
        label = (
            normalize_speaker_label(speaker)
            if isinstance(speaker, str)
            else None
        )
        entry = voice_map.get(label, {}) if label is not None else {}
        lines.append(
            {
                "ordinal": ordinal,
                "start_us": int(segment.start_us),
                "end_us": int(segment.end_us),
                "speaker": label,
                "display_name": names.get(label, label) if label else None,
                "text": overrides.get(ordinal, segment.translated_text),
                "original_text": segment.translated_text,
                "voice_id": entry.get("voice_id"),
                "synthesized": ordinal in synthesized,
            }
        )
    return lines


def effective_studio_mix(
    spec: Mapping[str, Any],
    *,
    narration_lufs: float,
    accompaniment_lufs: float,
) -> dict[str, float]:
    """Merge spec mix overrides over the stage defaults."""

    mix: dict[str, float] = {
        "narration_lufs": narration_lufs,
        "accompaniment_lufs": accompaniment_lufs,
    }
    mix.update(parse_studio_mix(spec.get("studio_mix")))
    return mix
