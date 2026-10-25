"""Automatic speaker diarization for the transcription stage.

This module answers one question: *which character speaks in each
transcript segment?* It does not separate overlapping voices into
separate audio tracks; it labels time ranges. A segment whose audio is
mostly covered by one diarization turn gets that turn's speaker, and a
segment nobody clearly owns keeps ``speaker=None``.

Two parts are deliberately independent:

- :func:`assign_speakers` is pure timeline math over transcript
  segments and diarization turns. It works with any diarizer backend
  and is the part unit-tested without a GPU or a model.
- :class:`PyannoteDiarizer` is the one shipped backend. It loads a
  pyannote pipeline **from local files only** (a directory the operator
  downloaded beforehand), runs it on the decoded 16 kHz PCM the ASR
  stage already produces, and releases the pipeline before returning,
  so the ASR model that loads next never shares VRAM with it.

Labels from pyannote look like ``SPEAKER_00``. They are anonymous:
mapping them to character names (Anna, Bob, ...) is a separate,
manual step, or the job of the subtitle labels this pipeline also
preserves. The narration stage can already give each anonymous label
its own Piper voice automatically (see ``auto_assign_piper_voices`` in
``speaker_voices.py``).
"""

from __future__ import annotations

import gc
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from dub_server.domain import TranscriptSegment, normalize_speaker_label

# A transcript segment needs at least this fraction of its duration
# covered by one speaker's turn to take that speaker. Below it, the
# segment is ambiguous (a cut, a crosstalk fragment) and stays unlabeled
# rather than being dubbed in the wrong character's voice.
DEFAULT_MIN_OVERLAP_FRACTION = 0.5
# Turns of one speaker separated by less than this are one speaking turn.
MERGE_GAP_US = 250_000


class DiarizationError(RuntimeError):
    """Typed failure safe to persist and expose through the Vietnamese API."""

    def __init__(self, code: str, message_vi: str, *, retryable: bool) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DiarizationTurn:
    """One continuous stretch of speech attributed to one speaker."""

    start_us: int
    end_us: int
    speaker: str

    def __post_init__(self) -> None:
        if self.start_us < 0 or self.end_us <= self.start_us:
            raise DiarizationError(
                "diarization_turn_invalid",
                "Khoảng thời gian diarization không hợp lệ",
                retryable=False,
            )
        speaker = normalize_speaker_label(self.speaker)
        if speaker is None:
            raise DiarizationError(
                "diarization_turn_invalid",
                "Turn diarization thiếu nhãn nhân vật",
                retryable=False,
            )
        object.__setattr__(self, "speaker", speaker)


class SpeakerDiarizer(Protocol):
    """Run diarization on a decoded PCM WAV and return sorted turns."""

    def diarize(
        self, audio_path: Path, *, duration_us: int
    ) -> tuple[DiarizationTurn, ...]: ...


def merge_turns(
    turns: Any, *, gap_us: int = MERGE_GAP_US
) -> tuple[DiarizationTurn, ...]:
    """Sort turns and merge same-speaker turns separated by a short gap."""

    ordered = sorted(turns, key=lambda turn: (turn.start_us, turn.end_us))
    merged: list[DiarizationTurn] = []
    for turn in ordered:
        if (
            merged
            and merged[-1].speaker == turn.speaker
            and turn.start_us - merged[-1].end_us <= gap_us
            and turn.start_us >= merged[-1].start_us
        ):
            merged[-1] = DiarizationTurn(
                merged[-1].start_us,
                max(merged[-1].end_us, turn.end_us),
                turn.speaker,
            )
            continue
        merged.append(turn)
    return tuple(merged)


def assign_speakers(
    segments: Any,
    turns: Any,
    *,
    min_overlap_fraction: float = DEFAULT_MIN_OVERLAP_FRACTION,
    only_unlabeled: bool = True,
) -> tuple[TranscriptSegment, ...]:
    """Label transcript segments from diarization turns by overlap.

    For each segment, the speaker whose turn overlaps it the most wins,
    but only when that overlap covers at least ``min_overlap_fraction``
    of the segment. Segments that already carry a speaker keep it when
    ``only_unlabeled`` is set, so manual subtitle labels always win
    over diarization. A segment straddling two speakers takes the
    dominant one; splitting such a segment is a subtitle-timing concern
    and is intentionally not done here.
    """

    if not 0.0 < min_overlap_fraction <= 1.0:
        raise DiarizationError(
            "diarization_threshold_invalid",
            "Ngưỡng overlap diarization phải nằm trong (0, 1]",
            retryable=False,
        )
    merged = merge_turns(turns)
    labeled: list[TranscriptSegment] = []
    for segment in segments:
        if only_unlabeled and getattr(segment, "speaker", None) is not None:
            labeled.append(segment)
            continue
        duration = segment.end_us - segment.start_us
        best_speaker: str | None = None
        best_overlap = 0
        for turn in merged:
            if turn.end_us <= segment.start_us or turn.start_us >= segment.end_us:
                continue
            overlap = min(turn.end_us, segment.end_us) - max(
                turn.start_us, segment.start_us
            )
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = turn.speaker
        if best_speaker is not None and best_overlap >= min_overlap_fraction * duration:
            labeled.append(replace(segment, speaker=best_speaker))
        else:
            labeled.append(segment)
    return tuple(labeled)


class PyannoteDiarizer:
    """Diarize with a locally stored pyannote pipeline.

    ``pipeline_path`` is a directory holding a previously downloaded
    pipeline (``config.yaml`` plus its model files), or a Hugging Face
    pipeline id when the operator explicitly wants a hub download. The
    pipeline is loaded, run, and released inside :meth:`diarize`, so no
    GPU memory is held after the call returns. pyannote-audio and torch
    are optional dependencies: importing this module never requires
    them, and a missing install fails here with a plain message instead
    of an import-time crash of the whole service.
    """

    def __init__(
        self,
        pipeline_path: Path | str,
        *,
        device: str = "cuda",
        num_speakers: int | None = None,
    ) -> None:
        raw = str(pipeline_path).strip()
        if not raw or "\x00" in raw:
            raise DiarizationError(
                "diarization_pipeline_invalid",
                "Đường dẫn pipeline diarization không hợp lệ",
                retryable=False,
            )
        candidate = Path(raw)
        if candidate.is_dir():
            self._pipeline_ref = str(candidate)
        else:
            # Not a local directory: treated as a hub id, which downloads
            # on first use and needs a token for gated pipelines.
            self._pipeline_ref = raw
        if device not in {"cuda", "cpu"}:
            raise DiarizationError(
                "diarization_device_invalid",
                "Thiết bị diarization phải là cuda hoặc cpu",
                retryable=False,
            )
        if num_speakers is not None and num_speakers <= 0:
            raise DiarizationError(
                "diarization_speakers_invalid",
                "Số lượng nhân vật diarization phải lớn hơn 0",
                retryable=False,
            )
        self._device = device
        self._num_speakers = num_speakers

    def diarize(
        self, audio_path: Path, *, duration_us: int
    ) -> tuple[DiarizationTurn, ...]:
        audio = Path(audio_path)
        if not audio.is_file():
            raise DiarizationError(
                "diarization_audio_missing",
                "Không tìm thấy file âm thanh để diarization",
                retryable=False,
            )
        try:
            from pyannote.audio import Pipeline
        except ImportError as error:
            raise DiarizationError(
                "diarization_backend_missing",
                "Chưa cài pyannote-audio; xem docs/auto-speaker-diarization.md",
                retryable=False,
            ) from error
        pipeline: Any = None
        try:
            pipeline = Pipeline.from_pretrained(self._pipeline_ref)
            pipeline.to(self._device)
            call_kwargs: dict[str, Any] = {}
            if self._num_speakers is not None:
                call_kwargs["num_speakers"] = self._num_speakers
            annotation = pipeline(str(audio), **call_kwargs)
            turns: list[DiarizationTurn] = []
            for segment, _track, label in annotation.itertracks(yield_label=True):
                start_us = max(0, round(segment.start * 1_000_000))
                end_us = min(duration_us, round(segment.end * 1_000_000))
                if end_us <= start_us:
                    continue
                turns.append(
                    DiarizationTurn(start_us, end_us, normalize_speaker_label(label) or label)
                )
            return merge_turns(turns)
        except DiarizationError:
            raise
        except Exception as error:
            raise DiarizationError(
                "diarization_failed",
                f"Diarization thất bại: {error}",
                retryable=False,
            ) from error
        finally:
            # Release the pipeline before the caller loads the next GPU
            # model (ASR). Holding it would stack two models in VRAM.
            if pipeline is not None:
                try:
                    pipeline.to("cpu")
                except Exception:
                    pass
                del pipeline
            gc.collect()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
