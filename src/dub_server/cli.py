"""Command-line client for the local FastAPI control plane."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat as stat_module
import subprocess
import sys
import time
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path
from typing import Any

import httpx
import typer

from . import __version__
from .config import Settings, read_secret
from .gpu import (
    GPU_SUPPORT_EXPERIMENTAL,
    GpuPreflightError,
    gpu_support_tier,
    inspect_gpu,
)

if sys.platform == "win32":
    # Windows shells may still expose a legacy code page.  Reconfiguring only
    # the CLI streams keeps Vietnamese help and errors printable.
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            _reconfigure(encoding="utf-8", errors="replace")


app = typer.Typer(
    name="dub",
    help="Điều khiển hệ thống thuyết minh video chạy cục bộ.",
    no_args_is_help=True,
)
models_app = typer.Typer(help="Xem danh mục model cục bộ.")
jobs_app = typer.Typer(help="Quản lý và theo dõi các job thuyết minh.")
stack_app = typer.Typer(help="Quản lý stack native trên máy chủ GPU.")
maintenance_app = typer.Typer(help="Dọn artifact và tạo SBOM phát hành.")
app.add_typer(models_app, name="models")
app.add_typer(jobs_app, name="jobs")
app.add_typer(stack_app, name="stack")
app.add_typer(maintenance_app, name="maintenance")


_ACTION_REQUIRED_STATUSES = frozenset(
    {"needs_language", "needs_subtitle_selection", "paused"}
)
_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", *_ACTION_REQUIRED_STATUSES}
)
_STAGE_LABELS = {
    "acquisition": "Tải nguồn",
    "subtitle": "Chọn phụ đề",
    "asr": "Nhận dạng lời nói",
    "translation": "Dịch sang tiếng Việt",
    "separation": "Tách lời diễn viên",
    "tts": "Tổng hợp lời thuyết minh",
    "timing": "Khớp thời lượng",
    "mix": "Trộn nhạc nền và lời mới",
    "export": "Xuất MP4",
    "verify": "Kiểm tra kết quả",
    "done": "Hoàn tất",
}
_MODEL_PROFILES: dict[str, tuple[str, ...]] = {
    "minimal": (
        "asr-faster-whisper-small",
        "mt-gemma4-e2b-q4",
        "separation-tiger-dnr",
        "tts-piper-vi-vais1000-medium",
    ),
    "balanced": (
        "asr-faster-whisper-small",
        "mt-gemma4-e2b-q4",
        "separation-tiger-dnr",
        "tts-neucodec-onnx-int8",
        "tts-vieneu-v2",
    ),
    "maximum": (
        "asr-faster-whisper-large-v3-turbo",
        "mt-gemma4-31b-q4",
        "separation-tiger-dnr",
        "tts-neucodec-onnx-int8",
        "tts-vieneu-v2",
    ),
}
_PROFILE_MINIMUM_VRAM_MIB = {
    "minimal": 6_144,
    "balanced": 8_192,
    "maximum": 22_528,
}


def _base_url(value: str | None) -> str:
    return (value or Settings().api_url).rstrip("/")


def _api_client(**kwargs: Any) -> httpx.Client:
    token = read_secret(Settings().api_token_file)
    if token is not None:
        kwargs["headers"] = {"Authorization": f"Bearer {token}"}
    return httpx.Client(**kwargs)


def _print_json(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _project_root() -> Path:
    configured = os.environ.get("DUB_PROJECT_ROOT") or os.environ.get("PROJECT_ROOT")
    root = Path(configured) if configured else Path(__file__).resolve().parents[2]
    return root.expanduser().resolve(strict=False)


def _human_bytes(value: int | float | None) -> str:
    if value is None:
        return "?"
    size = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{int(value)} B"


def _run_project_script(
    relative_path: str,
    arguments: list[str] | None = None,
) -> None:
    script = (_project_root() / relative_path).resolve(strict=False)
    if not script.is_file() or not script.is_relative_to(_project_root()):
        typer.echo(f"Không tìm thấy script quản trị: {script}", err=True)
        raise typer.Exit(code=2)
    if script.suffix == ".sh":
        if platform.system() != "Linux":
            typer.echo("Lệnh này chỉ chạy trên máy chủ Linux native", err=True)
            raise typer.Exit(code=2)
        command = ["bash", str(script), *(arguments or [])]
    elif script.suffix == ".ps1":
        if platform.system() != "Windows":
            typer.echo("Lệnh này chỉ chạy trên Windows native", err=True)
            raise typer.Exit(code=2)
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            typer.echo("Không tìm thấy PowerShell để chạy công cụ Windows", err=True)
            raise typer.Exit(code=2)
        command = [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *(arguments or []),
        ]
    else:
        command = [sys.executable, str(script), *(arguments or [])]
    try:
        result = subprocess.run(command, cwd=_project_root(), check=False)
    except OSError as exc:
        typer.echo(f"Không thể chạy công cụ quản trị: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)


def _run_native_admin_script(
    linux_path: str,
    windows_path: str,
    arguments: list[str] | None = None,
) -> None:
    system = platform.system()
    if system == "Linux":
        selected = linux_path
    elif system == "Windows":
        selected = windows_path
    else:
        typer.echo(f"Nền tảng native chưa được hỗ trợ: {system}", err=True)
        raise typer.Exit(code=2)
    _run_project_script(selected, arguments)


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"API trả về HTTP {response.status_code}"
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    if isinstance(detail, list):
        messages = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            location = ".".join(str(part) for part in item.get("loc", ()))
            message = str(item.get("msg", "Dữ liệu không hợp lệ"))
            messages.append(f"{location}: {message}" if location else message)
        if messages:
            return "; ".join(messages)
    if isinstance(detail, dict) and detail.get("message"):
        code = detail.get("code")
        message = str(detail["message"])
        return f"{message} [{code}]" if code else message
    return f"API trả về HTTP {response.status_code}"


def _request(
    method: str,
    path: str,
    *,
    api_url: str | None,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | list[tuple[str, Any]] | None = None,
    timeout_seconds: float = 30.0,
) -> Any:
    try:
        with _api_client(timeout=timeout_seconds) as client:
            response = client.request(
                method,
                f"{_base_url(api_url)}{path}",
                json=payload,
                params=params,
            )
    except httpx.HTTPError as exc:
        typer.echo(f"Không thể kết nối tới API cục bộ: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if response.is_error:
        typer.echo(_error_message(response), err=True)
        raise typer.Exit(code=1)
    try:
        return response.json()
    except ValueError as exc:
        typer.echo("API trả về dữ liệu không hợp lệ", err=True)
        raise typer.Exit(code=1) from exc


def _download_artifact(
    path: str,
    destination: Path,
    *,
    api_url: str | None,
    overwrite: bool,
    resume_partial: bool = True,
) -> None:
    resolved = destination.expanduser().resolve(strict=False)
    if resolved.exists() and not overwrite:
        typer.echo(
            f"File đã tồn tại: {resolved}. Dùng --overwrite để ghi đè.",
            err=True,
        )
        raise typer.Exit(code=1)
    temporary = resolved.with_name(f".{resolved.name}.part")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if not resume_partial:
            temporary.unlink(missing_ok=True)
        timeout = httpx.Timeout(None, connect=30.0)
        with _api_client(timeout=timeout) as client:
            def transfer(range_start: int) -> bool:
                headers = (
                    {"Range": f"bytes={range_start}-"} if range_start > 0 else None
                )
                with client.stream(
                    "GET",
                    f"{_base_url(api_url)}{path}",
                    headers=headers,
                ) as response:
                    if response.status_code == 416 and range_start > 0:
                        return False
                    if response.is_error:
                        typer.echo(_error_message(response), err=True)
                        raise typer.Exit(code=1)
                    append = range_start > 0 and response.status_code == 206
                    if range_start > 0 and not append:
                        temporary.unlink(missing_ok=True)
                        range_start = 0
                    total: int | None = None
                    content_range = response.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes \d+-\d+/(\d+)", content_range)
                    if match:
                        total = int(match.group(1))
                    elif response.headers.get("Content-Length", "").isdigit():
                        total = range_start + int(response.headers["Content-Length"])
                    mode = "ab" if append else "xb"
                    written = range_start
                    with temporary.open(mode) as stream:
                        for chunk in response.iter_bytes(1024 * 1024):
                            stream.write(chunk)
                            written += len(chunk)
                            if sys.stderr.isatty():
                                if total:
                                    fraction = min(1.0, written / total)
                                    width = 28
                                    filled = round(width * fraction)
                                    bar = "#" * filled + "-" * (width - filled)
                                    message = (
                                        f"\r[{bar}] {fraction * 100:5.1f}% "
                                        f"({_human_bytes(written)}/{_human_bytes(total)})"
                                    )
                                else:
                                    message = f"\rĐã tải {_human_bytes(written)}"
                                typer.echo(message, err=True, nl=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if sys.stderr.isatty():
                        typer.echo(err=True)
                    return True

            start = temporary.stat().st_size if temporary.is_file() else 0
            if not transfer(start):
                temporary.unlink(missing_ok=True)
                transfer(0)
        os.replace(temporary, resolved)
    except typer.Exit:
        if not resume_partial:
            temporary.unlink(missing_ok=True)
        raise
    except (OSError, httpx.HTTPError) as exc:
        if not resume_partial:
            temporary.unlink(missing_ok=True)
        typer.echo(f"Không thể tải file kết quả: {exc}", err=True)
        if resume_partial and temporary.is_file():
            typer.echo(f"Đã giữ phần tải dở để tiếp tục: {temporary}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(str(resolved))


def _upload_artifact(
    path: str,
    source: Path,
    *,
    api_url: str | None,
) -> dict[str, Any]:
    """Stream one local file as a raw request body without multipart buffering."""

    candidate = source.expanduser().absolute()
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        typer.echo(f"Không thể đọc file tải lên: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not candidate.is_file() or candidate.is_symlink() or metadata.st_size <= 0:
        typer.echo(f"File tải lên không hợp lệ: {candidate}", err=True)
        raise typer.Exit(code=1)

    sent = 0

    def chunks() -> Iterator[bytes]:
        nonlocal sent
        with resolved.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                sent += len(chunk)
                if sys.stderr.isatty():
                    fraction = min(1.0, sent / metadata.st_size)
                    width = 28
                    filled = round(width * fraction)
                    bar = "#" * filled + "-" * (width - filled)
                    typer.echo(
                        f"\r[{bar}] {fraction * 100:5.1f}% "
                        f"({_human_bytes(sent)}/{_human_bytes(metadata.st_size)})",
                        err=True,
                        nl=False,
                    )
                yield chunk

    try:
        timeout = httpx.Timeout(None, connect=30.0)
        with _api_client(timeout=timeout) as client:
            response = client.put(
                f"{_base_url(api_url)}{path}",
                content=chunks(),
                headers={
                    "Content-Length": str(metadata.st_size),
                    "Content-Type": "application/octet-stream",
                },
            )
    except (OSError, httpx.HTTPError) as exc:
        if sys.stderr.isatty() and sent:
            typer.echo(err=True)
        typer.echo(f"Không thể tải file lên API cục bộ: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if sys.stderr.isatty() and sent:
        typer.echo(err=True)
    if response.is_error:
        typer.echo(_error_message(response), err=True)
        raise typer.Exit(code=1)
    try:
        payload = response.json()
    except ValueError as exc:
        typer.echo("API trả về dữ liệu phiên tải file không hợp lệ", err=True)
        raise typer.Exit(code=1) from exc
    if not isinstance(payload, dict):
        typer.echo("API trả về dữ liệu phiên tải file không hợp lệ", err=True)
        raise typer.Exit(code=1)
    return payload


def _local_regular_size(path: Path) -> int:
    candidate = path.expanduser().absolute()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        typer.echo(f"Không thể đọc file tải lên: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not candidate.is_file() or candidate.is_symlink() or metadata.st_size <= 0:
        typer.echo(f"File tải lên không hợp lệ: {candidate}", err=True)
        raise typer.Exit(code=1)
    return metadata.st_size


def _parse_speaker_voices(values: list[str]) -> dict[str, dict[str, str | None]] | None:
    """Parse repeatable ``--speaker-voice NAME=value`` options.

    A value that looks like an audio path (audio extension or a slash)
    becomes ``reference_path``; anything else becomes ``voice_id`` (for
    example a Piper ``--speaker`` id). Returns ``None`` when no option
    was given, so the payload omits the field entirely.
    """

    result: dict[str, dict[str, str | None]] = {}
    for item in values:
        label, separator, value = item.partition("=")
        label = label.strip()
        value = value.strip()
        if not separator or not label or not value:
            raise typer.BadParameter(
                "--speaker-voice phải có dạng TEN=gia_tri, ví dụ Anna=3",
                param_hint="--speaker-voice",
            )
        lowered = value.lower()
        if lowered.endswith((".wav", ".mp3", ".flac", ".ogg")) or "/" in value or "\\" in value:
            result[label] = {"voice_id": None, "reference_path": value}
        else:
            result[label] = {"voice_id": value, "reference_path": None}
    return result or None


def _local_regular_sha256(path: Path) -> tuple[int, str]:
    """Hash a stable local regular file without following a final symlink."""

    candidate = path.expanduser().absolute()
    descriptor: int | None = None
    try:
        initial = candidate.lstat()
        if (
            not stat_module.S_ISREG(initial.st_mode)
            or candidate.is_symlink()
            or initial.st_size <= 0
        ):
            raise OSError("not a non-empty regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        opened = os.fstat(descriptor)
        initial_identity = (
            initial.st_dev,
            initial.st_ino,
            initial.st_size,
            initial.st_mtime_ns,
        )
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if not stat_module.S_ISREG(opened.st_mode) or opened_identity != initial_identity:
            raise OSError("file changed before hashing")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        final = os.fstat(descriptor)
        current = candidate.lstat()
        final_identity = (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
        )
        current_identity = (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        )
        if (
            candidate.is_symlink()
            or final_identity != initial_identity
            or current_identity != initial_identity
        ):
            raise OSError("file changed while hashing")
        return initial.st_size, digest.hexdigest()
    except OSError as exc:
        typer.echo(f"Không thể xác minh file tải lên: {candidate}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _iter_sse(response: httpx.Response) -> Iterator[dict[str, str]]:
    event: dict[str, str] = {}
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if event or data_lines:
                if data_lines:
                    event["data"] = "\n".join(data_lines)
                yield event
            event = {}
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
        elif field in {"id", "event", "retry"}:
            event[field] = value
    if event or data_lines:
        if data_lines:
            event["data"] = "\n".join(data_lines)
        yield event


def _progress_snapshot(job: dict[str, Any]) -> str:
    progress = max(0, min(1000, int(job.get("progress_permille", 0))))
    fraction = progress / 1000.0
    width = 30
    filled = round(width * fraction)
    bar = "█" * filled + "░" * (width - filled)
    stage = str(job.get("stage", ""))
    status = str(job.get("status", "unknown"))
    details = job.get("details") if isinstance(job.get("details"), dict) else {}
    extras: list[str] = []
    downloaded = details.get("downloaded_bytes")
    total = details.get("total_bytes")
    if isinstance(downloaded, int):
        extras.append(f"{_human_bytes(downloaded)}/{_human_bytes(total)}")
    speed = details.get("speed_bytes_per_second")
    if isinstance(speed, (int, float)) and speed > 0:
        extras.append(f"{_human_bytes(speed)}/s")
    eta = details.get("eta_seconds")
    if isinstance(eta, (int, float)) and eta >= 0:
        extras.append(f"ETA {int(eta)}s")
    completed_blocks = details.get("completed_blocks")
    total_blocks = details.get("total_blocks")
    if isinstance(completed_blocks, int) and isinstance(total_blocks, int):
        extras.append(f"block {completed_blocks}/{total_blocks}")
    suffix = f" | {' | '.join(extras)}" if extras else ""
    label = _STAGE_LABELS.get(stage, stage or "Khởi tạo")
    return f"[{bar}] {fraction * 100:5.1f}% | {label} | {status}{suffix}"


def _show_progress(job: dict[str, Any], *, final: bool = False) -> None:
    message = _progress_snapshot(job)
    if sys.stdout.isatty():
        typer.echo(f"\r\x1b[2K{message}", nl=final)
    else:
        typer.echo(message)


def _watch_job(
    job_id: str,
    *,
    api_url: str | None,
    after: int = 0,
) -> dict[str, Any]:
    job = _request("GET", f"/v1/jobs/{job_id}", api_url=api_url)
    _show_progress(job, final=str(job.get("status")) in _TERMINAL_STATUSES)
    if str(job.get("status")) in _TERMINAL_STATUSES:
        return job

    cursor = max(0, after)
    reconnects = 0
    try:
        while True:
            headers = {"Last-Event-ID": str(cursor)} if cursor else None
            timeout = httpx.Timeout(None, connect=30.0)
            try:
                with _api_client(timeout=timeout) as client:
                    with client.stream(
                        "GET",
                        f"{_base_url(api_url)}/v1/jobs/{job_id}/events",
                        params={"after": cursor},
                        headers=headers,
                    ) as response:
                        if response.is_error:
                            typer.echo(_error_message(response), err=True)
                            raise typer.Exit(code=1)
                        reconnects = 0
                        for event in _iter_sse(response):
                            raw_id = event.get("id", "")
                            if raw_id.isdigit():
                                cursor = max(cursor, int(raw_id))
                            raw_data = event.get("data")
                            if not raw_data:
                                continue
                            try:
                                envelope = json.loads(raw_data)
                            except json.JSONDecodeError:
                                continue
                            payload = envelope.get("payload", {})
                            if isinstance(payload, dict) and (
                                "status" in payload or "progress_permille" in payload
                            ):
                                job.update(payload)
                            else:
                                job = _request(
                                    "GET",
                                    f"/v1/jobs/{job_id}",
                                    api_url=api_url,
                                )
                            status_value = str(job.get("status", ""))
                            terminal = status_value in _TERMINAL_STATUSES
                            _show_progress(job, final=terminal)
                            if terminal:
                                return job
                job = _request("GET", f"/v1/jobs/{job_id}", api_url=api_url)
                if str(job.get("status")) in _TERMINAL_STATUSES:
                    _show_progress(job, final=True)
                    return job
            except httpx.HTTPError as exc:
                reconnects += 1
                if reconnects > 5:
                    typer.echo(f"Mất kết nối luồng tiến độ: {exc}", err=True)
                    raise typer.Exit(code=1) from exc
                time.sleep(min(5.0, float(reconnects)))
    except KeyboardInterrupt as exc:
        if sys.stdout.isatty():
            typer.echo()
        typer.echo("Đã dừng theo dõi; job vẫn tiếp tục chạy trên máy chủ", err=True)
        raise typer.Exit(code=130) from exc


@app.command()
def version() -> None:
    """Hiển thị phiên bản CLI và Python đang dùng."""

    try:
        package_version = metadata.version("vietdub")
    except metadata.PackageNotFoundError:
        package_version = f"{__version__}+source"
    _print_json(
        {
            "cli": package_version,
            "python": platform.python_version(),
            "platform": platform.platform(),
        }
    )


@app.command()
def doctor(
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Coi cảnh báo nền tảng native không được hỗ trợ là lỗi.",
    ),
) -> None:
    """Kiểm tra source, dung lượng, runtime GPU và API mà không sửa hệ thống."""

    checks: list[dict[str, Any]] = []

    def add(name: str, state: str, message: str) -> None:
        checks.append({"name": name, "status": state, "message": message})

    root = _project_root()
    required_files = (
        root / "pyproject.toml",
        root / "config/models.lock.json",
        root / "native/components.lock.json",
    )
    missing = [str(path) for path in required_files if not path.is_file()]
    add(
        "source",
        "error" if missing else "ok",
        f"Thiếu: {', '.join(missing)}" if missing else str(root),
    )
    supported_python = (3, 11) <= sys.version_info[:2] < (3, 13)
    add(
        "python",
        "ok" if supported_python else "error",
        platform.python_version(),
    )
    try:
        free_bytes = shutil.disk_usage(root).free
    except OSError as exc:
        add("disk", "error", str(exc))
    else:
        add(
            "disk",
            "ok" if free_bytes >= 25 * 1024**3 else "warning",
            f"Còn trống {_human_bytes(free_bytes)}",
        )

    native_system = platform.system()
    if native_system in {"Linux", "Windows"}:
        required_commands = ["ffmpeg", "ffprobe", "nvidia-smi"]
        if native_system == "Linux":
            required_commands.append("sqlite3")
        else:
            required_commands.extend(("nvcc", "powershell"))
        for command in required_commands:
            found = shutil.which(command)
            add(command, "ok" if found else "error", found or "Không tìm thấy")
        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi:
            gpu_settings = Settings()
            try:
                gpu_report = inspect_gpu(
                    require_gpu=True,
                    expected_gpu_uuid=gpu_settings.selected_gpu_uuid,
                    expected_cuda_architecture=(
                        gpu_settings.selected_cuda_architecture
                    ),
                    expected_cuda_toolkit_version=(
                        gpu_settings.selected_cuda_toolkit_version
                    ),
                )
            except GpuPreflightError as exc:
                report = exc.report
                detail = "; ".join((*report.errors, *report.warnings))
                add("gpu", "error", detail or "GPU preflight thất bại")
            else:
                selected = gpu_report.gpus[0]
                architecture = selected.compute_capability.replace(".", "")
                detail = (
                    f"{selected.name}; sm_{architecture}; "
                    f"{selected.memory_total_mib} MiB VRAM; "
                    f"PyTorch {gpu_report.torch.version}; "
                    f"CTranslate2 {gpu_report.ctranslate2.version}"
                )
                add(
                    "gpu",
                    "warning" if gpu_report.warnings else "ok",
                    detail
                    + (
                        f"; {'; '.join(gpu_report.warnings)}"
                        if gpu_report.warnings
                        else ""
                    ),
                )
    else:
        add(
            "native-runtime",
            "error" if strict else "warning",
            "Stack GPU native chỉ hỗ trợ Ubuntu Linux và Windows 10/11 x64",
        )

    try:
        with _api_client(timeout=5.0) as client:
            response = client.get(f"{_base_url(api_url)}/v1/health")
        if response.is_error:
            add("api", "error", _error_message(response))
        else:
            payload = response.json()
            api_state = str(payload.get("status", "unknown"))
            add(
                "api",
                "ok" if api_state == "ok" else "warning",
                f"{_base_url(api_url)} ({api_state})",
            )
    except (httpx.HTTPError, ValueError) as exc:
        add("api", "error", f"Không thể kiểm tra API: {exc}")

    overall = "error" if any(item["status"] == "error" for item in checks) else (
        "warning" if any(item["status"] == "warning" for item in checks) else "ok"
    )
    _print_json({"status": overall, "checks": checks})
    if overall == "error":
        raise typer.Exit(code=1)


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Địa chỉ bind; mặc định lấy từ DUB_API_HOST."),
    port: int | None = typer.Option(None, min=1, max=65535),
) -> None:
    """Khởi động API cục bộ."""

    import uvicorn

    from .api import create_app

    settings = Settings()
    effective_host = host or settings.api_host
    effective_port = port or settings.api_port
    settings = settings.model_copy(
        update={"api_host": effective_host, "api_port": effective_port}
    )
    uvicorn.run(
        create_app(settings=settings),
        host=effective_host,
        port=effective_port,
        reload=False,
    )


@app.command()
def health(
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Hiển thị trạng thái API, lưu trữ, model và GPU."""

    _print_json(_request("GET", "/v1/health", api_url=api_url))


@app.command()
def capabilities(
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Hiển thị các khả năng của pipeline cục bộ."""

    _print_json(_request("GET", "/v1/capabilities", api_url=api_url))


@models_app.command("list")
def list_models(
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Liệt kê model cục bộ mà không tải dữ liệu."""

    _print_json(_request("GET", "/v1/models", api_url=api_url))


@models_app.command("install")
def install_local_model(
    model_id: str = typer.Argument(..., help="ID model trong models.lock.json."),
    lock_path: Path | None = typer.Option(None, "--lock"),
    models_dir: Path | None = typer.Option(None, "--models-dir"),
) -> None:
    """Tải model đã khóa bằng trình quản lý mạng tường minh rồi xác minh SHA-256."""

    from .model_manager import install_model
    from .model_registry import ModelRegistryError

    settings = Settings()
    try:
        verified = install_model(
            lock_path or settings.models_lock_path,
            models_dir or settings.models_dir,
            model_id,
        )
    except (OSError, ValueError, ModelRegistryError) as exc:
        typer.echo(f"Không thể cài model {model_id}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _print_json(
        {
            "id": verified.model_id,
            "stage": verified.stage,
            "path": str(verified.path),
            "tree_sha256": verified.tree_sha256,
            "valid": True,
        }
    )


@models_app.command("verify")
def verify_local_model(
    model_id: str | None = typer.Argument(
        None,
        help="ID model trong models.lock.json; bỏ trống khi dùng --all.",
    ),
    all_models: bool = typer.Option(False, "--all", help="Kiểm tra mọi model đã cài."),
    lock_path: Path | None = typer.Option(None, "--lock"),
    models_dir: Path | None = typer.Option(None, "--models-dir"),
) -> None:
    """Băm lại toàn bộ file của một model đã cài mà không mở mạng."""

    from .model_manager import catalog_status, verify_model
    from .model_registry import ModelRegistryError

    settings = Settings()
    if (model_id is None) == (not all_models):
        raise typer.BadParameter("Hãy truyền MODEL_ID hoặc dùng --all, nhưng không dùng cả hai")
    try:
        if all_models:
            result = catalog_status(
                lock_path or settings.models_lock_path,
                models_dir or settings.models_dir,
            )
            _print_json(result)
            invalid = [
                item
                for item in result["models"]
                if item.get("installed") and not item.get("valid")
            ]
            if invalid:
                raise typer.Exit(code=1)
            return
        assert model_id is not None
        verified = verify_model(
            lock_path or settings.models_lock_path,
            models_dir or settings.models_dir,
            model_id,
        )
    except (OSError, ValueError, ModelRegistryError) as exc:
        typer.echo(f"Model {model_id} không hợp lệ: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _print_json(
        {
            "id": verified.model_id,
            "stage": verified.stage,
            "path": str(verified.path),
            "tree_sha256": verified.tree_sha256,
            "valid": True,
        }
    )


@models_app.command("show")
def show_local_model(
    model_id: str = typer.Argument(...),
    lock_path: Path | None = typer.Option(None, "--lock"),
    models_dir: Path | None = typer.Option(None, "--models-dir"),
) -> None:
    """Hiển thị manifest, dung lượng và trạng thái cài của một model."""

    from .config import load_model_catalog

    settings = Settings()
    try:
        catalog = load_model_catalog(
            lock_path or settings.models_lock_path,
            models_dir or settings.models_dir,
        )
        model = next(item for item in catalog.models if item.id == model_id)
    except StopIteration as exc:
        typer.echo(f"Không có model {model_id} trong catalog", err=True)
        raise typer.Exit(code=1) from exc
    except (OSError, ValueError) as exc:
        typer.echo(f"Không thể đọc catalog model: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    payload = model.model_dump(mode="json")
    files = payload.get("files", [])
    payload["size_bytes"] = sum(
        int(item.get("size", 0)) for item in files if isinstance(item, dict)
    )
    _print_json(payload)


@models_app.command("profiles")
def list_model_profiles(
    lock_path: Path | None = typer.Option(None, "--lock"),
) -> None:
    """Liệt kê các profile model cài sẵn và dung lượng tải dự kiến."""

    from .model_registry import read_model_catalog

    settings = Settings()
    try:
        catalog = read_model_catalog(lock_path or settings.models_lock_path)
    except (OSError, ValueError) as exc:
        typer.echo(f"Không thể đọc catalog model: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    entries = {str(item["id"]): item for item in catalog["models"]}
    profiles = []
    for name, model_ids in _MODEL_PROFILES.items():
        size = sum(
            sum(int(file["size"]) for file in entries[model_id].get("files", []))
            for model_id in model_ids
        )
        minimum_vram = max(
            _PROFILE_MINIMUM_VRAM_MIB[name],
            *(
                int(entries[model_id].get("minimum_vram_mib", 0) or 0)
                for model_id in model_ids
            ),
        )
        profiles.append(
            {
                "name": name,
                "models": list(model_ids),
                "download_bytes": size,
                "download_size": _human_bytes(size),
                "minimum_vram_mib": minimum_vram,
            }
        )
    _print_json({"profiles": profiles})


@models_app.command("recommend")
def recommend_model_profile(
    vram_mib: int | None = typer.Option(None, "--vram-mib", min=0),
) -> None:
    """Đề xuất profile model theo VRAM hiện có."""

    detected = vram_mib
    support_tier: str | None = None
    gpu_payload: dict[str, Any] | None = None
    detection_warning: str | None = None
    if detected is None:
        settings = Settings()
        try:
            report = inspect_gpu(
                require_gpu=True,
                expected_gpu_uuid=settings.selected_gpu_uuid,
                expected_cuda_architecture=settings.selected_cuda_architecture,
                expected_cuda_toolkit_version=(
                    settings.selected_cuda_toolkit_version
                ),
            )
            selected_gpu = report.gpus[0]
        except (GpuPreflightError, IndexError) as error:
            detection_warning = (
                "GPU logical 0 chưa vượt qua preflight; chọn cấu hình an toàn nhất"
            )
            if isinstance(error, GpuPreflightError) and error.report.errors:
                detection_warning = f"{detection_warning}: {error.report.errors[0]}"
        else:
            detected = selected_gpu.memory_total_mib
            support_tier = gpu_support_tier(selected_gpu)
            gpu_payload = {
                "uuid": selected_gpu.uuid,
                "name": selected_gpu.name,
                "compute_capability": selected_gpu.compute_capability,
            }
    if detected is None:
        profile = "minimal"
        reason = detection_warning or "Không đọc được VRAM; chọn cấu hình an toàn nhất"
    elif support_tier == GPU_SUPPORT_EXPERIMENTAL:
        profile = "minimal"
        reason = "GPU thuộc tier thử nghiệm; chỉ dùng profile minimal"
    elif detected >= 22_528:
        profile = "maximum"
        reason = "Đủ VRAM cho Gemma 4 31B Q4"
    elif detected >= 8_192:
        profile = "balanced"
        reason = "Đủ VRAM cho profile cân bằng"
    else:
        profile = "minimal"
        reason = "VRAM thấp; ưu tiên model nhỏ"
    _print_json(
        {
            "profile": profile,
            "vram_mib": detected,
            "gpu": gpu_payload,
            "support_tier": support_tier,
            "reason": reason,
            "models": list(_MODEL_PROFILES[profile]),
        }
    )


@models_app.command("install-profile")
def install_model_profile(
    profile: str = typer.Argument(..., help="minimal, balanced hoặc maximum"),
    yes: bool = typer.Option(False, "--yes", help="Xác nhận tải toàn bộ model."),
    lock_path: Path | None = typer.Option(None, "--lock"),
    models_dir: Path | None = typer.Option(None, "--models-dir"),
) -> None:
    """Cài tuần tự và xác minh trọn bộ model của một profile."""

    from .model_manager import install_model
    from .model_registry import ModelRegistryError

    if profile not in _MODEL_PROFILES:
        raise typer.BadParameter("Profile phải là minimal, balanced hoặc maximum")
    if not yes:
        raise typer.BadParameter(
            "Profile có thể tải hàng chục GiB; hãy dùng --yes để xác nhận",
            param_hint="--yes",
        )
    settings = Settings()
    selected_lock = lock_path or settings.models_lock_path
    selected_dir = models_dir or settings.models_dir
    if settings.compute_mode == "cpu":
        if profile != "minimal":
            raise typer.BadParameter("CPU mode chỉ hỗ trợ profile minimal")
    else:
        try:
            gpu_report = inspect_gpu(
                require_gpu=True,
                expected_gpu_uuid=settings.selected_gpu_uuid,
                expected_cuda_architecture=settings.selected_cuda_architecture,
                expected_cuda_toolkit_version=(
                    settings.selected_cuda_toolkit_version
                ),
            )
            selected_gpu = gpu_report.gpus[0]
        except (GpuPreflightError, IndexError) as exc:
            message = (
                "; ".join(exc.report.errors)
                if isinstance(exc, GpuPreflightError) and exc.report.errors
                else "không xác định được GPU logical 0"
            )
            typer.echo(
                f"Không thể xác minh GPU trước khi cài profile: {message}",
                err=True,
            )
            raise typer.Exit(code=1) from exc
        support_tier = gpu_support_tier(selected_gpu)
        required_vram_mib = _PROFILE_MINIMUM_VRAM_MIB[profile]
        if support_tier == GPU_SUPPORT_EXPERIMENTAL and profile != "minimal":
            typer.echo(
                "GPU thuộc tier thử nghiệm; chỉ được dùng profile minimal",
                err=True,
            )
            raise typer.Exit(code=1)
        if selected_gpu.memory_total_mib < required_vram_mib:
            typer.echo(
                f"Profile {profile} cần ít nhất {required_vram_mib} MiB VRAM; "
                f"GPU logical 0 chỉ có {selected_gpu.memory_total_mib} MiB",
                err=True,
            )
            raise typer.Exit(code=1)
    installed: list[dict[str, Any]] = []
    for model_id in _MODEL_PROFILES[profile]:
        typer.echo(f"Đang cài {model_id}...", err=True)
        try:
            verified = install_model(selected_lock, selected_dir, model_id)
        except (OSError, ValueError, ModelRegistryError) as exc:
            typer.echo(f"Không thể cài model {model_id}: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        installed.append(
            {
                "id": verified.model_id,
                "stage": verified.stage,
                "tree_sha256": verified.tree_sha256,
                "valid": True,
            }
        )
    _print_json({"profile": profile, "models": installed, "valid": True})


@app.command()
def search(
    query: str = typer.Argument(..., help="Tên nội dung cần tìm."),
    year: int | None = typer.Option(None, min=1888, max=2200),
    media_type: str = typer.Option("movie", "--type", help="movie hoặc series"),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Tìm trên các indexer do quản trị viên cấu hình."""

    if media_type != "movie":
        raise typer.BadParameter("Phase 1 chỉ hỗ trợ loại nội dung movie")
    _print_json(
        _request(
            "POST",
            "/v1/search",
            api_url=api_url,
            payload={"query": query, "year": year, "media_type": media_type},
        )
    )


@app.command("upload")
def upload_local_media(
    media: Path = typer.Argument(..., help="File video MP4 hoặc MKV cục bộ."),
    subtitle: Path | None = typer.Option(
        None,
        "--subtitle",
        help="File phụ đề SRT thủ công (không bắt buộc).",
    ),
    upload_id: str | None = typer.Option(
        None,
        "--upload-id",
        help="Thử lại một phiên upload dở dang thay vì tạo phiên mới.",
    ),
    rights_confirmed: bool = typer.Option(
        False,
        "--i-have-rights",
        help="Xác nhận bạn có quyền tải lên và xử lý nội dung.",
    ),
    source_language: str = typer.Option("auto", "--source-language"),
    timing_profile: str = typer.Option(
        "natural",
        "--timing-profile",
        help="natural ưu tiên nhịp nói; strict ưu tiên timestamp gốc.",
    ),
    asr_model: str | None = typer.Option(None, "--asr-model"),
    translation_model: str | None = typer.Option(None, "--translation-model"),
    separation_model: str | None = typer.Option(None, "--separation-model"),
    tts_model: str | None = typer.Option(None, "--tts-model"),
    voice_id: str | None = typer.Option(None, "--voice-id"),
    voice_reference: str | None = typer.Option(None, "--voice-reference"),
    voice_rights_confirmed: bool = typer.Option(
        False,
        "--i-have-voice-rights",
    ),
    speaker_voice: list[str] = typer.Option(
        [],
        "--speaker-voice",
        help="Lặp lại được, dạng TEN=gia_tri: Anna=3 (Piper speaker id) hoặc Bob=/path/bob.wav (file mẫu).",
    ),
    diarize_speakers: bool = typer.Option(
        False,
        "--diarize-speakers",
        help="Tự tách nhân vật bằng diarization trước khi dịch.",
    ),
    auto_speaker_voices: bool = typer.Option(
        False,
        "--auto-speaker-voices",
        help="Tự gán giọng Piper riêng cho từng nhân vật chưa có map tay.",
    ),
    no_qc: bool = typer.Option(
        False,
        "--no-qc",
        help="Bỏ qua bước AI nghe lại cuối Phase 4.",
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Theo dõi đến khi hoàn tất hoặc cần người dùng xử lý.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Tự tải MP4 khi dùng --wait và job hoàn tất.",
    ),
    overwrite: bool = typer.Option(False, "--overwrite"),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Tải trực tiếp MP4/MKV và SRT lên pipeline thuyết minh."""

    if not rights_confirmed:
        raise typer.BadParameter(
            "Phải dùng --i-have-rights để xác nhận quyền xử lý nội dung",
            param_hint="--i-have-rights",
        )
    media_extension = media.suffix.lower()
    if media_extension not in {".mp4", ".mkv"}:
        raise typer.BadParameter("File video phải có đuôi .mp4 hoặc .mkv")
    if subtitle is not None and subtitle.suffix.lower() != ".srt":
        raise typer.BadParameter("File phụ đề phải có đuôi .srt")
    normalized_language = source_language.strip().lower().replace("_", "-")
    if subtitle is not None and upload_id is None and normalized_language in {
        "",
        "auto",
        "und",
        "unknown",
        "mul",
        "zxx",
    }:
        raise typer.BadParameter(
            "Phải chọn --source-language cụ thể khi dùng --subtitle",
            param_hint="--source-language",
        )
    if timing_profile not in {"natural", "strict"}:
        raise typer.BadParameter("--timing-profile phải là natural hoặc strict")
    if output is not None and not wait:
        raise typer.BadParameter("--output chỉ dùng cùng --wait", param_hint="--output")
    voice = None
    if voice_id or voice_reference:
        if not voice_rights_confirmed:
            raise typer.BadParameter(
                "Phải dùng --i-have-voice-rights khi chọn giọng tham chiếu",
                param_hint="--i-have-voice-rights",
            )
        voice = {"voice_id": voice_id, "reference_path": voice_reference}
    speaker_voices = _parse_speaker_voices(speaker_voice)
    if speaker_voices and not voice_rights_confirmed:
        raise typer.BadParameter(
            "Phải dùng --i-have-voice-rights khi chọn giọng theo nhân vật",
            param_hint="--i-have-voice-rights",
        )

    media_size: int | None = None
    subtitle_size: int | None = None
    if upload_id is None:
        # Validate before creating a durable server session, otherwise a typo
        # or empty local file would leave an unnecessary orphan to clean up.
        media_size = _local_regular_size(media)
        if subtitle is not None:
            subtitle_size = _local_regular_size(subtitle)

    if upload_id is None:
        session = _request(
            "POST",
            "/v1/uploads",
            api_url=api_url,
            payload={
                "media_filename": media.name,
                "subtitle_filename": subtitle.name if subtitle is not None else None,
                "rights_confirmed": True,
                "source_language": normalized_language or "auto",
                "timing_profile": timing_profile,
                "models": {
                    "asr": asr_model,
                    "translation": translation_model,
                    "separation": separation_model,
                    "tts": tts_model,
                },
                "voice": voice,
                "voice_rights_confirmed": voice_rights_confirmed,
                "speaker_voices": speaker_voices,
                "diarize_speakers": diarize_speakers,
                "auto_speaker_voices": auto_speaker_voices,
                "qc_enabled": not no_qc,
            },
        )
        identifier = str(session["id"])
        typer.echo(f"Đã tạo phiên upload {identifier}", err=True)
    else:
        identifier = upload_id
        session = _request(
            "GET",
            f"/v1/uploads/{identifier}",
            api_url=api_url,
        )
        typer.echo(f"Đang thử lại phiên upload {identifier}", err=True)

    try:
        if str(session.get("status")) == "finalized":
            created = _request(
                "POST",
                f"/v1/uploads/{identifier}/finalize",
                api_url=api_url,
            )
        else:
            declared_media = session.get("media_filename")
            declared_subtitle = session.get("subtitle_filename")
            if upload_id is not None and declared_media != media.name:
                raise typer.BadParameter(
                    "Tên file video không khớp phiên upload đã tạo",
                    param_hint="media",
                )
            if upload_id is not None:
                if declared_subtitle is None and subtitle is not None:
                    raise typer.BadParameter(
                        "Phiên upload này không khai báo phụ đề SRT",
                        param_hint="--subtitle",
                    )
                if (
                    declared_subtitle is not None
                    and subtitle is not None
                    and declared_subtitle != subtitle.name
                ):
                    raise typer.BadParameter(
                        "Tên file SRT không khớp phiên upload đã tạo",
                        param_hint="--subtitle",
                    )
                if (
                    declared_subtitle is not None
                    and subtitle is None
                    and session.get("subtitle_size_bytes") is None
                ):
                    typer.echo(
                        "Phiên upload đang chờ SRT; hãy chạy lại với --subtitle và "
                        f"--upload-id {identifier}",
                        err=True,
                    )
                    raise typer.Exit(code=2)

            if media_size is None:
                media_size = _local_regular_size(media)
            media_matches = False
            if session.get("media_size_bytes") == media_size:
                typer.echo("Đang xác minh SHA-256 của video để tiếp tục...", err=True)
                _, local_media_digest = _local_regular_sha256(media)
                media_matches = session.get("media_sha256") == local_media_digest
            if not media_matches:
                session = _upload_artifact(
                    f"/v1/uploads/{identifier}/media",
                    media,
                    api_url=api_url,
                )
            else:
                typer.echo("Video đã có trên máy chủ, bỏ qua tải lại.", err=True)

            if subtitle is not None:
                if subtitle_size is None:
                    subtitle_size = _local_regular_size(subtitle)
                subtitle_matches = False
                if session.get("subtitle_size_bytes") == subtitle_size:
                    typer.echo("Đang xác minh SHA-256 của SRT để tiếp tục...", err=True)
                    _, local_subtitle_digest = _local_regular_sha256(subtitle)
                    subtitle_matches = (
                        session.get("subtitle_sha256") == local_subtitle_digest
                    )
                if not subtitle_matches:
                    session = _upload_artifact(
                        f"/v1/uploads/{identifier}/subtitle",
                        subtitle,
                        api_url=api_url,
                    )
                else:
                    typer.echo("SRT đã có trên máy chủ, bỏ qua tải lại.", err=True)
            created = _request(
                "POST",
                f"/v1/uploads/{identifier}/finalize",
                api_url=api_url,
            )
    except typer.Exit:
        typer.echo(
            "Phiên upload được giữ lại. Thử lại bằng "
            f"--upload-id {identifier}.",
            err=True,
        )
        raise

    _print_json(created)
    if not wait:
        return
    final = _watch_job(str(created["id"]), api_url=api_url)
    final_status = str(final.get("status"))
    if final_status == "completed":
        if output is not None:
            _download_artifact(
                f"/v1/jobs/{created['id']}/artifacts/video",
                output,
                api_url=api_url,
                overwrite=overwrite,
            )
        return
    if final_status in _ACTION_REQUIRED_STATUSES:
        typer.echo(
            "Job đang chờ thao tác. Dùng dub status hoặc language-select.",
            err=True,
        )
        raise typer.Exit(code=2)
    error = final.get("error")
    if isinstance(error, dict) and error.get("message"):
        typer.echo(str(error["message"]), err=True)
    raise typer.Exit(code=1)


@jobs_app.command("create")
@app.command("submit")
@app.command("run")
def run_job(
    release_id: str = typer.Option(..., "--release-id"),
    rights_confirmed: bool = typer.Option(
        False,
        "--i-have-rights",
        help="Xác nhận bạn có quyền tải và xử lý nội dung.",
    ),
    source_language: str = typer.Option("auto", "--source-language"),
    subtitle_mode: str = typer.Option("prefer", "--subtitle-mode"),
    timing_profile: str = typer.Option(
        "natural",
        "--timing-profile",
        help="natural ưu tiên nhịp nói; strict ưu tiên timestamp gốc.",
    ),
    asr_model: str | None = typer.Option(None, "--asr-model"),
    translation_model: str | None = typer.Option(None, "--translation-model"),
    separation_model: str | None = typer.Option(None, "--separation-model"),
    tts_model: str | None = typer.Option(None, "--tts-model"),
    voice_id: str | None = typer.Option(None, "--voice-id"),
    voice_reference: str | None = typer.Option(None, "--voice-reference"),
    voice_rights_confirmed: bool = typer.Option(
        False,
        "--i-have-voice-rights",
    ),
    speaker_voice: list[str] = typer.Option(
        [],
        "--speaker-voice",
        help="Lặp lại được, dạng TEN=gia_tri: Anna=3 (Piper speaker id) hoặc Bob=/path/bob.wav (file mẫu).",
    ),
    diarize_speakers: bool = typer.Option(
        False,
        "--diarize-speakers",
        help="Tự tách nhân vật bằng diarization trước khi dịch.",
    ),
    auto_speaker_voices: bool = typer.Option(
        False,
        "--auto-speaker-voices",
        help="Tự gán giọng Piper riêng cho từng nhân vật chưa có map tay.",
    ),
    no_qc: bool = typer.Option(
        False,
        "--no-qc",
        help="Bỏ qua bước AI nghe lại cuối Phase 4.",
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Theo dõi đến khi hoàn tất hoặc cần người dùng xử lý.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Tự tải MP4 khi dùng --wait và job hoàn tất.",
    ),
    overwrite: bool = typer.Option(False, "--overwrite"),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Tạo job tải và thuyết minh có xác nhận quyền."""

    if not rights_confirmed:
        raise typer.BadParameter(
            "Phải dùng --i-have-rights để xác nhận quyền tải và xử lý nội dung",
            param_hint="--i-have-rights",
        )
    if subtitle_mode not in {"prefer", "manual", "asr"}:
        raise typer.BadParameter("Chế độ phụ đề phải là prefer, manual hoặc asr")
    if timing_profile not in {"natural", "strict"}:
        raise typer.BadParameter("--timing-profile phải là natural hoặc strict")
    voice = None
    if voice_id or voice_reference:
        if not voice_rights_confirmed:
            raise typer.BadParameter(
                "Phải dùng --i-have-voice-rights khi chọn giọng tham chiếu",
                param_hint="--i-have-voice-rights",
            )
        voice = {"voice_id": voice_id, "reference_path": voice_reference}
    speaker_voices = _parse_speaker_voices(speaker_voice)
    if speaker_voices and not voice_rights_confirmed:
        raise typer.BadParameter(
            "Phải dùng --i-have-voice-rights khi chọn giọng theo nhân vật",
            param_hint="--i-have-voice-rights",
        )
    payload = {
        "release_id": release_id,
        "rights_confirmed": True,
        "source_language": source_language,
        "subtitle_mode": subtitle_mode,
        "timing_profile": timing_profile,
        "models": {
            "asr": asr_model,
            "translation": translation_model,
            "separation": separation_model,
            "tts": tts_model,
        },
        "voice": voice,
        "voice_rights_confirmed": voice_rights_confirmed,
        "speaker_voices": speaker_voices,
        "diarize_speakers": diarize_speakers,
        "auto_speaker_voices": auto_speaker_voices,
        "qc_enabled": not no_qc,
    }
    if output is not None and not wait:
        raise typer.BadParameter("--output chỉ dùng cùng --wait", param_hint="--output")
    created = _request("POST", "/v1/jobs", api_url=api_url, payload=payload)
    _print_json(created)
    if not wait:
        return
    final = _watch_job(str(created["id"]), api_url=api_url)
    final_status = str(final.get("status"))
    if final_status == "completed":
        if output is not None:
            _download_artifact(
                f"/v1/jobs/{created['id']}/artifacts/video",
                output,
                api_url=api_url,
                overwrite=overwrite,
            )
        return
    if final_status in _ACTION_REQUIRED_STATUSES:
        typer.echo(
            "Job đang chờ thao tác. Dùng dub status, subtitle-select hoặc language-select.",
            err=True,
        )
        raise typer.Exit(code=2)
    error = final.get("error")
    if isinstance(error, dict) and error.get("message"):
        typer.echo(str(error["message"]), err=True)
    raise typer.Exit(code=1)


@jobs_app.command("list")
def list_jobs_command(
    statuses: list[str] | None = typer.Option(
        None,
        "--status",
        help="Có thể lặp lại để lọc nhiều trạng thái.",
    ),
    limit: int = typer.Option(50, min=1, max=1000),
    oldest_first: bool = typer.Option(False, "--oldest-first"),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Liệt kê lịch sử job, mặc định mới nhất trước."""

    params: list[tuple[str, Any]] = [
        ("limit", limit),
        ("newest_first", not oldest_first),
    ]
    params.extend(("status", value) for value in statuses or [])
    _print_json(_request("GET", "/v1/jobs", api_url=api_url, params=params))


@jobs_app.command("watch")
@app.command("watch")
def watch_job_command(
    job_id: str = typer.Argument(...),
    after: int = typer.Option(0, min=0, help="ID event cuối đã nhận."),
    fetch_dir: Path | None = typer.Option(
        None,
        "--fetch-dir",
        help="Tự tải đủ MP4, SRT và timing report khi hoàn tất.",
    ),
    overwrite: bool = typer.Option(False, "--overwrite"),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Theo dõi SSE và hiển thị thanh tiến trình chi tiết theo stage."""

    final = _watch_job(job_id, api_url=api_url, after=after)
    final_status = str(final.get("status"))
    if final_status == "completed" and fetch_dir is not None:
        target_dir = fetch_dir.expanduser().resolve(strict=False)
        for kind, suffix in {
            "video": ".mp4",
            "subtitle": ".vi.srt",
            "timing": ".timing-report.json",
        }.items():
            _download_artifact(
                f"/v1/jobs/{job_id}/artifacts/{kind}",
                target_dir / f"{job_id}{suffix}",
                api_url=api_url,
                overwrite=overwrite,
            )
    elif final_status in _ACTION_REQUIRED_STATUSES:
        raise typer.Exit(code=2)
    elif final_status != "completed":
        raise typer.Exit(code=1)


@jobs_app.command("events")
@app.command("events")
def job_events_command(
    job_id: str = typer.Argument(...),
    after: int = typer.Option(0, min=0),
    once: bool = typer.Option(False, "--once", help="Đọc event hiện có rồi thoát."),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Xuất event SSE dạng JSON Lines cho script và automation."""

    try:
        timeout = httpx.Timeout(None, connect=30.0)
        with _api_client(timeout=timeout) as client:
            with client.stream(
                "GET",
                f"{_base_url(api_url)}/v1/jobs/{job_id}/events",
                params={"after": after, "once": once},
            ) as response:
                if response.is_error:
                    typer.echo(_error_message(response), err=True)
                    raise typer.Exit(code=1)
                for event in _iter_sse(response):
                    if not event.get("data"):
                        continue
                    try:
                        payload = json.loads(event["data"])
                    except json.JSONDecodeError:
                        payload = {"data": event["data"]}
                    payload["event_id"] = event.get("id")
                    payload["event"] = event.get("event")
                    typer.echo(json.dumps(payload, ensure_ascii=False))
    except httpx.HTTPError as exc:
        typer.echo(f"Không thể đọc luồng event: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@jobs_app.command("show")
@app.command()
def status(
    job_id: str = typer.Argument(...),
    refresh: bool = typer.Option(
        True,
        "--refresh/--no-refresh",
        help="Cập nhật tiến độ tải trước khi đọc trạng thái.",
    ),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Hiển thị trạng thái và checkpoint của một job."""

    if refresh:
        _request("POST", f"/v1/jobs/{job_id}/refresh", api_url=api_url)
    _print_json(_request("GET", f"/v1/jobs/{job_id}", api_url=api_url))


@jobs_app.command("subtitle-select")
@app.command("subtitle-select")
def subtitle_select(
    job_id: str = typer.Argument(...),
    subtitle_id: str = typer.Argument(...),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Chọn một phụ đề đã được đề xuất cho job thủ công."""

    _print_json(
        _request(
            "POST",
            f"/v1/jobs/{job_id}/subtitles/{subtitle_id}",
            api_url=api_url,
        )
    )


@jobs_app.command("subtitle-use-asr")
@app.command("subtitle-use-asr")
def subtitle_use_asr(
    job_id: str = typer.Argument(...),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Bỏ qua các phụ đề đề xuất và dùng ASR cục bộ."""

    _print_json(
        _request(
            "POST",
            f"/v1/jobs/{job_id}/subtitles/use-asr",
            api_url=api_url,
        )
    )


@jobs_app.command("language-select")
@app.command("language-select")
def language_select(
    job_id: str = typer.Argument(...),
    language: str = typer.Argument(..., help="Mã ngôn ngữ nguồn, ví dụ en, ja hoặc th."),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Chọn lại ngôn ngữ khi nhận dạng tự động chưa đủ tin cậy."""

    _print_json(
        _request(
            "POST",
            f"/v1/jobs/{job_id}/language",
            api_url=api_url,
            payload={"language": language},
        )
    )


@jobs_app.command("cancel")
@app.command()
def cancel(
    job_id: str = typer.Argument(...),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Hủy job và giữ artifact nguồn đã tải lên."""

    _print_json(
        _request("POST", f"/v1/jobs/{job_id}/cancel", api_url=api_url)
    )


@jobs_app.command("resume")
@app.command()
def resume(
    job_id: str = typer.Argument(...),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Tiếp tục job tạm dừng hoặc lỗi có thể thử lại."""

    _print_json(
        _request("POST", f"/v1/jobs/{job_id}/resume", api_url=api_url)
    )


@jobs_app.command("fetch")
@app.command("fetch")
def fetch_artifact(
    job_id: str = typer.Argument(...),
    kind: str = typer.Option(
        "video",
        "--kind",
        help="video, subtitle, timing hoặc all",
    ),
    output: Path | None = typer.Option(None, "--output", "-o"),
    overwrite: bool = typer.Option(False, "--overwrite"),
    resume_partial: bool = typer.Option(
        True,
        "--resume-partial/--no-resume-partial",
        help="Tiếp tục file .part bằng HTTP Range khi có thể.",
    ),
    api_url: str | None = typer.Option(None, envvar="DUB_API_URL"),
) -> None:
    """Tải MP4, SRT tiếng Việt hoặc timing report của job đã hoàn tất."""

    suffixes = {
        "video": ".mp4",
        "subtitle": ".vi.srt",
        "timing": ".timing-report.json",
    }
    if kind not in {*suffixes, "all"}:
        raise typer.BadParameter("Loại file phải là video, subtitle, timing hoặc all")
    if kind == "all":
        destination_dir = (output or Path.cwd()).expanduser().resolve(strict=False)
        if destination_dir.exists() and not destination_dir.is_dir():
            raise typer.BadParameter("Với --kind all, --output phải là thư mục")
        for artifact_kind, suffix in suffixes.items():
            _download_artifact(
                f"/v1/jobs/{job_id}/artifacts/{artifact_kind}",
                destination_dir / f"{job_id}{suffix}",
                api_url=api_url,
                overwrite=overwrite,
                resume_partial=resume_partial,
            )
        return
    destination = output or Path(f"{job_id}{suffixes[kind]}")
    _download_artifact(
        f"/v1/jobs/{job_id}/artifacts/{kind}",
        destination,
        api_url=api_url,
        overwrite=overwrite,
        resume_partial=resume_partial,
    )


@stack_app.command("start")
def stack_start() -> None:
    """Khởi động stack native (Windows: API và worker local-upload)."""

    _run_native_admin_script(
        "scripts/native-stack.sh", "windows/stack.ps1", ["start"]
    )


@stack_app.command("stop")
def stack_stop() -> None:
    """Dừng stack native và các tiến trình con được quản lý."""

    _run_native_admin_script(
        "scripts/native-stack.sh", "windows/stack.ps1", ["stop"]
    )


@stack_app.command("restart")
def stack_restart() -> None:
    """Khởi động lại sạch toàn bộ stack native."""

    _run_native_admin_script(
        "scripts/native-stack.sh", "windows/stack.ps1", ["restart"]
    )


@stack_app.command("status")
def stack_status() -> None:
    """Hiển thị trạng thái các tiến trình native."""

    _run_native_admin_script(
        "scripts/native-stack.sh", "windows/stack.ps1", ["status"]
    )


@stack_app.command("logs")
def stack_logs(
    lines: int = typer.Option(100, "--lines", "-n", min=1, max=100_000),
) -> None:
    """Hiển thị phần cuối log của toàn bộ dịch vụ."""

    if platform.system() == "Windows":
        _run_project_script("windows/stack.ps1", ["logs", "-Lines", str(lines)])
    else:
        _run_native_admin_script(
            "scripts/native-stack.sh",
            "windows/stack.ps1",
            ["logs", str(lines)],
        )


@stack_app.command("foreground")
def stack_foreground() -> None:
    """Chạy stack foreground cho terminal hoặc startup command."""

    _run_native_admin_script(
        "scripts/native-stack.sh", "windows/stack.ps1", ["foreground"]
    )


@stack_app.command("preflight")
def stack_preflight() -> None:
    """Chạy kiểm tra native đầy đủ bằng user dịch vụ."""

    if platform.system() == "Windows":
        _run_project_script("windows/preflight.ps1", ["-RequireRuntime"])
    else:
        _run_native_admin_script(
            "scripts/native-preflight.sh", "windows/preflight.ps1"
        )


@stack_app.command("acceptance")
def stack_acceptance(
    phase: str = typer.Option(
        "basic",
        "--phase",
        help="basic, phase2, phase3, phase4 hoặc all",
    ),
) -> None:
    """Chạy cổng nghiệm thu đã khóa cho runtime GPU."""

    if platform.system() == "Windows":
        if phase != "basic":
            typer.echo(
                "Windows MVP hiện chỉ có acceptance basic qua preflight",
                err=True,
            )
            raise typer.Exit(code=2)
        _run_project_script("windows/preflight.ps1", ["-RequireRuntime"])
        return

    scripts = {
        "basic": ["scripts/native-acceptance.sh"],
        "phase2": ["scripts/native-phase2-acceptance.sh"],
        "phase3": ["scripts/native-phase3-acceptance.sh"],
        "phase4": ["scripts/native-phase4-acceptance.sh"],
        "all": [
            "scripts/native-acceptance.sh",
            "scripts/native-phase2-acceptance.sh",
            "scripts/native-phase3-acceptance.sh",
            "scripts/native-phase4-acceptance.sh",
        ],
    }
    if phase not in scripts:
        raise typer.BadParameter("Phase phải là basic, phase2, phase3, phase4 hoặc all")
    for script in scripts[phase]:
        _run_project_script(script)


@maintenance_app.command("cleanup")
def maintenance_cleanup(
    apply: bool = typer.Option(False, "--apply", help="Thực sự xóa artifact trong plan."),
    yes: bool = typer.Option(False, "--yes", help="Xác nhận thao tác xóa."),
    retry_retention_days: int = typer.Option(7, min=0, max=3650),
) -> None:
    """Lập kế hoạch dọn job hủy/lỗi; mặc định không xóa gì."""

    if apply and not yes:
        raise typer.BadParameter(
            "Dùng --yes cùng --apply để xác nhận xóa artifact",
            param_hint="--yes",
        )
    arguments = ["--retry-retention-days", str(retry_retention_days)]
    if apply:
        arguments.append("--apply")
    _run_project_script("scripts/cleanup-job-artifacts.py", arguments)


@maintenance_app.command("sbom")
def maintenance_sbom(
    output: Path = typer.Option(
        Path("var/reports/sbom.cdx.json"),
        "--output",
        "-o",
    ),
) -> None:
    """Tạo CycloneDX SBOM từ các lockfile và môi trường đã cài."""

    arguments = ["--output", str(output.expanduser().resolve(strict=False))]
    native_receipt = Path("/usr/local/lib/llama.cpp/build-receipt.json")
    if native_receipt.is_file():
        arguments.extend(("--native-receipt", str(native_receipt)))
    _run_project_script(
        "scripts/generate-sbom.py",
        arguments,
    )


if __name__ == "__main__":
    app()
