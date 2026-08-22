"""Local and OpenSubtitles discovery with defensive subtitle handling."""

from __future__ import annotations

import asyncio
import hashlib
import io
import itertools
import json
import math
import os
import re
import shutil
import struct
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from dub_server import __version__
from dub_server.domain import (
    AcquisitionError,
    AcquisitionErrorCode,
    MediaAsset,
    MediaKind,
    SubtitleCandidate,
    SubtitleFormat,
    SubtitleInspection,
    SubtitleProvider,
    SubtitleSource,
)
from dub_server.opensubtitles import normalize_opensubtitles_api_root

_SUBTITLE_SUFFIXES = {
    ".srt": SubtitleFormat.SRT,
    ".vtt": SubtitleFormat.VTT,
    ".ass": SubtitleFormat.ASS,
    ".ssa": SubtitleFormat.ASS,
}
_LANGUAGE_ALIASES = {
    "ar": "ar",
    "ara": "ar",
    "arabic": "ar",
    "de": "de",
    "deu": "de",
    "ger": "de",
    "en": "en",
    "eng": "en",
    "english": "en",
    "es": "es",
    "spa": "es",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "id": "id",
    "ind": "id",
    "ja": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "ko": "ko",
    "kor": "ko",
    "korean": "ko",
    "ru": "ru",
    "rus": "ru",
    "th": "th",
    "tha": "th",
    "thai": "th",
    "vi": "vi",
    "vie": "vi",
    "vietnamese": "vi",
    "zh": "zh",
    "chi": "zh",
    "zho": "zh",
}
_SAFE_NAME_PATTERN = re.compile(r"[^a-z0-9]+")
_TIMING_PATTERN = re.compile(
    r"(?P<start>(?:\d{1,3}:)?\d{1,2}:\d{2}[,.]\d{2,3})\s*-->\s*"
    r"(?P<end>(?:\d{1,3}:)?\d{1,2}:\d{2}[,.]\d{2,3})"
)
_MAX_LOCAL_SUBTITLE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class EmbeddedSubtitleTrack:
    stream_index: int
    language: str
    format: SubtitleFormat
    title: str | None = None
    forced: bool = False
    hearing_impaired: bool = False


class EmbeddedSubtitleProbe(Protocol):
    async def inspect(self, media_path: Path) -> tuple[EmbeddedSubtitleTrack, ...]: ...

    async def extract(self, media_path: Path, stream_index: int, destination: Path) -> Path: ...


class FfprobeSubtitleProbe:
    """Safe subprocess wrapper; commands never pass through a shell."""

    def __init__(
        self,
        *,
        ffprobe_binary: str = "ffprobe",
        ffmpeg_binary: str = "ffmpeg",
        timeout_seconds: float = 30.0,
        max_output_bytes: int = _MAX_LOCAL_SUBTITLE_BYTES,
    ) -> None:
        self._ffprobe = ffprobe_binary
        self._ffmpeg = ffmpeg_binary
        self._timeout = timeout_seconds
        self._max_output_bytes = max_output_bytes

    async def inspect(self, media_path: Path) -> tuple[EmbeddedSubtitleTrack, ...]:
        command = (
            self._ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-select_streams",
            "s",
            "-show_entries",
            "stream=index,codec_name:stream_tags=language,title:stream_disposition=forced,hearing_impaired",
            "-of",
            "json",
            os.fspath(media_path),
        )
        output = await self._run(command)
        try:
            payload = json.loads(output)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _subtitle_error("ffprobe trả về dữ liệu phụ đề không hợp lệ", retryable=False) from exc
        streams = payload.get("streams") if isinstance(payload, dict) else None
        if not isinstance(streams, list):
            return ()
        tracks: list[EmbeddedSubtitleTrack] = []
        for raw in streams:
            if not isinstance(raw, dict):
                continue
            stream_index = _int(raw.get("index"))
            subtitle_format = _codec_format(_string(raw.get("codec_name")))
            tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
            disposition = (
                raw.get("disposition") if isinstance(raw.get("disposition"), dict) else {}
            )
            language = _normalize_language(_string(tags.get("language")))
            if stream_index is None or subtitle_format is None or language is None:
                continue
            tracks.append(
                EmbeddedSubtitleTrack(
                    stream_index=stream_index,
                    language=language,
                    format=subtitle_format,
                    title=_string(tags.get("title")) or None,
                    forced=bool(_int(disposition.get("forced")) or 0),
                    hearing_impaired=bool(_int(disposition.get("hearing_impaired")) or 0),
                )
            )
        return tuple(tracks)

    async def extract(self, media_path: Path, stream_index: int, destination: Path) -> Path:
        destination = destination.with_suffix(".srt")
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = (
            self._ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-protocol_whitelist",
            "file",
            "-i",
            os.fspath(media_path),
            "-map",
            f"0:{stream_index}",
            "-c:s",
            "srt",
            "-fs",
            str(self._max_output_bytes),
            os.fspath(destination),
        )
        try:
            await self._run(command)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination

    async def _run(self, command: tuple[str, ...]) -> bytes:
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(self._timeout):
                stdout, _ = await process.communicate()
        except TimeoutError as exc:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(Exception):
                    await process.communicate()
            raise _subtitle_error("Không thể đọc phụ đề nhúng", retryable=True) from exc
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(Exception):
                    await process.communicate()
            raise
        except OSError as exc:
            raise _subtitle_error("Không thể đọc phụ đề nhúng", retryable=True) from exc
        if process.returncode != 0:
            raise _subtitle_error("Không thể đọc phụ đề nhúng", retryable=False)
        return stdout


class CompositeSubtitleProvider(SubtitleProvider):
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        opensubtitles_api_key: str | None = None,
        opensubtitles_token: str | None = None,
        opensubtitles_base_url: str = "https://api.opensubtitles.com/api/v1",
        user_agent: str = f"ThuyetMinhOfflineGPU v{__version__}",
        embedded_probe: EmbeddedSubtitleProbe | None = None,
        timeout_seconds: float = 20.0,
        max_download_bytes: int = 20 * 1024 * 1024,
        max_archive_entries: int = 20,
        max_uncompressed_bytes: int = 40 * 1024 * 1024,
    ) -> None:
        self._client = client
        self._api_key = opensubtitles_api_key
        self._token = opensubtitles_token
        try:
            self._api_root = normalize_opensubtitles_api_root(
                opensubtitles_base_url
            )
        except ValueError as exc:
            raise ValueError("OpenSubtitles API URL không hợp lệ") from exc
        self._user_agent = user_agent
        self._probe = embedded_probe or FfprobeSubtitleProbe()
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_download_bytes = max_download_bytes
        self._max_archive_entries = max_archive_entries
        self._max_uncompressed_bytes = max_uncompressed_bytes
        self._known_candidates: dict[str, SubtitleCandidate] = {}

    async def find(self, media: MediaAsset) -> tuple[SubtitleCandidate, ...]:
        if not media.path.is_file():
            raise _subtitle_error("Không tìm thấy file media để dò phụ đề", retryable=False)
        local, embedded = await asyncio.gather(
            self._find_sidecars(media),
            self._find_embedded(media),
        )
        remote = (
            await self._find_remote(media)
            if self._api_key and self._token
            else ()
        )
        candidates = sorted(
            (*embedded, *local, *remote),
            key=lambda item: (item.score, item.high_confidence, not item.hearing_impaired),
            reverse=True,
        )
        unique: dict[str, SubtitleCandidate] = {}
        for candidate in candidates:
            unique.setdefault(candidate.subtitle_id, candidate)
        result = tuple(unique.values())
        self._known_candidates.update({item.subtitle_id: item for item in result})
        return result

    async def materialize(
        self,
        media: MediaAsset,
        candidate: SubtitleCandidate,
        destination: Path,
    ) -> Path:
        known = self._known_candidates.get(candidate.subtitle_id)
        if known != candidate:
            raise _subtitle_error("Phụ đề chưa được chọn từ kết quả hợp lệ", retryable=False)
        if destination.exists() and destination.is_symlink():
            raise _unsafe_archive_error("Đường dẫn đích phụ đề không an toàn")
        destination.parent.mkdir(parents=True, exist_ok=True)

        if candidate.source is SubtitleSource.EMBEDDED:
            if candidate.embedded_stream_index is None:
                raise _subtitle_error("Thiếu chỉ số phụ đề nhúng", retryable=False)
            output = await self._probe.extract(
                media.path, candidate.embedded_stream_index, destination.with_suffix(".srt")
            )
            await asyncio.to_thread(inspect_subtitle_file, output, media.duration_us)
            return output

        if candidate.source is SubtitleSource.SIDECAR:
            if candidate.local_path is None or not candidate.local_path.is_file():
                raise _subtitle_error("Không tìm thấy file phụ đề cục bộ", retryable=False)
            suffix = _canonical_suffix(candidate.format)
            output = destination.with_suffix(suffix)
            await asyncio.to_thread(_copy_and_validate, candidate.local_path, output, media.duration_us)
            return output

        if candidate.remote_file_id is None:
            raise _subtitle_error("Thiếu mã file OpenSubtitles", retryable=False)
        payload, filename = await self._download_remote(candidate.remote_file_id)
        subtitle_bytes, subtitle_format = _unpack_download(
            payload,
            filename=filename,
            preferred_format=candidate.format,
            max_entries=self._max_archive_entries,
            max_uncompressed_bytes=self._max_uncompressed_bytes,
        )
        inspect_subtitle_bytes(subtitle_bytes, subtitle_format, media.duration_us)
        output = destination.with_suffix(_canonical_suffix(subtitle_format))
        await asyncio.to_thread(_atomic_write, output, subtitle_bytes)
        return output

    async def _find_embedded(self, media: MediaAsset) -> tuple[SubtitleCandidate, ...]:
        tracks = await self._probe.inspect(media.path)
        requested_language = _normalize_language(media.source_language)
        candidates: list[SubtitleCandidate] = []
        for track in tracks:
            if track.language != requested_language:
                continue
            identity = f"embedded\0{media.path.resolve()}\0{track.stream_index}"
            candidates.append(
                SubtitleCandidate(
                    subtitle_id=_candidate_id(identity),
                    source=SubtitleSource.EMBEDDED,
                    language=track.language,
                    format=track.format,
                    score=100 if not track.forced else 45,
                    high_confidence=not track.forced,
                    release_name=track.title,
                    hearing_impaired=track.hearing_impaired,
                    forced=track.forced,
                    embedded_stream_index=track.stream_index,
                    matched_by="embedded_language",
                )
            )
        return tuple(candidates)

    async def _find_sidecars(self, media: MediaAsset) -> tuple[SubtitleCandidate, ...]:
        return await asyncio.to_thread(self._find_sidecars_sync, media)

    def _find_sidecars_sync(self, media: MediaAsset) -> tuple[SubtitleCandidate, ...]:
        requested_language = _normalize_language(media.source_language)
        media_stem = _safe_stem(media.path.stem)
        media_directory = media.path.parent.resolve(strict=False)
        candidates: list[SubtitleCandidate] = []
        for path in media.path.parent.iterdir():
            subtitle_format = _SUBTITLE_SUFFIXES.get(path.suffix.lower())
            resolved = path.resolve(strict=False)
            if (
                subtitle_format is None
                or path.is_symlink()
                or not resolved.is_relative_to(media_directory)
                or not resolved.is_file()
            ):
                continue
            candidate_stem = _safe_stem(path.stem)
            if not _is_sidecar_for(path.stem, media.path.stem):
                continue
            language = _filename_language(path.stem)
            language_match = language == requested_language
            exact_unnamed = candidate_stem == media_stem
            if language is not None and not language_match:
                continue
            try:
                inspect_subtitle_file(resolved, media.duration_us)
            except AcquisitionError:
                continue
            high_confidence = bool(language_match and not _looks_forced(path.stem))
            score = 96 if high_confidence else (72 if exact_unnamed else 60)
            identity = f"sidecar\0{resolved}"
            candidates.append(
                SubtitleCandidate(
                    subtitle_id=_candidate_id(identity),
                    source=SubtitleSource.SIDECAR,
                    language=language or requested_language or media.source_language,
                    format=subtitle_format,
                    score=score,
                    high_confidence=high_confidence,
                    release_name=path.name,
                    forced=_looks_forced(path.stem),
                    local_path=resolved,
                    matched_by="release_filename" if language_match else "filename_only",
                )
            )
        return tuple(candidates)

    async def _find_remote(self, media: MediaAsset) -> tuple[SubtitleCandidate, ...]:
        movie_hash = await asyncio.to_thread(compute_opensubtitles_hash, media.path)
        size = media.path.stat().st_size
        if movie_hash is not None:
            exact = await self._search_remote(
                {
                    "languages": _opensubtitles_language(media.source_language),
                    "moviehash": movie_hash,
                    "moviebytesize": str(size),
                },
                requested_language=media.source_language,
                matched_by="moviehash",
                exact=True,
                media=media,
            )
            if exact:
                return exact

        parameters: dict[str, str] = {
            "languages": _opensubtitles_language(media.source_language),
        }
        matched_by = "title_year"
        if media.imdb_id and media.imdb_id.lower().removeprefix("tt").isdigit():
            parameters["imdb_id"] = media.imdb_id.lower().removeprefix("tt")
            matched_by = "imdb_id"
        elif media.tmdb_id is not None:
            parameters["tmdb_id"] = str(media.tmdb_id)
            matched_by = "tmdb_id"
        else:
            parameters["query"] = media.title
            if media.year is not None:
                parameters["year"] = str(media.year)
            parameters["type"] = "movie" if media.media_kind is MediaKind.MOVIE else "episode"
        return await self._search_remote(
            parameters,
            requested_language=media.source_language,
            matched_by=matched_by,
            exact=False,
            media=media,
        )

    async def _search_remote(
        self,
        parameters: dict[str, str],
        *,
        requested_language: str,
        matched_by: str,
        exact: bool,
        media: MediaAsset,
    ) -> tuple[SubtitleCandidate, ...]:
        # OpenSubtitles canonicalizes search URLs by sorting query keys and
        # lower-casing the free-text query. Sending that canonical form avoids
        # a 301 HTML response which must never be parsed as an API payload.
        canonical_parameters = {
            key: (value.lower() if key == "query" else value)
            for key, value in sorted(parameters.items())
        }
        response = await self._api_request(
            "GET",
            "/subtitles",
            params=canonical_parameters,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise _subtitle_error("OpenSubtitles trả về dữ liệu không hợp lệ", retryable=True) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise _subtitle_error("OpenSubtitles trả về cấu trúc không hợp lệ", retryable=True)
        requested = _normalize_language(requested_language)
        results: list[SubtitleCandidate] = []
        for record in data:
            if not isinstance(record, dict) or not isinstance(record.get("attributes"), dict):
                continue
            attributes = record["attributes"]
            language = _normalize_language(_string(attributes.get("language")))
            if language is None or language != requested:
                continue
            files = attributes.get("files")
            if not isinstance(files, list):
                continue
            forced = bool(attributes.get("foreign_parts_only", False))
            release = _string(attributes.get("release")) or None
            fps = _positive_float(attributes.get("fps"))
            fps_match = bool(
                fps is not None and media.fps is not None and abs(fps - media.fps) <= 0.05
            )
            score = 100 if exact else (88 if matched_by in {"imdb_id", "tmdb_id"} else 68)
            if fps_match:
                score += 4
            if bool(attributes.get("from_trusted", False)):
                score += 2
            if forced:
                score = min(score, 45)
            for file_record in files:
                if not isinstance(file_record, dict):
                    continue
                file_id = _int(file_record.get("file_id"))
                filename = _string(file_record.get("file_name"))
                if file_id is None:
                    continue
                subtitle_format = _format_from_name(filename) or SubtitleFormat.SRT
                identity = f"opensubtitles\0{file_id}"
                results.append(
                    SubtitleCandidate(
                        subtitle_id=_candidate_id(identity),
                        source=SubtitleSource.OPENSUBTITLES,
                        language=language,
                        format=subtitle_format,
                        score=score,
                        high_confidence=exact and not forced,
                        release_name=release or filename or None,
                        fps=fps,
                        hearing_impaired=bool(attributes.get("hearing_impaired", False)),
                        forced=forced,
                        remote_file_id=file_id,
                        matched_by=matched_by,
                    )
                )
        return tuple(results)

    async def _download_remote(self, file_id: int) -> tuple[bytes, str]:
        response = await self._api_request(
            "POST", "/download", json_body={"file_id": file_id}
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise _subtitle_error("OpenSubtitles trả về liên kết tải không hợp lệ", retryable=True) from exc
        link = _string(payload.get("link")) if isinstance(payload, dict) else ""
        filename = _string(payload.get("file_name")) if isinstance(payload, dict) else ""
        parsed = urlsplit(link)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise _subtitle_error("OpenSubtitles trả về liên kết tải không an toàn", retryable=False)
        try:
            async with self._client.stream(
                "GET", link, timeout=self._timeout, follow_redirects=True
            ) as download:
                final_url = urlsplit(str(download.url))
                if (
                    final_url.scheme != "https"
                    or not final_url.netloc
                    or final_url.username
                    or final_url.password
                ):
                    raise _subtitle_error(
                        "Máy chủ phụ đề chuyển hướng tới liên kết không an toàn",
                        retryable=False,
                    )
                if download.status_code >= 400:
                    raise _subtitle_error(
                        "Không thể tải file phụ đề từ OpenSubtitles",
                        retryable=download.status_code >= 500 or download.status_code == 429,
                    )
                declared_size = _int(download.headers.get("Content-Length"))
                if declared_size is not None and declared_size > self._max_download_bytes:
                    raise _unsafe_archive_error("File phụ đề vượt giới hạn kích thước")
                chunks: list[bytes] = []
                received = 0
                async for chunk in download.aiter_bytes():
                    received += len(chunk)
                    if received > self._max_download_bytes:
                        raise _unsafe_archive_error("File phụ đề vượt giới hạn kích thước")
                    chunks.append(chunk)
        except AcquisitionError:
            raise
        except httpx.TimeoutException as exc:
            raise _subtitle_error("Tải phụ đề quá thời gian cho phép", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise _subtitle_error("Không thể kết nối máy chủ tải phụ đề", retryable=True) from exc
        return b"".join(chunks), filename

    async def _api_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> httpx.Response:
        headers = {
            "Api-Key": self._api_key or "",
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = await self._client.request(
                method,
                f"{self._api_root}{path}",
                params=params,
                json=json_body,
                headers=headers,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise _subtitle_error("OpenSubtitles phản hồi quá thời gian cho phép", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise _subtitle_error("Không thể kết nối tới OpenSubtitles", retryable=True) from exc
        if response.status_code in {401, 403}:
            raise _subtitle_error("OpenSubtitles từ chối thông tin xác thực", retryable=False)
        if response.status_code == 429 or response.status_code >= 500:
            raise _subtitle_error("OpenSubtitles tạm thời không khả dụng", retryable=True)
        if 300 <= response.status_code < 400:
            # The API key and bearer token must not be forwarded through an
            # implicit redirect. Search requests are already canonicalized;
            # any remaining redirect is unexpected and safe to retry later.
            raise _subtitle_error(
                "OpenSubtitles trả về chuyển hướng không mong đợi",
                retryable=True,
            )
        if response.status_code >= 400:
            raise _subtitle_error("OpenSubtitles từ chối yêu cầu", retryable=False)
        return response


def compute_opensubtitles_hash(path: Path) -> str | None:
    """Compute the official 64-bit first/last-64-KiB OpenSubtitles hash."""

    size = path.stat().st_size
    chunk_size = 64 * 1024
    if size < chunk_size * 2:
        return None
    checksum = size
    with path.open("rb") as stream:
        first = stream.read(chunk_size)
        stream.seek(-chunk_size, os.SEEK_END)
        last = stream.read(chunk_size)
    for offset in range(0, chunk_size, 8):
        checksum = (checksum + struct.unpack_from("<Q", first, offset)[0]) & 0xFFFFFFFFFFFFFFFF
        checksum = (checksum + struct.unpack_from("<Q", last, offset)[0]) & 0xFFFFFFFFFFFFFFFF
    return f"{checksum:016x}"


def inspect_subtitle_file(
    path: Path,
    media_duration_us: int,
    *,
    max_bytes: int = _MAX_LOCAL_SUBTITLE_BYTES,
) -> SubtitleInspection:
    try:
        if path.stat().st_size > max_bytes:
            raise _unsafe_archive_error("File phụ đề vượt giới hạn kích thước")
        with path.open("rb") as stream:
            payload = stream.read(max_bytes + 1)
    except OSError as exc:
        raise _subtitle_error("Không thể đọc file phụ đề", retryable=False) from exc
    if len(payload) > max_bytes:
        raise _unsafe_archive_error("File phụ đề vượt giới hạn kích thước")
    subtitle_format = _format_from_name(path.name)
    if subtitle_format is None:
        raise _subtitle_error("Định dạng phụ đề không được hỗ trợ", retryable=False)
    return inspect_subtitle_bytes(payload, subtitle_format, media_duration_us)


def inspect_subtitle_bytes(
    payload: bytes,
    subtitle_format: SubtitleFormat,
    media_duration_us: int,
) -> SubtitleInspection:
    text = _decode_subtitle(payload)
    timings = _parse_timings(text, subtitle_format)
    if not timings:
        raise _subtitle_error("Phụ đề không có mốc thời gian hợp lệ", retryable=False)
    invalid = sum(1 for start, end in timings if start < 0 or end <= start)
    if invalid > max(1, math.ceil(len(timings) * 0.1)):
        raise _subtitle_error("Phụ đề có quá nhiều mốc thời gian lỗi", retryable=False)
    valid = [(start, end) for start, end in timings if start >= 0 and end > start]
    tolerance = max(5_000_000, int(media_duration_us * 0.02))
    if not valid or max(end for _, end in valid) > media_duration_us + tolerance:
        raise _subtitle_error("Mốc thời gian phụ đề vượt quá thời lượng media", retryable=False)
    ordered = sorted(valid)
    overlaps = sum(
        1 for previous, current in itertools.pairwise(ordered) if current[0] < previous[1]
    )
    if overlaps > max(5, math.ceil(len(ordered) * 0.5)):
        raise _subtitle_error("Phụ đề có quá nhiều đoạn chồng lấn", retryable=False)
    return SubtitleInspection(
        format=subtitle_format,
        cue_count=len(valid),
        first_start_us=min(start for start, _ in valid),
        last_end_us=max(end for _, end in valid),
        overlap_count=overlaps,
    )


def _parse_timings(text: str, subtitle_format: SubtitleFormat) -> list[tuple[int, int]]:
    if subtitle_format in {SubtitleFormat.SRT, SubtitleFormat.VTT}:
        result: list[tuple[int, int]] = []
        for match in _TIMING_PATTERN.finditer(text):
            try:
                result.append((_clock_to_us(match.group("start")), _clock_to_us(match.group("end"))))
            except ValueError:
                continue
        return result

    result = []
    for line in text.splitlines():
        if not line.lstrip().lower().startswith("dialogue:"):
            continue
        fields = line.split(",", 3)
        if len(fields) < 3:
            continue
        try:
            result.append((_ass_clock_to_us(fields[1]), _ass_clock_to_us(fields[2])))
        except ValueError:
            continue
    return result


def _clock_to_us(value: str) -> int:
    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = 0
        minutes_text, seconds_text = parts
    elif len(parts) == 3:
        hours = int(parts[0])
        minutes_text, seconds_text = parts[1:]
    else:
        raise ValueError("invalid clock")
    minutes = int(minutes_text)
    seconds = float(seconds_text)
    if minutes >= 60 or seconds >= 60:
        raise ValueError("invalid clock")
    return round((hours * 3600 + minutes * 60 + seconds) * 1_000_000)


def _ass_clock_to_us(value: str) -> int:
    return _clock_to_us(value.strip())


def _decode_subtitle(payload: bytes) -> str:
    if b"\x00" not in payload[:4]:
        try:
            return payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-16", "cp1258", "cp1252"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise _subtitle_error("Encoding phụ đề không được hỗ trợ", retryable=False)


def _unpack_download(
    payload: bytes,
    *,
    filename: str,
    preferred_format: SubtitleFormat,
    max_entries: int,
    max_uncompressed_bytes: int,
) -> tuple[bytes, SubtitleFormat]:
    if not payload:
        raise _subtitle_error("File phụ đề tải về đang trống", retryable=True)
    if not payload.startswith(b"PK\x03\x04"):
        subtitle_format = _format_from_name(filename) or preferred_format
        return payload, subtitle_format
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise _unsafe_archive_error("Archive phụ đề bị hỏng") from exc
    with archive:
        entries = archive.infolist()
        if not entries or len(entries) > max_entries:
            raise _unsafe_archive_error("Archive phụ đề có số lượng file không an toàn")
        total_size = 0
        candidates: list[zipfile.ZipInfo] = []
        for entry in entries:
            normalized_name = entry.filename.replace("\\", "/")
            pure_path = PurePosixPath(normalized_name)
            if (
                pure_path.is_absolute()
                or ".." in pure_path.parts
                or (pure_path.parts and ":" in pure_path.parts[0])
                or entry.flag_bits & 0x1
            ):
                raise _unsafe_archive_error("Archive phụ đề chứa đường dẫn hoặc file không an toàn")
            if entry.is_dir():
                continue
            total_size += entry.file_size
            if total_size > max_uncompressed_bytes:
                raise _unsafe_archive_error("Archive phụ đề vượt giới hạn giải nén")
            if entry.file_size > 0 and entry.compress_size == 0:
                raise _unsafe_archive_error("Archive phụ đề có tỷ lệ nén bất thường")
            if entry.compress_size and entry.file_size / entry.compress_size > 200:
                raise _unsafe_archive_error("Archive phụ đề có tỷ lệ nén bất thường")
            if _format_from_name(entry.filename) is not None:
                candidates.append(entry)
        if not candidates:
            raise _subtitle_error("Archive không chứa định dạng phụ đề được hỗ trợ", retryable=False)
        candidates.sort(
            key=lambda item: (
                _format_from_name(item.filename) is preferred_format,
                -len(PurePosixPath(item.filename).parts),
                item.file_size,
            ),
            reverse=True,
        )
        selected = candidates[0]
        subtitle_format = _format_from_name(selected.filename)
        assert subtitle_format is not None
        extracted = archive.read(selected)
        if len(extracted) != selected.file_size:
            raise _unsafe_archive_error("Archive phụ đề giải nén không đầy đủ")
        return extracted, subtitle_format


def _copy_and_validate(source: Path, destination: Path, media_duration_us: int) -> None:
    inspect_subtitle_file(source, media_duration_us)
    temporary = destination.with_name(f".{destination.name}.part")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write(destination: Path, payload: bytes) -> None:
    temporary = destination.with_name(f".{destination.name}.part")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _format_from_name(filename: str) -> SubtitleFormat | None:
    return _SUBTITLE_SUFFIXES.get(Path(filename).suffix.lower())


def _codec_format(codec_name: str) -> SubtitleFormat | None:
    return {
        "subrip": SubtitleFormat.SRT,
        "srt": SubtitleFormat.SRT,
        "webvtt": SubtitleFormat.VTT,
        "ass": SubtitleFormat.ASS,
        "ssa": SubtitleFormat.ASS,
    }.get(codec_name.lower())


def _canonical_suffix(subtitle_format: SubtitleFormat) -> str:
    return f".{subtitle_format.value}"


def _normalize_language(language: str) -> str | None:
    normalized = language.strip().lower().replace("_", "-").split("-", 1)[0]
    return _LANGUAGE_ALIASES.get(normalized, normalized if len(normalized) in {2, 3} else None)


def _opensubtitles_language(language: str) -> str:
    return _normalize_language(language) or language.strip().lower()


def _filename_language(stem: str) -> str | None:
    tokens = re.split(r"[. _\-\[\]()]+", stem.lower())
    for token in reversed(tokens):
        language = _LANGUAGE_ALIASES.get(token)
        if language is not None:
            return language
    return None


def _safe_stem(value: str) -> str:
    return _SAFE_NAME_PATTERN.sub("", value.lower())


def _is_sidecar_for(candidate_stem: str, media_stem: str) -> bool:
    candidate = candidate_stem.casefold()
    media = media_stem.casefold()
    if candidate == media:
        return True
    return any(candidate.startswith(f"{media}{separator}") for separator in (".", "_", "-", " "))


def _looks_forced(stem: str) -> bool:
    return "forced" in re.split(r"[. _\-\[\]()]+", stem.lower())


def _candidate_id(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 and math.isfinite(parsed) else None


def _subtitle_error(message: str, *, retryable: bool) -> AcquisitionError:
    return AcquisitionError(
        AcquisitionErrorCode.SUBTITLE_UNAVAILABLE,
        message,
        retryable=retryable,
    )


def _unsafe_archive_error(message: str) -> AcquisitionError:
    return AcquisitionError(
        AcquisitionErrorCode.SUBTITLE_ARCHIVE_UNSAFE,
        message,
        retryable=False,
    )
