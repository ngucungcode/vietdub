#!/usr/bin/env python3
"""Exercise the real qBittorrent 4/5 adapter without downloading content."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from dub_server.acquisition.qbittorrent import QBittorrentDownloadClient
from dub_server.config import Settings, read_secret
from dub_server.domain import ReleaseCandidate

_SMOKE_HASH = "0123456789abcdef0123456789abcdef01234567"


async def run_smoke() -> None:
    settings = Settings()
    password = read_secret(settings.qbittorrent_password_file)
    if password is None:
        raise SystemExit("Chưa cấu hình mật khẩu qBittorrent")

    save_path = settings.incoming_dir / ".native-qbittorrent-smoke"
    save_path.mkdir(parents=True, exist_ok=True)
    release = ReleaseCandidate(
        release_id="native-qbittorrent-smoke",
        title="Native adapter smoke fixture",
        indexer_id=0,
        protocol="torrent",
        download_uri=f"magnet:?xt=urn:btih:{_SMOKE_HASH}",
        info_hash=_SMOKE_HASH,
    )
    task_id: str | None = None
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            downloads = QBittorrentDownloadClient(
                base_url=settings.qbittorrent_url,
                username=settings.qbittorrent_username,
                password=password,
                client=client,
                discovery_timeout_seconds=5.0,
            )
            task = await downloads.add(release, save_path, paused=True)
            task_id = task.task_id
            await downloads.resume(task.task_id)
            await downloads.pause(task.task_id)
            status = await downloads.status(task.task_id)
            await downloads.cancel(task.task_id, delete_files=False)
            task_id = None
        print(
            json.dumps(
                {
                    "adapter": "qbittorrent",
                    "task_hash_matched": task.task_id == _SMOKE_HASH,
                    "state_after_pause": status.state.value,
                    "cleaned_up": True,
                },
                ensure_ascii=False,
            )
        )
    finally:
        if task_id is not None:
            async with httpx.AsyncClient(follow_redirects=False) as cleanup_client:
                cleanup = QBittorrentDownloadClient(
                    base_url=settings.qbittorrent_url,
                    username=settings.qbittorrent_username,
                    password=password,
                    client=cleanup_client,
                )
                await cleanup.cancel(task_id, delete_files=False)
        try:
            Path(save_path).rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    asyncio.run(run_smoke())
