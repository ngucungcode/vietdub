"""Local-only administration helpers for external acquisition integrations.

This module deliberately exposes a very small projection of Prowlarr data and
never returns provider fields, URLs, response bodies, or authentication data.
OpenSubtitles credentials are validated before their API key and short-lived
token are committed to disk.  The account password is never persisted.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .opensubtitles import normalize_opensubtitles_api_root


class AdminIntegrationError(Exception):
    """A fully redacted error safe to expose through the local admin API."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


class ProwlarrAdminClient:
    """Constrained Prowlarr admin adapter with response allowlisting."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient,
        timeout_seconds: float = 20.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise AdminIntegrationError(
                status_code=503,
                code="prowlarr_configuration_invalid",
                message="Địa chỉ Prowlarr không hợp lệ",
                retryable=False,
            )
        if not api_key:
            raise AdminIntegrationError(
                status_code=503,
                code="prowlarr_not_configured",
                message="Prowlarr chưa được cấu hình",
                retryable=False,
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client
        self._timeout = httpx.Timeout(timeout_seconds)

    async def list_indexers(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/api/v1/indexer")
        try:
            payload = response.json()
        except ValueError as exc:
            raise _invalid_prowlarr_response() from exc
        if not isinstance(payload, list):
            raise _invalid_prowlarr_response()

        result: list[dict[str, Any]] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            indexer_id = _integer(raw.get("id"))
            name = _safe_text(raw.get("name"), max_length=300)
            if indexer_id is None or indexer_id < 1 or not name:
                continue
            status_payload = raw.get("status")
            status_record = status_payload if isinstance(status_payload, dict) else {}
            result.append(
                {
                    "id": indexer_id,
                    "name": name,
                    "definition_name": _optional_text(
                        raw.get("definitionName"), max_length=300
                    ),
                    "implementation_name": _optional_text(
                        raw.get("implementationName"), max_length=300
                    ),
                    "protocol": _optional_text(raw.get("protocol"), max_length=30),
                    "privacy": _optional_text(raw.get("privacy"), max_length=30),
                    "enabled": bool(raw.get("enable", False)),
                    "supports_search": bool(raw.get("supportsSearch", False)),
                    "supports_rss": bool(raw.get("supportsRss", False)),
                    "priority": _integer(raw.get("priority")),
                    "disabled_until": _optional_text(
                        status_record.get("disabledTill"), max_length=80
                    ),
                    "most_recent_failure": _optional_text(
                        status_record.get("mostRecentFailure"), max_length=80
                    ),
                }
            )
        result.sort(key=lambda item: (item["name"].casefold(), item["id"]))
        return result

    async def test_all(self) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/api/v1/indexer/testall",
            accepted_statuses={200, 204, 400},
            timeout_seconds=120.0,
        )
        if response.status_code in {200, 204} and not response.content.strip():
            return {
                "all_ok": True,
                "failed_count": 0,
                "results": [],
            }
        try:
            payload = response.json()
        except ValueError as exc:
            raise _invalid_prowlarr_response() from exc
        if not isinstance(payload, list):
            raise _invalid_prowlarr_response()

        results: list[dict[str, Any]] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            indexer_id = _integer(raw.get("id"))
            if indexer_id is None or indexer_id < 1:
                continue
            failures = raw.get("validationFailures")
            is_valid = raw.get("isValid")
            if not isinstance(is_valid, bool):
                is_valid = isinstance(failures, list) and not failures
            failure_count = len(failures) if isinstance(failures, list) else 0
            results.append(
                {
                    "indexer_id": indexer_id,
                    "ok": is_valid,
                    "failure_count": failure_count,
                }
            )
        results.sort(key=lambda item: item["indexer_id"])
        failed_count = sum(1 for item in results if not item["ok"])
        return {
            "all_ok": response.status_code in {200, 204} and failed_count == 0,
            "failed_count": failed_count,
            "results": results,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        accepted_statuses: set[int] | None = None,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers={"X-Api-Key": self._api_key, "Accept": "application/json"},
                timeout=(
                    httpx.Timeout(timeout_seconds)
                    if timeout_seconds is not None
                    else self._timeout
                ),
            )
        except httpx.TimeoutException as exc:
            raise AdminIntegrationError(
                status_code=503,
                code="prowlarr_timeout",
                message="Prowlarr phản hồi quá thời gian cho phép",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise AdminIntegrationError(
                status_code=503,
                code="prowlarr_unavailable",
                message="Không thể kết nối tới Prowlarr",
                retryable=True,
            ) from exc

        if accepted_statuses and response.status_code in accepted_statuses:
            return response
        if response.status_code in {401, 403}:
            raise AdminIntegrationError(
                status_code=503,
                code="prowlarr_auth_failed",
                message="Prowlarr từ chối khóa API đã cấu hình",
                retryable=False,
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise AdminIntegrationError(
                status_code=503,
                code="prowlarr_unavailable",
                message="Prowlarr đang tạm thời không khả dụng",
                retryable=True,
            )
        if response.status_code >= 400:
            raise AdminIntegrationError(
                status_code=502,
                code="prowlarr_request_rejected",
                message="Prowlarr từ chối yêu cầu quản trị",
                retryable=False,
            )
        return response


class OpenSubtitlesAdminClient:
    """Authenticate an OpenSubtitles account without retaining its password."""

    def __init__(
        self,
        *,
        api_url: str,
        user_agent: str,
        client: httpx.AsyncClient,
        timeout_seconds: float = 20.0,
    ) -> None:
        try:
            self._api_root = normalize_opensubtitles_api_root(api_url)
        except ValueError as exc:
            raise _invalid_opensubtitles_configuration() from exc
        self._user_agent = user_agent.strip()
        self._client = client
        self._timeout = httpx.Timeout(timeout_seconds)
        if not self._user_agent:
            raise AdminIntegrationError(
                status_code=503,
                code="opensubtitles_configuration_invalid",
                message="User-Agent OpenSubtitles chưa được cấu hình",
                retryable=False,
            )

    async def login_and_test(
        self,
        *,
        api_key: str,
        username: str,
        password: str,
    ) -> tuple[str, str, dict[str, int | bool | str | None]]:
        headers = {
            "Api-Key": api_key,
            "User-Agent": self._user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        login = await self._request(
            "POST",
            f"{self._api_root}/login",
            headers=headers,
            json_body={"username": username, "password": password},
        )
        try:
            login_payload = login.json()
        except ValueError as exc:
            raise _invalid_opensubtitles_response() from exc
        if not isinstance(login_payload, dict):
            raise _invalid_opensubtitles_response()
        token = login_payload.get("token")
        if not isinstance(token, str) or not token.strip() or len(token) > 16_384:
            raise _invalid_opensubtitles_response()
        token = token.strip()

        returned_base = login_payload.get("base_url")
        if not isinstance(returned_base, str) or not returned_base.strip():
            raise _invalid_opensubtitles_response()
        try:
            info_root = normalize_opensubtitles_api_root(returned_base)
        except ValueError as exc:
            raise _invalid_opensubtitles_response() from exc
        info = await self._request(
            "GET",
            f"{info_root}/infos/user",
            headers={
                "Api-Key": api_key,
                "Authorization": f"Bearer {token}",
                "User-Agent": self._user_agent,
                "Accept": "application/json",
            },
        )
        try:
            info_payload = info.json()
        except ValueError as exc:
            raise _invalid_opensubtitles_response() from exc
        data = info_payload.get("data") if isinstance(info_payload, dict) else None
        if not isinstance(data, dict):
            raise _invalid_opensubtitles_response()

        quota: dict[str, int | bool | str | None] = {
            "allowed_downloads": _integer(data.get("allowed_downloads")),
            "remaining_downloads": _integer(data.get("remaining_downloads")),
            "downloads_count": _integer(data.get("downloads_count")),
            "vip": bool(data.get("vip", False)),
            "level": _optional_text(data.get("level"), max_length=100),
        }
        return token, info_root, quota

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise AdminIntegrationError(
                status_code=503,
                code="opensubtitles_timeout",
                message="OpenSubtitles phản hồi quá thời gian cho phép",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise AdminIntegrationError(
                status_code=503,
                code="opensubtitles_unavailable",
                message="Không thể kết nối tới OpenSubtitles",
                retryable=True,
            ) from exc

        if response.status_code in {401, 403}:
            raise AdminIntegrationError(
                status_code=422,
                code="opensubtitles_auth_failed",
                message="OpenSubtitles từ chối thông tin đăng nhập",
                retryable=False,
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise AdminIntegrationError(
                status_code=503,
                code="opensubtitles_unavailable",
                message="OpenSubtitles đang tạm thời không khả dụng",
                retryable=True,
            )
        if response.status_code >= 400:
            raise AdminIntegrationError(
                status_code=422,
                code="opensubtitles_request_rejected",
                message="OpenSubtitles từ chối yêu cầu cấu hình",
                retryable=False,
            )
        return response


def atomic_write_secret_bundle(
    entries: Sequence[tuple[Path, str]],
) -> None:
    """Replace a secret set and roll back every partial set commit."""

    staged: list[tuple[Path, Path]] = []
    previous: dict[Path, bytes | None] = {}
    replaced: list[Path] = []
    try:
        normalized = tuple(entries)
        destinations = tuple(destination for destination, _value in normalized)
        if not normalized or len(set(destinations)) != len(destinations):
            raise OSError("secret paths must be non-empty and unique")
        if has_pending_secret_deletion(destinations):
            raise OSError("secret deletion cleanup is still pending")
        for destination, value in normalized:
            previous[destination] = _read_existing_secret(destination)
            staged.append((_stage_secret(destination, value), destination))
        for temporary, destination in staged:
            os.replace(temporary, destination)
            replaced.append(destination)
            os.chmod(destination, 0o600)
            _fsync_directory(destination.parent)
    except OSError as exc:
        for destination in reversed(replaced):
            old_payload = previous.get(destination)
            try:
                if old_payload is None:
                    destination.unlink(missing_ok=True)
                else:
                    rollback = _stage_secret_bytes(destination, old_payload)
                    try:
                        os.replace(rollback, destination)
                        os.chmod(destination, 0o600)
                    finally:
                        rollback.unlink(missing_ok=True)
                _fsync_directory(destination.parent)
            except OSError:
                # The primary typed failure remains safe. A subsequent request
                # will still report the store as unavailable for repair.
                pass
        raise AdminIntegrationError(
            status_code=503,
            code="secret_store_unavailable",
            message="Không thể lưu secret OpenSubtitles an toàn",
            retryable=True,
        ) from exc
    finally:
        for temporary, _destination in staged:
            with suppress(OSError):
                temporary.unlink()


def atomic_write_secret_pair(
    first_path: Path,
    first_value: str,
    second_path: Path,
    second_value: str,
) -> None:
    """Compatibility wrapper for a two-file secret transaction."""

    atomic_write_secret_bundle(
        ((first_path, first_value), (second_path, second_value))
    )


def delete_secret_bundle(paths: Sequence[Path]) -> None:
    """Transactionally deactivate a secret set, then clean its tombstones."""

    normalized = tuple(paths)
    if not normalized or len(set(normalized)) != len(normalized):
        raise AdminIntegrationError(
            status_code=503,
            code="secret_store_unavailable",
            message="Không thể xóa secret OpenSubtitles an toàn",
            retryable=True,
        )
    pairs = tuple(
        (path, _secret_deletion_tombstone(path))
        for path in normalized
    )
    all_paths = (*normalized, *(tombstone for _path, tombstone in pairs))
    if len(set(all_paths)) != len(all_paths):
        raise AdminIntegrationError(
            status_code=503,
            code="secret_store_unavailable",
            message="Không thể xóa secret OpenSubtitles an toàn",
            retryable=True,
        )
    try:
        for path, tombstone in pairs:
            _validate_secret_delete_path(path)
            _validate_secret_delete_path(tombstone)
    except OSError as exc:
        raise AdminIntegrationError(
            status_code=503,
            code="secret_store_unavailable",
            message="Không thể xóa secret OpenSubtitles an toàn",
            retryable=True,
        ) from exc

    # A tombstone next to a newly configured active file belongs to an older,
    # already committed deletion. Clean it before starting a new transaction.
    for path, tombstone in pairs:
        if not path.exists() or not tombstone.exists():
            continue
        try:
            os.unlink(tombstone)
            _fsync_directory(tombstone.parent)
        except OSError as exc:
            raise AdminIntegrationError(
                status_code=503,
                code="secret_cleanup_deferred",
                message="Chưa thể dọn file secret OpenSubtitles cũ",
                retryable=True,
            ) from exc

    active_before = tuple(path.exists() for path, _tombstone in pairs)
    pending_before = tuple(tombstone.exists() for _path, tombstone in pairs)
    rollback_safe = all(active_before) and not any(pending_before)
    renamed: list[tuple[Path, Path]] = []
    try:
        for path, tombstone in pairs:
            if tombstone.exists() or not path.exists():
                continue
            os.replace(path, tombstone)
            renamed.append((path, tombstone))
            _fsync_directory(path.parent)
    except OSError as exc:
        if rollback_safe:
            for path, tombstone in reversed(renamed):
                try:
                    os.replace(tombstone, path)
                    _fsync_directory(path.parent)
                except OSError:
                    # Keep the original typed failure. Retrying the delete is
                    # safe because deterministic tombstones are reconciled.
                    pass
        raise AdminIntegrationError(
            status_code=503,
            code="secret_store_unavailable",
            message="Không thể xóa secret OpenSubtitles an toàn",
            retryable=True,
        ) from exc

    # Both active paths are now absent, which is the commit point. Tombstone
    # cleanup may be retried without ever reactivating only half of the pair.
    cleanup_error: OSError | None = None
    for _path, tombstone in pairs:
        if not tombstone.exists():
            continue
        try:
            os.unlink(tombstone)
            _fsync_directory(tombstone.parent)
        except OSError as exc:
            cleanup_error = cleanup_error or exc
    if cleanup_error is not None:
        raise AdminIntegrationError(
            status_code=503,
            code="secret_cleanup_deferred",
            message="Đã ngắt cấu hình nhưng chưa thể dọn file secret cũ",
            retryable=True,
        ) from cleanup_error


def delete_secret_pair(first_path: Path, second_path: Path) -> None:
    """Compatibility wrapper for a two-file secret deletion."""

    delete_secret_bundle((first_path, second_path))


def _secret_deletion_tombstone(path: Path) -> Path:
    return path.with_name(f".{path.name}.delete-pending")


def _validate_secret_delete_path(path: Path) -> None:
    if path.is_symlink():
        raise OSError("secret path is a symbolic link")
    if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
        raise OSError("secret path is not a regular file")


def has_pending_secret_deletion(paths: Sequence[Path]) -> bool:
    """Return true for a pending or structurally unsafe deletion tombstone."""

    normalized = tuple(paths)
    if not normalized or len(set(normalized)) != len(normalized):
        return True
    try:
        for path in normalized:
            tombstone = _secret_deletion_tombstone(path)
            _validate_secret_delete_path(tombstone)
            if tombstone.exists():
                return True
    except OSError:
        return True
    return False


def can_manage_secret_bundle(paths: Sequence[Path]) -> bool:
    """Report whether a complete secret set can be safely replaced."""

    normalized = tuple(paths)
    if has_pending_secret_deletion(normalized):
        return False
    return _can_access_secret_bundle(normalized, require_file_write=True)


def can_delete_secret_bundle(paths: Sequence[Path]) -> bool:
    """Report whether active secrets and pending tombstones can be removed."""

    return _can_access_secret_bundle(tuple(paths), require_file_write=False)


def can_manage_secret_pair(first_path: Path, second_path: Path) -> bool:
    """Compatibility wrapper for checking a two-file secret transaction."""

    return can_manage_secret_bundle((first_path, second_path))


def _can_access_secret_bundle(
    paths: Sequence[Path],
    *,
    require_file_write: bool,
) -> bool:
    normalized = tuple(paths)
    if not normalized or len(set(normalized)) != len(normalized):
        return False

    for path in normalized:
        parent = path.parent
        if (
            not parent.exists()
            or parent.is_symlink()
            or not parent.is_dir()
            or not os.access(parent, os.W_OK | os.X_OK)
            or path.is_symlink()
        ):
            return False
        if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
            return False
        if require_file_write and path.exists() and not os.access(path, os.W_OK):
            return False
        tombstone = _secret_deletion_tombstone(path)
        if tombstone.is_symlink():
            return False
        if tombstone.exists() and not stat.S_ISREG(tombstone.lstat().st_mode):
            return False
    return True


def _stage_secret(destination: Path, value: str) -> Path:
    return _stage_secret_bytes(destination, f"{value}\n".encode())


def _stage_secret_bytes(destination: Path, payload: bytes) -> Path:
    parent = destination.parent
    if not parent.exists() or parent.is_symlink() or not parent.is_dir():
        raise OSError("secret directory is not a regular directory")
    if destination.is_symlink():
        raise OSError("secret path is a symbolic link")
    if destination.exists() and not stat.S_ISREG(destination.lstat().st_mode):
        raise OSError("secret path is not a regular file")

    temporary = parent / f".{destination.name}.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short secret write")
            view = view[written:]
        os.fsync(descriptor)
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        with suppress(OSError):
            temporary.unlink()
        raise
    else:
        os.close(descriptor)
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        with suppress(OSError):
            temporary.unlink()
        raise
    return temporary


def _read_existing_secret(path: Path) -> bytes | None:
    if path.is_symlink():
        raise OSError("secret path is a symbolic link")
    if not path.exists():
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65_536:
            raise OSError("secret path is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                raise OSError("short secret read")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _invalid_prowlarr_response() -> AdminIntegrationError:
    return AdminIntegrationError(
        status_code=502,
        code="prowlarr_invalid_response",
        message="Prowlarr trả về dữ liệu không hợp lệ",
        retryable=True,
    )


def _invalid_opensubtitles_configuration() -> AdminIntegrationError:
    return AdminIntegrationError(
        status_code=503,
        code="opensubtitles_configuration_invalid",
        message="Địa chỉ OpenSubtitles không hợp lệ",
        retryable=False,
    )


def _invalid_opensubtitles_response() -> AdminIntegrationError:
    return AdminIntegrationError(
        status_code=502,
        code="opensubtitles_invalid_response",
        message="OpenSubtitles trả về dữ liệu không hợp lệ",
        retryable=True,
    )


def _safe_text(value: object, *, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_length]


def _optional_text(value: object, *, max_length: int) -> str | None:
    text = _safe_text(value, max_length=max_length)
    return text or None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
