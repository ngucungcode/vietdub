"""AI listen-back: verify the dubbed lines by re-transcribing them.

After Phase 4 builds the narration timeline, this module answers the
director's question — *did the AI actually say what the script says?*
— by running ASR over the clean narration timeline (no music, no
effects) and comparing what was heard against each fitted line.

What it catches:

- lines the TTS skipped, swallowed, or hallucinated into other words
  (high word error rate against the script),
- lines that came out silent (nothing recognized in the slot).

What it deliberately does not claim:

- acting quality, emotion, or naturalness — a line can be perfectly
  intelligible and still sound flat;
- fine tone distinctions: ASR compares words, and two words that differ
  only by tone mark but transcribe identically are invisible here;
- speaker identity: the timeline is a single mixed track, so a line
  read in the wrong character's voice is not detectable from audio
  alone. Voice mix-ups are still caught by the studio's per-line
  voice records, not by this report.

Thresholds are intentionally forgiving: Vietnamese TTS plus ASR on
clean audio should be near-verbatim, so anything above 20% word error
already deserves a human ear, and above 50% is almost certainly a
real defect rather than an ASR quirk.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Sequence

# Word error rate at or below this is a pass. Above it up to
# REVIEW_WER the line is flagged for a quick listen; above that it is
# flagged as defective.
OK_WER = 0.0
REVIEW_WER = 0.2
BAD_WER = 0.5

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_qc_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Diacritics are kept: in Vietnamese they change the word, so
    stripping them would hide exactly the errors QC exists to catch.
    """

    lowered = unicodedata.normalize("NFC", text).casefold()
    cleaned = _PUNCTUATION.sub(" ", lowered)
    return " ".join(cleaned.split())


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Word-level Levenshtein distance over the normalized texts."""

    ref_words = normalize_qc_text(reference).split()
    hyp_words = normalize_qc_text(hypothesis).split()
    if not ref_words:
        return 0.0 if not hyp_words else 1.0
    previous = list(range(len(hyp_words) + 1))
    for i, ref_word in enumerate(ref_words, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp_words, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (0 if ref_word == hyp_word else 1),
                )
            )
        previous = current
    return previous[len(hyp_words)] / len(ref_words)


def qc_status_for(wer: float, *, heard_empty: bool) -> str:
    """Map a line's score to a studio flag."""

    if heard_empty:
        return "silent"
    if wer <= OK_WER:
        return "ok"
    if wer <= REVIEW_WER:
        return "minor"
    if wer <= BAD_WER:
        return "review"
    return "bad"


def align_heard_text(
    asr_segments: Sequence[Any],
    *,
    start_us: int,
    end_us: int,
) -> str:
    """Join the ASR text overlapping one fitted line's time slot.

    A segment counts when it overlaps the slot at all; ASR boundaries
    rarely match TTS slots exactly, so a majority-overlap rule would
    drop edge words. Segments are joined in time order.
    """

    heard: list[str] = []
    for segment in asr_segments:
        seg_start = int(getattr(segment, "start_us", 0))
        seg_end = int(getattr(segment, "end_us", 0))
        if seg_end <= start_us or seg_start >= end_us:
            continue
        text = str(getattr(segment, "text", "") or "").strip()
        if text:
            heard.append(text)
    return " ".join(heard)


def build_qc_report(
    fitted_blocks: Sequence[Any],
    asr_segments: Sequence[Any],
) -> dict[str, Any]:
    """Score every fitted line against what ASR heard in its slot."""

    lines: list[dict[str, Any]] = []
    counts = {"ok": 0, "minor": 0, "review": 0, "bad": 0, "silent": 0}
    wer_total = 0.0
    for ordinal, block in enumerate(fitted_blocks):
        reference = str(getattr(block, "text", "") or "")
        heard = align_heard_text(
            asr_segments,
            start_us=int(getattr(block, "start_us", 0)),
            end_us=int(getattr(block, "end_us", 0)),
        )
        wer = word_error_rate(reference, heard)
        status = qc_status_for(wer, heard_empty=not heard.strip())
        counts[status] += 1
        wer_total += wer
        lines.append(
            {
                "ordinal": ordinal,
                "start_us": int(getattr(block, "start_us", 0)),
                "end_us": int(getattr(block, "end_us", 0)),
                "reference": reference,
                "heard": heard,
                "wer": round(wer, 4),
                "status": status,
            }
        )
    total = len(lines)
    return {
        "schema_version": 1,
        "generated_from": "narration_timeline",
        "lines": lines,
        "summary": {
            "total": total,
            "ok": counts["ok"],
            "minor": counts["minor"],
            "review": counts["review"],
            "bad": counts["bad"],
            "silent": counts["silent"],
            "needs_attention": counts["review"] + counts["bad"] + counts["silent"],
            "mean_wer": round(wer_total / total, 4) if total else 0.0,
        },
    }
