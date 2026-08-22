"""Runtime configuration shared by the API and CLI.

Secrets are represented by file paths so Docker secrets never need to be
copied into the environment or returned by a public endpoint.
"""

from __future__ import annotations

import json
import re
import stat
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import __version__


class Settings(BaseSettings):
    """Application settings loaded from ``DUB_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="DUB_",
        extra="ignore",
    )

    database_path: Path = Path("/state/jobs.sqlite3")
    models_lock_path: Path = Path("/app/config/models.lock.json")
    models_dir: Path = Path("/models")
    incoming_dir: Path = Path("/data/incoming")
    jobs_dir: Path = Path("/data/jobs")
    output_dir: Path = Path("/data/output")
    incoming_quota_bytes: int = Field(
        default=100 * 1024**3,
        ge=1024 * 1024,
        le=16 * 1024**4,
    )
    upload_media_max_bytes: int = Field(
        default=16 * 1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=4 * 1024**4,
    )
    upload_subtitle_max_bytes: int = Field(
        default=16 * 1024 * 1024,
        ge=1024,
        le=256 * 1024**2,
    )
    upload_session_ttl_seconds: float = Field(
        default=7 * 24 * 60 * 60,
        ge=60.0,
        le=90 * 24 * 60 * 60,
    )
    completed_retention_days: int = Field(default=30, ge=0, le=3650)
    event_retention_days: int = Field(default=14, ge=0, le=3650)
    gpu_report_path: Path = Path("/state/gpu-health.json")
    gpu_report_max_age_seconds: float = Field(default=60.0, ge=5.0, le=600.0)
    compute_mode: Literal["gpu", "cpu"] = "gpu"
    selected_gpu_uuid: str | None = None
    selected_cuda_architecture: str | None = None
    selected_cuda_toolkit_version: str | None = None
    default_asr_model_id: str = "asr-faster-whisper-large-v3-turbo"
    default_translation_model_id: str = "mt-gemma4-31b-q4"
    default_separation_model_id: str = "separation-tiger-dnr"
    default_tts_model_id: str = "tts-vieneu-v2"
    tts_support_model_id: str = "tts-neucodec-onnx-int8"
    asr_compute_type: str = "float16"
    asr_language_confidence_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
    )
    offline_worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=30.0)
    llama_server_binary: Path = Path("/usr/local/lib/llama.cpp/llama-server")
    llama_server_port: int = Field(default=18081, ge=1024, le=65535)
    llama_context_size: int = Field(default=2048, ge=512, le=8192)
    llama_max_output_tokens: int = Field(default=512, ge=32, le=2048)
    llama_startup_timeout_seconds: float = Field(default=300.0, ge=10.0, le=900.0)
    llama_request_timeout_seconds: float = Field(default=180.0, ge=5.0, le=900.0)
    llama_gpu_layers: int = Field(default=-1, ge=-1, le=999)
    timing_rewrite_max_attempts: int = Field(default=3, ge=0, le=3)
    tiger_source_dir: Path = Path("/opt/tiger")
    vieneu_entrypoint: Path = Path("/opt/vieneu/vieneu-offline.py")
    separation_chunk_seconds: float = Field(default=120.0, ge=12.0, le=600.0)
    separation_context_seconds: float = Field(default=4.0, ge=0.0, le=12.0)
    separation_batch_size: int = Field(default=1, ge=1, le=16)
    narration_target_lufs: float = Field(default=-18.0, ge=-30.0, le=-10.0)
    output_target_lufs: float = Field(default=-16.0, ge=-24.0, le=-10.0)
    background_gain_db: float = Field(default=-2.0, ge=-20.0, le=6.0)
    narration_ducking_db: float = Field(default=6.0, ge=0.0, le=18.0)

    prowlarr_url: str = "http://prowlarr:9696"
    prowlarr_api_key_file: Path | None = Path("/run/secrets/prowlarr_api_key")
    qbittorrent_url: str = "http://qbittorrent:8080"
    qbittorrent_username: str = "admin"
    qbittorrent_password_file: Path | None = Path(
        "/run/secrets/qbittorrent_password"
    )
    opensubtitles_url: str = "https://api.opensubtitles.com/api/v1"
    opensubtitles_api_key_file: Path | None = Path(
        "/run/secrets/opensubtitles_api_key"
    )
    opensubtitles_token_file: Path | None = Path(
        "/run/secrets/opensubtitles_token"
    )
    opensubtitles_base_url_file: Path | None = Path(
        "/run/secrets/opensubtitles_base_url"
    )
    opensubtitles_user_agent: str = f"ThuyetMinhOfflineGPU v{__version__}"

    api_url: str = "http://127.0.0.1:8080"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_access_policy: Literal["local", "remote"] = "remote"
    api_allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost", "::1"]
    )
    api_token_file: Path | None = Path("/run/secrets/api_token")
    sse_poll_seconds: float = Field(default=0.5, ge=0.05, le=10.0)
    acquisition_monitor_seconds: float = Field(default=1.0, ge=0.1, le=30.0)

    @field_validator("api_allowed_hosts")
    @classmethod
    def validate_api_allowed_hosts(cls, hosts: list[str]) -> list[str]:
        if not hosts:
            raise ValueError("DUB_API_ALLOWED_HOSTS must not be empty")
        normalized: list[str] = []
        for host in hosts:
            value = host.strip().casefold()
            if (
                not value
                or value == "*"
                or (":" in value and value != "::1")
                or not re.fullmatch(r"[a-z0-9.-]+|::1", value)
            ):
                raise ValueError("DUB_API_ALLOWED_HOSTS contains an invalid host")
            normalized.append(value)
        return list(dict.fromkeys(normalized))

    def ensure_local_directories(self) -> None:
        """Create only application-owned state and artifact directories."""

        for path in (
            self.database_path.parent,
            self.models_dir,
            self.incoming_dir,
            self.jobs_dir,
            self.output_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


class ModelEntry(BaseModel):
    """A public, immutable model catalog entry."""

    model_config = ConfigDict(extra="allow")

    id: str
    stage: str
    backend: str
    license: str
    revision: str | None = None
    sha256: str | None = None
    languages: list[str] = Field(default_factory=list)
    minimum_vram_mib: int | None = Field(default=None, ge=0)
    installed: bool = False
    valid: bool = False


class ModelCatalog(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    models: list[ModelEntry] = Field(default_factory=list)


def load_model_catalog(lock_path: Path, models_dir: Path) -> ModelCatalog:
    """Read the lock file without downloading or changing model artifacts."""

    with lock_path.open("r", encoding="utf-8") as stream:
        raw: dict[str, Any] = json.load(stream)
    if raw.get("schema_version") != 1:
        raise ValueError("Phiên bản danh mục model không được hỗ trợ")
    raw_models = raw.get("models")
    if not isinstance(raw_models, list):
        raise ValueError("Danh mục model không hợp lệ")

    normalized: list[dict[str, Any]] = []
    for item in raw_models:
        if not isinstance(item, dict):
            raise ValueError("Một mục model trong danh mục không hợp lệ")
        model = dict(item)
        relative_path = model.get("path") or model.get("local_path") or model.get("id")
        artifact = models_dir / str(relative_path)
        model["installed"] = artifact.exists()
        verified_at = _matching_verification_receipt(
            models_dir,
            artifact,
            model,
        )
        # The receipt records a successful full hash by model-manager. Runtime
        # workers intentionally verify the complete tree again before use.
        model["valid"] = artifact.is_dir() and verified_at is not None
        model["verified_at"] = verified_at
        normalized.append(model)
    return ModelCatalog(schema_version=1, models=normalized)


def _matching_verification_receipt(
    models_dir: Path,
    artifact: Path,
    model: dict[str, Any],
) -> str | None:
    model_id = model.get("id")
    if (
        not isinstance(model_id, str)
        or not model_id
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in model_id)
    ):
        return None
    receipt = models_dir / ".verified" / f"{model_id}.json"
    try:
        receipt_status = receipt.lstat()
        if not stat.S_ISREG(receipt_status.st_mode):
            return None
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    if (
        payload.get("id") != model_id
        or payload.get("revision") != model.get("revision")
        or payload.get("tree_sha256")
        != (model.get("tree_sha256") or model.get("sha256"))
    ):
        return None
    locked_files = model.get("files")
    receipt_files = payload.get("files")
    if not isinstance(locked_files, list) or not isinstance(receipt_files, list):
        return None
    receipt_by_path = {
        item.get("path"): item
        for item in receipt_files
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    if len(receipt_by_path) != len(locked_files):
        return None
    for locked in locked_files:
        if not isinstance(locked, dict):
            return None
        relative = locked.get("path")
        expected_size = locked.get("size", locked.get("size_bytes"))
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or relative.startswith("/")
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
        ):
            return None
        receipt_file = receipt_by_path.get(relative)
        if not isinstance(receipt_file, dict):
            return None
        try:
            file_status = artifact.joinpath(*relative.split("/")).lstat()
        except OSError:
            return None
        if (
            not stat.S_ISREG(file_status.st_mode)
            or file_status.st_size != expected_size
            or receipt_file.get("size") != file_status.st_size
            or receipt_file.get("mtime_ns") != file_status.st_mtime_ns
        ):
            return None
    verified_at = payload.get("verified_at")
    return verified_at if isinstance(verified_at, str) and verified_at else None


def read_secret(path: Path | None) -> str | None:
    """Read a Docker secret and reject empty secret files."""

    if path is None:
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not value or value.upper() in {"REPLACE_ME", "CHANGE_ME"}:
        return None
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
