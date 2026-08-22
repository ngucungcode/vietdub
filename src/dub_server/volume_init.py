"""Initialize shared Docker volumes with one consistent unprivileged owner."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

DEFAULT_DIRECTORIES = (
    Path("/config"),
    Path("/models"),
    Path("/state"),
    Path("/data/incoming"),
    Path("/data/jobs"),
    Path("/data/output"),
)


def initialize_directories(
    directories: Iterable[Path],
    *,
    uid: int,
    gid: int,
    chown: Callable[[Path, int, int], None] | None = None,
) -> None:
    if uid <= 0 or gid <= 0:
        raise ValueError("UID/GID chạy ứng dụng phải lớn hơn 0")
    targets = tuple(directories)
    if any(not directory.is_absolute() for directory in targets):
        raise ValueError("Đường dẫn volume phải là đường dẫn tuyệt đối")
    owner_change = chown or getattr(os, "chown", None)
    if owner_change is None:
        raise RuntimeError("Hệ điều hành không hỗ trợ thay đổi UID/GID")
    for directory in targets:
        directory.mkdir(parents=True, exist_ok=True)
        for descendant in directory.rglob("*"):
            if descendant.is_symlink():
                continue
            owner_change(descendant, uid, gid)
        owner_change(directory, uid, gid)
        directory.chmod(0o770)


def main() -> None:
    uid = int(os.environ.get("DUB_APP_UID", "10001"))
    gid = int(os.environ.get("DUB_APP_GID", "10001"))
    initialize_directories(DEFAULT_DIRECTORIES, uid=uid, gid=gid)


if __name__ == "__main__":
    main()
