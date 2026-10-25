#!/usr/bin/env python3
"""Chạy bước dịch VietDub bằng OpenAI API ở tiến trình riêng, ngoài worker offline.

Worker chính của repo cài offline guard chặn mọi socket ngoài loopback tới
llama-server, nên backend OpenAI không thể chạy bên trong worker đó. Script
này chạy ở tiến trình riêng, dùng chung StateStore với worker, chỉ xử lý các
job dịch đang chờ (queue "mt"), 1 request / block, checkpoint từng block nên
fail giữa chừng không mất cả tập.

Cách dùng (trên máy có GPU, ngoài worker offline):
    PYTHONPATH=src python3 scripts/translate-openai.py
    PYTHONPATH=src python3 scripts/translate-openai.py --model gpt-4o --once

Key: file /run/secrets/openai_api_key (khuyến nghị) hoặc biến môi trường
OPENAI_API_KEY khi dev. Không bao giờ in key ra log.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import threading
import time
from pathlib import Path

# Cho phép chạy từ thư mục repo mà không cần cài package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dub_server.config import Settings  # noqa: E402
from dub_server.openai_translation import OpenAITranslator, read_openai_api_key  # noqa: E402
from dub_server.state import StateStore  # noqa: E402
from dub_server.translation_stage import TranslationStage  # noqa: E402
from dub_server.worker import process_next_translation_job  # noqa: E402


def _model_id_for(model: str) -> str:
    return f"mt-openai-{model.replace('/', '-').replace(' ', '-').lower()}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Chạy job dịch VietDub bằng OpenAI API (tiến trình riêng, ngoài worker offline)."
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="Model OpenAI cho bước dịch (mặc định: gpt-4o-mini). gpt-4o đắt hơn ~10-20 lần.",
    )
    parser.add_argument(
        "--api-key-file",
        type=Path,
        default=Path("/run/secrets/openai_api_key"),
        help="File chứa API key OpenAI (mặc định: /run/secrets/openai_api_key, fallback OPENAI_API_KEY).",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=512,
        help="Số token output tối đa cho mỗi block (mặc định: 512).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Chỉ xử lý tối đa 1 job rồi thoát (mặc định: lặp tới khi hết job).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=10.0,
        help="Giây chờ giữa 2 lần quét khi không còn job (mặc định: 10).",
    )
    args = parser.parse_args()

    shutdown = threading.Event()

    def _on_signal(_signum: int, _frame: object) -> None:
        shutdown.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    settings = Settings()  # đọc từ biến môi trường DUB_*
    api_key = read_openai_api_key(args.api_key_file)
    store = StateStore(settings.database_path)
    translator = OpenAITranslator(
        api_key=api_key,
        model=args.model,
        max_output_tokens=args.max_output_tokens,
    )
    model_id = _model_id_for(args.model)
    stage = TranslationStage(
        models_lock_path=settings.models_lock_path,
        models_dir=settings.models_dir,
        jobs_dir=settings.jobs_dir,
        default_translation_model_id=model_id,
        store=store,
        openai_translator=translator,
    )

    print(
        f"[translate-openai] backend=openai model={args.model} model_id={model_id} "
        f"db={settings.database_path}",
        flush=True,
    )
    print(
        "[translate-openai] VRAM dùng cho bước dịch: 0 MiB "
        "(không load model local). Ctrl+C để dừng.",
        flush=True,
    )

    processed = 0
    try:
        while not shutdown.is_set():
            did_work = asyncio.run(
                process_next_translation_job(
                    store,
                    stage,
                    shutdown=shutdown,
                    settings=settings,
                    # Không truyền gpu_report: bước OpenAI không dùng VRAM nên
                    # bỏ qua preflight kiểm tra VRAM của worker.
                )
            )
            if did_work:
                processed += 1
            if args.once or shutdown.is_set():
                break
            if not did_work:
                time.sleep(max(args.poll_interval, 1.0))
    finally:
        translator.close()

    print(f"[translate-openai] xong: đã xử lý {processed} job.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
