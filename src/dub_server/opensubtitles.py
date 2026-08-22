"""Shared OpenSubtitles API endpoint validation."""

from __future__ import annotations

from urllib.parse import urlsplit

OFFICIAL_OPENSUBTITLES_HOSTS = frozenset(
    {"api.opensubtitles.com", "vip-api.opensubtitles.com"}
)
DEFAULT_OPENSUBTITLES_API_ROOT = "https://api.opensubtitles.com/api/v1"


def normalize_opensubtitles_api_root(value: str) -> str:
    """Return an allowlisted ``https://<host>/api/v1`` root."""

    candidate = value.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in OFFICIAL_OPENSUBTITLES_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid OpenSubtitles API root")
    path = parsed.path.rstrip("/")
    if path not in {"", "/api/v1"}:
        raise ValueError("invalid OpenSubtitles API path")
    authority = parsed.hostname
    assert authority is not None
    return f"https://{authority}/api/v1"
