"""FastAPI upload control plane and durable offline job orchestration."""

import asyncio
import errno
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import sqlite3
import stat as stat_module
import time
import uuid
from collections.abc import MutableMapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

import httpx
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
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
    SubtitleFormat,
    normalize_speaker_label,
)
from .gpu import (
    NvidiaGpu,
    gpu_support_tier,
    inspect_gpu,  # noqa: F401 - re-exported seam that tests monkeypatch
    read_gpu_report,
)
from .media_probe import FfprobeMediaProbe, MediaProbe, MediaProbeError
from .model_manager import install_model
from .openai_translation import (
    OpenAITranslator,
    is_loopback_api_base_url,
    normalize_api_base_url,
    read_openai_api_key,
)
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
from .speaker_voices import parse_speaker_voices
from .studio import (
    STUDIO_EDITABLE_STATUSES,
    StudioError,
    build_cast_view,
    build_script_view,
    parse_line_overrides,
    parse_speaker_names,
    parse_studio_mix,
)
from .transcript import TranscriptError, parse_subtitle_file
from .translation_artifact import (
    TranslationArtifact,
    TranslationArtifactError,
    load_translation_artifact,
)
from .translation_stage import TranslationStage

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


class UploadCreateRequest(BaseModel):
    """Metadata declared before streaming local artifacts as raw bodies."""

    model_config = ConfigDict(extra="forbid")

    media_filename: str = Field(min_length=1, max_length=255)
    subtitle_filename: str | None = Field(default=None, min_length=1, max_length=255)
    rights_confirmed: bool = False
    source_language: str = Field(default="auto", min_length=2, max_length=35)
    timing_profile: Literal["natural", "strict"] = "natural"
    models: ModelSelection = Field(default_factory=ModelSelection)
    voice: VoiceSelection | None = None
    voice_rights_confirmed: bool = False
    # Per-character voices, keyed by the speaker label parsed from the
    # subtitle file (see docs/per-character-voices.md). Values use the
    # same shape as ``voice``; characters without an entry use the job's
    # default voice. Stored verbatim into the job spec.
    speaker_voices: dict[str, VoiceSelection] | None = None
    # Automatic speaker diarization (see docs/auto-speaker-diarization.md).
    # Requires a locally configured diarization pipeline on the server;
    # without one the job fails with a plain message instead of running
    # unlabeled. Only affects the transcript stage.
    diarize_speakers: bool = False
    # Automatic per-character Piper voices when no explicit entry covers
    # a speaker: voices are assigned by first appearance (or by the
    # SPEAKER_xx number for diarized labels). Piper only.
    auto_speaker_voices: bool = False
    # AI listen-back after Phase 4 (see docs/studio.md). Best-effort:
    # when False the job skips it entirely.
    qc_enabled: bool = True
    # Translation backend (see docs/custom-api-translation.md). "local"
    # uses the on-device Gemma model; "openai" and "api" both leave the
    # job at READY_TRANSLATION for the API process (endpoint
    # POST /v1/jobs/{id}/translate-openai or .../translate-api, or the
    # standalone scripts/translate-openai.py) because the offline worker
    # cannot reach the network. "openai" is the legacy value: it always
    # targets api.openai.com with a default model of gpt-4o-mini.
    # "api" is the generic OpenAI-compatible backend: it requires
    # translation_api_base_url and an explicit provider model id.
    # Stored verbatim into the job spec.
    translation_backend: Literal["local", "openai", "api"] = "local"
    # Model id for the API backends. Ignored for local jobs. At finalize
    # time the backend normalizes spec["models"]["translation"] to
    # "mt-openai-<model>" (openai) or "mt-api-<model>" (api).
    translation_model: str | None = Field(default=None, max_length=200)
    # Base URL for the "api" backend only, e.g. https://api.deepseek.com
    # or http://localhost:1234 (LM Studio). Normalized at finalize time
    # (a trailing /v1 or /v1/chat/completions is stripped, because the
    # translator appends /v1/chat/completions itself). Must be empty
    # for "openai" and "local" jobs.
    translation_api_base_url: str | None = Field(default=None, max_length=500)
    # Per-job separation tuning (see docs/profile-6gb.md). When set, the
    # Phase 4 stage uses these instead of the server-wide settings for
    # this job only; when None, the server defaults apply. This is what
    # lets the 6 GB preset shrink TIGER's chunk without touching the
    # server configuration for other jobs.
    separation_chunk_seconds: float | None = Field(default=None, ge=12.0, le=600.0)
    separation_context_seconds: float | None = Field(default=None, ge=0.0, le=12.0)
    separation_batch_size: int | None = Field(default=None, ge=1, le=16)
    # Final-review gate (see docs/studio.md): when True, Phase 4 stops
    # at AWAITING_APPROVAL with a preview instead of COMPLETED, and the
    # finished file is only released after POST .../review/approve.
    require_final_approval: bool = False
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

    opensubtitles: AdminIntegrationStatus


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


def _job_response(record: JobRecord) -> JobResponse:
    return JobResponse.model_validate(record.to_dict())


class StudioCastSpeaker(BaseModel):
    """One cast row: raw label plus optional display name and voice."""

    model_config = ConfigDict(extra="forbid")

    speaker: str = Field(min_length=1, max_length=120)
    display_name: str | None = Field(default=None, max_length=120)
    voice_id: str | None = Field(default=None, max_length=200)
    reference_path: str | None = Field(default=None, max_length=1000)


class StudioCastRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speakers: list[StudioCastSpeaker] = Field(min_length=1)


class StudioLineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


class StudioRetakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordinals: list[int] = Field(min_length=1)


class StudioMixRequest(BaseModel):
    """Mix overrides; fields are validated by parse_studio_mix."""

    model_config = ConfigDict(extra="allow")


def _studio_translation(
    store: StateStore, settings: Settings, job_id: str
) -> TranslationArtifact:
    """Load the sealed translation artifact a studio view is built from."""

    record = store.get_job(job_id)
    raw_digest = record.details.get("translated_transcript_sha256")
    source_digest = record.details.get("source_transcript_sha256")
    if not isinstance(raw_digest, str) or not isinstance(source_digest, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "studio_not_ready",
                "message": "Job chưa có bản dịch nên chưa mở được studio",
                "retryable": False,
            },
        )
    path = settings.jobs_dir.resolve(strict=False) / job_id / "translated-transcript.json"
    try:
        return load_translation_artifact(
            path,
            expected_sha256=raw_digest,
            expected_source_transcript_sha256=source_digest,
        )
    except (TranslationArtifactError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "studio_not_ready",
                "message": "Không đọc được bản dịch của job",
                "retryable": False,
            },
        ) from exc


def _studio_require_editable(record: JobRecord) -> None:
    if record.status not in STUDIO_EDITABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "studio_not_editable",
                "message": (
                    "Studio chỉ sửa được job đã hoàn tất hoặc đã dịch xong "
                    "đang chờ thuyết minh; job đang chạy thì chờ nó xong đã"
                ),
                "retryable": True,
            },
        )


def _studio_mix_view(record: JobRecord, settings: Settings) -> dict[str, float]:
    """Effective mix: stage defaults merged with the job's overrides."""

    mix: dict[str, float] = {
        "narration_lufs": float(settings.narration_target_lufs),
        "accompaniment_lufs": -24.0,
        "true_peak_db": -1.5,
        "ducking_threshold": 0.04,
        "ducking_ratio": 3.0,
        "ducking_attack_ms": 20.0,
        "ducking_release_ms": 300.0,
    }
    try:
        mix.update(parse_studio_mix(record.spec.get("studio_mix")))
    except StudioError:
        # A mix the API never wrote cannot exist; a corrupt spec must not
        # take down the read view, so the defaults stand.
        pass
    return mix


def _studio_view(
    store: StateStore, settings: Settings, record: JobRecord
) -> dict[str, Any]:
    artifact = _studio_translation(store, settings, record.id)
    checkpoint = store.get_checkpoint(record.id, JobStage.TTS)
    tts_payload = checkpoint.payload if checkpoint is not None else None
    models = record.spec.get("models")
    tts_model_id = models.get("tts") if isinstance(models, dict) else None
    script = build_script_view(
        artifact.result.segments, record.spec, tts_payload
    )
    qc_summary: dict[str, Any] | None = None
    qc_path = (
        settings.jobs_dir.resolve(strict=False) / record.id / "qc-report.json"
    )
    if qc_path.is_file():
        try:
            qc_report = json.loads(qc_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            qc_report = None
        if isinstance(qc_report, dict):
            summary = qc_report.get("summary")
            if isinstance(summary, dict):
                qc_summary = summary
            qc_lines = qc_report.get("lines")
            if isinstance(qc_lines, list):
                by_ordinal: dict[int, dict[str, Any]] = {}
                for item in qc_lines:
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("ordinal"), int)
                    ):
                        by_ordinal[int(item["ordinal"])] = item
                for row in script:
                    qc_row = by_ordinal.get(int(row["ordinal"]))
                    if qc_row is not None:
                        row["qc_status"] = qc_row.get("status")
                        row["qc_heard"] = qc_row.get("heard")
                        row["qc_wer"] = qc_row.get("wer")
    return {
        "job_id": record.id,
        "status": record.status.value,
        "editable": record.status in STUDIO_EDITABLE_STATUSES,
        "cast": build_cast_view(artifact.result.segments, record.spec),
        "script": script,
        "mix": _studio_mix_view(record, settings),
        # The TTS model id (not the backend name): the UI shows which
        # voice engine the job uses.
        "tts_backend": tts_model_id,
        "qc_summary": qc_summary,
    }


def _studio_requeue(store: StateStore, record: JobRecord) -> bool:
    """Send a completed job back to the Phase 4 queue.

    READY_TTS jobs are already queued, so they need no move. The worker
    picks READY_TTS up, reuses every checkpoint that is still valid,
    and rebuilds only what the studio changed.
    """

    if record.status is JobStatus.COMPLETED:
        store.update_status(
            record.id,
            JobStatus.READY_TTS,
            stage=JobStage.TTS,
            progress_permille=700,
            force=True,
        )
        return True
    return False


def _openai_key_path(settings: Settings) -> Path:
    """Where the API keeps an OpenAI key uploaded from the frontend.

    An explicit DUB_OPENAI_API_KEY_FILE wins; otherwise a 0600 file
    under jobs_dir. Server-side only: never served, never returned by
    any endpoint (only its configured/not-configured state is).
    """

    if settings.openai_api_key_file is not None:
        return Path(settings.openai_api_key_file)
    return settings.jobs_dir / ".openai_api_key"


def _openai_key_configured(settings: Settings) -> bool:
    try:
        return bool(_openai_key_path(settings).read_text(encoding="utf-8").strip())
    except OSError:
        return False


# Job ids with an OpenAI translation run active inside this API process
# (background task of POST /v1/jobs/{id}/translate-openai). Guards
# against double-running one job; a stale TRANSLATING job left behind
# by an API restart is not in this set, so it can be resumed.
_OPENAI_TRANSLATION_RUNNING: set[str] = set()

# Dashboard model-install jobs, keyed by model id or "6gb" for the
# preset job. Each value is {"job", "status", "models": [...], "error"}.
# A restart clears this in-memory map; a download interrupted that way
# is safe to re-run because verified models are skipped on start.
_MODEL_INSTALL_JOBS: dict[str, dict[str, Any]] = {}


class OpenAiKeyRequest(BaseModel):
    """Write-only OpenAI key upload. The key is never returned."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=1000)


class ReviewRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=500)


def _studio_drop_tts_blocks(
    store: StateStore,
    job_id: str,
    ordinals: set[int],
    *,
    drop_rewrites: bool,
) -> None:
    """Invalidate TTS checkpoint blocks so exactly those lines rebuild.

    Timing rewrites of an edited line are dropped too: a rewrite is a
    shortening of the *old* text and must never be applied to the new
    one. Retakes keep rewrites because the text is unchanged.
    """

    checkpoint = store.get_checkpoint(job_id, JobStage.TTS)
    if checkpoint is None or not isinstance(checkpoint.payload, dict):
        return
    payload = dict(checkpoint.payload)
    blocks = payload.get("blocks", [])
    if not isinstance(blocks, list):
        return
    kept_blocks = [
        item
        for item in blocks
        if not (isinstance(item, dict) and item.get("ordinal") in ordinals)
    ]
    changed = len(kept_blocks) != len(blocks)
    if drop_rewrites:
        rewrites = payload.get("timing_rewrites", [])
        if not isinstance(rewrites, list):
            rewrites = []
        kept_rewrites = [
            item
            for item in rewrites
            if not (isinstance(item, dict) and item.get("ordinal") in ordinals)
        ]
        changed = changed or len(kept_rewrites) != len(rewrites)
        payload["timing_rewrites"] = kept_rewrites
    if changed:
        payload["blocks"] = kept_blocks
        payload["completed"] = False
        store.save_checkpoint(job_id, JobStage.TTS, payload)


def _adapter_value(value: Any, name: str, default: Any = None) -> Any:
    if is_dataclass(value):
        return asdict(value).get(name, default)
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


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
    acquisition_service: Any | None = None,
    coordinator: Any | None = None,
    admin_http_client: httpx.AsyncClient | None = None,
    media_probe: MediaProbe | None = None,
) -> FastAPI:
    """Create an app with explicit adapter injection for deterministic tests."""

    configured_settings = settings or get_settings()
    admin_configuration_lock = asyncio.Lock()
    # Upload locks are weakly held: active/waiting coroutines retain their lock,
    # while completed, finalized, or unknown UUID requests cannot grow this
    # process-local registry for the lifetime of the API process.
    upload_session_locks: WeakValueDictionary[str, asyncio.Lock] = (
        WeakValueDictionary()
    )
    local_media_probe = media_probe or FfprobeMediaProbe()

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
        application.state.acquisition = None
        application.state.coordinator = None
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
        try:
            yield
        finally:
            upload_cleanup_stop.set()
            upload_cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await upload_cleanup_task
            if owned_admin_client is not None:
                await owned_admin_client.aclose()

    application = FastAPI(
        title="VietDub",
        version=__version__,
        description="Nhận MP4/MKV hợp pháp và chạy pipeline thuyết minh cục bộ.",
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

    StoreDependency = Annotated[StateStore, Depends(job_store)]
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
            "source_modes": ["upload"],
            "media_types": ["movie"],
            "subtitle_modes": ["prefer", "manual", "asr"],
            "source_language": {
                "automatic_detection": True,
                "vietnamese_passthrough": True,
                "availability_depends_on_selected_model": True,
            },
            "outputs": ["mp4", "srt", "timing-report.json"],
            "offline_inference": True,
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
            opensubtitles=AdminIntegrationStatus(
                configured=opensubtitles_configured,
                editable=editable,
                can_manage=editable,
                cleanup_pending=cleanup_pending,
                can_delete=can_delete,
            ),
        )

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

    # --- Dashboard model installation -------------------------------------
    # The 6 GB preset (docs/profile-6gb.md) needs exactly these local
    # models; translation runs through an API, so no local MT model is
    # in the list. Install jobs run in the background because a model
    # download is gigabytes and must not block the API event loop; the
    # frontend polls /v1/models/install-status and refreshes the catalog
    # when a job finishes. Installs are idempotent: a model that is
    # already installed and verified is skipped, never re-downloaded.
    six_gb_preset_model_ids = (
        "asr-faster-whisper-small",
        "separation-tiger-dnr",
        "tts-neucodec-onnx-int8",
        "tts-vieneu-v2",
    )

    def _load_catalog_or_raise() -> ModelCatalog:
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

    def _run_model_installs(job_key: str) -> None:
        """Background worker for one install job (sync: runs off-loop).

        Runs in Starlette's threadpool, one model at a time, in list
        order. A failed model stops the job; the models already done
        stay installed, and re-running the job resumes after them
        because installed-and-verified models are skipped on start.
        """

        job = _MODEL_INSTALL_JOBS[job_key]
        job["status"] = "running"
        job["error"] = None
        for entry in job["models"]:
            if entry["status"] == "skipped":
                continue
            entry["status"] = "running"
            entry["error"] = None
            try:
                install_model(
                    configured_settings.models_lock_path,
                    configured_settings.models_dir,
                    entry["id"],
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to dashboard
                entry["status"] = "failed"
                entry["error"] = str(exc)[:500]
                job["status"] = "failed"
                job["error"] = entry["error"]
                return
            entry["status"] = "done"
        job["status"] = "done"

    @application.get("/v1/models/install-status")
    def model_install_status() -> dict[str, Any]:
        """Tracked install jobs (single models and the 6 GB preset)."""

        return {"jobs": list(_MODEL_INSTALL_JOBS.values())}

    @application.post(
        "/v1/models/install-6gb",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def install_6gb_preset_models(
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        """Install the missing local models of the 6 GB preset.

        Registered before the single-model route below so the literal
        path is never swallowed by the ``{model_id}`` parameter.
        """

        existing = _MODEL_INSTALL_JOBS.get("6gb")
        if existing is not None and existing["status"] == "running":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "model_install_in_progress",
                    "message": "Đang tải model cho preset 6GB, vui lòng chờ",
                    "retryable": True,
                },
            )
        catalog = _load_catalog_or_raise()
        by_id = {entry.id: entry for entry in catalog.models}
        entries = []
        for model_id in six_gb_preset_model_ids:
            entry = by_id.get(model_id)
            ready = bool(entry is not None and entry.installed and entry.valid)
            entries.append(
                {
                    "id": model_id,
                    "status": "skipped" if ready else "pending",
                    "error": None,
                }
            )
        job = {"job": "6gb", "status": "pending", "models": entries, "error": None}
        _MODEL_INSTALL_JOBS["6gb"] = job
        if all(entry["status"] == "skipped" for entry in entries):
            job["status"] = "done"
            return job
        background_tasks.add_task(_run_model_installs, "6gb")
        job["status"] = "running"
        return job

    @application.post(
        "/v1/models/{model_id}/install",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def install_single_model(
        model_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        """Install one locked model in the background (or skip if ready)."""

        existing = _MODEL_INSTALL_JOBS.get(model_id)
        if existing is not None and existing["status"] == "running":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "model_install_in_progress",
                    "message": "Model này đang được tải, vui lòng chờ",
                    "retryable": True,
                },
            )
        catalog = _load_catalog_or_raise()
        by_id = {entry.id: entry for entry in catalog.models}
        entry = by_id.get(model_id)
        if entry is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "model_not_found",
                    "message": "Không có model này trong danh mục",
                    "retryable": False,
                },
            )
        ready = bool(entry.installed and entry.valid)
        job = {
            "job": model_id,
            "status": "done" if ready else "pending",
            "models": [
                {
                    "id": model_id,
                    "status": "skipped" if ready else "pending",
                    "error": None,
                }
            ],
            "error": None,
        }
        _MODEL_INSTALL_JOBS[model_id] = job
        if ready:
            return job
        background_tasks.add_task(_run_model_installs, model_id)
        job["status"] = "running"
        return job

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
        if (
            payload.voice is not None or payload.speaker_voices
        ) and payload.voice_rights_confirmed is not True:
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
            # Normalize the API translation choice so the translation
            # stage, the worker filter and the API runner all agree on
            # one model id (mirrors scripts/translate-openai.py).
            backend = spec.get("translation_backend")
            if backend == "openai":
                raw_base_url = spec.get("translation_api_base_url")
                if isinstance(raw_base_url, str) and raw_base_url.strip():
                    raise _upload_error(
                        status.HTTP_400_BAD_REQUEST,
                        "translation_api_base_url_invalid",
                        "Backend OpenAI dùng URL mặc định của OpenAI; "
                        "chọn backend API tùy chỉnh để nhập URL khác",
                    )
                raw_model = spec.get("translation_model")
                model = raw_model.strip() if isinstance(raw_model, str) else ""
                if not model:
                    model = "gpt-4o-mini"
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model):
                    raise _upload_error(
                        status.HTTP_400_BAD_REQUEST,
                        "translation_model_invalid",
                        "Tên model OpenAI không hợp lệ",
                    )
                models = spec.get("models")
                if not isinstance(models, dict):
                    models = {}
                    spec["models"] = models
                models["translation"] = f"mt-openai-{model}"
                spec["translation_model"] = model
                spec["translation_api_base_url"] = None
            elif backend == "api":
                raw_base_url = spec.get("translation_api_base_url")
                if not isinstance(raw_base_url, str) or not raw_base_url.strip():
                    raise _upload_error(
                        status.HTTP_400_BAD_REQUEST,
                        "translation_api_base_url_invalid",
                        "Backend API tùy chỉnh cần nhập API base URL, "
                        "ví dụ https://api.deepseek.com hoặc http://localhost:1234",
                    )
                try:
                    base_url = normalize_api_base_url(raw_base_url)
                except ValueError as exc:
                    raise _upload_error(
                        status.HTTP_400_BAD_REQUEST,
                        "translation_api_base_url_invalid",
                        f"API base URL không hợp lệ: {exc}",
                    ) from exc
                raw_model = spec.get("translation_model")
                model = raw_model.strip() if isinstance(raw_model, str) else ""
                if not model or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._\-/:@]*", model
                ):
                    raise _upload_error(
                        status.HTTP_400_BAD_REQUEST,
                        "translation_model_invalid",
                        "Backend API tùy chỉnh cần nhập tên model của nhà "
                        "cung cấp, ví dụ deepseek-chat hoặc tên model local",
                    )
                models = spec.get("models")
                if not isinstance(models, dict):
                    models = {}
                    spec["models"] = models
                # Provider model ids may contain "/" (OpenRouter) or ":"
                # (Ollama tags); the internal model id must stay a plain
                # slug, so those characters become "-".
                slug = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") or "model"
                models["translation"] = f"mt-api-{slug}"
                spec["translation_model"] = model
                spec["translation_api_base_url"] = base_url
            else:
                spec["translation_model"] = None
                spec["translation_api_base_url"] = None
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

    @application.post("/v1/search", include_in_schema=False)
    async def search_removed() -> None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    @application.post("/v1/jobs", include_in_schema=False)
    async def create_release_job_removed() -> None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

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

    @application.get("/v1/jobs/{job_id}/studio")
    def get_studio(job_id: str, store: StoreDependency) -> dict[str, Any]:
        try:
            record = store.get_job(job_id)
            return _studio_view(store, configured_settings, record)
        except JobNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc

    @application.put("/v1/jobs/{job_id}/studio/cast")
    def put_studio_cast(
        job_id: str, payload: StudioCastRequest, store: StoreDependency
    ) -> dict[str, Any]:
        try:
            record = store.get_job(job_id)
            _studio_require_editable(record)
            artifact = _studio_translation(store, configured_settings, job_id)
            known = {
                normalize_speaker_label(segment.speaker)
                for segment in artifact.result.segments
                if isinstance(segment.speaker, str)
            }
            known.discard(None)
            names = parse_speaker_names(record.spec.get("speaker_names"))
            raw_map = record.spec.get("speaker_voices")
            merged: dict[str, dict[str, Any]] = {}
            if isinstance(raw_map, dict):
                for label, entry in raw_map.items():
                    if isinstance(entry, dict):
                        merged[str(label)] = dict(entry)
            needs_rights = False
            for row in payload.speakers:
                label = normalize_speaker_label(row.speaker)
                if label is None or label not in known:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "code": "studio_speaker_unknown",
                            "message": f"Nhân vật không có trong kịch bản: {row.speaker}",
                            "retryable": False,
                        },
                    )
                if row.display_name is not None:
                    cleaned = normalize_speaker_label(row.display_name)
                    if cleaned is None:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail={
                                "code": "studio_speaker_names_invalid",
                                "message": "Tên hiển thị nhân vật không được để trống",
                                "retryable": False,
                            },
                        )
                    names[label] = cleaned
                if row.voice_id is None and row.reference_path is None:
                    merged.pop(label, None)
                else:
                    needs_rights = True
                    merged[label] = {
                        "voice_id": row.voice_id,
                        "reference_path": row.reference_path,
                    }
            if needs_rights and record.spec.get("voice_rights_confirmed") is not True:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "studio_voice_rights_required",
                        "message": (
                            "Job này chưa xác nhận quyền giọng; "
                            "không thể gán giọng mới trong studio"
                        ),
                        "retryable": False,
                    },
                )
            try:
                voice_map = parse_speaker_voices({"speaker_voices": merged})
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "studio_voice_invalid",
                        "message": str(exc),
                        "retryable": False,
                    },
                ) from exc
            record = store.update_spec(
                job_id,
                {
                    "speaker_names": names,
                    "speaker_voices": voice_map.as_spec(),
                },
            )
            requeued = _studio_requeue(store, record)
            view = _studio_view(store, configured_settings, record)
            view["requeued"] = requeued
            return view
        except JobNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc
        except StudioError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": exc.code,
                    "message": exc.message_vi,
                    "retryable": False,
                },
            ) from exc

    @application.put("/v1/jobs/{job_id}/studio/script/{ordinal}")
    def put_studio_line(
        job_id: str, ordinal: int, payload: StudioLineRequest, store: StoreDependency
    ) -> dict[str, Any]:
        try:
            record = store.get_job(job_id)
            _studio_require_editable(record)
            artifact = _studio_translation(store, configured_settings, job_id)
            block_count = len(artifact.result.segments)
            if not 0 <= ordinal < block_count:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "studio_ordinal_invalid",
                        "message": f"Câu thoại {ordinal} nằm ngoài kịch bản ({block_count} câu)",
                        "retryable": False,
                    },
                )
            cleaned = parse_line_overrides(
                {ordinal: payload.text}, block_count=block_count
            )[ordinal]
            overrides = parse_line_overrides(
                record.spec.get("studio_line_overrides"), block_count=block_count
            )
            overrides[ordinal] = cleaned
            record = store.update_spec(
                job_id,
                {
                    "studio_line_overrides": {
                        str(key): value for key, value in overrides.items()
                    }
                },
            )
            _studio_drop_tts_blocks(
                store, job_id, {ordinal}, drop_rewrites=True
            )
            requeued = _studio_requeue(store, record)
            return {"ordinal": ordinal, "text": cleaned, "requeued": requeued}
        except JobNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc
        except StudioError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": exc.code,
                    "message": exc.message_vi,
                    "retryable": False,
                },
            ) from exc

    @application.post("/v1/jobs/{job_id}/studio/retake")
    def post_studio_retake(
        job_id: str, payload: StudioRetakeRequest, store: StoreDependency
    ) -> dict[str, Any]:
        try:
            record = store.get_job(job_id)
            _studio_require_editable(record)
            artifact = _studio_translation(store, configured_settings, job_id)
            block_count = len(artifact.result.segments)
            seen: list[int] = []
            for ordinal in payload.ordinals:
                if (
                    isinstance(ordinal, bool)
                    or not isinstance(ordinal, int)
                    or not 0 <= ordinal < block_count
                ):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "code": "studio_ordinal_invalid",
                            "message": f"Câu thoại {ordinal} nằm ngoài kịch bản ({block_count} câu)",
                            "retryable": False,
                        },
                    )
                if ordinal not in seen:
                    seen.append(ordinal)
            _studio_drop_tts_blocks(
                store, job_id, set(seen), drop_rewrites=False
            )
            requeued = _studio_requeue(store, record)
            return {"retaken": seen, "requeued": requeued}
        except JobNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc

    @application.put("/v1/jobs/{job_id}/studio/mix")
    def put_studio_mix(
        job_id: str, payload: StudioMixRequest, store: StoreDependency
    ) -> dict[str, Any]:
        try:
            record = store.get_job(job_id)
            _studio_require_editable(record)
            updates = parse_studio_mix(payload.model_extra or {})
            if not updates:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "studio_mix_invalid",
                        "message": "Phải gửi ít nhất một thông số mix",
                        "retryable": False,
                    },
                )
            merged = parse_studio_mix(record.spec.get("studio_mix"))
            merged.update(updates)
            record = store.update_spec(job_id, {"studio_mix": merged})
            # A mix-only change leaves every TTS block and the timeline
            # untouched, so the export checkpoint must be cleared or the
            # old MP4 would simply be reused.
            store.save_checkpoint(
                job_id, JobStage.EXPORT, {"completed": False}
            )
            requeued = _studio_requeue(store, record)
            return {
                "mix": _studio_mix_view(record, configured_settings),
                "requeued": requeued,
            }
        except JobNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "job_not_found",
                    "message": "Không tìm thấy job",
                    "retryable": False,
                },
            ) from exc
        except StudioError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": exc.code,
                    "message": exc.message_vi,
                    "retryable": False,
                },
            ) from exc

    @application.get("/v1/jobs/{job_id}/studio/lines/{ordinal}/audio")
    def get_studio_line_audio(
        job_id: str, ordinal: int, store: StoreDependency
    ) -> FileResponse:
        try:
            record = store.get_job(job_id)
            checkpoint = store.get_checkpoint(job_id, JobStage.TTS)
            blocks = (
                checkpoint.payload.get("blocks", [])
                if checkpoint is not None
                and isinstance(checkpoint.payload, dict)
                else []
            )
            if not any(
                isinstance(item, dict) and item.get("ordinal") == ordinal
                for item in blocks
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "studio_line_not_synthesized",
                        "message": "Câu này chưa được tổng hợp nên chưa có audio nghe thử",
                        "retryable": True,
                    },
                )
            expected = (
                configured_settings.jobs_dir.resolve(strict=False)
                / job_id
                / "tts"
                / f"block-{ordinal:05d}.wav"
            )
            if not expected.is_file():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "studio_line_not_synthesized",
                        "message": "Không tìm thấy file audio của câu này",
                        "retryable": True,
                    },
                )
            return FileResponse(
                expected,
                media_type="audio/wav",
                filename=f"{job_id}-line-{ordinal}.wav",
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

    @application.get("/v1/settings/openai-key")
    @application.get("/v1/settings/api-key")
    def get_openai_key_status() -> dict[str, Any]:
        """Report only whether an API key is stored, never the key.

        Both paths share one stored key: the old /openai-key path is
        kept for existing dashboards, /api-key is the generic name.
        """

        return {"configured": _openai_key_configured(configured_settings)}

    @application.put("/v1/settings/openai-key")
    @application.put("/v1/settings/api-key")
    def put_openai_key(body: OpenAiKeyRequest) -> dict[str, Any]:
        """Store an API key server-side (0600 file). Write-only."""

        key = body.key.strip()
        if not key or any(character.isspace() for character in key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "openai_api_key_invalid",
                    "message": "API key OpenAI không hợp lệ",
                    "retryable": False,
                },
            )
        path = _openai_key_path(configured_settings)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(path, key.encode("utf-8"))
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                detail={
                    "code": "openai_api_key_store_failed",
                    "message": "Không lưu được API key OpenAI",
                    "retryable": True,
                },
            ) from exc
        return {"configured": True}

    @application.delete("/v1/settings/openai-key")
    @application.delete("/v1/settings/api-key")
    def delete_openai_key() -> dict[str, Any]:
        _openai_key_path(configured_settings).unlink(missing_ok=True)
        return {"configured": False}

    @application.post(
        "/v1/jobs/{job_id}/translate-openai",
        status_code=status.HTTP_202_ACCEPTED,
    )
    @application.post(
        "/v1/jobs/{job_id}/translate-api",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def translate_job_openai(
        job_id: str,
        background_tasks: BackgroundTasks,
        store: StoreDependency,
    ) -> dict[str, Any]:
        """Translate an API-backend job via its provider, outside the worker.

        The offline worker never claims these jobs (see worker.py), so
        the API process — which has no network guard — runs the
        translation in the background. The job then continues through
        Phase 4 on its own. A stale TRANSLATING job (API restarted
        mid-run) can be resumed here; a live run cannot be doubled.
        Works for both backends: "openai" (api.openai.com) and "api"
        (the job's stored custom base URL). A loopback base URL needs
        no stored key — local servers usually ignore the auth header,
        so a dummy "local" key is used instead of failing the job.
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
        if record.status not in {
            JobStatus.READY_TRANSLATION,
            JobStatus.TRANSLATING,
        }:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "translation_not_ready",
                    "message": "Job không ở trạng thái chờ dịch",
                    "retryable": True,
                },
            )
        job_backend = record.spec.get("translation_backend") or "local"
        if job_backend not in {"openai", "api"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "translation_backend_not_openai",
                    "message": "Job này dùng backend dịch local, không dịch qua API",
                    "retryable": False,
                },
            )
        if job_id in _OPENAI_TRANSLATION_RUNNING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "translation_in_progress",
                    "message": "Job đang được dịch qua API, vui lòng chờ",
                    "retryable": True,
                },
            )
        model = record.spec.get("translation_model")
        model = model.strip() if isinstance(model, str) and model.strip() else "gpt-4o-mini"
        if job_backend == "api":
            raw_base_url = record.spec.get("translation_api_base_url")
            if not isinstance(raw_base_url, str) or not raw_base_url.strip():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "translation_api_base_url_missing",
                        "message": "Job API tùy chỉnh này không có base URL đã lưu",
                        "retryable": False,
                    },
                )
            try:
                base_url = normalize_api_base_url(raw_base_url)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "translation_api_base_url_invalid",
                        "message": f"Base URL đã lưu của job không hợp lệ: {exc}",
                        "retryable": False,
                    },
                ) from exc
            model_id = (record.spec.get("models") or {}).get("translation") or (
                f"mt-api-{re.sub(r'[^A-Za-z0-9._-]+', '-', model).strip('-') or 'model'}"
            )
        else:
            base_url = "https://api.openai.com"
            model_id = f"mt-openai-{model}"
        key_path = _openai_key_path(configured_settings)
        try:
            api_key = read_openai_api_key(key_path if key_path.is_file() else None)
        except Exception as exc:
            # Loopback providers (LM Studio, Ollama, vLLM on this machine)
            # normally ignore the Authorization header, so a missing key
            # must not block a local job. Remote providers still fail
            # here with the same code the frontend already handles.
            if not is_loopback_api_base_url(base_url):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "openai_api_key_missing",
                        "message": (
                            "Chưa có API key: nhập key ở phần Cài đặt "
                            "trước khi dịch qua API"
                        ),
                        "retryable": False,
                    },
                ) from exc
            api_key = "local"

        _OPENAI_TRANSLATION_RUNNING.add(job_id)

        async def _run_translation() -> None:
            try:
                translator = OpenAITranslator(
                    api_key=api_key, model=model, base_url=base_url
                )
                try:
                    stage = TranslationStage(
                        models_lock_path=configured_settings.models_lock_path,
                        models_dir=configured_settings.models_dir,
                        jobs_dir=configured_settings.jobs_dir,
                        default_translation_model_id=model_id,
                        store=store,
                        openai_translator=translator,
                    )
                    await stage.run(job_id)
                finally:
                    translator.close()
            finally:
                _OPENAI_TRANSLATION_RUNNING.discard(job_id)

        background_tasks.add_task(_run_translation)
        return {
            "job_id": job_id,
            "status": "translating",
            "model": model,
            "base_url": base_url,
        }

    @application.get("/v1/jobs/{job_id}/review")
    def get_review(job_id: str, store: StoreDependency) -> dict[str, Any]:
        """Review screen data for a job awaiting final approval."""

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
        if record.status is not JobStatus.AWAITING_APPROVAL:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "review_not_ready",
                    "message": "Job chưa tới bước duyệt kết quả cuối",
                    "retryable": True,
                },
            )
        view = _studio_view(store, configured_settings, record)
        view["preview_video_url"] = f"/v1/jobs/{job_id}/review/video"
        view["preview_subtitle_url"] = f"/v1/jobs/{job_id}/review/subtitle"
        return view

    def _review_file(
        job_id: str, store: StateStore, kind: Literal["video", "subtitle"]
    ) -> FileResponse:
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
        if record.status is not JobStatus.AWAITING_APPROVAL:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "review_not_ready",
                    "message": "Job chưa tới bước duyệt kết quả cuối",
                    "retryable": True,
                },
            )
        output_root = configured_settings.output_dir.resolve(strict=False)
        if kind == "video":
            expected = output_root / f"{job_id}.mp4"
            result_field = "video_path"
            media_type = "video/mp4"
            filename = f"{job_id}.preview.mp4"
        else:
            expected = output_root / f"{job_id}.vi.srt"
            result_field = "srt_path"
            media_type = "application/x-subrip"
            filename = f"{job_id}.preview.vi.srt"
        raw_path = (record.result or {}).get(result_field)
        candidate = Path(raw_path).absolute() if isinstance(raw_path, str) else None
        if candidate != expected.absolute() or not expected.is_file():
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={
                    "code": "review_preview_missing",
                    "message": "File xem trước chưa sẵn sàng",
                    "retryable": True,
                },
            )
        return FileResponse(expected, media_type=media_type, filename=filename)

    @application.get("/v1/jobs/{job_id}/review/video")
    def get_review_video(job_id: str, store: StoreDependency) -> FileResponse:
        return _review_file(job_id, store, "video")

    @application.get("/v1/jobs/{job_id}/review/subtitle")
    def get_review_subtitle(job_id: str, store: StoreDependency) -> FileResponse:
        return _review_file(job_id, store, "subtitle")

    @application.post(
        "/v1/jobs/{job_id}/review/approve", response_model=JobResponse
    )
    def approve_review(job_id: str, store: StoreDependency) -> JobResponse:
        """Release the preview as the finished job (status COMPLETED)."""

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
        if record.status is not JobStatus.AWAITING_APPROVAL:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "review_not_ready",
                    "message": "Job chưa tới bước duyệt kết quả cuối",
                    "retryable": True,
                },
            )
        updated = store.update_status(
            job_id,
            JobStatus.COMPLETED,
            expected_status=JobStatus.AWAITING_APPROVAL,
            stage=JobStage.DONE,
            progress_permille=1000,
            details={**record.details, "phase4_step": "completed"},
        )
        return _job_response(updated)

    @application.post(
        "/v1/jobs/{job_id}/review/reject", response_model=JobResponse
    )
    def reject_review(
        job_id: str, body: ReviewRejectRequest, store: StoreDependency
    ) -> JobResponse:
        """Send the job back to the studio queue for fixes."""

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
        if record.status is not JobStatus.AWAITING_APPROVAL:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "review_not_ready",
                    "message": "Job chưa tới bước duyệt kết quả cuối",
                    "retryable": True,
                },
            )
        details = {**record.details, "phase4_step": "review_rejected"}
        if body.note:
            details["review_reject_note"] = body.note
        updated = store.update_status(
            job_id,
            JobStatus.READY_TTS,
            expected_status=JobStatus.AWAITING_APPROVAL,
            stage=JobStage.TTS,
            progress_permille=700,
            details=details,
        )
        return _job_response(updated)

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

    @application.post("/v1/jobs/{job_id}/cancel", response_model=JobResponse)
    def cancel_job(job_id: str, store: StoreDependency) -> JobResponse:
        try:
            current = store.get_job(job_id)
            if current.status is JobStatus.CANCELLED:
                return _job_response(current)
            running = current.status in {
                JobStatus.TRANSCRIBING,
                JobStatus.TRANSLATING,
                JobStatus.SEPARATING,
                JobStatus.SYNTHESIZING,
                JobStatus.TIMING,
                JobStatus.MIXING,
                JobStatus.MUXING,
                JobStatus.VERIFYING,
            }
            cancelled = store.request_cancel(job_id)
            if running:
                cancelled = store.update_status(
                    job_id,
                    JobStatus.CANCELLING,
                    expected_status=JobStatus.CANCELLING,
                    details={**cancelled.details, "offline_cancel_pending": True},
                    cancel_requested=True,
                )
            elif cancelled.status is not JobStatus.CANCELLED:
                cancelled = store.finalize_cancel(job_id)
            return _job_response(cancelled)
        except JobNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except InvalidTransition as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @application.post("/v1/jobs/{job_id}/resume", response_model=JobResponse)
    def resume_job(job_id: str, store: StoreDependency) -> JobResponse:
        try:
            record = store.resume(job_id)
        except JobNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except (InvalidTransition, ActiveJobExists) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "resume_conflict",
                    "message": str(exc),
                    "retryable": True,
                },
            ) from exc
        if record.status in {
            JobStatus.CREATED,
            JobStatus.SEARCHING,
            JobStatus.AWAITING_RELEASE_SELECTION,
            JobStatus.DOWNLOADING,
            JobStatus.SUBTITLE_MATCHING,
        }:
            record = store.update_status(
                record.id,
                JobStatus.FAILED,
                error_code="legacy_network_source_removed",
                error_message="Job nguồn mạng cũ không còn được hỗ trợ; hãy tải MP4/MKV lên",
                retryable=False,
            )
        return _job_response(record)

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
