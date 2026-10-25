"""Per-character voice selection for the narration stage.

A job's speaker-voice map lives in the job spec under ``speaker_voices``::

    "speaker_voices": {
        "Anna": {"voice_id": "3", "reference_path": null},
        "Bob": {"voice_id": null, "reference_path": "/workspace/voices/bob.wav"},
    }

Keys are character labels, normalized exactly like the labels parsed from
subtitle files (see :func:`dub_server.domain.normalize_speaker_label`), so a
label parsed as ``"Anna"`` matches a map key written as ``"  Anna "``.
Lookup is case-insensitive as a second chance: ``"ANNA"`` still finds the
``"Anna"`` entry instead of silently falling back to the default voice.

The pool in this module is the 6 GB VRAM safeguard: at most one
synthesizer process is open at a time. When the narration loop moves to a
block whose voice differs from the previous block's, the old synthesizer
is closed before the new one is built, so two GPU TTS runtimes never
coexist. Blocks are synthesized in timeline order, so a film that
alternates rapidly between characters pays a reload per switch; that
cost is deliberate, and it is why the docs recommend Piper (cheap
process start) over VieNeu when a cast alternates line by line.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from dub_server.domain import normalize_speaker_label


@dataclass(frozen=True, slots=True)
class SpeakerVoice:
    """One character's voice selection, mirroring ``VoiceSelection``."""

    voice_id: str | None = None
    reference_path: str | None = None

    @property
    def piper_speaker_id(self) -> int | None:
        """The Piper ``--speaker`` id, when ``voice_id`` is a plain integer."""

        if self.voice_id is None:
            return None
        text = self.voice_id.strip()
        if not text or (not text.isdigit() and not (text[:1] == "-" and text[1:].isdigit())):
            return None
        try:
            value = int(text)
        except ValueError:
            return None
        return value if value >= 0 else None

    def as_spec(self) -> dict[str, str | None]:
        """Canonical JSON form, used for checkpoint fingerprints."""

        return {"voice_id": self.voice_id, "reference_path": self.reference_path}


class SpeakerVoiceMap:
    """A validated speaker-voice map with case-insensitive lookup."""

    def __init__(self, voices: Mapping[str, SpeakerVoice]) -> None:
        self._voices = dict(voices)
        self._folded = {key.casefold(): key for key in voices}

    def __len__(self) -> int:
        return len(self._voices)

    def __bool__(self) -> bool:
        return bool(self._voices)

    def entries(self) -> dict[str, SpeakerVoice]:
        """A copy of the map, for building derived maps."""

        return dict(self._voices)

    def for_speaker(self, speaker: str | None) -> SpeakerVoice | None:
        """Return the voice for a label, or ``None`` for the default voice."""

        if speaker is None:
            return None
        normalized = normalize_speaker_label(speaker)
        if normalized is None:
            return None
        key = self._voices.get(normalized)
        if key is not None:
            return key
        folded = self._folded.get(normalized.casefold())
        if folded is None:
            return None
        return self._voices[folded]

    def as_spec(self) -> dict[str, dict[str, str | None]]:
        """Canonical JSON form, sorted by label for stable comparison."""

        return {key: voice.as_spec() for key, voice in sorted(self._voices.items())}


def parse_speaker_voices(spec: Mapping[str, Any] | None) -> SpeakerVoiceMap:
    """Parse and validate the ``speaker_voices`` entry of a job spec.

    Anything malformed raises ``ValueError`` with a plain message; the
    narration stage treats that as a job failure rather than silently
    dubbing every character in the default voice.
    """

    if spec is None:
        return SpeakerVoiceMap({})
    raw = spec.get("speaker_voices")
    if raw is None:
        return SpeakerVoiceMap({})
    if not isinstance(raw, Mapping):
        raise ValueError("speaker_voices must be a mapping of speaker to voice")
    voices: dict[str, SpeakerVoice] = {}
    for label, value in raw.items():
        speaker = normalize_speaker_label(label)
        if speaker is None:
            raise ValueError("speaker_voices has an empty speaker label")
        if not isinstance(value, Mapping):
            raise ValueError(f"speaker_voices[{label!r}] must be a mapping")
        voice_id = value.get("voice_id")
        reference_path = value.get("reference_path")
        if voice_id is not None and not isinstance(voice_id, str):
            raise ValueError(f"speaker_voices[{label!r}].voice_id must be text")
        if reference_path is not None and not isinstance(reference_path, str):
            raise ValueError(
                f"speaker_voices[{label!r}].reference_path must be text"
            )
        voice_id = voice_id.strip() if isinstance(voice_id, str) else None
        reference_path = (
            reference_path.strip() if isinstance(reference_path, str) else None
        )
        if voice_id == "":
            voice_id = None
        if reference_path == "":
            reference_path = None
        if len(voice_id or "") > 200 or len(reference_path or "") > 1000:
            raise ValueError(f"speaker_voices[{label!r}] value is too long")
        voices[speaker] = SpeakerVoice(
            voice_id=voice_id, reference_path=reference_path
        )
    return SpeakerVoiceMap(voices)


def auto_assign_piper_voices(
    speakers_in_order: Any, existing: SpeakerVoiceMap
) -> SpeakerVoiceMap:
    """Fill missing voices with Piper speaker ids, in first-appearance order.

    A diarized label like ``SPEAKER_01`` keeps its number when that id
    is still free; any other label takes the lowest free id. Labels
    that already have an explicit entry are never overridden, and ids
    already used by explicit entries are never reused. The result is
    only meaningful for a Piper backend: the narration stage applies
    it solely when the TTS model is Piper.
    """

    assigned = existing.entries()
    used: set[int] = {
        voice.piper_speaker_id
        for voice in assigned.values()
        if voice.piper_speaker_id is not None
    }
    assigned_folded = {key.casefold() for key in assigned}
    next_id = 0
    for raw_speaker in speakers_in_order:
        speaker = normalize_speaker_label(raw_speaker)
        if speaker is None or speaker.casefold() in assigned_folded:
            continue
        match = re.fullmatch(r"speaker_(\d+)", speaker.casefold())
        candidate = int(match.group(1)) if match else None
        if candidate is None or candidate in used:
            while next_id in used:
                next_id += 1
            candidate = next_id
            next_id += 1
        used.add(candidate)
        assigned[speaker] = SpeakerVoice(voice_id=str(candidate))
        assigned_folded.add(speaker.casefold())
    return SpeakerVoiceMap(assigned)


class NarrationSynthesizerLike(Protocol):
    """Structural subset of ``NarrationSynthesizer`` the pool needs."""

    async def close(self) -> None: ...


SynthesizerBuilder = Callable[[SpeakerVoice | None], NarrationSynthesizerLike]


class SpeakerSynthesizerPool:
    """Build synthesizers per voice, keeping at most one open.

    The builder receives the requested voice (or ``None`` for the job's
    default voice) and returns a synthesizer. The pool closes the live
    synthesizer before building a different one, and :meth:`close` shuts
    down whatever is still open. ``get`` is idempotent for the current
    voice, so consecutive blocks from one character reuse one process.
    """

    def __init__(self, builder: SynthesizerBuilder) -> None:
        self._builder = builder
        self._current_key: SpeakerVoice | None | object = _UNSET
        self._current: NarrationSynthesizerLike | None = None

    async def get(
        self, voice: SpeakerVoice | None
    ) -> NarrationSynthesizerLike:
        if self._current is not None and voice == self._current_key:
            return self._current
        await self.close()
        synthesizer = self._builder(voice)
        self._current = synthesizer
        self._current_key = voice
        return synthesizer

    async def close(self) -> None:
        if self._current is not None:
            current, self._current, self._current_key = (
                self._current,
                None,
                _UNSET,
            )
            await current.close()


class _Unset:
    def __repr__(self) -> str:
        return "<unset>"


_UNSET: Any = _Unset()
