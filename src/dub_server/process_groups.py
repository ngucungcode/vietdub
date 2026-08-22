"""Portable process-group lifecycle helpers for local native runtimes.

The GPU adapters can invoke synchronous helpers such as FFmpeg.  Killing only
the direct Python child would leave those helpers running and holding files or
GPU memory.  Every supported child is therefore started as a process-group
leader and cancellation targets the complete group/tree.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping
from contextlib import suppress
from typing import Protocol


class GroupProcessHandle(Protocol):
    """Process operations needed by :func:`signal_process_group`."""

    returncode: int | None

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


def process_group_spawn_options(
    platform_name: str | None = None,
) -> Mapping[str, object]:
    """Return the safe platform-specific options for ``create_subprocess_exec``.

    ``start_new_session`` makes the child both session and process-group leader
    on POSIX.  Windows needs ``CREATE_NEW_PROCESS_GROUP`` so a CTRL+BREAK can be
    addressed to that group before the forceful ``taskkill /T`` fallback.
    ``platform_name`` exists to make both branches testable on every host.
    """

    platform = os.name if platform_name is None else platform_name
    if platform == "nt":
        return {
            "creationflags": getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
            )
        }
    return {"start_new_session": True}


def signal_process_group(
    process: GroupProcessHandle,
    *,
    force: bool,
    platform_name: str | None = None,
) -> None:
    """Signal an owned child process group, falling back to the direct child.

    The fallback keeps injected test doubles and unusual event-loop process
    implementations working even when they do not expose ``pid`` or
    ``send_signal``.  Production asyncio processes always expose both.
    """

    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        _signal_direct_process(process, force=force)
        return

    platform = os.name if platform_name is None else platform_name
    if platform == "nt":
        if force:
            if not _force_kill_windows_tree(pid):
                _signal_direct_process(process, force=True)
            return
        send_signal = getattr(process, "send_signal", None)
        control_break = getattr(signal, "CTRL_BREAK_EVENT", None)
        if callable(send_signal) and control_break is not None:
            try:
                send_signal(control_break)
                return
            except (OSError, ProcessLookupError, RuntimeError, ValueError):
                pass
        _signal_direct_process(process, force=False)
        return

    group_signal = (
        getattr(signal, "SIGKILL", 9) if force else getattr(signal, "SIGTERM", 15)
    )
    try:
        _signal_posix_group(pid, group_signal)
    except (OSError, ProcessLookupError, PermissionError):
        _signal_direct_process(process, force=force)


def _signal_posix_group(group_id: int, group_signal: int) -> None:
    os.killpg(group_id, group_signal)


def _force_kill_windows_tree(pid: int) -> bool:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ("taskkill.exe", "/PID", str(pid), "/T", "/F"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2.0,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _signal_direct_process(process: GroupProcessHandle, *, force: bool) -> None:
    operation = process.kill if force else process.terminate
    with suppress(OSError, ProcessLookupError, RuntimeError):
        operation()
