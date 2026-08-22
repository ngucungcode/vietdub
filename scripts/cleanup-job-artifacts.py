#!/usr/bin/env python3
"""Safely clean cancelled/expired-retry job artifacts (dry-run by default)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dub_server.artifact_cleanup import execute_cleanup, plan_artifact_cleanup
from dub_server.config import Settings
from dub_server.state import JobStatus, StateStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dọn artifact job đã hủy và job retry quá hạn; mặc định chỉ dry-run"
        )
    )
    parser.add_argument("--database", type=Path)
    parser.add_argument("--jobs-root", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--retry-retention-days", type=int, default=7)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Thực sự xóa; nếu không có cờ này thì chỉ lập kế hoạch",
    )
    args = parser.parse_args()
    settings = Settings()
    database = args.database or settings.database_path
    jobs_root = args.jobs_root or settings.jobs_dir
    output_root = args.output_root or settings.output_dir
    store = StateStore(database)
    jobs = store.list_jobs(
        statuses=[JobStatus.CANCELLED, JobStatus.FAILED],
        limit=1000,
    )
    plan = plan_artifact_cleanup(
        jobs,
        jobs_root=jobs_root,
        output_root=output_root,
        retry_retention_days=args.retry_retention_days,
    )
    result = execute_cleanup(plan, apply=args.apply, state_store=store)
    print(
        json.dumps(
            {"plan": plan.to_dict(), "result": result.to_dict()},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
