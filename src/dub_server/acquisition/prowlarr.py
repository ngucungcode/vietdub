"""Generic Prowlarr search adapter.

No indexer, tracker, category, or release source is embedded here.  Prowlarr's
administrator is responsible for configuring authorized indexers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from dub_server.domain import (
    AcquisitionError,
    AcquisitionErrorCode,
    IndexerGateway,
    MediaQuery,
    ReleaseCandidate,
)


class ProwlarrIndexerGateway(IndexerGateway):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient,
        timeout_seconds: float = 15.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Prowlarr base URL không hợp lệ")
        if not api_key:
            raise ValueError("Prowlarr API key không được để trống")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client
        self._timeout = httpx.Timeout(timeout_seconds)

    async def search(self, query: MediaQuery) -> tuple[ReleaseCandidate, ...]:
        search_text = query.query if query.year is None else f"{query.query} {query.year}"
        try:
            response = await self._client.get(
                f"{self._base_url}/api/v1/search",
                params={"query": search_text, "type": "search"},
                headers={"X-Api-Key": self._api_key, "Accept": "application/json"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise AcquisitionError(
                AcquisitionErrorCode.INDEXER_UNAVAILABLE,
                "Prowlarr phản hồi quá thời gian cho phép",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise AcquisitionError(
                AcquisitionErrorCode.INDEXER_UNAVAILABLE,
                "Không thể kết nối tới Prowlarr",
                retryable=True,
            ) from exc

        if response.status_code in {401, 403}:
            raise AcquisitionError(
                AcquisitionErrorCode.INDEXER_UNAVAILABLE,
                "Prowlarr từ chối thông tin xác thực",
                retryable=False,
            )
        if response.status_code >= 500:
            raise AcquisitionError(
                AcquisitionErrorCode.INDEXER_UNAVAILABLE,
                "Prowlarr đang tạm thời không khả dụng",
                retryable=True,
            )
        if response.status_code >= 400:
            raise AcquisitionError(
                AcquisitionErrorCode.INVALID_RESPONSE,
                "Prowlarr từ chối yêu cầu tìm kiếm",
                retryable=False,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AcquisitionError(
                AcquisitionErrorCode.INVALID_RESPONSE,
                "Prowlarr trả về dữ liệu không hợp lệ",
                retryable=True,
            ) from exc
        if not isinstance(payload, list):
            raise AcquisitionError(
                AcquisitionErrorCode.INVALID_RESPONSE,
                "Prowlarr trả về cấu trúc dữ liệu không hợp lệ",
                retryable=True,
            )

        candidates: list[ReleaseCandidate] = []
        for raw in payload:
            candidate = self._parse_candidate(raw)
            if candidate is not None:
                candidates.append(candidate)
        candidates.sort(
            key=lambda item: (
                item.seeders if item.seeders is not None else -1,
                item.size_bytes if item.size_bytes is not None else -1,
            ),
            reverse=True,
        )
        return tuple(candidates)

    @staticmethod
    def _parse_candidate(raw: Any) -> ReleaseCandidate | None:
        if not isinstance(raw, Mapping):
            return None
        title = _text(raw.get("title"))
        guid = _text(raw.get("guid"))
        magnet = _text(raw.get("magnetUrl"))
        download = _text(raw.get("downloadUrl"))
        download_uri = magnet or download
        indexer_id = _integer(raw.get("indexerId"))
        if not title or not download_uri or indexer_id is None:
            return None
        protocol = _text(raw.get("protocol")).lower()
        if protocol and protocol != "torrent":
            return None
        scheme = urlsplit(download_uri).scheme.lower()
        if scheme not in {"http", "https", "magnet"}:
            return None

        identity = guid or download_uri
        release_id = hashlib.sha256(f"{indexer_id}\0{identity}".encode()).hexdigest()[:24]
        categories: list[str] = []
        category_payload = raw.get("categories")
        if isinstance(category_payload, list):
            for category in category_payload:
                if isinstance(category, Mapping):
                    name = _text(category.get("name"))
                else:
                    name = _text(category)
                if name:
                    categories.append(name)

        info_hash = _normalize_info_hash(_text(raw.get("infoHash")))
        if info_hash is None and magnet:
            info_hash = _magnet_info_hash(magnet)
        return ReleaseCandidate(
            release_id=release_id,
            title=title,
            indexer_id=indexer_id,
            protocol=protocol or "torrent",
            download_uri=download_uri,
            guid=guid,
            info_hash=info_hash,
            size_bytes=_non_negative_integer(raw.get("size")),
            seeders=_non_negative_integer(raw.get("seeders")),
            leechers=_non_negative_integer(raw.get("leechers")),
            published_at=_text(raw.get("publishDate")) or None,
            categories=tuple(categories),
        )


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _non_negative_integer(value: object) -> int | None:
    parsed = _integer(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _normalize_info_hash(value: str) -> str | None:
    normalized = value.strip().lower()
    if len(normalized) == 40 and all(character in "0123456789abcdef" for character in normalized):
        return normalized
    return None


def _magnet_info_hash(uri: str) -> str | None:
    query = parse_qs(urlsplit(uri).query)
    for exact_topic in query.get("xt", []):
        prefix = "urn:btih:"
        if exact_topic.lower().startswith(prefix):
            return _normalize_info_hash(exact_topic[len(prefix) :])
    return None
