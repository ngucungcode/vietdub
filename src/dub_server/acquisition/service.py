"""Rights-gated orchestration for network acquisition."""

from __future__ import annotations

from pathlib import Path

from dub_server.domain import (
    AcquisitionError,
    AcquisitionErrorCode,
    DownloadClient,
    DownloadedFile,
    DownloadStatus,
    DownloadTask,
    IndexerGateway,
    MediaAsset,
    MediaKind,
    MediaQuery,
    ReleaseCandidate,
    SubtitleCandidate,
    SubtitleProvider,
)


class AcquisitionService:
    """Coordinates adapters while keeping the legal gate in one place."""

    def __init__(
        self,
        *,
        indexer: IndexerGateway,
        downloads: DownloadClient,
        subtitles: SubtitleProvider,
    ) -> None:
        self._indexer = indexer
        self._downloads = downloads
        self._subtitles = subtitles
        self._known_releases: dict[str, ReleaseCandidate] = {}
        self._release_queries: dict[str, MediaQuery] = {}

    async def search(
        self,
        query: str | MediaQuery,
        year: int | None = None,
        media_type: MediaKind | str = MediaKind.MOVIE,
    ) -> tuple[ReleaseCandidate, ...]:
        if isinstance(query, MediaQuery):
            media_query = query
        else:
            try:
                kind = media_type if isinstance(media_type, MediaKind) else MediaKind(media_type)
            except ValueError as exc:
                raise ValueError("Loại nội dung không được hỗ trợ") from exc
            media_query = MediaQuery(query=query, year=year, media_kind=kind)
        releases = await self._indexer.search(media_query)
        self._known_releases.update({item.release_id: item for item in releases})
        self._release_queries.update(
            {item.release_id: media_query for item in releases}
        )
        return releases

    def release(self, release_id: str) -> ReleaseCandidate | None:
        return self._known_releases.get(release_id)

    def release_query(self, release_id: str) -> MediaQuery | None:
        return self._release_queries.get(release_id)

    def restore_release(self, release: ReleaseCandidate) -> None:
        """Restore a server-persisted selection after an API process restart."""

        self._known_releases[release.release_id] = release

    async def start_download(
        self,
        release: ReleaseCandidate | str,
        save_path: Path,
        *,
        rights_confirmed: bool,
        paused: bool = False,
    ) -> DownloadTask:
        if rights_confirmed is not True:
            raise AcquisitionError(
                AcquisitionErrorCode.RIGHTS_CONFIRMATION_REQUIRED,
                "Bạn phải xác nhận có quyền tải và xử lý nội dung này",
                retryable=False,
            )
        release_id = release if isinstance(release, str) else release.release_id
        selected = self._known_releases.get(release_id)
        if selected is None or (not isinstance(release, str) and selected != release):
            raise AcquisitionError(
                AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                "Bản phát hành chưa được chọn từ kết quả tìm kiếm hợp lệ",
                retryable=False,
            )
        return await self._downloads.add(selected, save_path, paused=paused)

    async def find_subtitles(self, media: MediaAsset) -> tuple[SubtitleCandidate, ...]:
        return await self._subtitles.find(media)

    async def download_status(self, task_id: str) -> DownloadStatus:
        return await self._downloads.status(task_id)

    async def download_files(self, task_id: str) -> tuple[DownloadedFile, ...]:
        return await self._downloads.files(task_id)

    async def cancel_download(self, task_id: str, delete_files: bool = False) -> None:
        await self._downloads.cancel(task_id, delete_files=delete_files)

    async def pause_download(self, task_id: str) -> None:
        await self._downloads.pause(task_id)

    async def resume_download(self, task_id: str) -> None:
        await self._downloads.resume(task_id)

    async def relocate_download(self, task_id: str, save_path: Path) -> None:
        await self._downloads.relocate(task_id, save_path)

    async def materialize_subtitle(
        self,
        media: MediaAsset,
        candidate: SubtitleCandidate,
        destination: Path,
    ) -> Path:
        return await self._subtitles.materialize(media, candidate, destination)
