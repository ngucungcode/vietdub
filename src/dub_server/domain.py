"""Shared, serializable contracts for acquisition and later pipeline stages.

All public timestamps use integer microseconds.  Network credentials and
provider download URLs deliberately live only on adapter-facing records and
are excluded from their representations.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable


class MediaKind(StrEnum):
    MOVIE = "movie"
    SERIES = "series"


def normalize_speaker_label(value: object | None) -> str | None:
    """Normalize a speaker/character label, or ``None`` when it is absent.

    Whitespace is collapsed and the label is capped at 120 characters so a
    stray subtitle line cannot smuggle a paragraph into a speaker key. The
    same function normalizes labels parsed from subtitles and keys supplied
    in a job's speaker-voice map, so the two sides always match.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Nhãn nhân vật phải là chuỗi")
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return normalized[:120]


@dataclass(frozen=True, slots=True)
class MediaQuery:
    query: str
    year: int | None = None
    media_kind: MediaKind = MediaKind.MOVIE

    def __post_init__(self) -> None:
        normalized = " ".join(self.query.split())
        if not normalized:
            raise ValueError("Từ khóa tìm kiếm không được để trống")
        if self.year is not None and not 1888 <= self.year <= 2200:
            raise ValueError("Năm phát hành không hợp lệ")
        object.__setattr__(self, "query", normalized)


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    release_id: str
    title: str
    indexer_id: int
    protocol: str
    download_uri: str = field(repr=False)
    guid: str = field(default="", repr=False)
    info_hash: str | None = None
    size_bytes: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    published_at: str | None = None
    categories: tuple[str, ...] = ()


class DownloadState(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    CHECKING = "checking"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DownloadTask:
    task_id: str
    name: str
    save_path: Path


@dataclass(frozen=True, slots=True)
class DownloadStatus:
    task_id: str
    state: DownloadState
    progress: float
    downloaded_bytes: int
    total_bytes: int
    speed_bytes_per_second: int = 0
    eta_seconds: int | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    relative_path: Path
    size_bytes: int
    progress: float


@dataclass(frozen=True, slots=True)
class MediaAsset:
    path: Path
    title: str
    duration_us: int
    source_language: str
    media_kind: MediaKind = MediaKind.MOVIE
    year: int | None = None
    fps: float | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    audio_stream_index: int | None = None
    audio_start_us: int = 0
    video_stream_index: int | None = None
    video_codec: str | None = None

    def __post_init__(self) -> None:
        if self.duration_us <= 0:
            raise ValueError("Thời lượng media phải lớn hơn 0")
        language = self.source_language.strip().lower().replace("_", "-")
        if not language:
            raise ValueError("Ngôn ngữ nguồn không được để trống")
        if self.audio_stream_index is not None and self.audio_stream_index < 0:
            raise ValueError("Chỉ số luồng âm thanh không hợp lệ")
        if self.video_stream_index is not None and self.video_stream_index < 0:
            raise ValueError("Chỉ số luồng hình không hợp lệ")
        video_codec = (
            self.video_codec.strip().lower() or None
            if self.video_codec is not None
            else None
        )
        object.__setattr__(self, "source_language", language)
        object.__setattr__(self, "video_codec", video_codec)


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One normalized transcript segment with integer microsecond timing.

    ``speaker`` is an optional character label parsed from a subtitle file
    (for example an ``NAME:`` prefix in SRT or the ``Name`` field in ASS).
    It is metadata only: it never becomes part of the spoken text, and it
    survives translation so the TTS stage can pick a voice per character.
    ``None`` means the speaker is unknown; labels are whitespace-normalized
    so ``"  An Na  "`` and ``"An Na"`` refer to the same character.
    """

    start_us: int
    end_us: int
    text: str
    average_log_probability: float | None = None
    no_speech_probability: float | None = None
    speaker: str | None = None

    def __post_init__(self) -> None:
        normalized_text = " ".join(self.text.split())
        if self.start_us < 0 or self.end_us <= self.start_us:
            raise ValueError("Mốc thời gian transcript không hợp lệ")
        if not normalized_text:
            raise ValueError("Nội dung transcript không được để trống")
        object.__setattr__(self, "text", normalized_text)
        object.__setattr__(self, "speaker", normalize_speaker_label(self.speaker))


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Serializable output shared by ASR and subtitle transcript sources."""

    source: str
    language: str
    language_probability: float
    duration_us: int
    segments: tuple[TranscriptSegment, ...]
    model_id: str | None = None

    def __post_init__(self) -> None:
        if self.source not in {"asr", "subtitle"}:
            raise ValueError("Nguồn transcript không hợp lệ")
        language = self.language.strip().lower().replace("_", "-")
        if not language:
            raise ValueError("Ngôn ngữ transcript không hợp lệ")
        if self.duration_us <= 0:
            raise ValueError("Thời lượng transcript không hợp lệ")
        if not 0.0 <= self.language_probability <= 1.0:
            raise ValueError("Độ tin cậy ngôn ngữ không hợp lệ")
        object.__setattr__(self, "language", language)


TranscriptionProgress = Callable[[int, int], None]


@runtime_checkable
class SpeechRecognizer(Protocol):
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
    ) -> TranscriptionResult: ...


class SubtitleSource(StrEnum):
    EMBEDDED = "embedded"
    SIDECAR = "sidecar"
    OPENSUBTITLES = "opensubtitles"


class SubtitleFormat(StrEnum):
    SRT = "srt"
    VTT = "vtt"
    ASS = "ass"


@dataclass(frozen=True, slots=True)
class SubtitleCandidate:
    subtitle_id: str
    source: SubtitleSource
    language: str
    format: SubtitleFormat
    score: int
    high_confidence: bool
    release_name: str | None = None
    fps: float | None = None
    hearing_impaired: bool = False
    forced: bool = False
    local_path: Path | None = None
    embedded_stream_index: int | None = None
    remote_file_id: int | None = field(default=None, repr=False)
    matched_by: str | None = None


@dataclass(frozen=True, slots=True)
class SubtitleInspection:
    format: SubtitleFormat
    cue_count: int
    first_start_us: int
    last_end_us: int
    overlap_count: int


class AcquisitionErrorCode(StrEnum):
    RIGHTS_CONFIRMATION_REQUIRED = "rights_confirmation_required"
    INVALID_RESPONSE = "invalid_response"
    INDEXER_UNAVAILABLE = "indexer_unavailable"
    DOWNLOAD_UNAVAILABLE = "download_unavailable"
    DOWNLOAD_NOT_FOUND = "download_not_found"
    SUBTITLE_UNAVAILABLE = "subtitle_unavailable"
    SUBTITLE_INVALID = "subtitle_invalid"
    SUBTITLE_ARCHIVE_UNSAFE = "subtitle_archive_unsafe"


class AcquisitionError(Exception):
    """A safe error value suitable for mapping to an API response."""

    def __init__(
        self,
        code: AcquisitionErrorCode,
        message_vi: str,
        *,
        retryable: bool,
    ) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable


@runtime_checkable
class IndexerGateway(Protocol):
    async def search(self, query: MediaQuery) -> tuple[ReleaseCandidate, ...]: ...


@runtime_checkable
class DownloadClient(Protocol):
    async def add(
        self,
        release: ReleaseCandidate,
        save_path: Path,
        *,
        paused: bool = False,
    ) -> DownloadTask: ...

    async def status(self, task_id: str) -> DownloadStatus: ...

    async def files(self, task_id: str) -> tuple[DownloadedFile, ...]: ...

    async def pause(self, task_id: str) -> None: ...

    async def resume(self, task_id: str) -> None: ...

    async def relocate(self, task_id: str, save_path: Path) -> None: ...

    async def cancel(self, task_id: str, *, delete_files: bool = False) -> None: ...


@runtime_checkable
class SubtitleProvider(Protocol):
    async def find(self, media: MediaAsset) -> tuple[SubtitleCandidate, ...]: ...

    async def materialize(
        self,
        media: MediaAsset,
        candidate: SubtitleCandidate,
        destination: Path,
    ) -> Path: ...
