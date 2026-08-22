"""Offline-only model manifest parsing and artifact verification.

This module deliberately has no Hugging Face dependency and performs no
network operations. Inference workers use :func:`resolve_verified_model`
before opening a model directory.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import stat
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

_COMMIT_SHA_LENGTH = 40
_SHA256_LENGTH = 64
_PLACEHOLDER_SHA256 = "0" * _SHA256_LENGTH
_FORBIDDEN_PATH_CHARACTERS = frozenset("*?[]:")
_MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z"
)
_VERIFICATION_CACHE_LIMIT = 32


class ModelRegistryError(ValueError):
    """Base error for an invalid catalog or local model artifact."""


class ModelNotFoundError(ModelRegistryError):
    """The requested model ID is not present in the immutable catalog."""


class ModelVerificationError(ModelRegistryError):
    """A local model directory does not exactly match its locked manifest."""


@dataclass(frozen=True, slots=True)
class LockedFile:
    """One regular file locked by path, byte size, and SHA-256."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class LockedModel:
    """Validated provisioning fields from one catalog entry."""

    entry: Mapping[str, Any]
    model_id: str
    stage: str
    repository: str
    revision: str
    relative_path: PurePosixPath
    tree_sha256: str
    files: tuple[LockedFile, ...]


@dataclass(frozen=True, slots=True)
class VerifiedModel:
    """A model directory that has passed a complete offline verification."""

    entry: Mapping[str, Any]
    path: Path
    tree_sha256: str

    @property
    def model_id(self) -> str:
        return str(self.entry["id"])

    @property
    def stage(self) -> str:
        return str(self.entry["stage"])


@dataclass(frozen=True, slots=True)
class _VerificationCacheEntry:
    """Identity of a model tree that was fully hashed in this process."""

    identity: tuple[tuple[object, ...], ...]


_verification_cache: OrderedDict[tuple[str, ...], _VerificationCacheEntry] = (
    OrderedDict()
)
_verification_cache_lock = threading.RLock()


def read_model_catalog(path: Path) -> dict[str, Any]:
    """Read and minimally validate a schema-v1 immutable catalog."""

    with path.open("r", encoding="utf-8") as stream:
        raw = json.load(stream)
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ModelRegistryError("Unsupported models.lock.json schema version")
    models = raw.get("models")
    if not isinstance(models, list) or not models:
        raise ModelRegistryError("The model catalog is empty")

    seen_ids: set[str] = set()
    for index, entry in enumerate(models):
        if not isinstance(entry, dict):
            raise ModelRegistryError(f"Catalog model at index {index} is not an object")
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ModelRegistryError(f"Catalog model at index {index} has no valid ID")
        if model_id in seen_ids:
            raise ModelRegistryError(f"Duplicate model ID: {model_id}")
        seen_ids.add(model_id)
    return raw


def find_model_entry(catalog: Mapping[str, Any], model_id: str) -> dict[str, Any]:
    """Return one catalog entry by exact ID without accepting aliases."""

    models = catalog.get("models")
    if not isinstance(models, list):
        raise ModelRegistryError("The model catalog has no model list")
    for entry in models:
        if isinstance(entry, dict) and entry.get("id") == model_id:
            return entry
    raise ModelNotFoundError(f"Unknown model ID: {model_id}")


def _safe_relative_path(value: object, *, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ModelRegistryError(f"{field} must be a non-empty POSIX relative path")
    if any(character in value for character in _FORBIDDEN_PATH_CHARACTERS):
        raise ModelRegistryError(f"{field} contains a forbidden path character")
    raw_parts = value.split("/")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in raw_parts):
        raise ModelRegistryError(f"Unsafe {field}: {value}")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ModelRegistryError(f"Unsafe {field}: {value}")
    return path


def _locked_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ModelRegistryError(f"{field} must be a SHA-256 string")
    digest = value.lower()
    if (
        len(digest) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in digest)
        or digest == _PLACEHOLDER_SHA256
    ):
        raise ModelRegistryError(f"{field} must be a non-placeholder SHA-256")
    return digest


def parse_model_manifest(entry: Mapping[str, Any]) -> LockedModel:
    """Validate the exact download and verification manifest for one model."""

    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id:
        raise ModelRegistryError("Model entry has no valid ID")
    if _MODEL_ID_PATTERN.fullmatch(model_id) is None:
        raise ModelRegistryError(f"Unsafe model ID: {model_id}")

    stage = entry.get("stage")
    repository = entry.get("repository")
    revision = entry.get("revision")
    if not isinstance(stage, str) or not stage:
        raise ModelRegistryError(f"Model {model_id} has no valid stage")
    if (
        not isinstance(repository, str)
        or _REPOSITORY_PATTERN.fullmatch(repository) is None
    ):
        raise ModelRegistryError(f"Model {model_id} has no valid repository")
    if (
        not isinstance(revision, str)
        or len(revision) != _COMMIT_SHA_LENGTH
        or any(character not in "0123456789abcdef" for character in revision)
    ):
        raise ModelRegistryError(
            f"Model {model_id} revision must be a full lowercase 40-character commit SHA"
        )

    raw_relative_path = entry.get("path") or entry.get("local_path") or model_id
    relative_path = _safe_relative_path(raw_relative_path, field=f"model {model_id} path")
    if relative_path.parts[0] == ".staging":
        raise ModelRegistryError(f"Model {model_id} path conflicts with staging storage")

    raw_files = entry.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ModelRegistryError(f"Model {model_id} has no exact file allowlist")

    locked_files: list[LockedFile] = []
    seen_paths: set[str] = set()
    seen_casefolded: set[str] = set()
    for index, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, dict):
            raise ModelRegistryError(f"Model {model_id} file {index} is not an object")
        relative_file = _safe_relative_path(
            raw_file.get("path"), field=f"model {model_id} file path"
        )
        if relative_file.parts[0] == ".cache":
            raise ModelRegistryError(f"Model {model_id} cannot lock manager metadata")
        normalized = relative_file.as_posix()
        folded = normalized.casefold()
        if normalized in seen_paths or folded in seen_casefolded:
            raise ModelRegistryError(f"Model {model_id} has duplicate file path: {normalized}")
        seen_paths.add(normalized)
        seen_casefolded.add(folded)

        raw_size = raw_file.get("size", raw_file.get("size_bytes"))
        if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
            raise ModelRegistryError(
                f"Model {model_id} file {normalized} has no valid byte size"
            )
        digest = _locked_sha256(
            raw_file.get("sha256"), field=f"model {model_id} file {normalized} sha256"
        )
        locked_files.append(LockedFile(normalized, raw_size, digest))

    for file_path in seen_paths:
        parents = PurePosixPath(file_path).parents
        if any(
            parent.as_posix() in seen_paths
            for parent in parents
            if parent.as_posix() != "."
        ):
            raise ModelRegistryError(f"Model {model_id} file paths overlap: {file_path}")

    tree_digest = _locked_sha256(
        entry.get("tree_sha256", entry.get("sha256")),
        field=f"model {model_id} tree_sha256",
    )
    frozen_entry = MappingProxyType(copy.deepcopy(dict(entry)))
    return LockedModel(
        entry=frozen_entry,
        model_id=model_id,
        stage=stage,
        repository=repository,
        revision=revision,
        relative_path=relative_path,
        tree_sha256=tree_digest,
        files=tuple(sorted(locked_files, key=lambda item: item.path)),
    )


def compute_tree_sha256(files: Iterable[tuple[str, int, str]]) -> str:
    """Hash a canonical JSON manifest sorted by POSIX path.

    The canonical payload is a UTF-8 JSON array of objects with ``path``,
    ``sha256``, and ``size`` keys, sorted by path, with sorted object keys and
    no insignificant whitespace.
    """

    normalized = [
        {"path": path, "size": size, "sha256": sha256.lower()}
        for path, size, sha256 in files
    ]
    normalized.sort(key=lambda item: item["path"])
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _hash_regular_file(path: Path, expected_size: int) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ModelVerificationError(f"Cannot safely open model file: {path}") from error

    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb", closefd=True) as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ModelVerificationError(f"Model artifact is not a regular file: {path}")
        if before.st_size != expected_size:
            raise ModelVerificationError(
                f"Model artifact size mismatch for {path}: "
                f"expected {expected_size}, got {before.st_size}"
            )
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if identity_after != identity_before:
            raise ModelVerificationError(f"Model artifact changed while hashing: {path}")
    return digest.hexdigest()


def _walk_regular_tree(root: Path) -> tuple[set[str], set[str]]:
    try:
        root_status = root.lstat()
    except FileNotFoundError as error:
        raise ModelVerificationError(f"Model directory is not installed: {root}") from error
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise ModelVerificationError(f"Model path is not a regular directory: {root}")

    files: set[str] = set()
    directories: set[str] = set()
    for current, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        for name in directory_names:
            directory = current_path / name
            status = directory.lstat()
            relative = directory.relative_to(root).as_posix()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise ModelVerificationError(
                    f"Symlink or special directory is forbidden: {relative}"
                )
            directories.add(relative)
        for name in file_names:
            artifact = current_path / name
            status = artifact.lstat()
            relative = artifact.relative_to(root).as_posix()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise ModelVerificationError(
                    f"Symlink or special file is forbidden: {relative}"
                )
            files.add(relative)
    return files, directories


def _expected_directories(expected_files: set[str]) -> set[str]:
    directories: set[str] = set()
    for file_path in expected_files:
        for parent in PurePosixPath(file_path).parents:
            normalized = parent.as_posix()
            if normalized != ".":
                directories.add(normalized)
    return directories


def _validate_tree_allowlist(
    manifest: LockedModel,
    observed_files: set[str],
    observed_directories: set[str],
) -> None:
    expected_files = {item.path for item in manifest.files}
    if observed_files != expected_files:
        missing = sorted(expected_files - observed_files)
        unexpected = sorted(observed_files - expected_files)
        raise ModelVerificationError(
            f"Model {manifest.model_id} file allowlist mismatch; "
            f"missing={missing}, unexpected={unexpected}"
        )

    expected_directories = _expected_directories(expected_files)
    if observed_directories != expected_directories:
        unexpected = sorted(observed_directories - expected_directories)
        missing = sorted(expected_directories - observed_directories)
        raise ModelVerificationError(
            f"Model {manifest.model_id} directory allowlist mismatch; "
            f"missing={missing}, unexpected={unexpected}"
        )


def _status_identity(path: Path, relative: str) -> tuple[object, ...]:
    """Return mutation-sensitive metadata without following symlinks."""

    try:
        status = path.lstat()
    except FileNotFoundError as error:
        raise ModelVerificationError(f"Model artifact disappeared: {path}") from error
    return (
        relative,
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _supports_safe_verification_cache(model_path: Path) -> bool:
    """Allow metadata reuse only on an explicitly read-only POSIX mount.

    Writable filesystems can preserve a same-size file's complete stat identity
    when two writes land inside one timestamp quantum. A read-only mount is the
    integrity boundary that makes metadata reuse safe for the offline worker;
    native/writable layouts deliberately perform complete SHA-256 every time.
    """

    read_only_flag = getattr(os, "ST_RDONLY", None)
    if os.name != "posix" or not isinstance(read_only_flag, int):
        return False
    try:
        return bool(os.statvfs(model_path).f_flag & read_only_flag)
    except OSError:
        return False


def _model_tree_identity(
    manifest: LockedModel, model_path: Path
) -> tuple[tuple[object, ...], ...]:
    """Build a cheap exact identity for a previously verified model tree.

    On a read-only POSIX model mount, the worker performs one complete SHA-256
    verification per process. Subsequent stages/jobs may reuse it only while
    the root, directory allowlist, file allowlist and mutation-sensitive
    metadata are unchanged. Writable and unsupported filesystems always hash.
    """

    observed_files, observed_directories = _walk_regular_tree(model_path)
    _validate_tree_allowlist(manifest, observed_files, observed_directories)

    records = [_status_identity(model_path, ".")]
    for relative in sorted(observed_directories):
        records.append(
            _status_identity(
                model_path.joinpath(*PurePosixPath(relative).parts),
                f"d:{relative}",
            )
        )
    expected_sizes = {item.path: item.size for item in manifest.files}
    for relative in sorted(observed_files):
        record = _status_identity(
            model_path.joinpath(*PurePosixPath(relative).parts),
            f"f:{relative}",
        )
        if record[4] != expected_sizes[relative]:
            raise ModelVerificationError(
                f"Model artifact size mismatch for {relative}: "
                f"expected {expected_sizes[relative]}, got {record[4]}"
            )
        records.append(record)
    return tuple(records)


def verify_model_directory(manifest: LockedModel, model_path: Path) -> VerifiedModel:
    """Fully verify a local model tree without any network access."""

    observed_files, observed_directories = _walk_regular_tree(model_path)
    _validate_tree_allowlist(manifest, observed_files, observed_directories)

    actual_records: list[tuple[str, int, str]] = []
    for locked_file in manifest.files:
        artifact = model_path.joinpath(*PurePosixPath(locked_file.path).parts)
        digest = _hash_regular_file(artifact, locked_file.size)
        if not hmac.compare_digest(digest, locked_file.sha256):
            raise ModelVerificationError(
                f"Model artifact SHA-256 mismatch: {locked_file.path}"
            )
        actual_records.append((locked_file.path, locked_file.size, digest))

    actual_tree_digest = compute_tree_sha256(actual_records)
    if not hmac.compare_digest(actual_tree_digest, manifest.tree_sha256):
        raise ModelVerificationError(
            f"Model {manifest.model_id} tree SHA-256 mismatch"
        )
    return VerifiedModel(
        entry=manifest.entry,
        path=model_path,
        tree_sha256=actual_tree_digest,
    )


def _path_exists_without_following(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def model_destination(models_dir: Path, manifest: LockedModel) -> Path:
    """Resolve a locked destination and reject symlinks in its parent chain."""

    if _path_exists_without_following(models_dir):
        status = models_dir.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise ModelVerificationError(
                f"Models root is not a regular directory: {models_dir}"
            )
    destination = models_dir.joinpath(*manifest.relative_path.parts)
    current = models_dir
    for part in manifest.relative_path.parts[:-1]:
        current = current / part
        if _path_exists_without_following(current):
            status = current.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise ModelVerificationError(
                    f"Unsafe model parent directory: {current}"
                )
    return destination


def resolve_verified_model(
    lock_path: Path,
    models_dir: Path,
    model_id: str,
    expected_stage: str,
) -> VerifiedModel:
    """Resolve an offline model, fully hashing it once per worker process.

    On a read-only POSIX mount, a warm lookup reuses the completed verification
    only when a cheap exact tree identity still matches. Any replacement,
    metadata mutation, extra path, missing path or changed catalog pin falls
    back to full SHA-256. Writable/unsupported filesystems hash every lookup.
    """

    catalog = read_model_catalog(lock_path)
    manifest = parse_model_manifest(find_model_entry(catalog, model_id))
    if manifest.stage != expected_stage:
        raise ModelVerificationError(
            f"Model {model_id} belongs to stage {manifest.stage}, not {expected_stage}"
        )
    destination = model_destination(models_dir, manifest)
    if not _supports_safe_verification_cache(destination):
        return verify_model_directory(manifest, destination)

    cache_key = (
        os.path.abspath(lock_path),
        os.path.abspath(models_dir),
        manifest.model_id,
        manifest.stage,
        manifest.revision,
        manifest.relative_path.as_posix(),
        manifest.tree_sha256,
        compute_tree_sha256(
            (item.path, item.size, item.sha256) for item in manifest.files
        ),
    )
    identity_before = _model_tree_identity(manifest, destination)
    with _verification_cache_lock:
        cached = _verification_cache.get(cache_key)
        if cached is not None and cached.identity == identity_before:
            _verification_cache.move_to_end(cache_key)
            return VerifiedModel(
                entry=manifest.entry,
                path=destination,
                tree_sha256=manifest.tree_sha256,
            )
        _verification_cache.pop(cache_key, None)

    verified = verify_model_directory(manifest, destination)
    identity_after = _model_tree_identity(manifest, destination)
    if identity_after != identity_before:
        raise ModelVerificationError(
            f"Model {manifest.model_id} changed during complete verification"
        )

    with _verification_cache_lock:
        _verification_cache[cache_key] = _VerificationCacheEntry(identity_after)
        _verification_cache.move_to_end(cache_key)
        while len(_verification_cache) > _VERIFICATION_CACHE_LIMIT:
            _verification_cache.popitem(last=False)
    return verified
