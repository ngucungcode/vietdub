"""Explicit, network-enabled model provisioning entry point.

Only this module may call Hugging Face ``snapshot_download``. Runtime workers
use :mod:`dub_server.model_registry`, which is strictly offline.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .model_registry import (
    LockedModel,
    ModelRegistryError,
    ModelVerificationError,
    VerifiedModel,
    find_model_entry,
    model_destination,
    parse_model_manifest,
    read_model_catalog,
    verify_model_directory,
)

SnapshotDownload = Callable[..., str]


def _write_verification_receipt(
    models_dir: Path,
    verified: VerifiedModel,
) -> None:
    """Publish a small API-facing receipt outside the locked model tree.

    Workers never trust this receipt: they hash every locked file on first use
    in each process and may then cache only while the model mount is read-only.
    The receipt only lets the control plane report the last successful explicit
    verification without re-reading gigabytes on every list request.
    """

    model_id = verified.model_id
    revision = verified.entry.get("revision")
    if not isinstance(revision, str):
        raise ModelVerificationError("Verified model has no locked revision")
    receipt_root = models_dir / ".verified"
    receipt_root.mkdir(mode=0o700, exist_ok=True)
    receipt = receipt_root / f"{model_id}.json"
    temporary = receipt_root / f".{model_id}.{os.getpid()}.tmp"
    file_state: list[dict[str, Any]] = []
    raw_files = verified.entry.get("files")
    if not isinstance(raw_files, list):
        raise ModelVerificationError("Verified model has no locked file list")
    for raw_file in raw_files:
        if not isinstance(raw_file, dict) or not isinstance(raw_file.get("path"), str):
            raise ModelVerificationError("Verified model has an invalid locked file")
        relative = PurePosixPath(raw_file["path"])
        status = verified.path.joinpath(*relative.parts).lstat()
        file_state.append(
            {
                "path": relative.as_posix(),
                "size": status.st_size,
                "mtime_ns": status.st_mtime_ns,
            }
        )
    file_state.sort(key=lambda item: item["path"])
    payload = {
        "schema_version": 1,
        "id": model_id,
        "stage": verified.stage,
        "revision": revision,
        "tree_sha256": verified.tree_sha256,
        "path": str(verified.path),
        "files": file_state,
        "verified_at": datetime.now(UTC).isoformat(),
    }
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, receipt)


def load_catalog(path: Path) -> dict[str, Any]:
    """Compatibility wrapper retained for Phase 1 callers and tests."""

    return read_model_catalog(path)


def _path_exists_without_following(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _validate_metadata_tree(root: Path) -> None:
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            status = (current_path / name).lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise ModelVerificationError(
                    "Unsafe Hugging Face local metadata directory"
                )
        for name in file_names:
            status = (current_path / name).lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise ModelVerificationError("Unsafe Hugging Face local metadata file")


def _remove_huggingface_local_metadata(payload: Path) -> None:
    """Remove only snapshot_download's documented local-dir metadata."""

    cache_root = payload / ".cache"
    if not _path_exists_without_following(cache_root):
        return
    status = cache_root.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise ModelVerificationError("Unsafe .cache object in downloaded model")
    children = {child.name for child in cache_root.iterdir()}
    if children != {"huggingface"}:
        raise ModelVerificationError(
            f"Unexpected .cache content in downloaded model: {sorted(children)}"
        )
    _validate_metadata_tree(cache_root)
    shutil.rmtree(cache_root)


def _ensure_models_root(models_dir: Path) -> None:
    if _path_exists_without_following(models_dir):
        status = models_dir.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise ModelVerificationError(f"Unsafe models root: {models_dir}")
    else:
        models_dir.mkdir(parents=True, exist_ok=False)


def _ensure_destination_parent(models_dir: Path, manifest: LockedModel) -> None:
    """Create locked parent components without traversing a symlink."""

    current = models_dir
    for part in manifest.relative_path.parts[:-1]:
        current = current / part
        if not _path_exists_without_following(current):
            current.mkdir()
        status = current.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise ModelVerificationError(f"Unsafe model parent directory: {current}")


def _snapshot_download() -> SnapshotDownload:
    # Keep the network-capable dependency isolated to this explicit manager.
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise ModelRegistryError(
            "huggingface_hub is required only for explicit model installation"
        ) from error
    return snapshot_download


def install_model(
    lock_path: Path,
    models_dir: Path,
    model_id: str,
    *,
    downloader: SnapshotDownload | None = None,
) -> VerifiedModel:
    """Download, verify, and atomically publish one locked model.

    Existing valid models are returned untouched. Existing invalid paths are
    preserved for operator inspection and must never be overwritten implicitly.
    """

    catalog = read_model_catalog(lock_path)
    manifest = parse_model_manifest(find_model_entry(catalog, model_id))
    _ensure_models_root(models_dir)
    destination = model_destination(models_dir, manifest)
    if _path_exists_without_following(destination):
        try:
            verified = verify_model_directory(manifest, destination)
            _write_verification_receipt(models_dir, verified)
            return verified
        except ModelVerificationError as error:
            raise ModelVerificationError(
                f"Existing model path is invalid and was preserved: {destination}"
            ) from error

    staging_root = models_dir / ".staging"
    if _path_exists_without_following(staging_root):
        status = staging_root.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise ModelVerificationError(f"Unsafe staging root: {staging_root}")
    else:
        staging_root.mkdir(mode=0o700)

    stage = Path(tempfile.mkdtemp(prefix=f"{manifest.model_id}-", dir=staging_root))
    payload = stage / "payload"
    payload.mkdir(mode=0o700)
    try:
        download = downloader or _snapshot_download()
        downloaded_path = Path(
            download(
                repo_id=manifest.repository,
                revision=manifest.revision,
                allow_patterns=[item.path for item in manifest.files],
                local_dir=payload,
                repo_type="model",
            )
        )
        payload_status = payload.lstat()
        if stat.S_ISLNK(payload_status.st_mode) or not stat.S_ISDIR(
            payload_status.st_mode
        ):
            raise ModelVerificationError(
                "snapshot_download replaced its assigned staging directory"
            )
        if downloaded_path.resolve() != payload.resolve():
            raise ModelVerificationError(
                "snapshot_download returned a path outside its assigned staging directory"
            )
        _remove_huggingface_local_metadata(payload)
        staged = verify_model_directory(manifest, payload)

        _ensure_destination_parent(models_dir, manifest)
        # Re-evaluate parent safety after creating any locked nested directory.
        destination = model_destination(models_dir, manifest)
        if _path_exists_without_following(destination):
            raise ModelVerificationError(
                "Model destination appeared during installation and was preserved: "
                f"{destination}"
            )
        payload.rename(destination)
        published = VerifiedModel(
            entry=staged.entry,
            path=destination,
            tree_sha256=staged.tree_sha256,
        )
        _write_verification_receipt(models_dir, published)
        return published
    finally:
        # The published destination has already moved out of this unique stage.
        shutil.rmtree(stage, ignore_errors=True)


def verify_model(lock_path: Path, models_dir: Path, model_id: str) -> VerifiedModel:
    """Explicitly verify one installed model without enabling network access."""

    catalog = read_model_catalog(lock_path)
    manifest = parse_model_manifest(find_model_entry(catalog, model_id))
    verified = verify_model_directory(
        manifest,
        model_destination(models_dir, manifest),
    )
    _write_verification_receipt(models_dir, verified)
    return verified


def catalog_status(lock_path: Path, models_dir: Path) -> dict[str, Any]:
    """Return a backward-compatible all-model verification summary."""

    catalog = read_model_catalog(lock_path)
    records: list[dict[str, Any]] = []
    for raw_entry in catalog["models"]:
        assert isinstance(raw_entry, dict)
        record: dict[str, Any] = {
            "id": raw_entry["id"],
            "stage": raw_entry.get("stage"),
            "installed": False,
            "valid": False,
        }
        try:
            manifest = parse_model_manifest(raw_entry)
            destination = model_destination(models_dir, manifest)
            record["installed"] = _path_exists_without_following(destination)
            if record["installed"]:
                verified = verify_model_directory(manifest, destination)
                record["valid"] = True
                record["tree_sha256"] = verified.tree_sha256
        except ModelRegistryError as error:
            record["error"] = str(error)
        records.append(record)
    return {
        "schema_valid": True,
        "artifacts_verified": bool(records)
        and all(record["valid"] for record in records),
        "count": len(records),
        "models": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the local model catalog")
    parser.add_argument("command", choices=("list", "install", "verify"))
    parser.add_argument("model_id", nargs="?")
    parser.add_argument("--model-id", dest="model_id_option")
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path(
            os.environ.get("DUB_MODELS_LOCK_PATH", "/app/config/models.lock.json")
        ),
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path(os.environ.get("DUB_MODELS_DIR", "/models")),
    )
    args = parser.parse_args()
    if args.model_id is not None and args.model_id_option is not None:
        parser.error("MODEL_ID and --model-id cannot be used together")
    selected_model_id = args.model_id_option or args.model_id
    try:
        catalog = load_catalog(args.lock)
        if args.command == "list":
            selected = catalog["models"]
            if selected_model_id is not None:
                selected = [find_model_entry(catalog, selected_model_id)]
            for model in selected:
                print(f"{model['stage']}\t{model['id']}\t{model['license']}")
            return
        if args.command == "install":
            if selected_model_id is None:
                parser.error("install requires MODEL_ID")
            result = install_model(args.lock, args.models_dir, selected_model_id)
            payload = {
                "id": result.model_id,
                "stage": result.stage,
                "path": str(result.path),
                "tree_sha256": result.tree_sha256,
                "valid": True,
            }
        elif selected_model_id is not None:
            result = verify_model(args.lock, args.models_dir, selected_model_id)
            payload = {
                "id": result.model_id,
                "stage": result.stage,
                "path": str(result.path),
                "tree_sha256": result.tree_sha256,
                "valid": True,
            }
        else:
            payload = catalog_status(args.lock, args.models_dir)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except (OSError, json.JSONDecodeError, ModelRegistryError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
