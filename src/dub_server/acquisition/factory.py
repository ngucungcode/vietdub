"""Explicit dependency wiring for the network-enabled API process."""

from __future__ import annotations

import httpx

from .. import __version__
from .prowlarr import ProwlarrIndexerGateway
from .qbittorrent import QBittorrentDownloadClient
from .service import AcquisitionService
from .subtitles import CompositeSubtitleProvider, EmbeddedSubtitleProbe


def build_acquisition_service(
    *,
    client: httpx.AsyncClient,
    prowlarr_url: str,
    prowlarr_api_key: str,
    qbittorrent_url: str,
    qbittorrent_username: str,
    qbittorrent_password: str,
    opensubtitles_api_key: str | None = None,
    opensubtitles_token: str | None = None,
    opensubtitles_base_url: str = "https://api.opensubtitles.com/api/v1",
    opensubtitles_user_agent: str = f"ThuyetMinhOfflineGPU v{__version__}",
    embedded_probe: EmbeddedSubtitleProbe | None = None,
) -> AcquisitionService:
    """Build adapters around one client whose lifecycle remains API-owned."""

    indexer = ProwlarrIndexerGateway(
        base_url=prowlarr_url,
        api_key=prowlarr_api_key,
        client=client,
    )
    downloads = QBittorrentDownloadClient(
        base_url=qbittorrent_url,
        username=qbittorrent_username,
        password=qbittorrent_password,
        client=client,
    )
    subtitles = CompositeSubtitleProvider(
        client=client,
        opensubtitles_api_key=opensubtitles_api_key,
        opensubtitles_token=opensubtitles_token,
        opensubtitles_base_url=opensubtitles_base_url,
        user_agent=opensubtitles_user_agent,
        embedded_probe=embedded_probe,
    )
    return AcquisitionService(indexer=indexer, downloads=downloads, subtitles=subtitles)
