"""ASGI entrypoint for source-tree runners."""

from __future__ import annotations

import sys
from pathlib import Path

_SOURCE = Path(__file__).resolve().parent / "src"
if str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from dub_server.api import app

__all__ = ["app"]
