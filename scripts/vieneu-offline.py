#!/usr/bin/env python3
"""Pinned, local-only VieNeu v2 inference entrypoint for worker subprocesses."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout, suppress
from pathlib import Path
from typing import Any, TextIO

_OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    "TOKENIZERS_PARALLELISM": "false",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True, type=Path)
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--output-file", type=Path)
    parser.add_argument("--speed", type=float)
    parser.add_argument("--reference-audio", type=Path)
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--local-files-only", action="store_true")
    return parser


def _local_path(path: Path, *, directory: bool) -> Path:
    raw = os.fspath(path)
    if "://" in raw or "\x00" in raw:
        raise ValueError("Only local filesystem paths are accepted")
    resolved = path.resolve(strict=True)
    if directory and not resolved.is_dir():
        raise ValueError(f"Expected a local directory: {resolved}")
    if not directory and not resolved.is_file():
        raise ValueError(f"Expected a local file: {resolved}")
    return resolved


def _install_local_codec_loader(codec_directory: Path) -> None:
    # Some dependency versions print import diagnostics to stdout.  In server
    # mode stdout is reserved exclusively for the JSON-lines protocol.
    with redirect_stdout(sys.stderr):
        from vieneu import utils as vieneu_utils

    model_path = _local_path(codec_directory / "model.onnx", directory=False)

    def from_local_directory(
        cls: type,
        repo_id: str,
        filename: str = "model.onnx",
        hf_token: str | None = None,
    ) -> object:
        del hf_token
        requested = _local_path(Path(repo_id), directory=True)
        if requested != codec_directory or filename != "model.onnx":
            raise ValueError("VieNeu codec must match the verified local ONNX model")
        return cls(os.fspath(model_path))

    vieneu_utils.NeuCodecOnnx.from_pretrained = classmethod(from_local_directory)


def _render_pcm16(
    raw_path: Path,
    output_path: Path,
    *,
    speed: float,
) -> None:
    ffmpeg = shutil.which(os.environ.get("DUB_FFMPEG_BINARY", "ffmpeg"))
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to normalize VieNeu output")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        (
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            os.fspath(raw_path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-af",
            f"atempo={speed:.8f}",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            os.fspath(output_path),
        ),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _load_engine(model_directory: Path, codec_directory: Path) -> Any:
    # Import/model logs must never share stdout with the JSON-lines protocol.
    with redirect_stdout(sys.stderr):
        from vieneu import Vieneu

        return Vieneu(
            mode="standard",
            backbone_repo=os.fspath(model_directory),
            backbone_device="cuda",
            codec_repo=os.fspath(codec_directory),
            codec_device="cpu",
            gguf_filename=None,
        )


def _validated_output_path(path: Path) -> Path:
    raw = os.fspath(path)
    if "://" in raw or "\x00" in raw:
        raise ValueError("Output must be a local filesystem path")
    output = path.resolve(strict=False)
    if output.suffix.casefold() != ".wav":
        raise ValueError("Output must be a WAV file")
    return output


def _synthesize(
    engine: Any,
    text: str,
    output_path: Path,
    *,
    speed: float,
) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("VieNeu input text is empty")
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise ValueError("VieNeu speed is invalid")
    if not 0.5 <= float(speed) <= 2.0:
        raise ValueError("VieNeu speed must be between 0.5 and 2.0")
    output = _validated_output_path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw_path = output.with_name(f".{output.stem}.{os.getpid()}.raw.wav")
    rendered_path = output.with_name(
        f".{output.stem}.{os.getpid()}.render.part.wav"
    )
    try:
        output.unlink(missing_ok=True)
        rendered_path.unlink(missing_ok=True)
        with redirect_stdout(sys.stderr):
            audio = engine.infer(text=text.strip(), apply_watermark=False)
            engine.save(audio, raw_path)
        _render_pcm16(raw_path, rendered_path, speed=float(speed))
        os.replace(rendered_path, output)
    finally:
        with suppress(OSError):
            raw_path.unlink(missing_ok=True)
        with suppress(OSError):
            rendered_path.unlink(missing_ok=True)


def _write_protocol(stream: TextIO, message: dict[str, object]) -> None:
    stream.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
    stream.write("\n")
    stream.flush()


def _serve(engine: Any, protocol_output: TextIO) -> int:
    protocol = 1
    _write_protocol(protocol_output, {"type": "ready", "protocol": protocol})
    for raw_line in sys.stdin.buffer:
        if len(raw_line) > 64 * 1024:
            _write_protocol(
                protocol_output,
                {"type": "error", "protocol": protocol, "code": "line_too_long"},
            )
            continue
        try:
            request = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _write_protocol(
                protocol_output,
                {"type": "error", "protocol": protocol, "code": "invalid_json"},
            )
            continue
        if not isinstance(request, dict) or request.get("protocol") != protocol:
            _write_protocol(
                protocol_output,
                {"type": "error", "protocol": protocol, "code": "invalid_protocol"},
            )
            continue
        request_type = request.get("type")
        if request_type == "close":
            _write_protocol(protocol_output, {"type": "closed", "protocol": protocol})
            return 0
        request_id = request.get("id")
        if (
            request_type != "synthesize"
            or isinstance(request_id, bool)
            or not isinstance(request_id, int)
            or request_id < 1
            or not isinstance(request.get("text"), str)
            or not isinstance(request.get("output_file"), str)
        ):
            _write_protocol(
                protocol_output,
                {"type": "error", "protocol": protocol, "code": "invalid_request"},
            )
            continue
        try:
            output = _validated_output_path(Path(request["output_file"]))
            _synthesize(
                engine,
                request["text"],
                output,
                speed=request.get("speed", 1.0),
            )
        except Exception as exc:
            with suppress(OSError):
                Path(request["output_file"]).unlink(missing_ok=True)
            _write_protocol(
                protocol_output,
                {
                    "type": "result",
                    "protocol": protocol,
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "code": "tts_failed",
                        "kind": type(exc).__name__,
                    },
                },
            )
            continue
        _write_protocol(
            protocol_output,
            {
                "type": "result",
                "protocol": protocol,
                "id": request_id,
                "ok": True,
            },
        )
    return 0


def _close_engine(engine: Any) -> None:
    with suppress(Exception), redirect_stdout(sys.stderr):
        engine.close()
    with suppress(Exception), redirect_stdout(sys.stderr):
        import torch

        torch.cuda.empty_cache()


def main(arguments: list[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    if not parsed.local_files_only:
        raise ValueError("VieNeu runtime requires --local-files-only")
    if parsed.reference_audio is not None:
        raise ValueError(
            "Voice cloning requires an authorized reference transcript and is not enabled"
        )

    for key, value in _OFFLINE_ENVIRONMENT.items():
        os.environ[key] = value
    model_directory = _local_path(parsed.model_path, directory=True)
    codec_value = os.environ.get("VIENEU_CODEC_PATH", "").strip()
    if not codec_value:
        raise ValueError("VIENEU_CODEC_PATH must name the verified local codec")
    codec_directory = _local_path(Path(codec_value), directory=True)

    # This is installed before importing Hugging Face/Transformers-backed code.
    from dub_server.offline import install_offline_network_guard

    install_offline_network_guard()
    _install_local_codec_loader(codec_directory)

    engine = _load_engine(model_directory, codec_directory)
    try:
        if parsed.server:
            if parsed.text_file is not None or parsed.output_file is not None:
                raise ValueError("Server mode accepts requests through stdin only")
            if parsed.speed is not None:
                raise ValueError("Server mode accepts speed per request")
            return _serve(engine, sys.stdout)
        if parsed.text_file is None or parsed.output_file is None or parsed.speed is None:
            raise ValueError("One-shot mode requires text, output, and speed")
        text_path = _local_path(parsed.text_file, directory=False)
        text = text_path.read_text(encoding="utf-8").strip()
        _synthesize(
            engine,
            text,
            _validated_output_path(parsed.output_file),
            speed=parsed.speed,
        )
        return 0
    finally:
        _close_engine(engine)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
