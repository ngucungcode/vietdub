"""FastAPI control plane for acquisition and durable job orchestration."""

import asyncio
import errno
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import shutil
import sqlite3
import stat as stat_module
import time
import uuid
from collections.abc import MutableMapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr

from . import __version__
from .admin_integrations import (
    AdminIntegrationError,
    OpenSubtitlesAdminClient,
    ProwlarrAdminClient,
    atomic_write_secret_bundle,
    can_delete_secret_bundle,
    can_manage_secret_bundle,
    delete_secret_bundle,
    has_pending_secret_deletion,
)
from .artifact_cleanup import (
    ArtifactCleanupError,
    execute_cleanup,
    plan_artifact_cleanup,
    remove_terminal_job_artifacts,
)
from .asr import TranscriptionError, normalize_whisper_language
from .config import (
    ModelCatalog,
    Settings,
    get_settings,
    load_model_catalog,
    read_secret,
)
from .domain import (
    AcquisitionError,
    AcquisitionErrorCode,
    MediaKind,
    MediaQuery,
    ReleaseCandidate,
    SubtitleFormat,
)
from .gpu import (
    NvidiaGpu,
    gpu_support_tier,
    inspect_gpu,  # noqa: F401 - re-exported seam that tests monkeypatch
    read_gpu_report,
)
from .media_probe import FfprobeMediaProbe, MediaProbe, MediaProbeError
from .opensubtitles import (
    DEFAULT_OPENSUBTITLES_API_ROOT,
    normalize_opensubtitles_api_root,
)
from .state import (
    ActiveJobExists,
    DuplicateJob,
    InvalidTransition,
    JobDeletionConflict,
    JobNotFound,
    JobRecord,
    JobStage,
    JobStatus,
    StateError,
    StateStore,
    StorageQuotaExceeded,
)
from .transcript import TranscriptError, parse_subtitle_file

_SHA256_HEX = frozenset("0123456789abcdef")
_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset(
    {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
)


def _opensubtitles_secret_paths(settings: Settings) -> tuple[Path, Path, Path] | None:
    paths = (
        settings.opensubtitles_api_key_file,
        settings.opensubtitles_token_file,
        settings.opensubtitles_base_url_file,
    )
    if any(path is None for path in paths):
        return None
    api_key_path, token_path, base_url_path = paths
    assert api_key_path is not None
    assert token_path is not None
    assert base_url_path is not None
    return api_key_path, token_path, base_url_path


def _configured_opensubtitles_api_root(settings: Settings) -> str:
    persisted = read_secret(settings.opensubtitles_base_url_file)
    return normalize_opensubtitles_api_root(
        persisted if persisted is not None else settings.opensubtitles_url
    )


def _acquisition_opensubtitles_configuration(
    settings: Settings,
) -> tuple[str | None, str | None, str]:
    """Load an all-or-disabled provider configuration without blocking startup."""

    try:
        api_key = read_secret(settings.opensubtitles_api_key_file)
        token = read_secret(settings.opensubtitles_token_file)
        api_root = _configured_opensubtitles_api_root(settings)
    except (OSError, ValueError):
        return None, None, DEFAULT_OPENSUBTITLES_API_ROOT
    if not api_key or not token:
        return None, None, api_root
    return api_key, token, api_root


def _sealed_file_sha256(path: Path) -> tuple[str, int]:
    """Hash one regular file without following a final-component symlink."""

    if path.is_symlink():
        raise OSError("artifact path is a symlink")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode):
            raise OSError("artifact is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if path.is_symlink():
            raise OSError("artifact path changed to a symlink")
        return digest.hexdigest(), metadata.st_size
    finally:
        os.close(descriptor)


class AcquisitionPort(Protocol):
    async def search(
        self,
        query: str | MediaQuery,
        year: int | None = None,
        media_type: MediaKind | str = MediaKind.MOVIE,
    ) -> Sequence[ReleaseCandidate]: ...

    async def start_download(
        self,
        release: ReleaseCandidate | str,
        save_path: Path,
        *,
        rights_confirmed: bool,
        paused: bool = False,
    ) -> Any: ...


class CoordinatorPort(Protocol):
    async def refresh(self, job_id: str) -> JobRecord | None: ...

    async def select_subtitle(
        self, job_id: str, subtitle_id: str
    ) -> JobRecord: ...

    async def select_asr(self, job_id: str) -> JobRecord: ...


_LOGGER = logging.getLogger(__name__)
_WEB_STATIC_ROOT = Path(__file__).resolve().with_name("web_static")
_UPLOAD_DISK_RESERVE_BYTES = 512 * 1024 * 1024
_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)


def _request_hostname(host_header: str) -> str | None:
    """Return one canonical host while rejecting malformed Host syntax."""

    if not host_header or any(character.isspace() for character in host_header):
        return None
    try:
        parsed = urlsplit(f"//{host_header}")
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
    ):
        return None
    return parsed.hostname.casefold()


def _with_gpu_support_tier(report: dict[str, Any]) -> dict[str, Any]:
    """Add the selected logical GPU's release support tier to health data."""

    payload = dict(report)
    payload["support_tier"] = None
    if payload.get("ready") is not True:
        return payload
    raw_gpus = payload.get("gpus")
    if not isinstance(raw_gpus, list) or not raw_gpus:
        return payload

    selected_uuid = payload.get("selected_gpu_uuid")
    raw_selected = next(
        (
            item
            for item in raw_gpus
            if isinstance(item, dict)
            and isinstance(selected_uuid, str)
            and item.get("uuid") == selected_uuid
        ),
        raw_gpus[0],
    )
    if not isinstance(raw_selected, dict):
        return payload

    required_text = (
        raw_selected.get("uuid"),
        raw_selected.get("name"),
        raw_selected.get("driver_version"),
        raw_selected.get("compute_capability"),
    )
    memory_total_mib = raw_selected.get("memory_total_mib")
    if (
        not all(isinstance(value, str) and value for value in required_text)
        or not isinstance(memory_total_mib, int)
        or isinstance(memory_total_mib, bool)
    ):
        return payload

    try:
        selected_gpu = NvidiaGpu(
            uuid=raw_selected["uuid"],
            name=raw_selected["name"],
            driver_version=raw_selected["driver_version"],
            memory_total_mib=memory_total_mib,
            compute_capability=raw_selected["compute_capability"],
        )
    except (KeyError, TypeError, ValueError):
        return payload
    payload["support_tier"] = gpu_support_tier(selected_gpu)
    return payload


async def _monitor_acquisition_jobs(
    coordinator: CoordinatorPort,
    store: StateStore,
    stop: asyncio.Event,
    interval_seconds: float,
) -> None:
    """Refresh only network acquisition stages; inference stays offline."""

    monitored = (JobStatus.DOWNLOADING, JobStatus.SUBTITLE_MATCHING)
    while not stop.is_set():
        for record in store.list_jobs(monitored, limit=100):
            if stop.is_set():
                return
            try:
                await coordinator.refresh(record.id)
            except JobNotFound:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # Adapter/coordinator errors are expected to be persisted as
                # typed job failures.  One broken job must not stop monitoring.
                _LOGGER.exception("Acquisition refresh failed for job %s", record.id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=300)
    year: int | None = Field(default=None, ge=1888, le=2200)
    media_type: Literal["movie"] = "movie"


class ReleaseResponse(BaseModel):
    release_id: str
    title: str
    indexer_id: int
    protocol: str
    info_hash: str | None = None
    size_bytes: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    published_at: str | None = None
    categories: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    results: list[ReleaseResponse]


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asr: str | None = None
    translation: str | None = None
    separation: str | None = None
    tts: str | None = None


class VoiceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_id: str | None = Field(default=None, max_length=200)
    reference_path: str | None = Field(default=None, max_length=1000)


def _normalized_glossary(value: dict[str, str]) -> dict[str, str]:
    if len(value) > 100:
        raise ValueError("Glossary không được vượt quá 100 thuật ngữ")
    normalized: dict[str, str] = {}
    for raw_source, raw_target in value.items():
        source = " ".join(raw_source.split())
        target = " ".join(raw_target.split())
        if not 1 <= len(source) <= 120 or not 1 <= len(target) <= 200:
            raise ValueError("Thuật ngữ phải dài từ 1 đến 120/200 ký tự")
        if source in normalized:
            raise ValueError("Thuật ngữ nguồn bị trùng sau khi chuẩn hóa")
        normalized[source] = target
    return dict(sorted(normalized.items()))


Glossary = Annotated[dict[str, str], AfterValidator(_normalized_glossary)]


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(min_length=1, max_length=500)
    rights_confirmed: bool = False
    source_language: str = Field(default="auto", min_length=2, max_length=35)
    subtitle_mode: Literal["prefer", "manual", "asr"] = "prefer"
    timing_profile: Literal["natural", "strict"] = "natural"
    lip_sync_mode: Literal["off", "draft", "final"] = "off"
    models: ModelSelection = Field(default_factory=ModelSelection)
    voice: VoiceSelection | None = None
    voice_rights_confirmed: bool = False
    glossary: Glossary = Field(default_factory=dict)


class UploadCreateRequest(BaseModel):
    """Metadata declared before streaming local artifacts as raw bodies."""

    model_config = ConfigDict(extra="forbid")

    media_filename: str = Field(min_length=1, max_length=255)
    subtitle_filename: str | None = Field(default=None, min_length=1, max_length=255)
    rights_confirmed: bool = False
    source_language: str = Field(default="auto", min_length=2, max_length=35)
    timing_profile: Literal["natural", "strict"] = "natural"
    lip_sync_mode: Literal["off", "draft", "final"] = "off"
    models: ModelSelection = Field(default_factory=ModelSelection)
    voice: VoiceSelection | None = None
    voice_rights_confirmed: bool = False
    glossary: Glossary = Field(default_factory=dict)


class _UploadSessionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal[
        "awaiting_media",
        "awaiting_subtitle",
        "ready",
        "finalized",
    ]
    request: UploadCreateRequest
    media_size_bytes: int | None = Field(default=None, ge=1)
    subtitle_size_bytes: int | None = Field(default=None, ge=1)
    media_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    subtitle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    job_id: str | None = None


class UploadSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal[
        "awaiting_media",
        "awaiting_subtitle",
        "ready",
        "finalized",
    ]
    media_filename: str
    subtitle_filename: str | None
    media_size_bytes: int | None
    subtitle_size_bytes: int | None
    media_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    subtitle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    job_id: str | None


def _upload_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": retryable},
    )


def _validated_upload_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise _upload_error(
            status.HTTP_404_NOT_FOUND,
            "upload_not_found",
            "Không tìm thấy phiên tải file",
        ) from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise _upload_error(
            status.HTTP_404_NOT_FOUND,
            "upload_not_found",
            "Không tìm thấy phiên tải file",
        )
    return canonical


def _validated_upload_filename(
    value: str,
    *,
    allowed_extensions: frozenset[str],
    code: str,
    message: str,
) -> tuple[str, str]:
    normalized = value.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "\x00" in normalized
        or "/" in normalized
        or "\\" in normalized
        or Path(normalized).name != normalized
    ):
        raise _upload_error(status.HTTP_422_UNPROCESSABLE_CONTENT, code, message)
    extension = Path(normalized).suffix.lower()
    if extension not in allowed_extensions:
        raise _upload_error(status.HTTP_422_UNPROCESSABLE_CONTENT, code, message)
    return normalized, extension


def _upload_directory(settings: Settings, upload_id: str) -> Path:
    identifier = _validated_upload_id(upload_id)
    root = settings.incoming_dir.resolve(strict=False)
    candidate = root / identifier
    try:
        metadata = candidate.lstat()
    except FileNotFoundError as exc:
        raise _upload_error(
            status.HTTP_404_NOT_FOUND,
            "upload_not_found",
            "Không tìm thấy phiên tải file",
        ) from exc
    if not stat_module.S_ISDIR(metadata.st_mode) or candidate.is_symlink():
        raise _upload_error(
            status.HTTP_409_CONFLICT,
            "upload_path_invalid",
            "Thư mục phiên tải file không an toàn",
        )
    if not candidate.absolute().is_relative_to(root.absolute()):
        raise _upload_error(
            status.HTTP_409_CONFLICT,
            "upload_path_invalid",
            "Thư mục phiên tải file không an toàn",
        )
    return candidate


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short upload write")
        view = view[written:]


def _read_regular_bytes(path: Path, *, maximum: int) -> bytes:
    if path.is_symlink():
        raise OSError("metadata path is a symlink")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise OSError("unsafe metadata file")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise OSError("short metadata read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if path.is_symlink():
            raise OSError("metadata path changed to a symlink")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
                raise
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, mode)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        if os.name != "nt":
            _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _load_upload_session(settings: Settings, upload_id: str) -> _UploadSessionState:
    directory = _upload_directory(settings, upload_id)
    try:
        payload = json.loads(
            _read_regular_bytes(
                directory / ".upload-session.json",
                maximum=64 * 1024,
            )
        )
        session = _UploadSessionState.model_validate(payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise _upload_error(
            status.HTTP_409_CONFLICT,
            "upload_session_invalid",
            "Checkpoint phiên tải file không hợp lệ",
        ) from exc
    if session.id != _validated_upload_id(upload_id):
        raise _upload_error(
            status.HTTP_409_CONFLICT,
            "upload_session_invalid",
            "Checkpoint phiên tải file không hợp lệ",
        )
    return session


def _save_upload_session(settings: Settings, session: _UploadSessionState) -> None:
    directory = _upload_directory(settings, session.id)
    _atomic_write_bytes(
        directory / ".upload-session.json",
        session.model_dump_json().encode("utf-8"),
    )


def _public_upload_session(session: _UploadSessionState) -> UploadSessionResponse:
    return UploadSessionResponse(
        id=session.id,
        status=session.status,
        media_filename=session.request.media_filename,
        subtitle_filename=session.request.subtitle_filename,
        media_size_bytes=session.media_size_bytes,
        subtitle_size_bytes=session.subtitle_size_bytes,
        media_sha256=session.media_sha256,
        subtitle_sha256=session.subtitle_sha256,
        job_id=session.job_id,
    )


def _next_upload_status(session: _UploadSessionState) -> str:
    if session.job_id is not None:
        return "finalized"
    if session.media_size_bytes is None:
        return "awaiting_media"
    if (
        session.request.subtitle_filename is not None
        and session.subtitle_size_bytes is None
    ):
        return "awaiting_subtitle"
    return "ready"


def _regular_file_identity(
    path: Path,
    *,
    maximum: int,
) -> tuple[int, int, int, int]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise _upload_error(
            status.HTTP_409_CONFLICT,
            "upload_incomplete",
            "Phiên tải file chưa nhận đủ artifact",
            retryable=True,
        ) from exc
    if (
        not stat_module.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_size <= 0
        or metadata.st_size > maximum
    ):
        raise _upload_error(
            status.HTTP_409_CONFLICT,
            "upload_artifact_invalid",
            "Artifact tải lên không phải file cục bộ an toàn",
        )
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _regular_file_size(path: Path, *, maximum: int) -> int:
    return _regular_file_identity(path, maximum=maximum)[2]


async def _stream_upload_body(
    request: Request,
    destination: Path,
    *,
    maximum: int,
    store: StateStore | None = None,
    upload_id: str | None = None,
    kind: Literal["media", "subtitle"] | None = None,
    quota_bytes: int | None = None,
) -> tuple[int, str]:
    raw_length = request.headers.get("content-length")
    reservation_id = str(uuid.uuid4())
    accounting = (
        store is not None
        and upload_id is not None
        and kind is not None
        and quota_bytes is not None
    )
    if raw_length is not None:
        try:
            declared_length = int(raw_length)
        except ValueError as exc:
            raise _upload_error(
                status.HTTP_400_BAD_REQUEST,
                "content_length_invalid",
                "Content-Length của file tải lên không hợp lệ",
            ) from exc
        if declared_length <= 0:
            raise _upload_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "upload_empty",
                "File tải lên không được để trống",
            )
        if declared_length > maximum:
            raise _upload_error(
                status.HTTP_413_CONTENT_TOO_LARGE,
                "upload_too_large",
                "File tải lên vượt giới hạn của máy chủ",
            )
        try:
            free_bytes = shutil.disk_usage(destination.parent).free
        except OSError as exc:
            raise _upload_error(
                status.HTTP_507_INSUFFICIENT_STORAGE,
                "upload_disk_check_failed",
                "Không thể kiểm tra dung lượng trống cho file tải lên",
                retryable=True,
            ) from exc
        if declared_length + _UPLOAD_DISK_RESERVE_BYTES > free_bytes:
            raise _upload_error(
                status.HTTP_507_INSUFFICIENT_STORAGE,
                "upload_disk_full",
                "Không đủ dung lượng trống cho file tải lên",
                retryable=True,
            )
        if accounting:
            try:
                store.reserve_upload_bytes(
                    upload_id,
                    kind,
                    reservation_id=reservation_id,
                    target_bytes=declared_length,
                    safety_bytes=_UPLOAD_DISK_RESERVE_BYTES,
                    quota_bytes=quota_bytes,
                )
            except StorageQuotaExceeded as exc:
                raise _upload_error(
                    status.HTTP_507_INSUFFICIENT_STORAGE,
                    "upload_quota_exceeded",
                    "Dung lượng upload đang dùng hoặc đã giữ chỗ vượt quota",
                    retryable=True,
                ) from exc
            except StateError as exc:
                raise _upload_error(
                    status.HTTP_409_CONFLICT,
                    "upload_reservation_conflict",
                    "Artifact đang được một request khác tải lên",
                    retryable=True,
                ) from exc

    # A killed API process can leave only its unique unpublished part.  A
    # retry owns the per-session lock and may safely discard those regular
    # local remnants while preserving the last atomically published file.
    for stale in destination.parent.glob(f".{destination.name}.*.part"):
        try:
            stale_metadata = stale.lstat()
        except FileNotFoundError:
            continue
        if stat_module.S_ISREG(stale_metadata.st_mode) or stale.is_symlink():
            stale.unlink(missing_ok=True)

    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.part"
    )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    completed = False
    received = 0
    digest = hashlib.sha256()
    try:
        descriptor = os.open(temporary, flags, 0o640)
        async for chunk in request.stream():
            if not chunk:
                continue
            received += len(chunk)
            if received > maximum:
                raise _upload_error(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    "upload_too_large",
                    "File tải lên vượt giới hạn của máy chủ",
                )
            if raw_length is None:
                try:
                    free_bytes = shutil.disk_usage(destination.parent).free
                except OSError as exc:
                    raise _upload_error(
                        status.HTTP_507_INSUFFICIENT_STORAGE,
                        "upload_disk_check_failed",
                        "Không thể kiểm tra dung lượng trống cho file tải lên",
                        retryable=True,
                    ) from exc
                if len(chunk) + _UPLOAD_DISK_RESERVE_BYTES > free_bytes:
                    raise _upload_error(
                        status.HTTP_507_INSUFFICIENT_STORAGE,
                        "upload_disk_full",
                        "Không đủ dung lượng trống cho file tải lên",
                        retryable=True,
                    )
                if accounting:
                    try:
                        store.reserve_upload_bytes(
                            upload_id,
                            kind,
                            reservation_id=reservation_id,
                            target_bytes=received,
                            safety_bytes=_UPLOAD_DISK_RESERVE_BYTES,
                            quota_bytes=quota_bytes,
                        )
                    except StorageQuotaExceeded as exc:
                        raise _upload_error(
                            status.HTTP_507_INSUFFICIENT_STORAGE,
                            "upload_quota_exceeded",
                            "Dung lượng upload đang dùng hoặc đã giữ chỗ vượt quota",
                            retryable=True,
                        ) from exc
            digest.update(chunk)
            await asyncio.to_thread(_write_all, descriptor, chunk)
        if received <= 0:
            raise _upload_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "upload_empty",
                "File tải lên không được để trống",
            )
        if raw_length is not None and received != int(raw_length):
            raise _upload_error(
                status.HTTP_400_BAD_REQUEST,
                "upload_size_mismatch",
                "Kích thước file nhận được không khớp Content-Length",
                retryable=True,
            )
        await asyncio.to_thread(os.fsync, descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, destination)
        if accounting:
            store.commit_upload_bytes(
                upload_id,
                kind,
                reservation_id,
                committed_bytes=received,
            )
        completed = True
        return received, digest.hexdigest()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not completed:
            temporary.unlink(missing_ok=True)
            if accounting:
                store.release_upload_reservation(upload_id, kind, reservation_id)


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    maximum: int,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> int:
    if source.is_symlink():
        raise OSError("upload source is a symlink")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(source, flags)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.part"
    )
    destination_descriptor: int | None = None
    try:
        metadata = os.fstat(source_descriptor)
        identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        if (
            not stat_module.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > maximum
            or (expected_identity is not None and identity != expected_identity)
        ):
            raise OSError("unsafe upload source")
        write_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        destination_descriptor = os.open(temporary, write_flags, 0o640)
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(source_descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError("short upload copy")
            _write_all(destination_descriptor, chunk)
            remaining -= len(chunk)
        os.fsync(destination_descriptor)
        if source.is_symlink():
            raise OSError("upload source changed to a symlink")
        os.close(destination_descriptor)
        destination_descriptor = None
        os.replace(temporary, destination)
        return metadata.st_size
    finally:
        os.close(source_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        temporary.unlink(missing_ok=True)


def _expired_upload_session_directory(
    settings: Settings,
    raw_identifier: str,
    *,
    cutoff: float,
) -> Path | None:
    """Return one safe expired session directory after checking it in place."""

    try:
        identifier = str(uuid.UUID(raw_identifier))
    except (ValueError, AttributeError):
        return None
    if identifier != raw_identifier.lower():
        return None
    root = settings.incoming_dir.resolve(strict=False)
    candidate = root / identifier
    session_path = candidate / ".upload-session.json"
    try:
        candidate_metadata = candidate.lstat()
        session_metadata = session_path.lstat()
    except OSError:
        return None
    if (
        not stat_module.S_ISDIR(candidate_metadata.st_mode)
        or candidate.is_symlink()
        or not stat_module.S_ISREG(session_metadata.st_mode)
        or session_path.is_symlink()
        or session_metadata.st_mtime >= cutoff
        or not candidate.absolute().is_relative_to(root.absolute())
    ):
        return None
    return candidate


def _expired_upload_session_identifiers(settings: Settings) -> tuple[str, ...]:
    """Return UUID sessions whose checkpoint metadata is older than the TTL."""

    root = settings.incoming_dir.resolve(strict=False)
    try:
        candidates = tuple(root.iterdir())
    except FileNotFoundError:
        return ()
    except OSError:
        _LOGGER.exception("Could not scan upload sessions for expiration")
        return ()
    cutoff = time.time() - settings.upload_session_ttl_seconds
    return tuple(
        candidate.name
        for candidate in candidates
        if _expired_upload_session_directory(
            settings,
            candidate.name,
            cutoff=cutoff,
        )
        is not None
    )


def _cleanup_stale_upload_sessions(
    settings: Settings,
    store: StateStore,
    *,
    identifiers: Sequence[str] | None = None,
) -> None:
    """Remove only expired, unfinalized UUID upload directories."""

    selected = (
        _expired_upload_session_identifiers(settings)
        if identifiers is None
        else tuple(identifiers)
    )
    cutoff = time.time() - settings.upload_session_ttl_seconds
    jobs_root = settings.jobs_dir.resolve(strict=False)
    for raw_identifier in selected:
        try:
            identifier = str(uuid.UUID(raw_identifier))
        except (ValueError, AttributeError):
            continue
        candidate = _expired_upload_session_directory(
            settings,
            raw_identifier,
            cutoff=cutoff,
        )
        if candidate is None:
            continue
        try:
            session = _load_upload_session(settings, identifier)
        except HTTPException:
            continue
        if session.status == "finalized":
            continue
        try:
            store.get_job(identifier)
        except JobNotFound:
            pass
        else:
            continue
        prepared = jobs_root / identifier
        try:
            prepared_metadata = prepared.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            _LOGGER.exception(
                "Could not inspect prepared artifacts for expired upload %s",
                identifier,
            )
            continue
        else:
            if not (
                stat_module.S_ISDIR(prepared_metadata.st_mode)
                and not prepared.is_symlink()
                and prepared.absolute().is_relative_to(jobs_root.absolute())
            ):
                _LOGGER.error(
                    "Refusing to remove unsafe prepared artifacts for upload %s",
                    identifier,
                )
                continue
            try:
                shutil.rmtree(prepared)
            except OSError:
                _LOGGER.exception(
                    "Could not remove prepared artifacts for expired upload %s",
                    identifier,
                )
                continue
        try:
            shutil.rmtree(candidate)
        except OSError:
            _LOGGER.exception(
                "Could not remove expired upload session %s",
                identifier,
            )


async def _monitor_stale_upload_sessions(
    settings: Settings,
    store: StateStore,
    stop: asyncio.Event,
    *,
    interval_seconds: float,
    session_locks: MutableMapping[str, asyncio.Lock],
) -> None:
    """Run bounded periodic TTL cleanup without blocking the API event loop."""

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            try:
                identifiers = await asyncio.to_thread(
                    _expired_upload_session_identifiers,
                    settings,
                )
                for identifier in identifiers:
                    if stop.is_set():
                        return
                    lock = session_locks.setdefault(identifier, asyncio.Lock())
                    async with lock:
                        await asyncio.to_thread(
                            _cleanup_stale_upload_sessions,
                            settings,
                            store,
                            identifiers=(identifier,),
                        )
            except Exception:
                _LOGGER.exception("Periodic upload-session cleanup failed")


class LanguageSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(min_length=2, max_length=35)


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    release_id: str
    status: str
    stage: str
    progress_permille: int
    spec: dict[str, Any]
    details: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    cancel_requested: bool
    pinned: bool
    revision: int
    created_at: str
    updated_at: str


class JobListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[JobResponse]
    count: int


class AdminIntegrationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configured: bool
    editable: bool
    can_manage: bool
    cleanup_pending: bool = False
    can_delete: bool = False


class AdminIntegrationsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prowlarr: AdminIntegrationStatus
    opensubtitles: AdminIntegrationStatus


class ProwlarrIndexerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    definition_name: str | None = None
    implementation_name: str | None = None
    protocol: str | None = None
    privacy: str | None = None
    enabled: bool
    supports_search: bool
    supports_rss: bool
    priority: int | None = None
    disabled_until: str | None = None
    most_recent_failure: str | None = None


class ProwlarrIndexerListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ProwlarrIndexerResponse]
    count: int


class ProwlarrTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    indexer_id: int
    ok: bool
    failure_count: int


class ProwlarrTestAllResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all_ok: bool
    failed_count: int
    results: list[ProwlarrTestResult]


class OpenSubtitlesConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=1, max_length=4096)
    username: str = Field(min_length=1, max_length=256)
    password: SecretStr = Field(min_length=1, max_length=4096)


class OpenSubtitlesDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal["DELETE_OPENSUBTITLES_CREDENTIALS"]


class OpenSubtitlesConfigurationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configured: bool
    authenticated: bool
    restart_required: bool
    quota: dict[str, int | bool | str | None] | None = None


def _public_release(candidate: ReleaseCandidate) -> ReleaseResponse:
    """Never expose provider URLs or opaque GUIDs through the public API."""

    return ReleaseResponse(
        release_id=candidate.release_id,
        title=candidate.title,
        indexer_id=candidate.indexer_id,
        protocol=candidate.protocol,
        info_hash=candidate.info_hash,
        size_bytes=candidate.size_bytes,
        seeders=candidate.seeders,
        leechers=candidate.leechers,
        published_at=candidate.published_at,
        categories=list(candidate.categories),
    )


def _job_response(record: JobRecord) -> JobResponse:
    return JobResponse.model_validate(record.to_dict())


def _adapter_value(value: Any, name: str, default: Any = None) -> Any:
    if is_dataclass(value):
        return asdict(value).get(name, default)
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _release_snapshot(candidate: ReleaseCandidate) -> dict[str, Any] | None:
    """Build a restart token without persisting provider URLs or credentials."""

    info_hash = (candidate.info_hash or "").strip().lower()
    if len(info_hash) != 40 or any(
        character not in "0123456789abcdef" for character in info_hash
    ):
        return None
    safe_magnet = f"magnet:?xt=urn:btih:{info_hash}"
    if candidate.download_uri.strip().lower() != safe_magnet:
        # HTTP URLs and magnets with tracker parameters may contain private
        # tokens. Re-resolve them from the saved search query after restart.
        return None
    return {
        "release_id": candidate.release_id,
        "title": candidate.title,
        "indexer_id": candidate.indexer_id,
        "protocol": "torrent",
        "download_uri": safe_magnet,
        "guid": "",
        "info_hash": info_hash,
        "size_bytes": candidate.size_bytes,
        "seeders": candidate.seeders,
        "leechers": candidate.leechers,
        "published_at": candidate.published_at,
        "categories": list(candidate.categories),
    }


def _release_from_snapshot(
    payload: object,
    *,
    expected_release_id: str,
) -> ReleaseCandidate | None:
    if not isinstance(payload, dict):
        return None
    try:
        values = dict(payload)
        values["categories"] = tuple(values.get("categories") or ())
        candidate = ReleaseCandidate(**values)
    except (TypeError, ValueError):
        return None
    expected_uri = f"magnet:?xt=urn:btih:{(candidate.info_hash or '').lower()}"
    if (
        candidate.release_id != expected_release_id
        or candidate.guid
        or candidate.download_uri.lower() != expected_uri
    ):
        return None
    return candidate


def _acquisition_error_status(error: AcquisitionError) -> int:
    code = error.code.value
    if code == "rights_confirmation_required":
        return status.HTTP_403_FORBIDDEN
    if code in {"download_unavailable", "download_not_found"}:
        return status.HTTP_409_CONFLICT
    if code == "invalid_response":
        return status.HTTP_502_BAD_GATEWAY
    return status.HTTP_503_SERVICE_UNAVAILABLE if error.retryable else 400


def _validate_model_selection(
    selection: ModelSelection,
    catalog: ModelCatalog,
    *,
    gpu_report: dict[str, Any] | None = None,
) -> None:
    available_vram_mib: int | None = None
    if gpu_report is not None and gpu_report.get("ready") is True:
        gpus = gpu_report.get("gpus")
        if isinstance(gpus, list) and gpus and isinstance(gpus[0], dict):
            reported_vram = gpus[0].get("memory_total_mib")
            if (
                isinstance(reported_vram, int)
                and not isinstance(reported_vram, bool)
                and reported_vram > 0
            ):
                available_vram_mib = reported_vram

    by_id = {entry.id: entry for entry in catalog.models}
    requested = {
        "asr": selection.asr,
        "mt": selection.translation,
        "separation": selection.separation,
        "tts": selection.tts,
    }
    for expected_stage, model_id in requested.items():
        if model_id is None:
            continue
        entry = by_id.get(model_id)
        if entry is None or entry.stage != expected_stage:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "invalid_model_selection",
                    "message": (
                        f"Model {model_id} không thuộc lựa chọn {expected_stage}"
                    ),
                    "retryable": False,
                },
            )
        if (
            available_vram_mib is not None
            and entry.minimum_vram_mib is not None
            and entry.minimum_vram_mib > available_vram_mib
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "model_vram_insufficient",
                    "message": (
                        f"Model {model_id} cần tối thiểu "
                        f"{entry.minimum_vram_mib} MiB VRAM nhưng GPU logical 0 "
                        f"chỉ có {available_vram_mib} MiB"
                    ),
                    "retryable": False,
                },
            )


def create_app(
    *,
    settings: Settings | None = None,
    state_store: StateStore | None = None,
    acquisition_service: AcquisitionPort | None = None,
    coordinator: CoordinatorPort | None = None,
    admin_http_client: httpx.AsyncClient | None = None,
    media_probe: MediaProbe | None = None,
) -> FastAPI:
    """Create an app with explicit adapter injection for deterministic tests."""

    configured_settings = settings or get_settings()
    active_job_operations: dict[str, asyncio.Task[Any]] = {}
    cancellation_reconciliations: dict[str, asyncio.Task[None]] = {}
    backend_mutation_lock = asyncio.Lock()
    admin_configuration_lock = asyncio.Lock()
    # Upload locks are weakly held: active/waiting coroutines retain their lock,
    # while completed, finalized, or unknown UUID requests cannot grow this
    # process-local registry for the lifetime of the API process.
    upload_session_locks: WeakValueDictionary[str, asyncio.Lock] = (
        WeakValueDictionary()
    )
    local_media_probe = media_probe or FfprobeMediaProbe()

    async def mutate_backend(
        operation: Any,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        async def invoke() -> Any:
            async with backend_mutation_lock:
                return await operation(*args, **kwargs)

        if timeout is None:
            return await invoke()
        return await asyncio.wait_for(invoke(), timeout=max(0.05, timeout))

    async def pause_backend_for_cancel(
        store: StateStore,
        service: Any,
        job_id: str,
        task_id: str,
        *,
        timeout: float,
    ) -> str:
        """Pause one task without racing a new owner's backend mutation."""

        async def pause_if_unowned() -> str:
            async with backend_mutation_lock:
                current = store.get_job(job_id)
                if (
                    not current.active_slot
                    and any(
                        active.id != job_id
                        for active in store.list_active_jobs(limit=1000)
                    )
                ):
                    return "deferred"
                pause_download = getattr(service, "pause_download", None)
                if not callable(pause_download):
                    return "unsupported"
                await pause_download(task_id)
                return "paused"

        try:
            return await asyncio.wait_for(
                pause_if_unowned(),
                timeout=max(0.05, timeout),
            )
        except Exception:
            return "failed"

    def schedule_cancel_reconciliation(
        owner: asyncio.Task[Any],
        store: StateStore,
        service: Any,
        job_id: str,
    ) -> None:
        existing = cancellation_reconciliations.get(job_id)
        if existing is not None and not existing.done():
            return

        async def reconcile() -> None:
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError:
                if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                    raise
            except Exception:  # noqa: S110 - the owner's failure is reported on its own job record
                pass
            current = store.get_job(job_id)
            if current.status is not JobStatus.CANCELLING:
                return
            task_id = current.details.get("task_id")
            if not task_id:
                store.finalize_cancel(job_id)
                return
            result = await pause_backend_for_cancel(
                store,
                service,
                job_id,
                str(task_id),
                timeout=1.5,
            )
            if result == "paused":
                store.finalize_cancel(job_id)
            elif result == "deferred":
                store.append_warning(
                    job_id,
                    "backend_cleanup_deferred",
                    "Chưa dừng torrent cũ vì một job khác đang hoạt động",
                )
            else:
                store.append_warning(
                    job_id,
                    "download_pause_failed",
                    "Chưa thể hoàn tất hủy torrent sau thao tác tải đang chạy",
                )

        task = asyncio.create_task(
            reconcile(),
            name=f"cancel-reconcile-{job_id}",
        )
        cancellation_reconciliations[job_id] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if cancellation_reconciliations.get(job_id) is completed:
                cancellation_reconciliations.pop(job_id, None)

        task.add_done_callback(discard)

    def track_job_operation(job_id: str) -> None:
        """Expose in-flight torrent mutations to the cancellation endpoint.

        Cancellation first records the durable CANCELLING state.  The request
        that currently owns a qBittorrent mutation then performs the final
        pause, which prevents a late resume/add call from running after a
        cancellation has already been finalized.
        """

        task = asyncio.current_task()
        if task is None:
            return
        existing = active_job_operations.get(job_id)
        if existing is not None and existing is not task and not existing.done():
            raise InvalidTransition("Job đang có một thao tác tải nguồn khác")
        active_job_operations[job_id] = task

        def discard(completed: asyncio.Task[Any]) -> None:
            if active_job_operations.get(job_id) is completed:
                active_job_operations.pop(job_id, None)

        task.add_done_callback(discard)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        owned_admin_client: httpx.AsyncClient | None = None
        configured_admin_client = admin_http_client
        if configured_admin_client is None:
            owned_admin_client = httpx.AsyncClient(follow_redirects=False)
            configured_admin_client = owned_admin_client
        application.state.admin_http_client = configured_admin_client
        if state_store is None:
            configured_settings.ensure_local_directories()
            application.state.job_store = StateStore(
                configured_settings.database_path
            )
        else:
            application.state.job_store = state_store
        committed_uploads: dict[tuple[str, str], int] = {}
        if configured_settings.incoming_dir.is_dir():
            for directory in configured_settings.incoming_dir.iterdir():
                if not directory.is_dir() or directory.is_symlink():
                    continue
                for candidate in directory.glob("source.*"):
                    if not candidate.is_file() or candidate.is_symlink():
                        continue
                    kind = "subtitle" if candidate.suffix.casefold() == ".srt" else "media"
                    committed_uploads[(directory.name, kind)] = candidate.stat().st_size
        application.state.job_store.reconcile_upload_storage(committed_uploads)
        await asyncio.to_thread(
            _cleanup_stale_upload_sessions,
            configured_settings,
            application.state.job_store,
        )
        for interrupted in application.state.job_store.list_jobs(
            (JobStatus.CREATED,),
            limit=1000,
        ):
            application.state.job_store.update_status(
                interrupted.id,
                JobStatus.FAILED,
                error_code="download_start_interrupted",
                error_message=(
                    "API đã dừng khi đang khởi tạo tải nguồn; hãy chọn Tiếp tục"
                ),
                retryable=True,
            )
        client: httpx.AsyncClient | None = None
        configured_acquisition = acquisition_service
        if configured_acquisition is None:
            prowlarr_key = read_secret(configured_settings.prowlarr_api_key_file)
            qbittorrent_password = read_secret(
                configured_settings.qbittorrent_password_file
            )
            if prowlarr_key and qbittorrent_password:
                from .acquisition import build_acquisition_service

                (
                    opensubtitles_api_key,
                    opensubtitles_token,
                    opensubtitles_api_root,
                ) = _acquisition_opensubtitles_configuration(configured_settings)
                client = httpx.AsyncClient(follow_redirects=False)
                configured_acquisition = build_acquisition_service(
                    client=client,
                    prowlarr_url=configured_settings.prowlarr_url,
                    prowlarr_api_key=prowlarr_key,
                    qbittorrent_url=configured_settings.qbittorrent_url,
                    qbittorrent_username=configured_settings.qbittorrent_username,
                    qbittorrent_password=qbittorrent_password,
                    opensubtitles_api_key=opensubtitles_api_key,
                    opensubtitles_token=opensubtitles_token,
                    opensubtitles_base_url=opensubtitles_api_root,
                    opensubtitles_user_agent=(
                        configured_settings.opensubtitles_user_agent
                    ),
                )
        configured_coordinator = coordinator
        if configured_coordinator is None and configured_acquisition is not None:
            from .acquisition.coordinator import AcquisitionCoordinator

            configured_coordinator = AcquisitionCoordinator(
                configured_acquisition,
                application.state.job_store,
                configured_settings.incoming_dir,
                configured_settings.jobs_dir,
            )
        application.state.acquisition = configured_acquisition
        application.state.coordinator = configured_coordinator
        for pending_start in application.state.job_store.list_jobs(
            (JobStatus.DOWNLOADING,),
            limit=1000,
        ):
            if pending_start.details.get("backend_started") is not False:
                continue
            task_id = pending_start.details.get("task_id")
            resume_download = getattr(configured_acquisition, "resume_download", None)
            if not task_id or not callable(resume_download):
                application.state.job_store.update_status(
                    pending_start.id,
                    JobStatus.FAILED,
                    error_code="download_start_interrupted",
                    error_message=(
                        "API đã dừng trước khi xác nhận trình tải nguồn bắt đầu"
                    ),
                    retryable=True,
                )
                continue
            try:
                await mutate_backend(
                    resume_download,
                    str(task_id),
                    timeout=15.0,
                )
            except Exception:
                pause_result = await pause_backend_for_cancel(
                    application.state.job_store,
                    configured_acquisition,
                    pending_start.id,
                    str(task_id),
                    timeout=1.5,
                )
                message = (
                    "Không thể khôi phục trình tải nguồn sau khi API khởi động lại"
                )
                if pause_result == "paused":
                    application.state.job_store.update_status(
                        pending_start.id,
                        JobStatus.FAILED,
                        error_code="download_start_interrupted",
                        error_message=message,
                        retryable=True,
                    )
                else:
                    application.state.job_store.update_status(
                        pending_start.id,
                        JobStatus.DOWNLOADING,
                        details={
                            **pending_start.details,
                            "backend_started": True,
                            "backend_state_uncertain": True,
                        },
                        error_code="download_start_interrupted",
                        error_message=message,
                        retryable=True,
                        expected_status=JobStatus.DOWNLOADING,
                    )
            else:
                application.state.job_store.update_status(
                    pending_start.id,
                    JobStatus.DOWNLOADING,
                    details={
                        **pending_start.details,
                        "backend_started": True,
                    },
                    expected_status=JobStatus.DOWNLOADING,
                )
        for pending_cancel in application.state.job_store.list_jobs(
            (JobStatus.CANCELLING,),
            limit=1000,
        ):
            task_id = pending_cancel.details.get("task_id")
            if not task_id:
                application.state.job_store.finalize_cancel(pending_cancel.id)
            else:
                pause_result = await pause_backend_for_cancel(
                    application.state.job_store,
                    configured_acquisition,
                    pending_cancel.id,
                    str(task_id),
                    timeout=1.5,
                )
                if pause_result == "paused":
                    application.state.job_store.finalize_cancel(pending_cancel.id)
                elif pause_result == "deferred":
                    application.state.job_store.append_warning(
                        pending_cancel.id,
                        "backend_cleanup_deferred",
                        "Chưa dừng torrent cũ vì một job khác đang hoạt động",
                    )
                else:
                    application.state.job_store.append_warning(
                        pending_cancel.id,
                        "download_pause_failed",
                        "Chưa thể hoàn tất hủy torrent sau khi API khởi động lại",
                    )
        upload_cleanup_stop = asyncio.Event()
        upload_cleanup_interval = max(
            60.0,
            min(3600.0, configured_settings.upload_session_ttl_seconds / 4),
        )
        upload_cleanup_task = asyncio.create_task(
            _monitor_stale_upload_sessions(
                configured_settings,
                application.state.job_store,
                upload_cleanup_stop,
                interval_seconds=upload_cleanup_interval,
                session_locks=upload_session_locks,
            ),
            name="upload-session-cleanup",
        )
        application.state.upload_cleanup_task = upload_cleanup_task
        monitor_stop = asyncio.Event()
        monitor_task: asyncio.Task[None] | None = None
        if configured_coordinator is not None:
            monitor_task = asyncio.create_task(
                _monitor_acquisition_jobs(
                    configured_coordinator,
                    application.state.job_store,
                    monitor_stop,
                    configured_settings.acquisition_monitor_seconds,
                ),
                name="acquisition-job-monitor",
            )
        application.state.acquisition_monitor_task = monitor_task
        try:
            yield
        finally:
            upload_cleanup_stop.set()
            upload_cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await upload_cleanup_task
            monitor_stop.set()
            if monitor_task is not None:
                monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor_task
            pending_reconciliations = tuple(cancellation_reconciliations.values())
            for reconciliation in pending_reconciliations:
                reconciliation.cancel()
            if pending_reconciliations:
                await asyncio.gather(
                    *pending_reconciliations,
                    return_exceptions=True,
                )
            if client is not None:
                await client.aclose()
            if owned_admin_client is not None:
                await owned_admin_client.aclose()

    application = FastAPI(
        title="Thuyết Minh Offline GPU",
        version=__version__,
        description="Điều khiển tải nguồn hợp pháp và pipeline thuyết minh cục bộ.",
        lifespan=lifespan,
    )
    application.state.settings = configured_settings
    application.state.upload_session_locks = upload_session_locks

    @application.middleware("http")
    async def require_remote_api_token(request: Request, call_next: Any) -> Any:
        """Apply explicit access policy to every HTTP surface."""

        if configured_settings.api_access_policy == "local":
            return await call_next(request)
        try:
            token = read_secret(configured_settings.api_token_file)
        except OSError:
            token = None
        if token is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "detail": {
                        "code": "api_remote_access_disabled",
                        "message": "API từ xa chưa được cấu hình token",
                        "retryable": False,
                    }
                },
            )
        if len(token.encode("utf-8")) < 32:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "detail": {
                        "code": "api_remote_token_invalid",
                        "message": "Token API từ xa phải dài ít nhất 32 byte",
                        "retryable": False,
                    }
                },
            )
        scheme, _, credential = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(credential, token):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer"},
                content={
                    "detail": {
                        "code": "api_auth_required",
                        "message": "Token API không hợp lệ",
                        "retryable": False,
                    }
                },
            )
        return await call_next(request)

    @application.middleware("http")
    async def enforce_http_security(request: Request, call_next: Any) -> Any:
        hostname = _request_hostname(request.headers.get("Host", ""))
        allowed_hosts = {host.casefold() for host in configured_settings.api_allowed_hosts}
        if hostname not in allowed_hosts:
            response = PlainTextResponse("Invalid host header", status_code=400)
        else:
            response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if (
            configured_settings.api_access_policy == "remote"
            or request.url.path.startswith("/v1/admin/")
        ):
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Pragma"] = "no-cache"
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request,
        exception: RequestValidationError,
    ) -> JSONResponse:
        fields = [
            ".".join(str(part) for part in item.get("loc", ()) if part != "body")
            for item in exception.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": {
                    "code": "invalid_request",
                    "message": "Dữ liệu yêu cầu không hợp lệ",
                    "retryable": False,
                    "fields": fields,
                }
            },
        )

    def job_store(request: Request) -> StateStore:
        return request.app.state.job_store

    def acquisition(request: Request) -> AcquisitionPort:
        service = request.app.state.acquisition
        if service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "acquisition_not_configured",
                    "message": "Dịch vụ tìm kiếm và tải nguồn chưa được cấu hình",
                    "retryable": True,
                },
            )
        return service

    def job_coordinator(request: Request) -> CoordinatorPort:
        configured = request.app.state.coordinator
        if configured is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "coordinator_not_configured",
                    "message": "Bộ điều phối tải nguồn chưa được cấu hình",
                    "retryable": True,
                },
            )
        return configured

    def local_admin_request(
        request: Request,
        marker: Annotated[
            str | None,
            Header(alias="X-Dub-Admin-Request"),
        ] = None,
    ) -> None:
        """Require an explicit header and a direct loopback client."""

        host = request.client.host if request.client is not None else ""
        try:
            address = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            allowed = False
        else:
            mapped = getattr(address, "ipv4_mapped", None)
            allowed = address.is_loopback or bool(mapped and mapped.is_loopback)
        if marker != "1" or not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "admin_local_access_required",
                    "message": "Tính năng quản trị chỉ dùng được từ máy cục bộ",
                    "retryable": False,
                },
            )

    def admin_error(error: AdminIntegrationError) -> HTTPException:
        return HTTPException(
            status_code=error.status_code,
            detail={
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
            },
        )

    def prowlarr_admin(request: Request) -> ProwlarrAdminClient:
        try:
            api_key = read_secret(configured_settings.prowlarr_api_key_file)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "prowlarr_secret_unreadable",
                    "message": "Không thể đọc khóa API Prowlarr",
                    "retryable": False,
                },
            ) from exc
        if api_key is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "prowlarr_not_configured",
                    "message": "Prowlarr chưa được cấu hình",
                    "retryable": False,
                },
            )
        try:
            return ProwlarrAdminClient(
                base_url=configured_settings.prowlarr_url,
                api_key=api_key,
                client=request.app.state.admin_http_client,
            )
        except AdminIntegrationError as exc:
            raise admin_error(exc) from exc

    StoreDependency = Annotated[StateStore, Depends(job_store)]
    AcquisitionDependency = Annotated[AcquisitionPort, Depends(acquisition)]
    CoordinatorDependency = Annotated[CoordinatorPort, Depends(job_coordinator)]
    LocalAdminDependency = Annotated[None, Depends(local_admin_request)]

    @application.get("/v1/health")
    def health(
        request: Request,
        store: StoreDependency,
    ) -> Any:
        try:
            journal_mode = store.journal_mode()
            database_status = "ok"
        except sqlite3.Error:
            journal_mode = "unknown"
            database_status = "error"

        try:
            catalog = load_model_catalog(
                configured_settings.models_lock_path,
                configured_settings.models_dir,
            )
            catalog_status = "ok"
            model_count = len(catalog.models)
        except (OSError, ValueError, json.JSONDecodeError):
            catalog_status = "missing_or_invalid"
            model_count = 0

        gpu_report = _with_gpu_support_tier(
            read_gpu_report(
                configured_settings.gpu_report_path,
                max_age_seconds=configured_settings.gpu_report_max_age_seconds,
            )
        )

        healthy = database_status == "ok"
        degraded = (
            not gpu_report.get("ready", False)
            or catalog_status != "ok"
            or request.app.state.acquisition is None
        )
        payload = {
            "status": "error" if not healthy else ("degraded" if degraded else "ok"),
            "api_version": "v1",
            "compute_mode": configured_settings.compute_mode,
            "database": {
                "status": database_status,
                "journal_mode": journal_mode,
            },
            "model_catalog": {
                "status": catalog_status,
                "count": model_count,
            },
            "acquisition_configured": request.app.state.acquisition is not None,
            "coordinator_configured": request.app.state.coordinator is not None,
            "gpu": gpu_report,
        }
        if not healthy:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=payload,
            )
        return payload

    @application.get("/v1/capabilities")
    def capabilities() -> dict[str, Any]:
        return {
            "api_version": "v1",
            "media_types": ["movie"],
            "subtitle_modes": ["prefer", "manual", "asr"],
            "source_language": {
                "automatic_detection": True,
                "vietnamese_passthrough": True,
                "availability_depends_on_selected_model": True,
            },
            "outputs": ["mp4", "srt", "timing-report.json"],
            "offline_inference": True,
            "lip_sync": {
                "modes": ["off", "draft", "final"],
                "default": "off",
                "draft_backend": "musetalk-1.5",
                "draft_minimum_vram_mib": 4096,
                "final_backend": "latentsync-1.6",
                "final_minimum_vram_mib": 18432,
                "gpu_acceptance_required": True,
            },
            "cinematic_audio": {
                "dialogue_removed": True,
                "music_and_effects_preserved": True,
                "narration_ducking": True,
            },
            "models": {
                "runtime_selection": True,
                "explicit_install_only": True,
                "runtime_downloads": False,
            },
            "artifact_download_endpoint": True,
            "local_upload": {
                "enabled": True,
                "media_extensions": [".mp4", ".mkv"],
                "subtitle_extensions": [".srt"],
                "media_max_bytes": configured_settings.upload_media_max_bytes,
                "subtitle_max_bytes": configured_settings.upload_subtitle_max_bytes,
                "session_ttl_seconds": (
                    configured_settings.upload_session_ttl_seconds
                ),
                "timing_profiles": ["natural", "strict"],
            },
            "one_active_job_per_gpu": True,
            "drm_supported": False,
        }

    @application.get(
        "/v1/admin/integrations",
        response_model=AdminIntegrationsResponse,
    )
    def admin_integrations(
        _admin: LocalAdminDependency,
    ) -> AdminIntegrationsResponse:
        try:
            prowlarr_configured = (
                read_secret(configured_settings.prowlarr_api_key_file) is not None
            )
        except OSError:
            prowlarr_configured = False
        try:
            opensubtitles_api_key = read_secret(
                configured_settings.opensubtitles_api_key_file
            )
            opensubtitles_token = read_secret(
                configured_settings.opensubtitles_token_file
            )
            persisted_opensubtitles_base_url = read_secret(
                configured_settings.opensubtitles_base_url_file
            )
            normalize_opensubtitles_api_root(
                persisted_opensubtitles_base_url
                if persisted_opensubtitles_base_url is not None
                else configured_settings.opensubtitles_url
            )
            opensubtitles_base_url_valid = True
        except (AdminIntegrationError, OSError, ValueError):
            opensubtitles_api_key = None
            opensubtitles_token = None
            opensubtitles_base_url_valid = False

        opensubtitles_paths = _opensubtitles_secret_paths(configured_settings)
        opensubtitles_configured = bool(
            opensubtitles_api_key
            and opensubtitles_token
            and opensubtitles_base_url_valid
        )
        cleanup_pending = bool(
            opensubtitles_paths is not None
            and has_pending_secret_deletion(opensubtitles_paths)
        )
        editable = bool(
            opensubtitles_paths is not None
            and can_manage_secret_bundle(opensubtitles_paths)
        )
        can_delete = bool(
            opensubtitles_paths is not None
            and can_delete_secret_bundle(opensubtitles_paths)
            and (opensubtitles_configured or cleanup_pending)
        )
        return AdminIntegrationsResponse(
            prowlarr=AdminIntegrationStatus(
                configured=prowlarr_configured,
                editable=False,
                can_manage=False,
            ),
            opensubtitles=AdminIntegrationStatus(
                configured=opensubtitles_configured,
                editable=editable,
                can_manage=editable,
                cleanup_pending=cleanup_pending,
                can_delete=can_delete,
            ),
        )

    @application.get(
        "/v1/admin/prowlarr/indexers",
        response_model=ProwlarrIndexerListResponse,
    )
    async def admin_prowlarr_indexers(
        request: Request,
        _admin: LocalAdminDependency,
    ) -> ProwlarrIndexerListResponse:
        adapter = prowlarr_admin(request)
        try:
            items = await adapter.list_indexers()
        except AdminIntegrationError as exc:
            raise admin_error(exc) from exc
        validated = [ProwlarrIndexerResponse.model_validate(item) for item in items]
        return ProwlarrIndexerListResponse(items=validated, count=len(validated))

    @application.post(
        "/v1/admin/prowlarr/test-all",
        response_model=ProwlarrTestAllResponse,
    )
    async def admin_prowlarr_test_all(
        request: Request,
        _admin: LocalAdminDependency,
    ) -> ProwlarrTestAllResponse:
        adapter = prowlarr_admin(request)
        try:
            payload = await adapter.test_all()
        except AdminIntegrationError as exc:
            raise admin_error(exc) from exc
        return ProwlarrTestAllResponse.model_validate(payload)

    @application.put(
        "/v1/admin/opensubtitles",
        response_model=OpenSubtitlesConfigurationResponse,
    )
    async def admin_configure_opensubtitles(
        payload: OpenSubtitlesConfigureRequest,
        request: Request,
        _admin: LocalAdminDependency,
    ) -> OpenSubtitlesConfigurationResponse:
        opensubtitles_paths = _opensubtitles_secret_paths(configured_settings)
        if opensubtitles_paths is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "secret_store_read_only",
                    "message": (
                        "Bản triển khai này không cho phép sửa secret OpenSubtitles từ web"
                    ),
                    "retryable": False,
                },
            )
        if has_pending_secret_deletion(opensubtitles_paths):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "secret_cleanup_pending",
                    "message": (
                        "Secret OpenSubtitles cũ chưa được dọn; hãy thử xóa cấu hình lại"
                    ),
                    "retryable": True,
                },
            )
        if not can_manage_secret_bundle(opensubtitles_paths):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "secret_store_read_only",
                    "message": (
                        "Bản triển khai này không cho phép sửa secret OpenSubtitles từ web"
                    ),
                    "retryable": False,
                },
            )
        api_key_path, token_path, base_url_path = opensubtitles_paths

        api_key = payload.api_key.get_secret_value().strip()
        username = payload.username.strip()
        password = payload.password.get_secret_value()
        if not api_key or not username or not password:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "invalid_opensubtitles_credentials",
                    "message": "Thông tin đăng nhập OpenSubtitles không hợp lệ",
                    "retryable": False,
                },
            )

        async with admin_configuration_lock:
            if has_pending_secret_deletion(opensubtitles_paths):
                raise admin_error(
                    AdminIntegrationError(
                        status_code=409,
                        code="secret_cleanup_pending",
                        message="Secret OpenSubtitles cũ chưa được dọn; hãy thử xóa cấu hình lại",
                        retryable=True,
                    )
                )
            if not can_manage_secret_bundle(opensubtitles_paths):
                raise admin_error(
                    AdminIntegrationError(
                        status_code=503,
                        code="secret_store_read_only",
                        message="Bản triển khai này không cho phép sửa secret OpenSubtitles từ web",
                        retryable=False,
                    )
                )
            try:
                adapter = OpenSubtitlesAdminClient(
                    api_url=configured_settings.opensubtitles_url,
                    user_agent=configured_settings.opensubtitles_user_agent,
                    client=request.app.state.admin_http_client,
                )
                token, api_root, quota = await adapter.login_and_test(
                    api_key=api_key,
                    username=username,
                    password=password,
                )
                await asyncio.to_thread(
                    atomic_write_secret_bundle,
                    (
                        (api_key_path, api_key),
                        (token_path, token),
                        (base_url_path, api_root),
                    ),
                )
            except AdminIntegrationError as exc:
                raise admin_error(exc) from exc
        return OpenSubtitlesConfigurationResponse(
            configured=True,
            authenticated=True,
            restart_required=True,
            quota=quota,
        )

    @application.delete(
        "/v1/admin/opensubtitles",
        response_model=OpenSubtitlesConfigurationResponse,
    )
    async def admin_delete_opensubtitles(
        _payload: OpenSubtitlesDeleteRequest,
        _admin: LocalAdminDependency,
    ) -> OpenSubtitlesConfigurationResponse:
        opensubtitles_paths = _opensubtitles_secret_paths(configured_settings)
        if (
            opensubtitles_paths is None
            or not can_delete_secret_bundle(opensubtitles_paths)
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "secret_store_read_only",
                    "message": (
                        "Bản triển khai này không cho phép sửa secret OpenSubtitles từ web"
                    ),
                    "retryable": False,
                },
            )
        async with admin_configuration_lock:
            if not can_delete_secret_bundle(opensubtitles_paths):
                raise admin_error(
                    AdminIntegrationError(
                        status_code=503,
                        code="secret_store_read_only",
                        message="Bản triển khai này không cho phép sửa secret OpenSubtitles từ web",
                        retryable=False,
                    )
                )
            try:
                await asyncio.to_thread(
                    delete_secret_bundle,
                    opensubtitles_paths,
                )
            except AdminIntegrationError as exc:
                raise admin_error(exc) from exc
        return OpenSubtitlesConfigurationResponse(
            configured=False,
            authenticated=False,
            restart_required=True,
        )

    @application.get("/v1/models", response_model=ModelCatalog)
    def models() -> ModelCatalog:
        try:
            return load_model_catalog(
                configured_settings.models_lock_path,
                configured_settings.models_dir,
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "model_catalog_missing",
                    "message": "Không tìm thấy danh mục model cục bộ",
                    "retryable": False,
                },
            ) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": "model_catalog_invalid",
                    "message": "Danh mục model cục bộ không hợp lệ",
                    "retryable": False,
                },
            ) from exc

    @application.post(
        "/v1/uploads",
        response_model=UploadSessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_upload_session(
        payload: UploadCreateRequest,
    ) -> UploadSessionResponse:
        """Reserve an application-owned directory before raw-body uploads."""

        if payload.rights_confirmed is not True:
            raise _upload_error(
                status.HTTP_403_FORBIDDEN,
                "rights_confirmation_required",
                "Bạn phải xác nhận có quyền tải lên và xử lý nội dung này",
            )
        if payload.voice is not None and payload.voice_rights_confirmed is not True:
            raise _upload_error(
                status.HTTP_403_FORBIDDEN,
                "voice_rights_confirmation_required",
                "Bạn phải xác nhận có quyền sử dụng giọng tham chiếu",
            )
        media_filename, _ = _validated_upload_filename(
            payload.media_filename,
            allowed_extensions=frozenset({".mp4", ".mkv"}),
            code="unsupported_upload_media",
            message="Chỉ chấp nhận file video MP4 hoặc MKV",
        )
        subtitle_filename: str | None = None
        if payload.subtitle_filename is not None:
            subtitle_filename, _ = _validated_upload_filename(
                payload.subtitle_filename,
                allowed_extensions=frozenset({".srt"}),
                code="unsupported_upload_subtitle",
                message="Phụ đề tải lên phải là file SRT",
            )
        source_language = payload.source_language.strip().lower().replace("_", "-")
        if not source_language:
            raise _upload_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "source_language_invalid",
                "Ngôn ngữ nguồn không hợp lệ",
            )
        if subtitle_filename is not None and source_language in {
            "auto",
            "und",
            "unknown",
            "mul",
            "zxx",
        }:
            raise _upload_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "subtitle_language_required",
                "Phải chọn ngôn ngữ nguồn cụ thể khi tải phụ đề SRT",
            )

        try:
            catalog = load_model_catalog(
                configured_settings.models_lock_path,
                configured_settings.models_dir,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise _upload_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "model_catalog_unavailable",
                "Danh mục model cục bộ chưa sẵn sàng",
            ) from exc
        effective_models = ModelSelection(
            asr=(payload.models.asr or configured_settings.default_asr_model_id),
            translation=(
                payload.models.translation
                or configured_settings.default_translation_model_id
            ),
            separation=(
                payload.models.separation
                or configured_settings.default_separation_model_id
            ),
            tts=(payload.models.tts or configured_settings.default_tts_model_id),
        )
        _validate_model_selection(
            effective_models,
            catalog,
            gpu_report=read_gpu_report(
                configured_settings.gpu_report_path,
                max_age_seconds=configured_settings.gpu_report_max_age_seconds,
            ),
        )
        frozen_request = UploadCreateRequest.model_validate(
            {
                **payload.model_dump(mode="json"),
                "media_filename": media_filename,
                "subtitle_filename": subtitle_filename,
                "source_language": source_language,
                "models": effective_models.model_dump(mode="json"),
            }
        )

        configured_settings.ensure_local_directories()
        incoming_root = configured_settings.incoming_dir.resolve(strict=False)
        for _ in range(5):
            identifier = str(uuid.uuid4())
            directory = incoming_root / identifier
            try:
                directory.mkdir(mode=0o700, parents=False, exist_ok=False)
            except FileExistsError:
                continue
            break
        else:  # pragma: no cover - UUID collisions are not practically reachable.
            raise _upload_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "upload_session_create_failed",
                "Không thể cấp mã phiên tải file",
                retryable=True,
            )
        session = _UploadSessionState(
            id=identifier,
            status="awaiting_media",
            request=frozen_request,
        )
        try:
            _save_upload_session(configured_settings, session)
        except OSError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise _upload_error(
                status.HTTP_507_INSUFFICIENT_STORAGE,
                "upload_session_create_failed",
                "Không thể lưu checkpoint phiên tải file",
                retryable=True,
            ) from exc
        return _public_upload_session(session)

    @application.get(
        "/v1/uploads/{upload_id}",
        response_model=UploadSessionResponse,
    )
    def get_upload_session(upload_id: str) -> UploadSessionResponse:
        return _public_upload_session(
            _load_upload_session(configured_settings, upload_id)
        )

    async def receive_upload_artifact(
        upload_id: str,
        request: Request,
        store: StateStore,
        *,
        kind: Literal["media", "subtitle"],
    ) -> UploadSessionResponse:
        identifier = _validated_upload_id(upload_id)
        lock = upload_session_locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            session = _load_upload_session(configured_settings, identifier)
            if session.status == "finalized":
                raise _upload_error(
                    status.HTTP_409_CONFLICT,
                    "upload_already_finalized",
                    "Phiên tải file đã được tạo thành job",
                )
            directory = _upload_directory(configured_settings, identifier)
            if kind == "media":
                _, extension = _validated_upload_filename(
                    session.request.media_filename,
                    allowed_extensions=frozenset({".mp4", ".mkv"}),
                    code="unsupported_upload_media",
                    message="Chỉ chấp nhận file video MP4 hoặc MKV",
                )
                destination = directory / f"source{extension}"
                maximum = configured_settings.upload_media_max_bytes
                update_key = "media_size_bytes"
                digest_key = "media_sha256"
            else:
                if session.request.subtitle_filename is None:
                    raise _upload_error(
                        status.HTTP_409_CONFLICT,
                        "subtitle_not_declared",
                        "Phiên tải file này không khai báo phụ đề SRT",
                    )
                destination = directory / "source.srt"
                maximum = configured_settings.upload_subtitle_max_bytes
                update_key = "subtitle_size_bytes"
                digest_key = "subtitle_sha256"
            try:
                received, digest = await _stream_upload_body(
                    request,
                    destination,
                    maximum=maximum,
                    store=store,
                    upload_id=identifier,
                    kind=kind,
                    quota_bytes=configured_settings.incoming_quota_bytes,
                )
                updated = session.model_copy(
                    update={update_key: received, digest_key: digest}
                )
                updated = updated.model_copy(
                    update={"status": _next_upload_status(updated)}
                )
                _save_upload_session(configured_settings, updated)
            except HTTPException:
                raise
            except OSError as exc:
                raise _upload_error(
                    status.HTTP_507_INSUFFICIENT_STORAGE,
                    "upload_write_failed",
                    "Không thể ghi file tải lên",
                    retryable=True,
                ) from exc
            return _public_upload_session(updated)

    @application.put(
        "/v1/uploads/{upload_id}/media",
        response_model=UploadSessionResponse,
    )
    async def upload_media(
        upload_id: str,
        request: Request,
        store: StoreDependency,
    ) -> UploadSessionResponse:
        return await receive_upload_artifact(upload_id, request, store, kind="media")

    @application.put(
        "/v1/uploads/{upload_id}/subtitle",
        response_model=UploadSessionResponse,
    )
    async def upload_subtitle(
        upload_id: str,
        request: Request,
        store: StoreDependency,
    ) -> UploadSessionResponse:
        return await receive_upload_artifact(upload_id, request, store, kind="subtitle")

    @application.post(
        "/v1/uploads/{upload_id}/finalize",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def finalize_upload(
        upload_id: str,
        store: StoreDependency,
    ) -> JobResponse:
        identifier = _validated_upload_id(upload_id)
        lock = upload_session_locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            session = _load_upload_session(configured_settings, identifier)
            directory = _upload_directory(configured_settings, identifier)
            expected_release_id = f"local-upload:{identifier}"
            try:
                existing = store.get_job(identifier)
            except JobNotFound:
                existing = None
            if existing is not None:
                if existing.release_id != expected_release_id:
                    raise _upload_error(
                        status.HTTP_409_CONFLICT,
                        "upload_job_conflict",
                        "Mã phiên tải file đã thuộc về một job khác",
                    )
                if session.job_id is None:
                    session = session.model_copy(
                        update={"status": "finalized", "job_id": existing.id}
                    )
                    try:
                        _save_upload_session(configured_settings, session)
                    except OSError:
                        _LOGGER.warning(
                            "Could not repair finalized upload metadata for %s",
                            identifier,
                        )
                try:
                    (directory / "source.srt").unlink(missing_ok=True)
                except OSError:
                    _LOGGER.warning(
                        "Could not remove finalized upload subtitle for %s",
                        identifier,
                    )
                return _job_response(existing)
            if session.status != "ready":
                raise _upload_error(
                    status.HTTP_409_CONFLICT,
                    "upload_incomplete",
                    "Phải tải đủ video và phụ đề đã khai báo trước khi tạo job",
                    retryable=True,
                )

            _, media_extension = _validated_upload_filename(
                session.request.media_filename,
                allowed_extensions=frozenset({".mp4", ".mkv"}),
                code="unsupported_upload_media",
                message="Chỉ chấp nhận file video MP4 hoặc MKV",
            )
            media_path = directory / f"source{media_extension}"
            media_identity = _regular_file_identity(
                media_path,
                maximum=configured_settings.upload_media_max_bytes,
            )
            try:
                media_digest, media_size = await asyncio.to_thread(
                    _sealed_file_sha256,
                    media_path,
                )
            except OSError as exc:
                raise _upload_error(
                    status.HTTP_409_CONFLICT,
                    "upload_artifact_changed",
                    "Không thể xác minh file video đã tải lên",
                    retryable=True,
                ) from exc
            if (
                media_size != session.media_size_bytes
                or media_digest != session.media_sha256
            ):
                raise _upload_error(
                    status.HTTP_409_CONFLICT,
                    "upload_artifact_changed",
                    "File video đã thay đổi sau khi tải lên",
                    retryable=True,
                )
            try:
                media = await local_media_probe.probe(
                    media_path,
                    source_language=session.request.source_language,
                    title=Path(session.request.media_filename).stem,
                    require_h264_passthrough=True,
                    allow_hevc_transcode=True,
                )
            except MediaProbeError as exc:
                raise _upload_error(
                    (
                        status.HTTP_503_SERVICE_UNAVAILABLE
                        if exc.retryable
                        else status.HTTP_422_UNPROCESSABLE_CONTENT
                    ),
                    exc.code,
                    exc.message_vi,
                    retryable=exc.retryable,
                ) from exc
            if media_identity != _regular_file_identity(
                media_path,
                maximum=configured_settings.upload_media_max_bytes,
            ):
                raise _upload_error(
                    status.HTTP_409_CONFLICT,
                    "upload_artifact_changed",
                    "File video đã thay đổi trong lúc kiểm tra",
                    retryable=True,
                )

            selected_subtitle: dict[str, Any] | None = None
            source_subtitle_path: Path | None = None
            if session.request.subtitle_filename is not None:
                uploaded_subtitle = directory / "source.srt"
                subtitle_identity = _regular_file_identity(
                    uploaded_subtitle,
                    maximum=configured_settings.upload_subtitle_max_bytes,
                )
                try:
                    subtitle_digest, subtitle_size = await asyncio.to_thread(
                        _sealed_file_sha256,
                        uploaded_subtitle,
                    )
                except OSError as exc:
                    raise _upload_error(
                        status.HTTP_409_CONFLICT,
                        "upload_artifact_changed",
                        "Không thể xác minh file phụ đề đã tải lên",
                        retryable=True,
                    ) from exc
                if (
                    subtitle_size != session.subtitle_size_bytes
                    or subtitle_digest != session.subtitle_sha256
                ):
                    raise _upload_error(
                        status.HTTP_409_CONFLICT,
                        "upload_artifact_changed",
                        "File phụ đề đã thay đổi sau khi tải lên",
                        retryable=True,
                    )
                try:
                    await asyncio.to_thread(
                        parse_subtitle_file,
                        uploaded_subtitle,
                        language=session.request.source_language,
                        duration_us=media.duration_us,
                        subtitle_format=SubtitleFormat.SRT,
                    )
                except (OSError, TranscriptError, ValueError) as exc:
                    raise _upload_error(
                        status.HTTP_422_UNPROCESSABLE_CONTENT,
                        "uploaded_subtitle_invalid",
                        "File SRT tải lên không tạo được transcript hợp lệ",
                    ) from exc
                if subtitle_identity != _regular_file_identity(
                    uploaded_subtitle,
                    maximum=configured_settings.upload_subtitle_max_bytes,
                ):
                    raise _upload_error(
                        status.HTTP_409_CONFLICT,
                        "upload_artifact_changed",
                        "File phụ đề đã thay đổi trong lúc kiểm tra",
                        retryable=True,
                    )
                jobs_root = configured_settings.jobs_dir.resolve(strict=False)
                job_directory = jobs_root / identifier
                try:
                    job_directory.mkdir(mode=0o750, parents=False, exist_ok=True)
                    job_metadata = job_directory.lstat()
                except OSError as exc:
                    raise _upload_error(
                        status.HTTP_507_INSUFFICIENT_STORAGE,
                        "subtitle_store_failed",
                        "Không thể chuẩn bị thư mục phụ đề của job",
                        retryable=True,
                    ) from exc
                if (
                    not stat_module.S_ISDIR(job_metadata.st_mode)
                    or job_directory.is_symlink()
                    or not job_directory.absolute().is_relative_to(jobs_root.absolute())
                ):
                    raise _upload_error(
                        status.HTTP_409_CONFLICT,
                        "upload_path_invalid",
                        "Thư mục artifact của job không an toàn",
                    )
                source_subtitle_path = job_directory / "source-subtitle.srt"
                try:
                    _copy_regular_file(
                        uploaded_subtitle,
                        source_subtitle_path,
                        maximum=configured_settings.upload_subtitle_max_bytes,
                        expected_identity=subtitle_identity,
                    )
                except OSError as exc:
                    raise _upload_error(
                        status.HTTP_507_INSUFFICIENT_STORAGE,
                        "subtitle_store_failed",
                        "Không thể lưu phụ đề SRT cho job",
                        retryable=True,
                    ) from exc
                selected_subtitle = {
                    "subtitle_id": f"upload:{identifier}",
                    "source": "local_upload",
                    "language": session.request.source_language,
                    "format": "srt",
                    "score": 1.0,
                    "high_confidence": True,
                    "release_name": session.request.subtitle_filename,
                }

            selected_media = asdict(media)
            selected_media.pop("path", None)
            selected_media["media_kind"] = media.media_kind.value
            selected_media["relative_path"] = media_path.name
            selected_media["size_bytes"] = media_size
            transcript_source = (
                "subtitle" if source_subtitle_path is not None else "asr"
            )
            details: dict[str, Any] = {
                "source_kind": "local_upload",
                "source_media_path": str(media_path),
                "selected_media": selected_media,
                "downloaded_bytes": media_size,
                "total_bytes": media_size,
                "download_progress": 1.0,
                "stage_progress_permille": 1000,
                "transcript_source": transcript_source,
                "selected_subtitle": selected_subtitle,
                "source_subtitle_path": (
                    str(source_subtitle_path)
                    if source_subtitle_path is not None
                    else None
                ),
                "subtitle_candidates": [],
                "upload": {
                    "id": identifier,
                    "media_filename": session.request.media_filename,
                    "subtitle_filename": session.request.subtitle_filename,
                },
            }
            spec = session.request.model_dump(mode="json")
            spec.update(
                {
                    "release_id": expected_release_id,
                    "source_kind": "local_upload",
                    "subtitle_mode": (
                        "manual" if transcript_source == "subtitle" else "asr"
                    ),
                }
            )
            acquisition_checkpoint = {
                "source": "local_upload",
                "selected_media": selected_media,
                "source_media_path": str(media_path),
                "downloaded_bytes": media_size,
                "total_bytes": media_size,
                "download_progress": 1.0,
            }
            subtitle_checkpoint = {
                "mode": spec["subtitle_mode"],
                "transcript_source": transcript_source,
                "selected_subtitle": selected_subtitle,
                "source_subtitle_path": details["source_subtitle_path"],
                "candidates": [],
            }
            try:
                record = store.create_ready_offline_job(
                    expected_release_id,
                    spec,
                    details,
                    acquisition_checkpoint=acquisition_checkpoint,
                    subtitle_checkpoint=subtitle_checkpoint,
                    job_id=identifier,
                )
            except ActiveJobExists as exc:
                active = store.list_active_jobs(limit=1)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "active_job_exists",
                        "message": str(exc),
                        "retryable": True,
                        "active_job_id": active[0].id if active else None,
                    },
                ) from exc
            except DuplicateJob as exc:
                record = store.get_job(identifier)
                if record.release_id != expected_release_id:
                    raise _upload_error(
                        status.HTTP_409_CONFLICT,
                        "upload_job_conflict",
                        "Mã phiên tải file đã thuộc về một job khác",
                    ) from exc

            session = session.model_copy(
                update={"status": "finalized", "job_id": record.id}
            )
            try:
                _save_upload_session(configured_settings, session)
                if source_subtitle_path is not None:
                    (directory / "source.srt").unlink(missing_ok=True)
            except OSError:
                _LOGGER.warning(
                    "Could not seal finalized upload metadata for %s",
                    identifier,
                )
            return _job_response(record)

    @application.delete(
        "/v1/uploads/{upload_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_upload(upload_id: str, store: StoreDependency) -> Response:
        identifier = _validated_upload_id(upload_id)
        lock = upload_session_locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            session = _load_upload_session(configured_settings, identifier)
            try:
                existing = store.get_job(identifier)
            except JobNotFound:
                existing = None
            if session.status == "finalized" or existing is not None:
                raise _upload_error(
                    status.HTTP_409_CONFLICT,
                    "upload_already_finalized",
                    "Phiên đã thành job; hãy dùng API hủy job",
                )
            directory = _upload_directory(configured_settings, identifier)
            incoming_root = configured_settings.incoming_dir.resolve(strict=False)
            if not directory.absolute().is_relative_to(incoming_root.absolute()):
                raise _upload_error(
                    status.HTTP_409_CONFLICT,
                    "upload_path_invalid",
                    "Thư mục phiên tải file không an toàn",
                )
            prepared = configured_settings.jobs_dir.resolve(strict=False) / identifier
            try:
                prepared_metadata = prepared.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _LOGGER.exception(
                    "Could not inspect prepared artifacts for upload %s",
                    identifier,
                )
                raise _upload_error(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "upload_cleanup_failed",
                    "Không thể xóa dữ liệu phiên tải file; hãy thử lại",
                    retryable=True,
                ) from exc
            else:
                jobs_root = configured_settings.jobs_dir.resolve(strict=False)
                if not (
                    stat_module.S_ISDIR(prepared_metadata.st_mode)
                    and not prepared.is_symlink()
                    and prepared.absolute().is_relative_to(jobs_root.absolute())
                ):
                    _LOGGER.error(
                        "Refusing to remove unsafe prepared artifacts for upload %s",
                        identifier,
                    )
                    raise _upload_error(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        "upload_cleanup_failed",
                        "Không thể xóa dữ liệu phiên tải file; hãy thử lại",
                        retryable=True,
                    )
                try:
                    shutil.rmtree(prepared)
                except OSError as exc:
                    _LOGGER.exception(
                        "Could not remove prepared artifacts for upload %s",
                        identifier,
                    )
                    raise _upload_error(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        "upload_cleanup_failed",
                        "Không thể xóa dữ liệu phiên tải file; hãy thử lại",
                        retryable=True,
                    ) from exc
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                _LOGGER.exception(
                    "Could not remove upload session %s",
                    identifier,
                )
                raise _upload_error(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "upload_cleanup_failed",
                    "Không thể xóa dữ liệu phiên tải file; hãy thử lại",
                    retryable=True,
                ) from exc
            return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.post("/v1/search", response_model=SearchResponse)
    async def search(
        payload: SearchRequest,
        service: AcquisitionDependency,
    ) -> SearchResponse:
        query = " ".join(payload.query.split())
        if not query:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "empty_query",
                    "message": "Từ khóa tìm kiếm không được để trống",
                    "retryable": False,
                },
            )
        try:
            results = await service.search(
                MediaQuery(
                    query=query,
                    year=payload.year,
                    media_kind=MediaKind(payload.media_type),
                )
            )
        except AcquisitionError as exc:
            raise HTTPException(
                status_code=_acquisition_error_status(exc),
                detail={
                    "code": exc.code.value,
                    "message": exc.message_vi,
                    "retryable": exc.retryable,
                },
            ) from exc
        return SearchResponse(results=[_public_release(item) for item in results])

    @application.post(
        "/v1/jobs",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_job(
        payload: JobCreateRequest,
        store: StoreDependency,
        service: AcquisitionDependency,
    ) -> JobResponse:
        if payload.rights_confirmed is not True:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "rights_confirmation_required",
                    "message": "Bạn phải xác nhận có quyền tải và xử lý nội dung này",
                    "retryable": False,
                },
            )
        if payload.voice is not None and payload.voice_rights_confirmed is not True:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "voice_rights_confirmation_required",
                    "message": "Bạn phải xác nhận có quyền sử dụng giọng tham chiếu",
                    "retryable": False,
                },
            )

        try:
            catalog = load_model_catalog(
                configured_settings.models_lock_path,
                configured_settings.models_dir,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "model_catalog_unavailable",
                    "message": "Danh mục model cục bộ chưa sẵn sàng",
                    "retryable": False,
                },
            ) from exc
        effective_models = ModelSelection(
            asr=(payload.models.asr or configured_settings.default_asr_model_id),
            translation=(
                payload.models.translation
                or configured_settings.default_translation_model_id
            ),
            separation=(
                payload.models.separation
                or configured_settings.default_separation_model_id
            ),
            tts=(payload.models.tts or configured_settings.default_tts_model_id),
        )
        _validate_model_selection(
            effective_models,
            catalog,
            gpu_report=read_gpu_report(
                configured_settings.gpu_report_path,
                max_age_seconds=configured_settings.gpu_report_max_age_seconds,
            ),
        )

        active_jobs = store.list_active_jobs(limit=1)
        if active_jobs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "active_job_exists",
                    "message": "Đã có một job nặng đang hoạt động trên GPU",
                    "retryable": True,
                    "active_job_id": active_jobs[0].id,
                },
            )

        spec = payload.model_dump(mode="json")
        # Freeze defaults into the durable job so changing server config can
        # never silently switch a resumed inference job to a different model.
        spec["models"] = effective_models.model_dump(mode="json")
        release_lookup = getattr(service, "release", None)
        selected_release = (
            release_lookup(payload.release_id) if callable(release_lookup) else None
        )
        query_lookup = getattr(service, "release_query", None)
        release_query = (
            query_lookup(payload.release_id) if callable(query_lookup) else None
        )
        if isinstance(release_query, MediaQuery):
            spec["year"] = release_query.year
            spec["media_type"] = release_query.media_kind.value
            spec["search_query"] = release_query.query

        try:
            record = store.create_job(
                payload.release_id,
                spec,
            )
        except ActiveJobExists as exc:
            active = store.list_active_jobs(limit=1)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "active_job_exists",
                    "message": str(exc),
                    "retryable": True,
                    "active_job_id": active[0].id if active else None,
                },
            ) from exc
        if isinstance(selected_release, ReleaseCandidate):
            restart_token = _release_snapshot(selected_release)
            if restart_token is not None:
                store.save_checkpoint(
                    record.id,
                    JobStage.ACQUISITION,
                    {"release": restart_token},
                )
        track_job_operation(record.id)
        destination = configured_settings.incoming_dir / record.id
        created_task_id: str | None = None
        created_task_name = ""
        created_resume_attempted = False

        async def settle_created_task_cancel() -> JobRecord:
            current = store.get_job(record.id)
            if created_task_id is None or current.status is JobStatus.CANCELLED:
                return current
            current = store.update_status(
                record.id,
                current.status,
                details={
                    **current.details,
                    "task_id": created_task_id,
                    "name": created_task_name,
                },
            )
            pause_result = await pause_backend_for_cancel(
                store,
                service,
                record.id,
                created_task_id,
                timeout=1.5,
            )
            if pause_result != "paused":
                warning_code = (
                    "backend_cleanup_deferred"
                    if pause_result == "deferred"
                    else "download_pause_failed"
                )
                return store.append_warning(
                    record.id,
                    warning_code,
                    "Job đã hủy nhưng chưa thể tạm dừng torrent; dữ liệu nguồn vẫn được giữ lại",
                )
            return store.finalize_cancel(record.id)

        async def fail_created_download(
            code: str,
            message: str,
            *,
            retryable: bool,
        ) -> JobRecord:
            current = store.get_job(record.id)
            if created_resume_attempted and created_task_id is not None:
                pause_result = await pause_backend_for_cancel(
                    store,
                    service,
                    record.id,
                    created_task_id,
                    timeout=1.5,
                )
                if pause_result != "paused":
                    return store.update_status(
                        current.id,
                        current.status,
                        details={
                            **current.details,
                            "task_id": created_task_id,
                            "name": created_task_name,
                            "backend_started": True,
                            "backend_state_uncertain": True,
                        },
                        error_code=code,
                        error_message=message,
                        retryable=True,
                    )
            return store.update_status(
                current.id,
                JobStatus.FAILED,
                error_code=code,
                error_message=message,
                retryable=retryable,
            )

        try:
            task = await mutate_backend(
                service.start_download,
                payload.release_id,
                destination,
                rights_confirmed=True,
                paused=True,
            )
            created_task_id = str(_adapter_value(task, "task_id", ""))
            created_task_name = str(_adapter_value(task, "name", ""))
            if not created_task_id:
                raise RuntimeError("Download adapter returned no task id")
            current = store.get_job(record.id)
            if current.status in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
                return _job_response(await settle_created_task_cancel())
            record = store.update_status(
                record.id,
                JobStatus.DOWNLOADING,
                stage=JobStage.ACQUISITION,
                details={
                    "task_id": created_task_id,
                    "name": created_task_name,
                    "downloaded_bytes": 0,
                    "total_bytes": None,
                    "speed_bytes_per_second": 0,
                    "eta_seconds": None,
                    "backend_started": False,
                },
            )
            store.save_checkpoint(
                record.id,
                JobStage.ACQUISITION,
                {"task_id": created_task_id},
            )
            current = store.get_job(record.id)
            if current.status in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
                return _job_response(await settle_created_task_cancel())
            resume_download = getattr(service, "resume_download", None)
            if not callable(resume_download):
                raise RuntimeError("Acquisition service has no resume method")
            created_resume_attempted = True
            await mutate_backend(resume_download, created_task_id)
            current = store.get_job(record.id)
            if current.status in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
                return _job_response(await settle_created_task_cancel())
            record = store.update_status(
                record.id,
                JobStatus.DOWNLOADING,
                details={**current.details, "backend_started": True},
                expected_status=JobStatus.DOWNLOADING,
            )
        except AcquisitionError as exc:
            current = store.get_job(record.id)
            if current.status is JobStatus.CANCELLING:
                if created_task_id is not None:
                    return _job_response(await settle_created_task_cancel())
                return _job_response(store.finalize_cancel(record.id))
            if current.status is JobStatus.CANCELLED:
                return _job_response(current)
            await fail_created_download(
                exc.code.value,
                exc.message_vi,
                retryable=exc.retryable,
            )
            raise HTTPException(
                status_code=_acquisition_error_status(exc),
                detail={
                    "code": exc.code.value,
                    "message": exc.message_vi,
                    "retryable": exc.retryable,
                    "job_id": record.id,
                },
            ) from exc
        except ActiveJobExists as exc:
            active = store.list_active_jobs(limit=1)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "active_job_exists",
                    "message": str(exc),
                    "retryable": True,
                    "active_job_id": active[0].id if active else None,
                },
            ) from exc
        except InvalidTransition as exc:
            current = store.get_job(record.id)
            if current.status is JobStatus.CANCELLING:
                if created_task_id is not None:
                    return _job_response(await settle_created_task_cancel())
                return _job_response(store.finalize_cancel(record.id))
            if current.status is JobStatus.CANCELLED:
                return _job_response(current)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "invalid_job_transition",
                    "message": str(exc),
                    "retryable": False,
                },
            ) from exc
        except Exception as exc:
            current = store.get_job(record.id)
            if current.status is JobStatus.CANCELLING:
                if created_task_id is not None:
                    return _job_response(await settle_created_task_cancel())
                return _job_response(store.finalize_cancel(record.id))
            if current.status is JobStatus.CANCELLED:
                return _job_response(current)
            message = "Không thể khởi tạo tác vụ tải nguồn"
            await fail_created_download(
                "download_start_failed",
                message,
                retryable=True,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "download_start_failed",
                    "message": message,
                    "retryable": True,
                    "job_id": record.id,
                },
            ) from exc
        return _job_response(record)

    @application.get("/v1/jobs", response_model=JobListResponse)
    def list_jobs(
        store: StoreDependency,
        statuses: Annotated[
            list[JobStatus] | None,
            Query(alias="status"),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        newest_first: bool = True,
    ) -> JobListResponse:
        records = store.list_jobs(
            statuses=statuses,
            limit=limit,
            newest_first=newest_first,
        )
        items = [_job_response(record) for record in records]
        return JobListResponse(items=items, count=len(items))

    @application.get("/v1/storage/usage")
    def storage_usage(store: StoreDependency) -> dict[str, dict[str, int]]:
        usage = store.upload_storage_usage()
        return {
            "incoming": {
                "committed_bytes": usage.committed_bytes,
                "reserved_bytes": usage.reserved_bytes,
                "quota_bytes": configured_settings.incoming_quota_bytes,
                "free_bytes": shutil.disk_usage(configured_settings.incoming_dir).free,
            }
        }

    @application.post("/v1/storage/cleanup")
    def cleanup_storage(payload: dict[str, bool], store: StoreDependency) -> dict[str, Any]:
        apply = payload.get("apply") is True
        plan = plan_artifact_cleanup(
            store.list_jobs(limit=1000),
            jobs_root=configured_settings.jobs_dir,
            output_root=configured_settings.output_dir,
            completed_retention_days=configured_settings.completed_retention_days,
        )
        job_ids = sorted({action.job_id for action in plan.actions})
        if not apply:
            return {"dry_run": True, "job_ids": job_ids, **plan.to_dict()}
        result = execute_cleanup(plan, apply=True, state_store=store)
        failed_ids = {
            action.job_id
            for action in plan.actions
            if action not in result.removed and action not in result.missing
        }
        deleted: list[str] = []
        for job_id in job_ids:
            if job_id in failed_ids:
                continue
            try:
                current = store.get_job(job_id)
                if store.delete_terminal_job(job_id, current.revision):
                    deleted.append(job_id)
            except (JobNotFound, JobDeletionConflict):
                continue
        return {"dry_run": False, "deleted_job_ids": deleted, **result.to_dict()}

    @application.put("/v1/jobs/{job_id}/pin", response_model=JobResponse)
    def pin_job(job_id: str, payload: dict[str, bool], store: StoreDependency) -> JobResponse:
        try:
            return _job_response(
                store.set_job_pinned(job_id, payload.get("pinned") is True)
            )
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail={"code": "job_not_found"}) from exc

    @application.get("/v1/jobs/{job_id}", response_model=JobResponse)
    def get_job(job_id: str, store: StoreDependency) -> JobResponse:
        try:
            return _job_response(store.get_job(job_id))
        except JobNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc

    @application.delete(
        "/v1/jobs/{job_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_job(job_id: str, store: StoreDependency) -> Response:
        try:
            current = store.get_job(job_id)

            def remove_artifacts(_locked: JobRecord) -> bool:
                remove_terminal_job_artifacts(
                    job_id,
                    incoming_root=configured_settings.incoming_dir,
                    jobs_root=configured_settings.jobs_dir,
                    output_root=configured_settings.output_dir,
                )
                return True

            store.delete_terminal_job(
                job_id,
                current.revision,
                operation=remove_artifacts,
            )
        except JobNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc
        except JobDeletionConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "job_not_deletable",
                    "message": str(exc),
                    "retryable": False,
                },
            ) from exc
        except ArtifactCleanupError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "job_artifact_unsafe",
                    "message": str(exc),
                    "retryable": False,
                },
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "job_cleanup_failed",
                    "message": "Không thể xóa toàn bộ artifact job",
                    "retryable": True,
                },
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get("/v1/jobs/{job_id}/artifacts/{kind}")
    def get_job_artifact(
        job_id: str,
        kind: Literal["video", "subtitle", "timing"],
        store: StoreDependency,
    ) -> FileResponse:
        """Serve only deterministic, completed Phase 4 artifacts.

        Paths persisted in SQLite are treated as untrusted metadata.  The
        server derives each allowed path from its configured output/job roots
        and compares it with the sealed result before opening the file.
        """

        try:
            record = store.get_job(job_id)
        except JobNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc
        if record.status is not JobStatus.COMPLETED or record.result is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "artifact_not_ready",
                    "message": "Kết quả thuyết minh chưa sẵn sàng",
                    "retryable": True,
                },
            )

        if kind == "video":
            artifact_root = configured_settings.output_dir.resolve(strict=False)
            expected = artifact_root / f"{job_id}.mp4"
            result_field = "video_path"
            digest_field = "video_sha256"
            filename = f"{job_id}.mp4"
            media_type = "video/mp4"
        elif kind == "subtitle":
            artifact_root = configured_settings.output_dir.resolve(strict=False)
            expected = artifact_root / f"{job_id}.vi.srt"
            result_field = "srt_path"
            digest_field = "srt_sha256"
            filename = f"{job_id}.vi.srt"
            media_type = "application/x-subrip"
        else:
            artifact_root = configured_settings.jobs_dir.resolve(strict=False)
            expected = artifact_root / job_id / "timing-report.json"
            result_field = "timing_report_path"
            digest_field = "timing_report_sha256"
            filename = f"{job_id}.timing-report.json"
            media_type = "application/json"

        expected = expected.absolute()
        raw_path = record.result.get(result_field)
        raw_digest = record.result.get(digest_field)
        candidate = Path(raw_path).absolute() if isinstance(raw_path, str) else None
        if (
            candidate != expected
            or not expected.is_relative_to(artifact_root)
            or not isinstance(raw_digest, str)
            or len(raw_digest) != 64
            or any(character not in _SHA256_HEX for character in raw_digest)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "artifact_record_invalid",
                    "message": "Thông tin file kết quả không hợp lệ",
                    "retryable": False,
                },
            )
        try:
            if not expected.parent.resolve(strict=True).is_relative_to(artifact_root):
                raise OSError("artifact parent escapes configured root")
            actual_digest, actual_size = _sealed_file_sha256(expected)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={
                    "code": "artifact_missing",
                    "message": "File kết quả không còn trên máy chủ",
                    "retryable": False,
                },
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "artifact_record_invalid",
                    "message": "File kết quả không phải artifact cục bộ an toàn",
                    "retryable": False,
                },
            ) from exc
        recorded_size = record.result.get("size_bytes") if kind == "video" else None
        if actual_digest != raw_digest or (
            recorded_size is not None
            and (
                isinstance(recorded_size, bool)
                or not isinstance(recorded_size, int)
                or recorded_size != actual_size
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "artifact_integrity_failed",
                    "message": "File kết quả đã thay đổi sau khi được niêm phong",
                    "retryable": False,
                },
            )
        return FileResponse(
            expected,
            media_type=media_type,
            filename=filename,
        )

    @application.post(
        "/v1/jobs/{job_id}/language",
        response_model=JobResponse,
    )
    def select_source_language(
        job_id: str,
        payload: LanguageSelectionRequest,
        store: StoreDependency,
    ) -> JobResponse:
        """Resume local ASR after an uncertain automatic detection result."""

        try:
            selected = normalize_whisper_language(payload.language)
            if selected is None:
                raise TranscriptionError(
                    "unsupported_language",
                    "Bạn phải chọn một ngôn ngữ nguồn cụ thể",
                    retryable=False,
                )
            current = store.get_job(job_id)
            if current.status is not JobStatus.NEEDS_LANGUAGE:
                raise InvalidTransition(
                    "Job không chờ người dùng chọn ngôn ngữ nguồn"
                )
            details = dict(current.details)
            details["source_language_override"] = selected
            details["source_language_selected_by_user"] = True
            details.pop("language_detection_candidates", None)
            details.pop("language_candidates", None)
            resumed = store.update_status(
                job_id,
                JobStatus.TRANSCRIBING,
                expected_status=JobStatus.NEEDS_LANGUAGE,
                stage=JobStage.ASR,
                details=details,
            )
            return _job_response(resumed)
        except JobNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc
        except (InvalidTransition, TranscriptionError) as exc:
            code = (
                exc.code
                if isinstance(exc, TranscriptionError)
                else "invalid_job_transition"
            )
            message = (
                exc.message_vi if isinstance(exc, TranscriptionError) else str(exc)
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": code,
                    "message": message,
                    "retryable": False,
                },
            ) from exc

    @application.post("/v1/jobs/{job_id}/refresh", response_model=JobResponse)
    async def refresh_job(
        job_id: str,
        store: StoreDependency,
        configured: CoordinatorDependency,
    ) -> JobResponse:
        try:
            store.get_job(job_id)
            refreshed = await configured.refresh(job_id)
            record = refreshed if isinstance(refreshed, JobRecord) else store.get_job(job_id)
            return _job_response(record)
        except JobNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc
        except InvalidTransition as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "invalid_job_transition",
                    "message": str(exc),
                    "retryable": False,
                },
            ) from exc
        except AcquisitionError as exc:
            raise HTTPException(
                status_code=_acquisition_error_status(exc),
                detail={
                    "code": exc.code.value,
                    "message": exc.message_vi,
                    "retryable": exc.retryable,
                },
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "refresh_failed",
                    "message": "Không thể cập nhật tiến độ tải nguồn",
                    "retryable": True,
                },
            ) from exc

    @application.post(
        "/v1/jobs/{job_id}/subtitles/use-asr",
        response_model=JobResponse,
    )
    async def select_asr_for_job(
        job_id: str,
        store: StoreDependency,
        configured: CoordinatorDependency,
    ) -> JobResponse:
        try:
            store.get_job(job_id)
            return _job_response(await configured.select_asr(job_id))
        except JobNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc
        except ActiveJobExists as exc:
            active = store.list_active_jobs(limit=1)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "active_job_exists",
                    "message": str(exc),
                    "retryable": True,
                    "active_job_id": active[0].id if active else None,
                },
            ) from exc
        except AcquisitionError as exc:
            raise HTTPException(
                status_code=_acquisition_error_status(exc),
                detail={
                    "code": exc.code.value,
                    "message": exc.message_vi,
                    "retryable": exc.retryable,
                },
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "subtitle_selection_failed",
                    "message": "Không thể xác nhận dùng nhận dạng giọng nói",
                    "retryable": True,
                },
            ) from exc

    @application.post(
        "/v1/jobs/{job_id}/subtitles/{subtitle_id}",
        response_model=JobResponse,
    )
    async def select_subtitle_for_job(
        job_id: str,
        subtitle_id: str,
        store: StoreDependency,
        configured: CoordinatorDependency,
    ) -> JobResponse:
        try:
            store.get_job(job_id)
            return _job_response(
                await configured.select_subtitle(job_id, subtitle_id)
            )
        except JobNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc
        except ActiveJobExists as exc:
            active = store.list_active_jobs(limit=1)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "active_job_exists",
                    "message": str(exc),
                    "retryable": True,
                    "active_job_id": active[0].id if active else None,
                },
            ) from exc
        except AcquisitionError as exc:
            raise HTTPException(
                status_code=_acquisition_error_status(exc),
                detail={
                    "code": exc.code.value,
                    "message": exc.message_vi,
                    "retryable": exc.retryable,
                },
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "subtitle_selection_failed",
                    "message": "Không thể sử dụng phụ đề đã chọn",
                    "retryable": True,
                },
            ) from exc

    @application.post("/v1/jobs/{job_id}/cancel", response_model=JobResponse)
    async def cancel_job(
        job_id: str,
        request: Request,
        store: StoreDependency,
    ) -> JobResponse:
        try:
            current = store.get_job(job_id)
            if current.status is JobStatus.CANCELLED:
                return _job_response(current)
            offline_running = current.status in {
                JobStatus.TRANSCRIBING,
                JobStatus.SUBTITLE_SELECTED,
                JobStatus.TRANSLATING,
                JobStatus.SEPARATING,
                JobStatus.SYNTHESIZING,
                JobStatus.TIMING,
                JobStatus.MIXING,
                JobStatus.MUXING,
                JobStatus.VERIFYING,
            }
            offline_idle = current.status in {
                JobStatus.READY_OFFLINE,
                JobStatus.NEEDS_LANGUAGE,
                JobStatus.READY_TRANSLATION,
                JobStatus.READY_TTS,
            } or (
                current.status in {JobStatus.PAUSED, JobStatus.FAILED}
                and current.previous_status
                in {
                    JobStatus.READY_OFFLINE,
                    JobStatus.TRANSCRIBING,
                    JobStatus.SUBTITLE_SELECTED,
                    JobStatus.NEEDS_LANGUAGE,
                    JobStatus.READY_TRANSLATION,
                    JobStatus.TRANSLATING,
                    JobStatus.READY_TTS,
                    JobStatus.SEPARATING,
                    JobStatus.SYNTHESIZING,
                    JobStatus.TIMING,
                    JobStatus.MIXING,
                    JobStatus.MUXING,
                    JobStatus.VERIFYING,
                }
            )
            if offline_running or offline_idle or bool(
                current.details.get("offline_cancel_pending")
            ):
                cancelled = store.request_cancel(job_id)
                if offline_running:
                    cancelled = store.update_status(
                        job_id,
                        JobStatus.CANCELLING,
                        expected_status=JobStatus.CANCELLING,
                        details={
                            **cancelled.details,
                            "offline_cancel_pending": True,
                        },
                        cancel_requested=True,
                    )
                    # The worker owns native/GPU cleanup and finalizes only
                    # after inference has returned and VRAM is released.
                    return _job_response(cancelled)
                if bool(cancelled.details.get("offline_cancel_pending")):
                    return _job_response(cancelled)
                return _job_response(store.finalize_cancel(job_id))
            cancelled = store.request_cancel(job_id)
            if cancelled.status is JobStatus.CANCELLED:
                return _job_response(cancelled)
            in_flight = active_job_operations.get(job_id)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 1.8
            if (
                in_flight is not None
                and in_flight is not asyncio.current_task()
                and not in_flight.done()
            ):
                # Give a nearly-complete owner a chance to perform its ordered
                # compensation. Long adapter calls keep the durable
                # CANCELLING state and finish the pause themselves later.
                try:
                    await asyncio.wait_for(
                        asyncio.shield(in_flight),
                        timeout=max(0.0, deadline - loop.time()),
                    )
                except TimeoutError:
                    schedule_cancel_reconciliation(
                        in_flight,
                        store,
                        getattr(request.app.state, "acquisition", None),
                        job_id,
                    )
                    return _job_response(store.get_job(job_id))
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise
                except Exception:  # noqa: S110 - the store is re-read below to decide the real outcome
                    pass
                cancelled = store.get_job(job_id)
                if cancelled.status is JobStatus.CANCELLED:
                    return _job_response(cancelled)
            task_id = cancelled.details.get("task_id")
            service = getattr(request.app.state, "acquisition", None)
            if task_id:
                pause_result = await pause_backend_for_cancel(
                    store,
                    service,
                    job_id,
                    str(task_id),
                    timeout=max(0.05, deadline - loop.time()),
                )
                if pause_result == "paused":
                    cancelled = store.finalize_cancel(job_id)
                elif pause_result == "deferred":
                    cancelled = store.append_warning(
                        job_id,
                        "backend_cleanup_deferred",
                        "Chưa dừng torrent cũ vì một job khác đang hoạt động",
                    )
                else:
                    cancelled = store.append_warning(
                        job_id,
                        "download_pause_failed",
                        "Job đã hủy nhưng chưa thể tạm dừng torrent; dữ liệu nguồn vẫn được giữ lại",
                    )
            else:
                cancelled = store.finalize_cancel(job_id)
            return _job_response(cancelled)
        except JobNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc
        except InvalidTransition as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "invalid_job_transition",
                    "message": str(exc),
                    "retryable": False,
                },
            ) from exc
        except AcquisitionError as exc:
            raise HTTPException(
                status_code=_acquisition_error_status(exc),
                detail={
                    "code": exc.code.value,
                    "message": exc.message_vi,
                    "retryable": exc.retryable,
                },
            ) from exc

    @application.post("/v1/jobs/{job_id}/resume", response_model=JobResponse)
    async def resume_job(
        job_id: str,
        store: StoreDependency,
        service: AcquisitionDependency,
    ) -> JobResponse:
        operation_task_id: str | None = None
        operation_task_name = ""
        owns_operation = False
        resume_attempted = False

        async def settle_operation_cancel(task_id: str) -> JobRecord | None:
            current = store.get_job(job_id)
            if current.status not in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
                return None
            if current.status is JobStatus.CANCELLED:
                return current
            current = store.update_status(
                job_id,
                current.status,
                details={
                    **current.details,
                    "task_id": task_id,
                    "name": operation_task_name or current.details.get("name", ""),
                },
            )
            pause_result = await pause_backend_for_cancel(
                store,
                service,
                job_id,
                task_id,
                timeout=1.5,
            )
            if pause_result == "paused":
                if current.status is JobStatus.CANCELLING:
                    current = store.finalize_cancel(job_id)
            elif pause_result == "deferred":
                current = store.append_warning(
                    job_id,
                    "backend_cleanup_deferred",
                    "Chưa dừng torrent cũ vì một job khác đang hoạt động",
                )
            else:
                current = store.append_warning(
                    job_id,
                    "download_pause_failed",
                    "Job đã hủy nhưng chưa thể tạm dừng torrent; dữ liệu nguồn vẫn được giữ lại",
                )
            return current

        async def fail_resume_download(
            code: str,
            message: str,
            *,
            retryable: bool,
        ) -> JobRecord:
            current = store.get_job(job_id)
            if resume_attempted and operation_task_id is not None:
                pause_result = await pause_backend_for_cancel(
                    store,
                    service,
                    job_id,
                    operation_task_id,
                    timeout=1.5,
                )
                if pause_result not in {"paused", "deferred"} and current.active_slot:
                    return store.update_status(
                        current.id,
                        current.status,
                        details={
                            **current.details,
                            "task_id": operation_task_id,
                            "name": operation_task_name,
                            "backend_started": True,
                            "backend_state_uncertain": True,
                        },
                        error_code=code,
                        error_message=message,
                        retryable=True,
                    )
            return store.update_status(
                current.id,
                JobStatus.FAILED,
                error_code=code,
                error_message=message,
                retryable=retryable,
                force=(current.status is JobStatus.PAUSED),
            )

        try:
            initial = store.get_job(job_id)
            initial_task_id = initial.details.get("task_id")
            if initial_task_id:
                operation_task_id = str(initial_task_id)
                operation_task_name = str(initial.details.get("name", ""))
            track_job_operation(job_id)
            owns_operation = True
            active_jobs = [
                active
                for active in store.list_active_jobs(limit=2)
                if active.id != job_id
            ]
            if active_jobs:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "active_job_exists",
                        "message": "Đã có một job nặng đang hoạt động trên GPU",
                        "retryable": True,
                        "active_job_id": active_jobs[0].id,
                    },
                )
            record = store.resume(job_id)
            if record.status in {
                JobStatus.READY_OFFLINE,
                JobStatus.TRANSCRIBING,
                JobStatus.SUBTITLE_SELECTED,
                JobStatus.NEEDS_LANGUAGE,
                JobStatus.READY_TRANSLATION,
                JobStatus.TRANSLATING,
                JobStatus.READY_TTS,
                JobStatus.SEPARATING,
                JobStatus.SYNTHESIZING,
                JobStatus.TIMING,
                JobStatus.MIXING,
                JobStatus.MUXING,
                JobStatus.VERIFYING,
            }:
                # Offline stages are resumed by the GPU worker from their
                # durable artifact/checkpoint. A completed torrent task ID in
                # details must never route this request back to qBittorrent.
                return _job_response(record)
            resume_download = getattr(service, "resume_download", None)
            task_id = record.details.get("task_id")
            if task_id:
                operation_task_id = str(task_id)
                operation_task_name = str(record.details.get("name", ""))
                if record.status is JobStatus.DOWNLOADING:
                    record = store.update_status(
                        record.id,
                        record.status,
                        details={**record.details, "backend_started": False},
                        expected_status=record.status,
                    )
                if resume_download is None:
                    raise RuntimeError("Acquisition service has no resume method")
                relocate_download = getattr(service, "relocate_download", None)
                if callable(relocate_download):
                    await mutate_backend(
                        relocate_download,
                        operation_task_id,
                        configured_settings.incoming_dir / record.id,
                    )
                    current = store.get_job(job_id)
                    if current.status in {
                        JobStatus.CANCELLING,
                        JobStatus.CANCELLED,
                    }:
                        cancelled = await settle_operation_cancel(operation_task_id)
                        return _job_response(cancelled or current)
                resume_attempted = True
                await mutate_backend(resume_download, operation_task_id)
                current = store.get_job(job_id)
                if current.status in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
                    cancelled = await settle_operation_cancel(operation_task_id)
                    return _job_response(cancelled or current)
                if current.status is JobStatus.DOWNLOADING:
                    current = store.update_status(
                        current.id,
                        current.status,
                        details={**current.details, "backend_started": True},
                        expected_status=JobStatus.DOWNLOADING,
                    )
                record = current
            else:
                checkpoint = store.get_checkpoint(
                    record.id,
                    JobStage.ACQUISITION,
                )
                restored_release = _release_from_snapshot(
                    checkpoint.payload.get("release") if checkpoint else None,
                    expected_release_id=record.release_id,
                )
                restore_release = getattr(service, "restore_release", None)
                if restored_release is not None and callable(restore_release):
                    restore_release(restored_release)
                elif callable(restore_release):
                    search_query = record.spec.get("search_query")
                    if isinstance(search_query, str) and search_query.strip():
                        try:
                            media_kind = MediaKind(
                                str(record.spec.get("media_type", MediaKind.MOVIE.value))
                            )
                        except ValueError:
                            media_kind = MediaKind.MOVIE
                        search_results = await service.search(
                            MediaQuery(
                                query=search_query,
                                year=(
                                    int(record.spec["year"])
                                    if record.spec.get("year") is not None
                                    else None
                                ),
                                media_kind=media_kind,
                            )
                        )
                        restored_release = next(
                            (
                                candidate
                                for candidate in search_results
                                if candidate.release_id == record.release_id
                            ),
                            None,
                        )
                    if restored_release is None:
                        raise AcquisitionError(
                            AcquisitionErrorCode.DOWNLOAD_UNAVAILABLE,
                            "Không thể khôi phục bản phát hành; hãy tìm kiếm và chọn lại",
                            retryable=True,
                        )
                task = await mutate_backend(
                    service.start_download,
                    restored_release or record.release_id,
                    configured_settings.incoming_dir / record.id,
                    rights_confirmed=True,
                    paused=True,
                )
                operation_task_id = str(_adapter_value(task, "task_id", ""))
                operation_task_name = str(_adapter_value(task, "name", ""))
                task_id = operation_task_id
                if not operation_task_id:
                    raise RuntimeError("Download adapter returned no task id")
                current = store.get_job(job_id)
                if current.status in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
                    current = store.update_status(
                        job_id,
                        current.status,
                        details={
                            **current.details,
                            "task_id": operation_task_id,
                            "name": operation_task_name,
                        },
                    )
                    if cancelled := await settle_operation_cancel(operation_task_id):
                        return _job_response(cancelled)
                record = store.update_status(
                    record.id,
                    JobStatus.DOWNLOADING,
                    stage=JobStage.ACQUISITION,
                    details={
                        **record.details,
                        "task_id": operation_task_id,
                        "name": operation_task_name,
                        "downloaded_bytes": 0,
                        "total_bytes": None,
                        "speed_bytes_per_second": 0,
                        "eta_seconds": None,
                        "backend_started": False,
                    },
                )
                store.save_checkpoint(
                    record.id,
                    JobStage.ACQUISITION,
                    {"task_id": operation_task_id},
                )
                current = store.get_job(job_id)
                if current.status in {
                    JobStatus.CANCELLING,
                    JobStatus.CANCELLED,
                }:
                    cancelled = await settle_operation_cancel(operation_task_id)
                    return _job_response(cancelled or current)
                if resume_download is None:
                    raise RuntimeError("Acquisition service has no resume method")
                resume_attempted = True
                await mutate_backend(resume_download, operation_task_id)
                current = store.get_job(job_id)
                if current.status in {
                    JobStatus.CANCELLING,
                    JobStatus.CANCELLED,
                }:
                    cancelled = await settle_operation_cancel(operation_task_id)
                    return _job_response(cancelled or current)
                record = store.update_status(
                    current.id,
                    JobStatus.DOWNLOADING,
                    details={**current.details, "backend_started": True},
                    expected_status=JobStatus.DOWNLOADING,
                )
            return _job_response(record)
        except JobNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc
        except InvalidTransition as exc:
            current = store.get_job(job_id)
            if current.status is JobStatus.CANCELLING:
                if owns_operation and operation_task_id is not None:
                    cancelled = await settle_operation_cancel(operation_task_id)
                    return _job_response(cancelled or current)
                if owns_operation:
                    return _job_response(store.finalize_cancel(job_id))
                return _job_response(current)
            if current.status is JobStatus.CANCELLED:
                return _job_response(current)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "invalid_job_transition",
                    "message": str(exc),
                    "retryable": False,
                },
            ) from exc
        except ActiveJobExists as exc:
            active = store.list_active_jobs(limit=1)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "active_job_exists",
                    "message": str(exc),
                    "retryable": True,
                    "active_job_id": active[0].id if active else None,
                },
            ) from exc
        except AcquisitionError as exc:
            current = store.get_job(job_id)
            if current.status is JobStatus.CANCELLING:
                if owns_operation and operation_task_id is not None:
                    cancelled = await settle_operation_cancel(operation_task_id)
                    return _job_response(cancelled or current)
                if owns_operation:
                    return _job_response(store.finalize_cancel(job_id))
                return _job_response(current)
            if current.status is JobStatus.CANCELLED:
                return _job_response(current)
            failed = await fail_resume_download(
                exc.code.value,
                exc.message_vi,
                retryable=exc.retryable,
            )
            raise HTTPException(
                status_code=_acquisition_error_status(exc),
                detail={
                    "code": exc.code.value,
                    "message": exc.message_vi,
                    "retryable": exc.retryable,
                    "job_id": failed.id,
                },
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            current = store.get_job(job_id)
            if current.status is JobStatus.CANCELLING:
                if owns_operation and operation_task_id is not None:
                    cancelled = await settle_operation_cancel(operation_task_id)
                    return _job_response(cancelled or current)
                if owns_operation:
                    return _job_response(store.finalize_cancel(job_id))
                return _job_response(current)
            if current.status is JobStatus.CANCELLED:
                return _job_response(current)
            message = "Không thể tiếp tục tác vụ tải nguồn"
            failed = await fail_resume_download(
                "download_resume_failed",
                message,
                retryable=True,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "download_resume_failed",
                    "message": message,
                    "retryable": True,
                    "job_id": failed.id,
                },
            ) from exc

    @application.get("/v1/jobs/{job_id}/events")
    async def job_events(
        request: Request,
        job_id: str,
        store: StoreDependency,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        once: bool = False,
    ) -> StreamingResponse:
        try:
            store.get_job(job_id)
        except JobNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc

        cursor = after
        if last_event_id is not None:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "invalid_event_cursor",
                        "message": "Last-Event-ID không hợp lệ",
                        "retryable": False,
                    },
                ) from exc

        async def stream():
            nonlocal cursor
            while True:
                events = store.list_events(job_id, after_id=cursor)
                for event in events:
                    cursor = event.id
                    data = json.dumps(
                        {
                            "type": event.event_type,
                            "job_id": event.job_id,
                            "created_at": event.created_at,
                            "payload": event.payload,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"id: {event.id}\nevent: {event.event_type}\ndata: {data}\n\n"
                if once:
                    return
                if await request.is_disconnected():
                    return
                current = store.get_job(job_id)
                if current.status in {JobStatus.COMPLETED, JobStatus.CANCELLED}:
                    return
                if not events:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(configured_settings.sse_poll_seconds)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    web_index = _WEB_STATIC_ROOT / "index.html"
    if web_index.is_file():
        # API and documentation routes are registered first, so this final
        # catch-all mount cannot shadow /v1, /docs, or /openapi.json.
        application.mount(
            "/",
            StaticFiles(directory=_WEB_STATIC_ROOT, html=True),
            name="web-dashboard",
        )
        application.state.web_dashboard_enabled = True
    else:
        application.state.web_dashboard_enabled = False

    return application


app = create_app()
