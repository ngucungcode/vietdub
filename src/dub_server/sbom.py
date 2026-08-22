"""CycloneDX SBOM generation without a runtime SBOM dependency.

The generator inventories the Python environment together with lock manifests
for downloaded models, native/system components, and the embedded web build.
It does not inspect or download payloads; immutable lock metadata is the source
of truth for those components.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.metadata
import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from . import __version__

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_NORMALIZE_NAME_RE = re.compile(r"[-_.]+")


class SbomError(RuntimeError):
    """Raised when a lock manifest cannot produce a trustworthy SBOM."""


def build_cyclonedx_sbom(
    models_lock_path: Path,
    native_lock_path: Path,
    *,
    web_lock_path: Path | None = None,
    python_lock_path: Path | None = None,
    native_receipt_path: Path | None = None,
    distributions: Iterable[importlib.metadata.Distribution] | None = None,
    generated_at: datetime | None = None,
    serial_number: str | None = None,
) -> dict[str, Any]:
    """Build a CycloneDX 1.6 document from local, read-only inputs."""

    models_lock = _read_lock(models_lock_path, expected_collection="models")
    native_lock = _read_lock(native_lock_path, expected_collection="components")
    _validate_native_lock(native_lock["components"])
    native_receipt = (
        _read_llama_receipt(native_receipt_path, native_lock["components"])
        if native_receipt_path is not None
        else None
    )
    timestamp = generated_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    else:
        timestamp = timestamp.astimezone(UTC)

    if python_lock_path is None:
        distribution_list = tuple(
            importlib.metadata.distributions()
            if distributions is None
            else distributions
        )
        python_components = _python_components(distribution_list)
        application_version = _application_version(distribution_list)
    else:
        distribution_list = ()
        python_components = _python_lock_components(python_lock_path)
        application_version = __version__
    model_components = _model_components(models_lock["models"])
    native_components = _native_components(
        native_lock["components"],
        llama_receipt=native_receipt,
    )
    web_components: list[dict[str, Any]] = []
    if web_lock_path is not None:
        web_lock = _read_web_lock(web_lock_path)
        web_components = _npm_components(web_lock["packages"])
    components = sorted(
        [
            *python_components,
            *model_components,
            *native_components,
            *web_components,
        ],
        key=lambda item: item["bom-ref"],
    )
    refs = [item["bom-ref"] for item in components]
    application_ref = "application:vietdub"

    lock_properties = [
        {
            "name": "thuyetminh:models-lock-sha256",
            "value": _file_sha256(models_lock_path),
        },
        {
            "name": "thuyetminh:native-lock-sha256",
            "value": _file_sha256(native_lock_path),
        },
    ]
    if web_lock_path is not None:
        lock_properties.append(
            {
                "name": "thuyetminh:web-lock-sha256",
                "value": _file_sha256(web_lock_path),
            }
        )
    if python_lock_path is not None:
        lock_properties.extend(
            (
                {
                    "name": "thuyetminh:python-runtime-lock-sha256",
                    "value": _file_sha256(python_lock_path),
                },
                {
                    "name": "thuyetminh:python-runtime-dependency-coverage",
                    "value": "transitive",
                },
            )
        )
    if native_receipt_path is not None:
        lock_properties.append(
            {
                "name": "thuyetminh:native-build-receipt-sha256",
                "value": _file_sha256(native_receipt_path),
            }
        )

    return {
        "$schema": "https://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": serial_number or f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "component": {
                "type": "application",
                "bom-ref": application_ref,
                "name": "vietdub",
                "version": application_version,
                "licenses": [{"license": {"id": "GPL-3.0-or-later"}}],
            },
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "stdlib-sbom-generator",
                        "version": "5",
                    }
                ]
            },
            "properties": lock_properties,
        },
        "components": components,
        "dependencies": [
            {"ref": application_ref, "dependsOn": refs},
            *({"ref": ref, "dependsOn": []} for ref in refs),
        ],
    }


def write_cyclonedx_sbom(
    destination: Path,
    models_lock_path: Path,
    native_lock_path: Path,
    *,
    web_lock_path: Path | None = None,
    python_lock_path: Path | None = None,
    native_receipt_path: Path | None = None,
    distributions: Iterable[importlib.metadata.Distribution] | None = None,
    generated_at: datetime | None = None,
    serial_number: str | None = None,
) -> dict[str, Any]:
    """Build and atomically publish one UTF-8 CycloneDX JSON document."""

    document = build_cyclonedx_sbom(
        models_lock_path,
        native_lock_path,
        web_lock_path=web_lock_path,
        python_lock_path=python_lock_path,
        native_receipt_path=native_receipt_path,
        distributions=distributions,
        generated_at=generated_at,
        serial_number=serial_number,
    )
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    target = Path(destination).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".part",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return document


def _read_lock(path: Path, *, expected_collection: str) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SbomError(f"Không thể đọc lock manifest: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise SbomError(f"Lock manifest không đúng schema_version 1: {path}")
    collection = raw.get(expected_collection)
    expected_type = list if expected_collection == "models" else dict
    if not isinstance(collection, expected_type):
        raise SbomError(
            f"Lock manifest thiếu collection {expected_collection}: {path}"
        )
    return raw


def _read_web_lock(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SbomError(f"Không thể đọc web package lock: {path}") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("lockfileVersion") not in {2, 3}
        or not isinstance(raw.get("packages"), dict)
    ):
        raise SbomError(f"Web package lock không đúng schema npm: {path}")
    return raw


def _read_llama_receipt(
    path: Path,
    components: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SbomError(f"Không thể đọc native build receipt: {path}") from exc
    llama = components.get("llama_cpp")
    if not isinstance(llama, dict) or not isinstance(raw, dict):
        raise SbomError("Native build receipt không khớp component llama_cpp")
    if raw.get("schema_version") != 1:
        raise SbomError("Native build receipt không đúng schema_version 1")
    for key in ("release", "commit"):
        if raw.get(key) != llama.get(key):
            raise SbomError(f"Native build receipt không khớp llama_cpp {key}")
    cuda_version = raw.get("cuda_version")
    if cuda_version not in llama.get("cuda_supported_versions", []):
        raise SbomError("Native build receipt có CUDA toolkit không được hỗ trợ")
    cuda_architectures = raw.get("cuda_architectures")
    if not isinstance(cuda_architectures, str) or not re.fullmatch(
        r"[0-9]{2,3}(;[0-9]{2,3})*", cuda_architectures
    ):
        raise SbomError("Native build receipt có CUDA architecture không hợp lệ")
    supported_architectures = {
        str(value) for value in llama.get("cuda_supported_architectures", [])
    }
    if any(
        architecture not in supported_architectures
        for architecture in cuda_architectures.split(";")
    ):
        raise SbomError("Native build receipt có CUDA architecture không được hỗ trợ")
    binaries = raw.get("binaries")
    if not isinstance(binaries, dict):
        raise SbomError("Native build receipt thiếu binary hashes")
    receipt_root = Path(path).resolve(strict=True).parent
    for binary_name, receipt_key in (
        ("llama-server", "llama_server_sha256"),
        ("llama-cli", "llama_cli_sha256"),
    ):
        expected_digest = binaries.get(receipt_key)
        binary_path = receipt_root / binary_name
        if (
            not isinstance(expected_digest, str)
            or _SHA256_RE.fullmatch(expected_digest) is None
            or not binary_path.is_file()
            or _file_sha256(binary_path) != expected_digest.lower()
        ):
            raise SbomError(f"Native build receipt không xác minh được {binary_name}")
    return raw


def _validate_native_lock(components: Mapping[str, Any]) -> None:
    """Validate CUDA metadata whose meaning must remain unambiguous in SBOMs."""

    for name, raw in components.items():
        if not isinstance(raw, dict):
            continue
        cuda_keys = {
            "cuda_version",
            "cuda_supported_versions",
            "cuda_supported_architectures",
            "cuda_default_build_architecture",
            "cuda_architectures",
        }
        if not cuda_keys.intersection(raw):
            continue
        if "cuda_architectures" in raw:
            raise SbomError(
                f"Component {name} dùng trường cuda_architectures mơ hồ; "
                "hãy tách supported và default build architecture"
            )
        cuda_version = raw.get("cuda_version")
        if not isinstance(cuda_version, str) or not cuda_version.strip():
            raise SbomError(f"Component {name} có cuda_version không hợp lệ")
        supported_versions = raw.get("cuda_supported_versions")
        if (
            not isinstance(supported_versions, list)
            or not supported_versions
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"[0-9]+\.[0-9]+", value) is None
                for value in supported_versions
            )
            or supported_versions != sorted(set(supported_versions))
        ):
            raise SbomError(
                f"Component {name} có cuda_supported_versions không hợp lệ"
            )
        if cuda_version not in supported_versions:
            raise SbomError(
                f"Component {name} có cuda_version không thuộc ma trận hỗ trợ"
            )
        supported = raw.get("cuda_supported_architectures")
        if (
            not isinstance(supported, list)
            or not supported
            or any(type(value) is not int or value < 10 for value in supported)
            or supported != sorted(set(supported))
        ):
            raise SbomError(
                f"Component {name} có cuda_supported_architectures không hợp lệ"
            )
        default = raw.get("cuda_default_build_architecture")
        if type(default) is not int or default not in supported:
            raise SbomError(
                f"Component {name} có cuda_default_build_architecture "
                "không thuộc ma trận hỗ trợ"
            )


def _python_components(
    distributions: Iterable[importlib.metadata.Distribution],
) -> list[dict[str, Any]]:
    by_ref: dict[str, dict[str, Any]] = {}
    for distribution in distributions:
        metadata = distribution.metadata
        name = str(metadata.get("Name") or "").strip()
        version = str(getattr(distribution, "version", "") or "").strip()
        if not name or not version:
            continue
        normalized = _NORMALIZE_NAME_RE.sub("-", name).lower()
        purl = f"pkg:pypi/{quote(normalized, safe='-')}@{quote(version, safe='.+-')}"
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": f"python:{normalized}@{version}",
            "name": name,
            "version": version,
            "purl": purl,
            "properties": [
                {"name": "thuyetminh:source", "value": "python-distribution"}
            ],
        }
        description = str(metadata.get("Summary") or "").strip()
        if description:
            component["description"] = description
        license_name = str(
            metadata.get("License-Expression") or metadata.get("License") or ""
        ).strip()
        if license_name and license_name.upper() != "UNKNOWN":
            component["licenses"] = [_license(license_name)]
        homepage = str(metadata.get("Home-page") or "").strip()
        if homepage.startswith(("https://", "http://")):
            component["externalReferences"] = [
                {"type": "website", "url": homepage}
            ]
        by_ref[component["bom-ref"]] = component
    return list(by_ref.values())


def _python_lock_components(path: Path) -> list[dict[str, Any]]:
    """Inventory every exact runtime pin from a pip-compile style lock."""

    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SbomError(f"Không thể đọc Python runtime lock: {path}") from exc

    requirement_pattern = re.compile(
        r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
        r"(?:\[(?P<extras>[A-Za-z0-9._,-]+)\])?"
        r"==(?P<version>[^\s;@]+)(?:\s*;\s*(?P<marker>.+?))?(?:\s*\\)?$"
    )
    components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(lines, start=1):
        requirement = raw_line.strip()
        if (
            not requirement
            or requirement.startswith("#")
            or requirement.startswith("--hash=")
        ):
            continue
        matched = requirement_pattern.fullmatch(requirement)
        if matched is None:
            raise SbomError(
                "Python runtime lock chỉ chấp nhận dependency ghim chính xác "
                f"name==version với hash/marker tùy chọn; dòng {line_number}: {requirement}"
            )
        name = matched.group("name")
        version = matched.group("version")
        normalized = _NORMALIZE_NAME_RE.sub("-", name).lower()
        if normalized in seen:
            raise SbomError(f"Python runtime lock có package trùng: {normalized}")
        seen.add(normalized)
        properties = [
            {
                "name": "thuyetminh:source",
                "value": "python-runtime-lock",
            },
            {
                "name": "thuyetminh:python:requirement",
                "value": requirement.removesuffix("\\").rstrip(),
            },
        ]
        extras = matched.group("extras")
        if extras:
            properties.append(
                {
                    "name": "thuyetminh:python:extras",
                    "value": json.dumps(
                        sorted(set(extras.split(","))),
                        separators=(",", ":"),
                    ),
                }
            )
        marker = matched.group("marker")
        if marker:
            properties.append(
                {"name": "thuyetminh:python:marker", "value": marker.strip()}
            )
        components.append(
            {
                "type": "library",
                "bom-ref": f"python:{normalized}@{version}",
                "name": name,
                "version": version,
                "purl": (
                    f"pkg:pypi/{quote(normalized, safe='-')}@"
                    f"{quote(version, safe='.+-')}"
                ),
                "scope": "required",
                "properties": properties,
            }
        )
    if not components:
        raise SbomError(f"Python runtime lock không chứa dependency: {path}")
    return components


def _model_components(models: list[Any]) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in models:
        if not isinstance(raw, dict):
            raise SbomError("models.lock.json chứa model không hợp lệ")
        identifier = _required_text(raw, "id", "model")
        if identifier in seen:
            raise SbomError(f"ID model bị trùng trong lock manifest: {identifier}")
        seen.add(identifier)
        revision = str(raw.get("revision") or "unversioned")
        component: dict[str, Any] = {
            "type": "machine-learning-model",
            "bom-ref": f"model:{identifier}@{revision}",
            "name": identifier,
            "version": revision,
            "properties": _properties(
                raw,
                prefix="thuyetminh:model",
                keys=(
                    "stage",
                    "backend",
                    "repository",
                    "path",
                    "compute_type",
                    "minimum_vram_mib",
                    "quality_tier",
                    "selectable",
                ),
            ),
        }
        license_name = str(raw.get("license") or "").strip()
        if license_name:
            component["licenses"] = [_license(license_name)]
        digest = raw.get("tree_sha256") or raw.get("sha256")
        if isinstance(digest, str) and _SHA256_RE.fullmatch(digest):
            component["hashes"] = [
                {"alg": "SHA-256", "content": digest.lower()}
            ]
        repository = str(raw.get("repository") or "").strip()
        if repository:
            if repository.startswith(("https://", "http://")):
                url = repository
            else:
                url = f"https://huggingface.co/{repository}/tree/{quote(revision, safe='')}"
            component["externalReferences"] = [
                {"type": "distribution", "url": url}
            ]
        files = raw.get("files")
        if isinstance(files, list):
            total_size = sum(
                item.get("size", 0)
                for item in files
                if isinstance(item, dict)
                and isinstance(item.get("size"), int)
                and not isinstance(item.get("size"), bool)
            )
            component["properties"].extend(
                [
                    {
                        "name": "thuyetminh:model:file-count",
                        "value": str(len(files)),
                    },
                    {
                        "name": "thuyetminh:model:locked-size-bytes",
                        "value": str(total_size),
                    },
                ]
            )
        components.append(component)
    return components


def _native_components(
    components: Mapping[str, Any],
    *,
    llama_receipt: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name, raw in components.items():
        if not isinstance(name, str) or not name or not isinstance(raw, dict):
            raise SbomError("components.lock.json chứa component không hợp lệ")
        version = str(
            raw.get("version")
            or raw.get("release")
            or raw.get("commit")
            or "system-managed"
        )
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": f"native:{name}@{version}",
            "name": name,
            "version": version,
            "properties": _properties(
                raw,
                prefix="thuyetminh:native",
                keys=(
                    "package",
                    "source",
                    "commit",
                    "cuda_supported_versions",
                    "cuda_supported_architectures",
                    "cuda_default_build_architecture",
                    "runtime_path",
                ),
            ),
        }
        preferred_cuda_version = raw.get("cuda_version")
        if preferred_cuda_version is not None:
            component["properties"].append(
                {
                    "name": "thuyetminh:native:cuda-preferred-version",
                    "value": str(preferred_cuda_version),
                }
            )
        if name == "llama_cpp" and llama_receipt is not None:
            binaries = llama_receipt["binaries"]
            component["properties"].extend(
                [
                    {
                        "name": "thuyetminh:native:cuda-build-version",
                        "value": str(llama_receipt["cuda_version"]),
                    },
                    {
                        "name": "thuyetminh:native:cuda-build-architectures",
                        "value": str(llama_receipt["cuda_architectures"]),
                    },
                    {
                        "name": "thuyetminh:native:llama-server-sha256",
                        "value": str(binaries["llama_server_sha256"]).lower(),
                    },
                    {
                        "name": "thuyetminh:native:llama-cli-sha256",
                        "value": str(binaries["llama_cli_sha256"]).lower(),
                    },
                ]
            )
        license_name = str(raw.get("license") or "").strip()
        if license_name:
            component["licenses"] = [_license(license_name)]
        digest = raw.get("sha256")
        if isinstance(digest, str) and _SHA256_RE.fullmatch(digest):
            component["hashes"] = [
                {"alg": "SHA-256", "content": digest.lower()}
            ]
        references: list[dict[str, str]] = []
        repository = str(raw.get("repository") or "").strip()
        url = str(raw.get("url") or "").strip()
        if repository.startswith(("https://", "http://")):
            references.append({"type": "vcs", "url": repository})
        if url.startswith(("https://", "http://")):
            references.append({"type": "distribution", "url": url})
        if references:
            component["externalReferences"] = references
        result.append(component)
    return result


def _npm_components(packages: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Inventory every pinned npm package, including build-only dependencies."""

    by_ref: dict[str, dict[str, Any]] = {}
    scopes_by_ref: dict[str, set[str]] = {}
    paths_by_ref: dict[str, set[str]] = {}
    for package_path, raw in packages.items():
        if not package_path or not isinstance(package_path, str):
            continue
        if not isinstance(raw, dict):
            raise SbomError("web package lock chứa package không hợp lệ")
        version = str(raw.get("version") or "").strip()
        name = _npm_package_name(package_path)
        if not name or not version:
            continue
        encoded_name = "/".join(quote(part, safe="") for part in name.split("/"))
        encoded_version = quote(version, safe=".+-~")
        bom_ref = f"npm:{name}@{version}"
        scope = "development" if raw.get("dev") is True else "runtime"
        scopes_by_ref.setdefault(bom_ref, set()).add(scope)
        paths_by_ref.setdefault(bom_ref, set()).add(package_path)

        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": bom_ref,
            "name": name,
            "version": version,
            "purl": f"pkg:npm/{encoded_name}@{encoded_version}",
        }
        license_name = str(raw.get("license") or "").strip()
        if license_name:
            component["licenses"] = [_license(license_name)]
        integrity_hash = _npm_integrity_hash(raw.get("integrity"))
        if integrity_hash is not None:
            component["hashes"] = [integrity_hash]
        resolved = str(raw.get("resolved") or "").strip()
        parsed_resolved = urlsplit(resolved)
        if (
            parsed_resolved.scheme == "https"
            and parsed_resolved.hostname
            and not parsed_resolved.username
            and not parsed_resolved.password
            and not parsed_resolved.query
            and not parsed_resolved.fragment
        ):
            component["externalReferences"] = [
                {"type": "distribution", "url": resolved}
            ]
        by_ref.setdefault(bom_ref, component)

    for bom_ref, component in by_ref.items():
        scopes = scopes_by_ref[bom_ref]
        effective_scope = "runtime" if "runtime" in scopes else "development"
        component["scope"] = "required" if effective_scope == "runtime" else "excluded"
        component["properties"] = [
            {"name": "thuyetminh:npm:scope", "value": effective_scope},
            {
                "name": "thuyetminh:npm:lock-paths",
                "value": json.dumps(sorted(paths_by_ref[bom_ref]), separators=(",", ":")),
            },
        ]
    return list(by_ref.values())


def _npm_package_name(package_path: str) -> str | None:
    marker = "node_modules/"
    if marker not in package_path:
        return None
    name = package_path.rsplit(marker, 1)[1].strip("/")
    if not name or "/node_modules/" in name:
        return None
    if name.startswith("@"):
        parts = name.split("/")
        return name if len(parts) == 2 and all(parts) else None
    return name if "/" not in name else None


def _npm_integrity_hash(value: Any) -> dict[str, str] | None:
    if not isinstance(value, str) or "-" not in value:
        return None
    algorithm, encoded = value.split("-", 1)
    cyclone_algorithm = {
        "sha256": "SHA-256",
        "sha384": "SHA-384",
        "sha512": "SHA-512",
    }.get(algorithm.lower())
    if cyclone_algorithm is None:
        return None
    try:
        digest = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    expected_bytes = {"SHA-256": 32, "SHA-384": 48, "SHA-512": 64}
    if len(digest) != expected_bytes[cyclone_algorithm]:
        return None
    return {"alg": cyclone_algorithm, "content": digest.hex()}


def _application_version(
    distributions: Iterable[importlib.metadata.Distribution],
) -> str:
    for distribution in distributions:
        name = str(distribution.metadata.get("Name") or "")
        if _NORMALIZE_NAME_RE.sub("-", name).lower() == "vietdub":
            return str(distribution.version)
    try:
        return importlib.metadata.version("vietdub")
    except importlib.metadata.PackageNotFoundError:
        return __version__


def _license(value: str) -> dict[str, dict[str, str]]:
    # Package metadata frequently uses short legacy labels such as "BSD" or
    # "Apache" that look like identifiers but are not valid SPDX IDs. Keep the
    # exact declared value as a CycloneDX license name unless a dedicated SPDX
    # parser is introduced; this remains schema-valid without inventing an ID.
    return {"license": {"name": value[:512]}}


def _properties(
    raw: Mapping[str, Any],
    *,
    prefix: str,
    keys: Iterable[str],
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        encoded = (
            json.dumps(value, ensure_ascii=False, sort_keys=True)
            if isinstance(value, (dict, list))
            else str(value).lower()
            if isinstance(value, bool)
            else str(value)
        )
        result.append({"name": f"{prefix}:{key.replace('_', '-')}", "value": encoded})
    return result


def _required_text(raw: Mapping[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SbomError(f"{label} thiếu trường {key}")
    return value.strip()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
