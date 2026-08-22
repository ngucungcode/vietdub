"""Duration fitting and sample-exact 48 kHz narration timeline assembly."""

from __future__ import annotations

import asyncio
import math
import os
import time
import wave
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn, Protocol

from .narration import Cancellation

TIMELINE_SAMPLE_RATE = 48_000
TIMELINE_CHANNELS = 1
TIMELINE_SAMPLE_WIDTH_BYTES = 2
NATURAL_BORROW_WINDOW_US = 800_000
NATURAL_MAX_SILENT_BORROW_US = 2_000_000
NATURAL_SILENT_GAP_GUARD_US = 120_000
NATURAL_MAX_ELASTIC_CENTER_DRIFT_US = 2_000_000
NATURAL_MAX_TOTAL_SPEED = 1.20
NATURAL_MAX_ADJACENT_SPEED_DELTA = 0.08


class TimingError(RuntimeError):
    """A typed timing failure safe to persist in a job checkpoint."""

    def __init__(
        self,
        code: str,
        message_vi: str,
        *,
        retryable: bool,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable
        self.details = dict(details or {})


class TimingProfile(StrEnum):
    """How narration timestamps trade exact subtitle sync for natural speech."""

    NATURAL = "natural"
    STRICT = "strict"


class TimingQuality(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    SEVERE = "severe"


@dataclass(frozen=True, slots=True)
class NarrationTimingInput:
    """One translated block and the measured duration of its synthesized WAV."""

    start_us: int
    end_us: int
    source_duration_us: int
    native_speed: float = 1.0
    source_frame_count: int | None = None
    source_sample_rate: int | None = None

    def __post_init__(self) -> None:
        if self.start_us < 0 or self.end_us <= self.start_us:
            raise TimingError(
                "timing_slot_invalid",
                "Mốc thời gian của lời thuyết minh không hợp lệ",
                retryable=False,
            )
        if self.source_duration_us <= 0 or not math.isfinite(self.native_speed):
            raise TimingError(
                "timing_speed_invalid",
                "Thời lượng hoặc tốc độ lời thuyết minh không hợp lệ",
                retryable=False,
            )
        if not 0.5 <= self.native_speed <= 2.0:
            raise TimingError(
                "timing_speed_invalid",
                "Tốc độ TTS gốc không hợp lệ",
                retryable=False,
            )
        if (self.source_frame_count is None) != (self.source_sample_rate is None):
            raise TimingError(
                "timing_audio_invalid",
                "Metadata sample của âm thanh TTS không đầy đủ",
                retryable=False,
            )
        if self.source_frame_count is not None and (
            isinstance(self.source_frame_count, bool)
            or not isinstance(self.source_frame_count, int)
            or isinstance(self.source_sample_rate, bool)
            or not isinstance(self.source_sample_rate, int)
            or self.source_frame_count <= 0
            or self.source_sample_rate <= 0
        ):
            raise TimingError(
                "timing_audio_invalid",
                "Metadata sample của âm thanh TTS không hợp lệ",
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class PlannedNarrationSlot:
    """A non-overlapping slot selected before sample-exact FFmpeg fitting."""

    start_us: int
    end_us: int
    planned_total_speed: float

    def __post_init__(self) -> None:
        if self.start_us < 0 or self.end_us <= self.start_us:
            raise TimingError(
                "timing_slot_invalid",
                "Mốc thời gian đã lập kế hoạch không hợp lệ",
                retryable=False,
            )
        if self.planned_total_speed <= 0 or not math.isfinite(
            self.planned_total_speed
        ):
            raise TimingError(
                "timing_speed_invalid",
                "Tốc độ lời thuyết minh đã lập kế hoạch không hợp lệ",
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class FittedNarrationBlock:
    path: Path
    start_us: int
    end_us: int
    text: str
    source_duration_us: int
    target_frame_count: int
    output_frame_count: int
    native_speed: float
    atempo_speed: float
    total_speed: float
    padded_frame_count: int
    quality: TimingQuality

    def __post_init__(self) -> None:
        if self.start_us < 0 or self.end_us <= self.start_us:
            raise TimingError(
                "timing_slot_invalid",
                "Mốc thời gian của lời thuyết minh không hợp lệ",
                retryable=False,
            )
        if self.target_frame_count <= 0 or self.output_frame_count <= 0:
            raise TimingError(
                "timing_sample_count_invalid",
                "Số sample của lời thuyết minh không hợp lệ",
                retryable=False,
            )
        if self.output_frame_count != self.target_frame_count:
            raise TimingError(
                "timing_sample_count_mismatch",
                "Lời thuyết minh chưa khớp chính xác số sample của slot",
                retryable=False,
            )
        if self.source_duration_us <= 0 or self.native_speed <= 0 or self.atempo_speed <= 0:
            raise TimingError(
                "timing_speed_invalid",
                "Thông số tốc độ lời thuyết minh không hợp lệ",
                retryable=False,
            )
        if self.padded_frame_count < 0:
            raise TimingError(
                "timing_padding_invalid",
                "Số sample im lặng bù không hợp lệ",
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class NarrationTimeline:
    path: Path
    duration_us: int
    frame_count: int
    sample_rate: int
    channels: int
    sample_width_bytes: int
    block_count: int


TimingProgress = Callable[[int, int], None]


class ProcessHandle(Protocol):
    returncode: int | None

    async def communicate(self) -> tuple[bytes | None, bytes | None]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., Awaitable[ProcessHandle]]


async def _default_process_factory(*command: str, **kwargs: object) -> ProcessHandle:
    return await asyncio.create_subprocess_exec(*command, **kwargs)


@dataclass(frozen=True, slots=True)
class _NaturalWindow:
    ordinal: int
    lower_us: int
    upper_us: int
    preferred_center_us: int
    work_duration_us: int
    source_frame_count: int | None
    source_sample_rate: int | None
    native_speed_ppm: int


def plan_narration_slots(
    blocks: Sequence[NarrationTimingInput],
    *,
    duration_us: int,
    profile: TimingProfile | str = TimingProfile.NATURAL,
    borrow_window_us: int = NATURAL_BORROW_WINDOW_US,
    maximum_silent_borrow_us: int | None = None,
    silence_guard_us: int = NATURAL_SILENT_GAP_GUARD_US,
    maximum_total_speed: float = NATURAL_MAX_TOTAL_SPEED,
    maximum_adjacent_speed_delta: float = NATURAL_MAX_ADJACENT_SPEED_DELTA,
) -> tuple[PlannedNarrationSlot, ...]:
    """Plan deterministic narration slots before invoking FFmpeg.

    ``strict`` preserves every translated timestamp.  ``natural`` synthesizes at
    a stable native speed and places speech inside an expanded local window.
    Local rolling chains are compressed only when their combined speech would
    collide; neighbouring blocks are then raised (never slowed) just enough to
    avoid an audible speed step.  No block may exceed ``maximum_total_speed``.
    """

    try:
        selected_profile = (
            profile if isinstance(profile, TimingProfile) else TimingProfile(profile)
        )
    except (TypeError, ValueError) as exc:
        raise TimingError(
            "timing_profile_invalid",
            "Chế độ khớp thời lượng không hợp lệ",
            retryable=False,
        ) from exc
    if (
        isinstance(duration_us, bool)
        or not isinstance(duration_us, int)
        or duration_us <= 0
    ):
        raise TimingError(
            "timeline_duration_invalid",
            "Thời lượng video không hợp lệ để lập timeline thuyết minh",
            retryable=False,
        )
    if not blocks:
        raise TimingError(
            "timeline_empty",
            "Không có lời thuyết minh để lập timeline",
            retryable=False,
        )
    if borrow_window_us < 0:
        raise ValueError("borrow_window_us must not be negative")
    if maximum_silent_borrow_us is not None and (
        isinstance(maximum_silent_borrow_us, bool)
        or not isinstance(maximum_silent_borrow_us, int)
        or maximum_silent_borrow_us < borrow_window_us
    ):
        raise ValueError(
            "maximum_silent_borrow_us must be an integer greater than or equal "
            "to borrow_window_us"
        )
    if (
        isinstance(silence_guard_us, bool)
        or not isinstance(silence_guard_us, int)
        or silence_guard_us < 0
    ):
        raise ValueError("silence_guard_us must be a non-negative integer")
    if not 1.0 <= maximum_total_speed <= 2.0:
        raise ValueError("maximum_total_speed must be between 1.0 and 2.0")
    if not 0 <= maximum_adjacent_speed_delta <= maximum_total_speed - 1.0:
        raise ValueError(
            "maximum_adjacent_speed_delta must fit inside the natural speed range"
        )

    previous_end_us = 0
    for block in blocks:
        if not isinstance(block, NarrationTimingInput):
            raise TimingError(
                "timing_block_invalid",
                "Khối thuyết minh dùng để lập timeline không hợp lệ",
                retryable=False,
            )
        if block.start_us < previous_end_us or block.end_us > duration_us:
            raise TimingError(
                "timing_source_overlap",
                "Timestamp bản dịch bị overlap hoặc vượt thời lượng video",
                retryable=False,
            )
        previous_end_us = block.end_us

    if selected_profile is TimingProfile.STRICT:
        return tuple(
            PlannedNarrationSlot(
                start_us=block.start_us,
                end_us=block.end_us,
                planned_total_speed=(
                    block.source_duration_us
                    * block.native_speed
                    / (block.end_us - block.start_us)
                ),
            )
            for block in blocks
        )

    base_windows = tuple(
        _NaturalWindow(
            ordinal=ordinal,
            lower_us=max(0, block.start_us - borrow_window_us),
            upper_us=min(duration_us, block.end_us + borrow_window_us),
            preferred_center_us=(block.start_us + block.end_us) // 2,
            # Reconstruct duration at native TTS speed 1.0.  Keeping this as an
            # integer makes checkpoint/retry planning byte-for-byte stable.
            work_duration_us=max(
                1, round(block.source_duration_us * block.native_speed)
            ),
            source_frame_count=block.source_frame_count,
            source_sample_rate=block.source_sample_rate,
            native_speed_ppm=round(block.native_speed * 1_000_000),
        )
        for ordinal, block in enumerate(blocks)
    )
    try:
        return _plan_natural_windows(
            base_windows,
            borrow_window_us=borrow_window_us,
            maximum_total_speed=maximum_total_speed,
            maximum_adjacent_speed_delta=maximum_adjacent_speed_delta,
        )
    except TimingError as base_failure:
        if (
            base_failure.code != "timing_rewrite_required"
            or maximum_silent_borrow_us is None
            or maximum_silent_borrow_us == borrow_window_us
        ):
            raise

    elastic_windows = _elastic_natural_windows(
        blocks,
        base_windows,
        duration_us=duration_us,
        maximum_silent_borrow_us=maximum_silent_borrow_us,
        silence_guard_us=silence_guard_us,
    )
    planned = _plan_natural_windows(
        elastic_windows,
        borrow_window_us=borrow_window_us,
        maximum_total_speed=maximum_total_speed,
        maximum_adjacent_speed_delta=maximum_adjacent_speed_delta,
    )
    _postvalidate_elastic_schedule(
        base_windows,
        elastic_windows,
        planned,
        maximum_total_speed=maximum_total_speed,
    )
    return planned


def _plan_natural_windows(
    windows: Sequence[_NaturalWindow],
    *,
    borrow_window_us: int,
    maximum_total_speed: float,
    maximum_adjacent_speed_delta: float,
) -> tuple[PlannedNarrationSlot, ...]:
    """Run the legacy deterministic planner against prepared windows."""

    maximum_speed_ppm = round(maximum_total_speed * 1_000_000)
    speed_delta_ppm = round(maximum_adjacent_speed_delta * 1_000_000)

    # Check intrinsic local capacity in source order so an oversized block is
    # never misclassified as congestion caused by its predecessors.
    for window in windows:
        available_us = window.upper_us - window.lower_us
        if _duration_at_speed(window, maximum_speed_ppm) > available_us:
            _raise_rewrite_required(
                (window,),
                failure_index=0,
                available_us=available_us,
                maximum_speed_ppm=maximum_speed_ppm,
                maximum_total_speed=maximum_total_speed,
            )
    speeds = [
        _minimum_window_speed(window, maximum_speed_ppm) for window in windows
    ]

    # Expanded windows form independent components only when their possible
    # placement ranges no longer touch.  Splitting on an idle point observed
    # only at maximum speed is unsafe: a slower selected speed can consume that
    # gap again.  Each connected component needs at most one bounded binary
    # search, keeping planning O(n log speed_range).
    chains = _independent_natural_chains(windows)
    for chain_start, chain_end in chains:
        chain = windows[chain_start:chain_end]
        if _first_schedule_failure(
            chain, speeds[chain_start:chain_end]
        )[0] is None:
            continue
        failure_index, available_us = _first_schedule_failure(
            chain, [maximum_speed_ppm] * len(chain)
        )
        if failure_index is not None:
            _raise_rewrite_required(
                chain,
                failure_index=failure_index,
                available_us=available_us,
                maximum_speed_ppm=maximum_speed_ppm,
                maximum_total_speed=maximum_total_speed,
            )
        low = 1_000_000
        high = maximum_speed_ppm
        while low < high:
            candidate = (low + high) // 2
            candidate_speeds = [
                max(speeds[chain_start + offset], candidate)
                for offset in range(len(chain))
            ]
            if _first_schedule_failure(chain, candidate_speeds)[0] is None:
                high = candidate
            else:
                low = candidate + 1
        for index in range(chain_start, chain_end):
            speeds[index] = max(speeds[index], low)

    # Only raise a slower nearby neighbour.  Shortening an already feasible
    # schedule cannot introduce overlap, while a bounded slope removes sudden
    # speed jumps without accelerating speech separated by a long silent scene.
    for chain_start, chain_end in _nearby_natural_chains(
        windows, maximum_gap_us=borrow_window_us * 2
    ):
        for index in range(chain_start + 1, chain_end):
            speeds[index] = max(
                speeds[index], speeds[index - 1] - speed_delta_ppm
            )
        for index in range(chain_end - 2, chain_start - 1, -1):
            speeds[index] = max(
                speeds[index], speeds[index + 1] - speed_delta_ppm
            )

    failure_index, available_us = _first_schedule_failure(windows, speeds)
    if failure_index is not None:  # Defensive typed failure for persisted jobs.
        _raise_rewrite_required(
            windows,
            failure_index=failure_index,
            available_us=available_us,
            maximum_speed_ppm=maximum_speed_ppm,
            maximum_total_speed=maximum_total_speed,
        )
    scheduled = _centered_natural_schedule(windows, speeds)
    return tuple(
        PlannedNarrationSlot(
            start_us=slot_start_us,
            end_us=slot_end_us,
            planned_total_speed=(
                window.work_duration_us / (slot_end_us - slot_start_us)
            ),
        )
        for window, (slot_start_us, slot_end_us) in zip(
            windows, scheduled, strict=True
        )
    )


def _elastic_natural_windows(
    blocks: Sequence[NarrationTimingInput],
    base_windows: Sequence[_NaturalWindow],
    *,
    duration_us: int,
    maximum_silent_borrow_us: int,
    silence_guard_us: int,
) -> tuple[_NaturalWindow, ...]:
    """Extend only into source silence while preserving every base envelope."""

    elastic: list[_NaturalWindow] = []
    for index, (block, base) in enumerate(
        zip(blocks, base_windows, strict=True)
    ):
        lower_us = base.lower_us
        desired_lower_us = max(0, block.start_us - maximum_silent_borrow_us)
        if desired_lower_us < lower_us:
            silent_floor_us = (
                0
                if index == 0
                else blocks[index - 1].end_us + silence_guard_us
            )
            lower_us = min(lower_us, max(desired_lower_us, silent_floor_us))

        upper_us = base.upper_us
        desired_upper_us = min(
            duration_us, block.end_us + maximum_silent_borrow_us
        )
        if desired_upper_us > upper_us:
            silent_ceiling_us = (
                duration_us
                if index + 1 == len(blocks)
                else blocks[index + 1].start_us - silence_guard_us
            )
            upper_us = max(upper_us, min(desired_upper_us, silent_ceiling_us))

        elastic.append(
            _NaturalWindow(
                ordinal=base.ordinal,
                lower_us=lower_us,
                upper_us=upper_us,
                preferred_center_us=base.preferred_center_us,
                work_duration_us=base.work_duration_us,
                source_frame_count=base.source_frame_count,
                source_sample_rate=base.source_sample_rate,
                native_speed_ppm=base.native_speed_ppm,
            )
        )
    return tuple(elastic)


def _postvalidate_elastic_schedule(
    base_windows: Sequence[_NaturalWindow],
    elastic_windows: Sequence[_NaturalWindow],
    planned: Sequence[PlannedNarrationSlot],
    *,
    maximum_total_speed: float,
) -> None:
    """Reject elastic placements that lose scene locality after scheduling."""

    previous_end_us = 0
    maximum_speed_ppm = round(maximum_total_speed * 1_000_000)
    for index, (base, elastic, slot) in enumerate(
        zip(base_windows, elastic_windows, planned, strict=True)
    ):
        slot_center_us = (slot.start_us + slot.end_us) // 2
        intersects_base = (
            slot.end_us > base.lower_us and slot.start_us < base.upper_us
        )
        center_drift_us = abs(slot_center_us - base.preferred_center_us)
        valid_geometry = (
            elastic.lower_us <= slot.start_us < slot.end_us <= elastic.upper_us
            and slot.start_us >= previous_end_us
        )
        respects_speed_cap = (
            slot.end_us - slot.start_us
            >= _duration_at_speed(elastic, maximum_speed_ppm)
        )
        if (
            valid_geometry
            and intersects_base
            and center_drift_us <= NATURAL_MAX_ELASTIC_CENTER_DRIFT_US
            and respects_speed_cap
        ):
            previous_end_us = slot.end_us
            continue

        required_us = _duration_at_speed(elastic, maximum_speed_ppm)
        if not intersects_base:
            locality_deficit_us = (
                base.lower_us - slot.end_us + 1
                if slot.end_us <= base.lower_us
                else slot.start_us - base.upper_us + 1
            )
        else:
            drift_deficit_us = (
                center_drift_us - NATURAL_MAX_ELASTIC_CENTER_DRIFT_US
            )
            capacity_deficit_us = required_us - (
                slot.end_us - slot.start_us
            )
            geometry_deficit_us = max(
                elastic.lower_us - slot.start_us,
                slot.end_us - elastic.upper_us,
                previous_end_us - slot.start_us,
                0,
            )
            locality_deficit_us = max(
                1,
                drift_deficit_us,
                capacity_deficit_us,
                geometry_deficit_us,
            )
        available_us = max(0, required_us - locality_deficit_us)
        _raise_rewrite_required(
            elastic_windows,
            failure_index=index,
            available_us=available_us,
            maximum_speed_ppm=maximum_speed_ppm,
            maximum_total_speed=maximum_total_speed,
            failure_kind="elastic_postvalidation",
            schedule_deficit_us=locality_deficit_us,
        )


def _duration_at_speed(window: _NaturalWindow, speed_ppm: int) -> int:
    if window.source_frame_count is not None:
        if window.source_sample_rate is None:  # pragma: no cover - dataclass input guard
            raise AssertionError("sample rate is required with an exact frame count")
        frame_numerator = (
            window.source_frame_count
            * TIMELINE_SAMPLE_RATE
            * window.native_speed_ppm
        )
        frame_denominator = window.source_sample_rate * speed_ppm
        required_frames = (
            frame_numerator + frame_denominator - 1
        ) // frame_denominator
        return max(
            1,
            (
                required_frames * 1_000_000
                + TIMELINE_SAMPLE_RATE
                - 1
            )
            // TIMELINE_SAMPLE_RATE,
        )
    return max(
        1,
        (window.work_duration_us * 1_000_000 + speed_ppm - 1) // speed_ppm,
    )


def _minimum_window_speed(window: _NaturalWindow, maximum_speed_ppm: int) -> int:
    available_us = window.upper_us - window.lower_us
    if _duration_at_speed(window, maximum_speed_ppm) > available_us:
        raise AssertionError("window capacity must be checked before speed search")
    low = 1_000_000
    high = maximum_speed_ppm
    while low < high:
        candidate = (low + high) // 2
        if _duration_at_speed(window, candidate) <= available_us:
            high = candidate
        else:
            low = candidate + 1
    return low


def _first_schedule_failure(
    windows: Sequence[_NaturalWindow], speeds: Sequence[int]
) -> tuple[int | None, int]:
    """Return the first failed local ordinal and its remaining window time."""

    cursor = 0
    for index, (window, speed_ppm) in enumerate(
        zip(windows, speeds, strict=True)
    ):
        earliest = max(window.lower_us, cursor)
        slot_duration_us = _duration_at_speed(window, speed_ppm)
        available_us = max(0, window.upper_us - earliest)
        if slot_duration_us > available_us:
            return index, available_us
        cursor = earliest + slot_duration_us
    return None, 0


def _independent_natural_chains(
    windows: Sequence[_NaturalWindow],
) -> tuple[tuple[int, int], ...]:
    """Partition expanded windows into non-touching connected components."""

    if not windows:
        return ()
    chains: list[tuple[int, int]] = []
    chain_start = 0
    component_upper_us = windows[0].upper_us
    for index, window in enumerate(windows[1:], start=1):
        if window.lower_us >= component_upper_us:
            chains.append((chain_start, index))
            chain_start = index
            component_upper_us = window.upper_us
        else:
            component_upper_us = max(component_upper_us, window.upper_us)
    chains.append((chain_start, len(windows)))
    return tuple(chains)


def _nearby_natural_chains(
    windows: Sequence[_NaturalWindow], *, maximum_gap_us: int
) -> tuple[tuple[int, int], ...]:
    """Group windows whose narration remains perceptually adjacent."""

    if not windows:
        return ()
    chains: list[tuple[int, int]] = []
    chain_start = 0
    for index, window in enumerate(windows[1:], start=1):
        previous = windows[index - 1]
        if window.lower_us - previous.upper_us > maximum_gap_us:
            chains.append((chain_start, index))
            chain_start = index
    chains.append((chain_start, len(windows)))
    return tuple(chains)


def _centered_natural_schedule(
    windows: Sequence[_NaturalWindow], speeds: Sequence[int]
) -> tuple[tuple[int, int], ...]:
    """Centre a known-feasible ordered schedule inside its local windows."""

    durations = tuple(
        _duration_at_speed(window, speed_ppm)
        for window, speed_ppm in zip(windows, speeds, strict=True)
    )
    if _first_schedule_failure(windows, speeds)[0] is not None:  # pragma: no cover
        raise AssertionError("a natural schedule must be feasible before centring")

    latest_starts = [0] * len(windows)
    next_start: int | None = None
    for index in range(len(windows) - 1, -1, -1):
        window = windows[index]
        slot_duration_us = durations[index]
        latest = window.upper_us - slot_duration_us
        if next_start is not None:
            latest = min(latest, next_start - slot_duration_us)
        if latest < window.lower_us:  # pragma: no cover - guarded by forward pass
            raise AssertionError("forward-feasible schedule must have a latest start")
        latest_starts[index] = latest
        next_start = latest

    schedule: list[tuple[int, int]] = []
    previous_end = 0
    for index, (window, slot_duration_us) in enumerate(
        zip(windows, durations, strict=True)
    ):
        lower = max(window.lower_us, previous_end)
        upper = latest_starts[index]
        preferred = window.preferred_center_us - slot_duration_us // 2
        start_us = min(max(preferred, lower), upper)
        end_us = start_us + slot_duration_us
        schedule.append((start_us, end_us))
        previous_end = end_us
    return tuple(schedule)


def _raise_rewrite_required(
    windows: Sequence[_NaturalWindow],
    *,
    failure_index: int,
    available_us: int,
    maximum_speed_ppm: int,
    maximum_total_speed: float,
    failure_kind: str | None = None,
    schedule_deficit_us: int | None = None,
) -> NoReturn:
    window = windows[failure_index]
    required_us = _duration_at_speed(window, maximum_speed_ppm)
    group_start_index = _critical_group_start_index(
        windows,
        failure_index=failure_index,
        maximum_speed_ppm=maximum_speed_ppm,
    )
    deficit_us = (
        max(1, required_us - available_us)
        if schedule_deficit_us is None
        else max(1, schedule_deficit_us)
    )
    group_windows = windows[group_start_index : failure_index + 1]
    candidates = [
        {
            "ordinal": candidate.ordinal,
            "required_duration_us": (
                candidate_required_us := _duration_at_speed(
                    candidate, maximum_speed_ppm
                )
            ),
            "target_available_duration_us": max(
                1, candidate_required_us - deficit_us
            ),
            "work_duration_us": candidate.work_duration_us,
        }
        for candidate in group_windows
    ]
    candidates.sort(
        key=lambda candidate: (
            -int(candidate["required_duration_us"]),
            abs(window.ordinal - int(candidate["ordinal"])),
            int(candidate["ordinal"]),
        )
    )
    del candidates[3:]
    selected_failure_kind = failure_kind or (
        "single_window_capacity"
        if group_start_index == failure_index
        else "critical_chain_capacity"
    )
    formatted_maximum_speed = f"{maximum_total_speed:.2f}".replace(".", ",")
    raise TimingError(
        "timing_rewrite_required",
        (
            f"Khối thuyết minh {window.ordinal + 1} cần khoảng "
            f"{required_us / 1_000_000:.2f} giây nhưng cửa sổ gần cảnh "
            f"chỉ còn {available_us / 1_000_000:.2f} giây; cần rút gọn "
            f"bản dịch thay vì tăng tốc giọng quá {formatted_maximum_speed}×"
        ),
        retryable=False,
        details={
            "profile": TimingProfile.NATURAL.value,
            "ordinal": window.ordinal,
            "required_duration_us": required_us,
            "available_duration_us": available_us,
            "maximum_total_speed": maximum_total_speed,
            "failure_kind": selected_failure_kind,
            "failure_ordinal": window.ordinal,
            "critical_group_start_ordinal": windows[group_start_index].ordinal,
            "critical_group_end_ordinal": window.ordinal,
            "schedule_deficit_us": deficit_us,
            "rewrite_candidates": candidates,
        },
    )


def _critical_group_start_index(
    windows: Sequence[_NaturalWindow],
    *,
    failure_index: int,
    maximum_speed_ppm: int,
) -> int:
    """Find the busy prefix after the last idle reset before a failure."""

    cursor_us = 0
    group_start_index = 0
    for index, window in enumerate(windows[: failure_index + 1]):
        if cursor_us <= window.lower_us:
            group_start_index = index
        earliest_us = max(window.lower_us, cursor_us)
        if index == failure_index:
            break
        cursor_us = earliest_us + _duration_at_speed(window, maximum_speed_ppm)
    return group_start_index


class FfmpegTimingFitter:
    """Fit a local PCM WAV to one timeline slot using FFmpeg ``atempo``.

    Speech is never truncated to solve a large under-run.  Slowdown is capped
    at 0.80x and the remaining tail is padded with silence.  FFmpeg's output is
    resampled, padded/trimmed for rounding only, and faded in/out in the same
    filter graph before an atomic publish.
    """

    def __init__(
        self,
        *,
        ffmpeg_binary: str = "ffmpeg",
        process_factory: ProcessFactory | None = None,
        minimum_slowdown: float = 0.80,
        fade_duration_us: int = 5_000,
        poll_interval_seconds: float = 0.05,
        stop_grace_seconds: float = 1.0,
        timeout_seconds: float | None = 300.0,
    ) -> None:
        if not ffmpeg_binary.strip() or "://" in ffmpeg_binary:
            raise ValueError("ffmpeg_binary must be a local executable")
        if not 0.5 <= minimum_slowdown <= 1.0:
            raise ValueError("minimum_slowdown must be between 0.5 and 1.0")
        if fade_duration_us < 0:
            raise ValueError("fade_duration_us must not be negative")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if stop_grace_seconds < 0:
            raise ValueError("stop_grace_seconds must not be negative")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._ffmpeg = ffmpeg_binary
        self._process_factory = process_factory or _default_process_factory
        self._minimum_slowdown = minimum_slowdown
        self._fade_duration_us = fade_duration_us
        self._poll_interval_seconds = poll_interval_seconds
        self._stop_grace_seconds = stop_grace_seconds
        self._timeout_seconds = timeout_seconds

    async def fit(
        self,
        input_path: Path,
        output_path: Path,
        *,
        start_us: int,
        end_us: int,
        text: str = "",
        native_speed: float = 1.0,
        maximum_total_speed: float | None = None,
        cancellation: Cancellation | None = None,
        on_progress: TimingProgress | None = None,
    ) -> FittedNarrationBlock:
        if start_us < 0 or end_us <= start_us:
            raise TimingError(
                "timing_slot_invalid",
                "Mốc thời gian của lời thuyết minh không hợp lệ",
                retryable=False,
            )
        if not 0.5 <= native_speed <= 2.0:
            raise TimingError(
                "timing_speed_invalid",
                "Tốc độ TTS gốc không hợp lệ",
                retryable=False,
            )
        if maximum_total_speed is not None and not 1.0 <= maximum_total_speed <= 2.0:
            raise TimingError(
                "timing_speed_invalid",
                "Giới hạn tốc độ lời thuyết minh không hợp lệ",
                retryable=False,
            )
        destination = Path(output_path).resolve(strict=False)
        if destination.suffix.lower() != ".wav":
            raise TimingError(
                "timing_output_invalid",
                "File timing đầu ra phải có phần mở rộng .wav",
                retryable=False,
            )
        try:
            source = Path(input_path).resolve(strict=True)
        except OSError as exc:
            raise TimingError(
                "timing_input_missing",
                "Không tìm thấy file TTS cần khớp thời lượng",
                retryable=True,
            ) from exc
        if not source.is_file() or source == destination:
            raise TimingError(
                "timing_input_invalid",
                "File TTS đầu vào không hợp lệ",
                retryable=False,
            )

        source_metadata = _inspect_wav(source, require_timeline_format=False)
        target_frames = microseconds_to_samples(end_us) - microseconds_to_samples(start_us)
        if target_frames <= 0:
            raise TimingError(
                "timing_slot_too_short",
                "Slot thuyết minh quá ngắn để chứa một sample",
                retryable=False,
            )
        raw_atempo = (
            source_metadata.frame_count
            * TIMELINE_SAMPLE_RATE
            / (source_metadata.sample_rate * target_frames)
        )
        applied_atempo = max(self._minimum_slowdown, raw_atempo)
        estimated_content_frames = min(
            target_frames,
            round(
                source_metadata.frame_count
                * TIMELINE_SAMPLE_RATE
                / (source_metadata.sample_rate * applied_atempo)
            ),
        )
        padded_frames = max(0, target_frames - estimated_content_frames)
        total_speed = native_speed * applied_atempo
        if maximum_total_speed is not None and total_speed > maximum_total_speed:
            work_duration_us = round(source_metadata.duration_us * native_speed)
            required_duration_us = math.ceil(work_duration_us / maximum_total_speed)
            raise TimingError(
                "timing_rewrite_required",
                (
                    "Câu thuyết minh vẫn quá dài sau khi mượn khoảng lặng; "
                    "cần rút gọn bản dịch thay vì tăng tốc giọng quá 1,20×"
                ),
                retryable=False,
                details={
                    "profile": TimingProfile.NATURAL.value,
                    "required_duration_us": required_duration_us,
                    "available_duration_us": end_us - start_us,
                    "maximum_total_speed": maximum_total_speed,
                },
            )
        quality = classify_timing_quality(total_speed)

        temporary = destination.with_name(f".{destination.stem}.timing.part.wav")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise TimingError(
                "timing_output_unavailable",
                "Không thể chuẩn bị nơi lưu lời thuyết minh đã khớp",
                retryable=True,
            ) from exc
        if _is_cancelled(cancellation):
            raise TimingError(
                "timing_cancelled",
                "Đã hủy khớp thời lượng lời thuyết minh",
                retryable=True,
            )

        filters = [
            *(f"atempo={factor:.10f}" for factor in decompose_atempo(applied_atempo)),
            f"aresample={TIMELINE_SAMPLE_RATE}",
            f"apad=whole_len={target_frames}",
            f"atrim=end_sample={target_frames}",
        ]
        fade_frames = min(
            microseconds_to_samples(self._fade_duration_us), target_frames // 2
        )
        if fade_frames > 0:
            filters.extend(
                [
                    f"afade=t=in:ss=0:ns={fade_frames}",
                    f"afade=t=out:ss={target_frames - fade_frames}:ns={fade_frames}",
                ]
            )
        command = (
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
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-filter:a",
            ",".join(filters),
            "-ac",
            str(TIMELINE_CHANNELS),
            "-ar",
            str(TIMELINE_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            os.fspath(temporary),
        )
        if any("://" in value for value in command):
            raise TimingError(
                "timing_network_forbidden",
                "Khớp thời lượng không được dùng tài nguyên mạng",
                retryable=False,
            )

        process: ProcessHandle | None = None
        communication: asyncio.Task[tuple[bytes | None, bytes | None]] | None = None
        started = time.monotonic()
        try:
            _report_progress(on_progress, 0, 1)
            try:
                process = await self._process_factory(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise TimingError(
                    "timing_runtime_unavailable",
                    "Không thể khởi động FFmpeg để khớp thời lượng",
                    retryable=False,
                ) from exc
            communication = asyncio.create_task(process.communicate())
            while not communication.done():
                if _is_cancelled(cancellation):
                    await self._stop_process(process, communication)
                    raise TimingError(
                        "timing_cancelled",
                        "Đã hủy khớp thời lượng lời thuyết minh",
                        retryable=True,
                    )
                if (
                    self._timeout_seconds is not None
                    and time.monotonic() - started >= self._timeout_seconds
                ):
                    await self._stop_process(process, communication)
                    raise TimingError(
                        "timing_timeout",
                        "Khớp thời lượng vượt quá thời gian cho phép",
                        retryable=True,
                    )
                await asyncio.wait({communication}, timeout=self._poll_interval_seconds)
            _, _ = await communication
            if process.returncode != 0:
                raise TimingError(
                    "timing_ffmpeg_failed",
                    "FFmpeg không thể khớp thời lượng lời thuyết minh",
                    retryable=True,
                )
            output_metadata = _inspect_wav(temporary, require_timeline_format=True)
            if output_metadata.frame_count != target_frames:
                raise TimingError(
                    "timing_sample_count_mismatch",
                    "FFmpeg tạo sai số sample của slot thuyết minh",
                    retryable=True,
                )
            try:
                os.replace(temporary, destination)
            except OSError as exc:
                raise TimingError(
                    "timing_output_unavailable",
                    "Không thể lưu lời thuyết minh đã khớp thời lượng",
                    retryable=True,
                ) from exc
            _report_progress(on_progress, 1, 1)
            return FittedNarrationBlock(
                path=destination,
                start_us=start_us,
                end_us=end_us,
                text=" ".join(text.split()),
                source_duration_us=source_metadata.duration_us,
                target_frame_count=target_frames,
                output_frame_count=output_metadata.frame_count,
                native_speed=native_speed,
                atempo_speed=applied_atempo,
                total_speed=total_speed,
                padded_frame_count=padded_frames,
                quality=quality,
            )
        except asyncio.CancelledError:
            if process is not None and communication is not None:
                await self._stop_process(process, communication)
            raise
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    async def _stop_process(
        self,
        process: ProcessHandle,
        communication: asyncio.Task[tuple[bytes | None, bytes | None]],
    ) -> None:
        if communication.done() or process.returncode is not None:
            with suppress(Exception):
                await communication
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(
                asyncio.shield(communication), timeout=self._stop_grace_seconds
            )
            return
        except TimeoutError:
            pass
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(Exception):
            await communication


def build_timeline_wav(
    blocks: Sequence[FittedNarrationBlock],
    output_path: Path,
    *,
    duration_us: int,
    cancellation: Cancellation | None = None,
    on_progress: TimingProgress | None = None,
) -> NarrationTimeline:
    """Place fitted blocks at rounded microsecond sample positions atomically."""

    if duration_us <= 0:
        raise TimingError(
            "timeline_duration_invalid",
            "Thời lượng timeline thuyết minh không hợp lệ",
            retryable=False,
        )
    if not blocks:
        raise TimingError(
            "timeline_empty",
            "Không có lời thuyết minh để tạo timeline",
            retryable=False,
        )
    destination = Path(output_path).resolve(strict=False)
    if destination.suffix.lower() != ".wav":
        raise TimingError(
            "timeline_output_invalid",
            "Timeline đầu ra phải có phần mở rộng .wav",
            retryable=False,
        )
    total_frames = microseconds_to_samples(duration_us)
    if total_frames <= 0:
        raise TimingError(
            "timeline_duration_invalid",
            "Timeline quá ngắn để chứa một sample",
            retryable=False,
        )
    temporary = destination.with_name(f".{destination.stem}.timeline.part.wav")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise TimingError(
            "timeline_output_unavailable",
            "Không thể chuẩn bị nơi lưu timeline thuyết minh",
            retryable=True,
        ) from exc

    previous_end_us = 0
    current_frame = 0
    try:
        with wave.open(os.fspath(temporary), "wb") as destination_wav:
            destination_wav.setnchannels(TIMELINE_CHANNELS)
            destination_wav.setsampwidth(TIMELINE_SAMPLE_WIDTH_BYTES)
            destination_wav.setframerate(TIMELINE_SAMPLE_RATE)
            for ordinal, block in enumerate(blocks):
                if not isinstance(block, FittedNarrationBlock):
                    raise TimingError(
                        "timeline_block_invalid",
                        "Khối thuyết minh không hợp lệ",
                        retryable=False,
                    )
                if block.start_us < previous_end_us or block.end_us > duration_us:
                    raise TimingError(
                        "timeline_overlap",
                        "Timeline thuyết minh bị overlap hoặc vượt thời lượng video",
                        retryable=False,
                    )
                start_frame = microseconds_to_samples(block.start_us)
                end_frame = microseconds_to_samples(block.end_us)
                expected_frames = end_frame - start_frame
                if expected_frames != block.target_frame_count:
                    raise TimingError(
                        "timeline_slot_mismatch",
                        "Số sample của khối không khớp timestamp timeline",
                        retryable=False,
                    )
                metadata = _inspect_wav(block.path, require_timeline_format=True)
                if metadata.frame_count != expected_frames:
                    raise TimingError(
                        "timeline_block_length_mismatch",
                        "Khối thuyết minh không đúng số sample của slot",
                        retryable=False,
                    )
                _write_silence(
                    destination_wav,
                    start_frame - current_frame,
                    cancellation=cancellation,
                )
                _copy_wav_frames(
                    block.path,
                    destination_wav,
                    expected_frames=expected_frames,
                    cancellation=cancellation,
                )
                current_frame = end_frame
                previous_end_us = block.end_us
                _report_progress(on_progress, ordinal + 1, len(blocks))
            _write_silence(
                destination_wav,
                total_frames - current_frame,
                cancellation=cancellation,
            )
        metadata = _inspect_wav(temporary, require_timeline_format=True)
        if metadata.frame_count != total_frames:
            raise TimingError(
                "timeline_sample_count_mismatch",
                "Timeline thuyết minh không đúng tổng số sample",
                retryable=True,
            )
        if _is_cancelled(cancellation):
            raise TimingError(
                "timing_cancelled",
                "Đã hủy tạo timeline thuyết minh",
                retryable=True,
            )
        try:
            os.replace(temporary, destination)
        except OSError as exc:
            raise TimingError(
                "timeline_output_unavailable",
                "Không thể lưu timeline thuyết minh",
                retryable=True,
            ) from exc
        return NarrationTimeline(
            path=destination,
            duration_us=duration_us,
            frame_count=total_frames,
            sample_rate=TIMELINE_SAMPLE_RATE,
            channels=TIMELINE_CHANNELS,
            sample_width_bytes=TIMELINE_SAMPLE_WIDTH_BYTES,
            block_count=len(blocks),
        )
    except TimingError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise TimingError(
            "timeline_write_failed",
            "Không thể tạo timeline WAV thuyết minh",
            retryable=True,
        ) from exc
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def microseconds_to_samples(value_us: int, *, sample_rate: int = TIMELINE_SAMPLE_RATE) -> int:
    if isinstance(value_us, bool) or not isinstance(value_us, int) or value_us < 0:
        raise TimingError(
            "timing_timestamp_invalid",
            "Timestamp thuyết minh không hợp lệ",
            retryable=False,
        )
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    return (value_us * sample_rate + 500_000) // 1_000_000


def decompose_atempo(speed: float) -> tuple[float, ...]:
    if not 0.5 <= speed <= 10_000:
        raise TimingError(
            "timing_speed_invalid",
            "Hệ số atempo không hợp lệ",
            retryable=False,
        )
    remaining = speed
    factors: list[float] = []
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return tuple(factors)


def classify_timing_quality(total_speed: float) -> TimingQuality:
    if total_speed <= 1.35:
        return TimingQuality.NORMAL
    if total_speed <= 1.70:
        return TimingQuality.WARNING
    return TimingQuality.SEVERE


@dataclass(frozen=True, slots=True)
class _WavMetadata:
    sample_rate: int
    channels: int
    sample_width_bytes: int
    frame_count: int
    duration_us: int


def _inspect_wav(path: Path, *, require_timeline_format: bool) -> _WavMetadata:
    try:
        with wave.open(os.fspath(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_rate = stream.getframerate()
            sample_width = stream.getsampwidth()
            frame_count = stream.getnframes()
            compression = stream.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise TimingError(
            "timing_audio_invalid",
            "File WAV cho bước khớp thời lượng không hợp lệ",
            retryable=False,
        ) from exc
    if (
        frame_count <= 0
        or channels != 1
        or sample_width != TIMELINE_SAMPLE_WIDTH_BYTES
        or sample_rate <= 0
        or compression != "NONE"
        or (require_timeline_format and sample_rate != TIMELINE_SAMPLE_RATE)
    ):
        raise TimingError(
            "timing_audio_invalid",
            (
                "Khối thuyết minh phải là PCM mono 48 kHz 16-bit"
                if require_timeline_format
                else "Âm thanh TTS phải là PCM mono 16-bit hợp lệ"
            ),
            retryable=False,
        )
    duration_us = (frame_count * 1_000_000 + sample_rate // 2) // sample_rate
    return _WavMetadata(
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        frame_count=frame_count,
        duration_us=duration_us,
    )


def _write_silence(
    destination: wave.Wave_write,
    frame_count: int,
    *,
    cancellation: Cancellation | None,
) -> None:
    if frame_count < 0:
        raise TimingError(
            "timeline_overlap",
            "Timeline thuyết minh bị overlap",
            retryable=False,
        )
    silence_chunk = b"\0" * (16_384 * TIMELINE_SAMPLE_WIDTH_BYTES)
    remaining = frame_count
    while remaining:
        if _is_cancelled(cancellation):
            raise TimingError(
                "timing_cancelled",
                "Đã hủy tạo timeline thuyết minh",
                retryable=True,
            )
        count = min(remaining, 16_384)
        destination.writeframesraw(silence_chunk[: count * TIMELINE_SAMPLE_WIDTH_BYTES])
        remaining -= count


def _copy_wav_frames(
    source_path: Path,
    destination: wave.Wave_write,
    *,
    expected_frames: int,
    cancellation: Cancellation | None,
) -> None:
    copied = 0
    with wave.open(os.fspath(source_path), "rb") as source:
        while copied < expected_frames:
            if _is_cancelled(cancellation):
                raise TimingError(
                    "timing_cancelled",
                    "Đã hủy tạo timeline thuyết minh",
                    retryable=True,
                )
            payload = source.readframes(min(16_384, expected_frames - copied))
            if not payload:
                break
            frames = len(payload) // TIMELINE_SAMPLE_WIDTH_BYTES
            destination.writeframesraw(payload)
            copied += frames
    if copied != expected_frames:
        raise TimingError(
            "timeline_block_length_mismatch",
            "Không đọc đủ sample của khối thuyết minh",
            retryable=False,
        )


def _is_cancelled(cancellation: Cancellation | None) -> bool:
    if cancellation is None:
        return False
    try:
        return bool(cancellation() if callable(cancellation) else cancellation.is_cancelled())
    except Exception as exc:
        raise TimingError(
            "timing_cancellation_invalid",
            "Không thể kiểm tra trạng thái hủy timing",
            retryable=False,
        ) from exc


def _report_progress(
    callback: TimingProgress | None, completed: int, total: int
) -> None:
    if callback is None:
        return
    try:
        callback(completed, total)
    except Exception as exc:
        raise TimingError(
            "timing_progress_failed",
            "Không thể cập nhật tiến độ khớp thời lượng",
            retryable=False,
        ) from exc


__all__ = [
    "NATURAL_BORROW_WINDOW_US",
    "NATURAL_MAX_ADJACENT_SPEED_DELTA",
    "NATURAL_MAX_ELASTIC_CENTER_DRIFT_US",
    "NATURAL_MAX_SILENT_BORROW_US",
    "NATURAL_MAX_TOTAL_SPEED",
    "NATURAL_SILENT_GAP_GUARD_US",
    "TIMELINE_CHANNELS",
    "TIMELINE_SAMPLE_RATE",
    "TIMELINE_SAMPLE_WIDTH_BYTES",
    "FfmpegTimingFitter",
    "FittedNarrationBlock",
    "NarrationTimeline",
    "NarrationTimingInput",
    "PlannedNarrationSlot",
    "ProcessFactory",
    "TimingError",
    "TimingProfile",
    "TimingProgress",
    "TimingQuality",
    "build_timeline_wav",
    "classify_timing_quality",
    "decompose_atempo",
    "microseconds_to_samples",
    "plan_narration_slots",
]
