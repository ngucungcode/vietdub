"""qBittorrent WebUI API adapter with explicit authentication and timeouts."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx

from dub_server.domain import (
    AcquisitionError,
    AcquisitionErrorCode,
    DownloadClient,
    DownloadedFile,
    DownloadState,
    DownloadStatus,
    DownloadTask,
    ReleaseCandidate,
)

_HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


class QBittorrentDownloadClient(DownloadClient):
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        client: httpx.AsyncClient,
        timeout_seconds: float = 15.0,
        discovery_timeout_seconds: float = 10.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("qBittorrent base URL không hợp lệ")
        if not username or not password:
            raise ValueError("Thông tin xác thực qBittorrent không được để trống")
        self._base_url = base_url.rstrip("/")
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._username = username
        self._password = password
        self._client = client
        self._timeout = httpx.Timeout(timeout_seconds)
        self._discovery_timeout = max(0.0, discovery_timeout_seconds)
        self._authenticated = False
        self._auth_lock = asyncio.Lock()
        self._major_version: int | None = None
        self._version_lock = asyncio.Lock()

    async def add(
        self,
        release: ReleaseCandidate,
        save_path: Path,
        *,
        paused: bool = False,
    ) -> DownloadTask:
        source_scheme = urlsplit(release.download_uri).scheme.lower()
        if source_scheme not in {"http", "https", "magnet"}:
            raise AcquisitionError(
                AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                "Nguồn tải không được hỗ trợ",
                retryable=False,
            )
        if not save_path.is_absolute():
            raise AcquisitionError(
                AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                "Thư mục tải phải là đường dẫn tuyệt đối",
                retryable=False,
            )

        tag_digest = hashlib.sha256(release.release_id.encode()).hexdigest()[:16]
        tag = f"dub-{tag_digest}"
        task_id = _normalize_hash(release.info_hash)
        existing_task_id = await self._find_owned_hash({"tag": tag})
        if existing_task_id is None and task_id is not None:
            existing_task_id = await self._find_owned_hash({"hashes": task_id})
        if existing_task_id is not None:
            await self.relocate(existing_task_id, save_path)
            await self._set_running(existing_task_id, running=not paused)
            return DownloadTask(
                task_id=existing_task_id,
                name=release.title,
                save_path=save_path,
            )
        response = await self._post(
            "/api/v2/torrents/add",
            data={
                "urls": release.download_uri,
                "savepath": save_path.as_posix(),
                "paused": str(paused).lower(),
                "tags": tag,
            },
            multipart=True,
        )
        if response.status_code != 200 or response.text.strip().lower() not in {"ok.", "ok"}:
            raise AcquisitionError(
                AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                "qBittorrent không chấp nhận nội dung đã chọn",
                retryable=response.status_code >= 500,
            )

        # qBittorrent's canonical WebAPI hash can differ from an upstream v1
        # info-hash for BitTorrent v2/hybrid torrents. Resolve the exact tagged
        # task after add instead of trusting provider metadata.
        task_id = await self._discover_hash(tag)
        await self.relocate(task_id, save_path)
        await self._set_running(task_id, running=not paused)
        return DownloadTask(task_id=task_id, name=release.title, save_path=save_path)

    async def relocate(self, task_id: str, save_path: Path) -> None:
        torrent_hash = _required_hash(task_id)
        if not save_path.is_absolute():
            raise AcquisitionError(
                AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                "Thư mục tải phải là đường dẫn tuyệt đối",
                retryable=False,
            )
        await self._post(
            "/api/v2/torrents/setLocation",
            data={"hashes": torrent_hash, "location": save_path.as_posix()},
        )

    async def status(self, task_id: str) -> DownloadStatus:
        torrent_hash = _required_hash(task_id)
        response = await self._get(
            "/api/v2/torrents/info",
            params={"hashes": torrent_hash},
        )
        payload = _json_list(response, "qBittorrent trả về trạng thái không hợp lệ")
        if not payload:
            raise AcquisitionError(
                AcquisitionErrorCode.DOWNLOAD_NOT_FOUND,
                "Không tìm thấy tác vụ tải trong qBittorrent",
                retryable=False,
            )
        raw = payload[0]
        state_name = _string(raw.get("state"))
        state = _map_state(state_name)
        total = _non_negative_int(raw.get("size")) or _non_negative_int(raw.get("total_size")) or 0
        downloaded = _non_negative_int(raw.get("completed")) or 0
        progress = _float(raw.get("progress"), default=(downloaded / total if total else 0.0))
        if progress >= 0.999999 and state not in {DownloadState.FAILED, DownloadState.CHECKING}:
            state = DownloadState.COMPLETED
        eta_raw = _non_negative_int(raw.get("eta"))
        eta = eta_raw if eta_raw is not None and eta_raw < 8_640_000 else None
        return DownloadStatus(
            task_id=torrent_hash,
            state=state,
            progress=min(1.0, max(0.0, progress)),
            downloaded_bytes=downloaded,
            total_bytes=total,
            speed_bytes_per_second=_non_negative_int(raw.get("dlspeed")) or 0,
            eta_seconds=eta,
            error_message="qBittorrent báo lỗi dữ liệu tải" if state is DownloadState.FAILED else None,
        )

    async def files(self, task_id: str) -> tuple[DownloadedFile, ...]:
        torrent_hash = _required_hash(task_id)
        response = await self._get(
            "/api/v2/torrents/files",
            params={"hash": torrent_hash},
        )
        payload = _json_list(response, "qBittorrent trả về danh sách file không hợp lệ")
        files: list[DownloadedFile] = []
        for raw in payload:
            name = _string(raw.get("name"))
            relative = PurePosixPath(name)
            if (
                not name
                or relative.is_absolute()
                or ".." in relative.parts
                or (relative.parts and ":" in relative.parts[0])
            ):
                raise AcquisitionError(
                    AcquisitionErrorCode.INVALID_RESPONSE,
                    "qBittorrent trả về đường dẫn file không an toàn",
                    retryable=False,
                )
            files.append(
                DownloadedFile(
                    relative_path=Path(*relative.parts),
                    size_bytes=_non_negative_int(raw.get("size")) or 0,
                    progress=min(1.0, max(0.0, _float(raw.get("progress"), default=0.0))),
                )
            )
        return tuple(files)

    async def cancel(self, task_id: str, *, delete_files: bool = False) -> None:
        torrent_hash = _required_hash(task_id)
        response = await self._post(
            "/api/v2/torrents/delete",
            data={"hashes": torrent_hash, "deleteFiles": str(delete_files).lower()},
        )
        if response.status_code != 200:
            raise AcquisitionError(
                AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                "Không thể hủy tác vụ qBittorrent",
                retryable=response.status_code >= 500,
            )

    async def pause(self, task_id: str) -> None:
        await self._set_running(task_id, running=False)

    async def resume(self, task_id: str) -> None:
        await self._set_running(task_id, running=True)

    async def _set_running(self, task_id: str, *, running: bool) -> None:
        torrent_hash = _required_hash(task_id)
        major_version = await self._get_major_version()
        if major_version >= 5:
            endpoint = "/api/v2/torrents/start" if running else "/api/v2/torrents/stop"
        else:
            endpoint = "/api/v2/torrents/resume" if running else "/api/v2/torrents/pause"
        response = await self._post(endpoint, data={"hashes": torrent_hash})
        if response.status_code != 200:
            action = "tiếp tục" if running else "tạm dừng"
            raise AcquisitionError(
                AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                f"Không thể {action} tác vụ qBittorrent",
                retryable=response.status_code >= 500,
            )

    async def _get_major_version(self) -> int:
        """Negotiate the pause/resume API used by qBittorrent 4 and 5."""

        if self._major_version is not None:
            return self._major_version
        async with self._version_lock:
            if self._major_version is not None:
                return self._major_version
            response = await self._get("/api/v2/app/version", params={})
            match = re.search(r"(?:^|[^0-9])(\d+)(?:\.|$)", response.text.strip())
            if match is None:
                raise AcquisitionError(
                    AcquisitionErrorCode.INVALID_RESPONSE,
                    "qBittorrent trả về phiên bản không hợp lệ",
                    retryable=False,
                )
            major_version = int(match.group(1))
            if major_version < 4:
                raise AcquisitionError(
                    AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                    "Phiên bản qBittorrent không được hỗ trợ",
                    retryable=False,
                )
            self._major_version = major_version
            return major_version

    async def _discover_hash(self, tag: str) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._discovery_timeout
        while True:
            response = await self._get("/api/v2/torrents/info", params={"tag": tag})
            payload = _json_list(response, "qBittorrent trả về tác vụ không hợp lệ")
            hashes = [
                normalized
                for raw in payload
                if (normalized := _normalize_hash(_string(raw.get("hash")))) is not None
            ]
            if len(hashes) == 1:
                return hashes[0]
            if len(hashes) > 1:
                raise AcquisitionError(
                    AcquisitionErrorCode.INVALID_RESPONSE,
                    "Không thể xác định duy nhất tác vụ qBittorrent vừa tạo",
                    retryable=False,
                )
            if loop.time() >= deadline:
                raise AcquisitionError(
                    AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                    "qBittorrent chưa công bố mã tác vụ tải",
                    retryable=True,
                )
            await asyncio.sleep(0.2)

    async def _find_owned_hash(self, params: Mapping[str, str]) -> str | None:
        response = await self._get("/api/v2/torrents/info", params=params)
        payload = _json_list(
            response,
            "qBittorrent trả về tác vụ không hợp lệ",
        )
        if not payload:
            return None
        hashes = {
            normalized
            for item in payload
            if (normalized := _normalize_hash(_string(item.get("hash")))) is not None
        }
        if len(payload) != 1 or len(hashes) != 1:
            raise AcquisitionError(
                AcquisitionErrorCode.INVALID_RESPONSE,
                "Không thể xác định duy nhất torrent đã có",
                retryable=False,
            )
        tags = {
            tag.strip()
            for tag in _string(payload[0].get("tags")).split(",")
            if tag.strip()
        }
        if not any(tag.startswith("dub-") for tag in tags):
            raise AcquisitionError(
                AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                "Torrent đã tồn tại nhưng không thuộc ứng dụng này",
                retryable=False,
            )
        return next(iter(hashes))

    async def _ensure_authenticated(self) -> None:
        if self._authenticated:
            return
        async with self._auth_lock:
            if self._authenticated:
                return
            try:
                response = await self._client.post(
                    f"{self._base_url}/api/v2/auth/login",
                    data={"username": self._username, "password": self._password},
                    headers=self._request_headers,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as exc:
                raise _download_error("qBittorrent phản hồi quá thời gian cho phép") from exc
            except httpx.HTTPError as exc:
                raise _download_error("Không thể kết nối tới qBittorrent") from exc
            if response.status_code != 200 or response.text.strip().lower() not in {"ok.", "ok"}:
                raise AcquisitionError(
                    AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                    "qBittorrent từ chối thông tin xác thực",
                    retryable=False,
                )
            self._authenticated = True

    async def _get(self, path: str, *, params: Mapping[str, str]) -> httpx.Response:
        for attempt in range(2):
            await self._ensure_authenticated()
            try:
                response = await self._client.get(
                    f"{self._base_url}{path}",
                    params=params,
                    headers=self._request_headers,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as exc:
                raise _download_error("qBittorrent phản hồi quá thời gian cho phép") from exc
            except httpx.HTTPError as exc:
                raise _download_error("Không thể kết nối tới qBittorrent") from exc
            if response.status_code == 403 and attempt == 0:
                self._authenticated = False
                continue
            self._validate_response(response)
            return response
        raise AssertionError("qBittorrent authentication retry loop exhausted")

    async def _post(
        self,
        path: str,
        *,
        data: Mapping[str, str],
        multipart: bool = False,
    ) -> httpx.Response:
        for attempt in range(2):
            await self._ensure_authenticated()
            try:
                if multipart:
                    response = await self._client.post(
                        f"{self._base_url}{path}",
                        files={key: (None, value) for key, value in data.items()},
                        headers=self._request_headers,
                        timeout=self._timeout,
                    )
                else:
                    response = await self._client.post(
                        f"{self._base_url}{path}",
                        data=data,
                        headers=self._request_headers,
                        timeout=self._timeout,
                    )
            except httpx.TimeoutException as exc:
                raise _download_error("qBittorrent phản hồi quá thời gian cho phép") from exc
            except httpx.HTTPError as exc:
                raise _download_error("Không thể kết nối tới qBittorrent") from exc
            if response.status_code == 403 and attempt == 0:
                self._authenticated = False
                continue
            self._validate_response(response)
            return response
        raise AssertionError("qBittorrent authentication retry loop exhausted")

    @property
    def _request_headers(self) -> dict[str, str]:
        return {"Origin": self._origin, "Referer": self._origin}

    @staticmethod
    def _validate_response(response: httpx.Response) -> None:
        if response.status_code == 403:
            raise AcquisitionError(
                AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                "Phiên qBittorrent không còn hợp lệ",
                retryable=True,
            )
        if response.status_code >= 400:
            raise AcquisitionError(
                AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                "qBittorrent từ chối yêu cầu",
                retryable=response.status_code >= 500,
            )


def _download_error(message: str) -> AcquisitionError:
    return AcquisitionError(
        AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
        message,
        retryable=True,
    )


def _required_hash(value: str) -> str:
    normalized = _normalize_hash(value)
    if normalized is None:
        raise AcquisitionError(
            AcquisitionErrorCode.DOWNLOAD_NOT_FOUND,
            "Mã tác vụ tải không hợp lệ",
            retryable=False,
        )
    return normalized


def _normalize_hash(value: str | None) -> str | None:
    if value and _HASH_PATTERN.fullmatch(value):
        return value.lower()
    return None


def _json_list(response: httpx.Response, message: str) -> list[Mapping[str, Any]]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise AcquisitionError(
            AcquisitionErrorCode.INVALID_RESPONSE, message, retryable=True
        ) from exc
    if not isinstance(payload, list) or not all(isinstance(item, Mapping) for item in payload):
        raise AcquisitionError(AcquisitionErrorCode.INVALID_RESPONSE, message, retryable=True)
    return payload


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _float(value: object, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _map_state(state: str) -> DownloadState:
    normalized = state.lower()
    if normalized in {"error", "missingfiles"}:
        return DownloadState.FAILED
    if normalized in {"pausedup", "pauseddl", "stoppedup", "stoppeddl"}:
        return DownloadState.PAUSED
    if normalized in {"checkingup", "checkingdl", "checkingresumedata", "moving"}:
        return DownloadState.CHECKING
    if normalized in {"queuedup", "queueddl"}:
        return DownloadState.QUEUED
    if normalized in {
        "downloading",
        "forceddl",
        "metadl",
        "stalleddl",
        "allocating",
    }:
        return DownloadState.DOWNLOADING
    if normalized in {"uploading", "forcedup", "stalledup"}:
        return DownloadState.COMPLETED
    return DownloadState.UNKNOWN
