"""Process-level egress guard for native inference workers.

The managed GPU container cannot create a network namespace. This audit hook
fails closed for Python socket calls after the worker has completed startup.
It complements, but does not claim to replace, kernel-level isolation.
"""

from __future__ import annotations

import socket
import sys
import threading
from collections.abc import Iterable
from ipaddress import ip_address
from typing import Any


class OfflineNetworkError(PermissionError):
    """Raised when an inference process attempts an Internet socket call."""


_install_lock = threading.Lock()
_installed = False


def install_offline_network_guard(
    *,
    allowed_loopback_ports: Iterable[int] = (),
) -> None:
    """Block Internet sockets while optionally allowing fixed local services."""

    global _installed
    with _install_lock:
        if _installed:
            return
        ports = frozenset(allowed_loopback_ports)
        if any(
            isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
            for port in ports
        ):
            raise ValueError("Cổng loopback ngoại tuyến không hợp lệ")

        def allowed_loopback(host: object, port: object) -> bool:
            if isinstance(port, str) and port.isdecimal():
                port = int(port)
            if not isinstance(port, int) or port not in ports or not isinstance(host, str):
                return False
            try:
                return ip_address(host).is_loopback
            except ValueError:
                return False

        def audit(event: str, arguments: tuple[Any, ...]) -> None:
            if event == "socket.getaddrinfo":
                if len(arguments) >= 2 and allowed_loopback(
                    arguments[0], arguments[1]
                ):
                    return
                raise OfflineNetworkError(
                    "Worker offline không được phép phân giải địa chỉ mạng"
                )
            if event != "socket.connect" or not arguments:
                return
            candidate = arguments[0]
            family = getattr(candidate, "family", None)
            if family in {socket.AF_INET, socket.AF_INET6}:
                address = arguments[1] if len(arguments) >= 2 else None
                if (
                    isinstance(address, tuple)
                    and len(address) >= 2
                    and allowed_loopback(address[0], address[1])
                ):
                    return
                raise OfflineNetworkError(
                    "Worker offline không được phép mở kết nối mạng"
                )

        sys.addaudithook(audit)
        _installed = True


__all__ = ["OfflineNetworkError", "install_offline_network_guard"]
