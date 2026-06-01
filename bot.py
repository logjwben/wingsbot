
"""
Dallas Wings Twitter → Bluesky Mirror Bot (Async Edition)

Polls an RSS.app feed of the Wings' Twitter account and
mirrors new posts to a Bluesky account, including images and videos.
"""

import os
import re
import json
import time
import mimetypes
import asyncio
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import feedparser
import httpx
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
RSS_FEED_URL    = os.environ["RSS_FEED_URL"]       # RSS.app feed URL
BSKY_HANDLE     = os.environ["BSKY_HANDLE"]        # e.g. wingsupdates.bsky.social
BSKY_PASSWORD   = os.environ["BSKY_APP_PASSWORD"]  # Bluesky app password
STATE_FILE      = Path("seen_ids.json")
MAX_NEW_POSTS   = 5          # safety cap per run to prevent flooding
MAX_IMAGES      = 4          # Bluesky limit
IMAGE_MAX_BYTES = 999_000    # just under Bluesky's 1 MB image limit
VIDEO_MAX_BYTES = 100_000_000  # 100 MB Bluesky video limit
BSKY_API        = "https://bsky.social/xrpc"
VIDEO_API       = "https://video.bsky.app/xrpc"

# Concurrency limits
MAX_CONCURRENT_POSTS = 3
MAX_CONCURRENT_MEDIA = 4

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("wings-bot")

# ── State helpers ─────────────────────────────────────────────────────────────

def load_seen() -> Set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text())
        if isinstance(data, list):
            return set(str(x) for x in data)
        return set()
    except Exception as e:
        log.warning("Failed to read seen_ids.json, starting fresh: %s", e)
        return set()

def save_seen(seen: Set[str]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(list(seen))))
    tmp.replace(STATE_FILE)

# ── Utility helpers ───────────────────────────────────────────────────────────

def is_video_url(url: str) -> bool:
    video_exts = (".mp4", ".mov", ".m4v", ".webm")
    return any(url.lower().split("?")[0].endswith(ext) for ext in video_exts)

def is_image_url(url: str) -> bool:
    image_exts = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    return any(url.lower().split("?")[0].endswith(ext) for ext in image_exts)

def guess_mime(url: str, default: str = "image/jpeg") -> str:
    clean = url.split("?")[0]
    mime, _ = mimetypes.guess_type(clean)
    return mime or default

async def download_media(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int,
) -> Optional[bytes]:
    """Download media bytes, returning None if too large or failed."""
    try:
        async with client.stream("GET", url, timeout=30) as resp:
            resp.raise_for_status()
            chunks: List[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                total += len(chunk)
                if total > max_bytes:
                    log.info("    Media too large (>%.1fMB), skipping: %s", max_bytes / 1_000_000, url)
                    return None
                chunks.append(chunk)
        return b"".join(chunks)
    except Exception as e:
        log.warning("    Failed to download media %s: %s", url, e)
        return None

# ── Bluesky auth ──────────────────────────────────────────────────────────────

async def bsky_login(client: httpx.AsyncClient) -> Dict[str, Any]:
    resp = await client.post(
        f"{BSKY_API}/com.atproto.server.createSession",
        json={"identifier": BSKY_HANDLE, "password": BSKY_PASSWORD},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()   # contains accessJwt, refreshJwt, did

# ── Media upload helpers ──────────────────────────────────────────────────────

async def upload_image(
    client: httpx.AsyncClient,
    session: Dict[str, Any],
    image_bytes: bytes,
    mime: str,
) -> Optional[Dict[str, Any]]:
    """Upload image bytes to Bluesky blob store, return blob ref dict."""
    try:
        resp = await client.post(
            f"{BSKY_API}/com.atproto.repo.uploadBlob",
            headers={
                "Authorization": f"Bearer {session['accessJwt']}",
                "Content-Type": mime,
            },
            content=image_bytes,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["blob"]
    except Exception as e:
        log.warning("    Image upload failed: %s", e)
        return None

async def get_video_service_token(
    client: httpx.AsyncClient,
    session: Dict[str, Any],
) -> Optional[str]:
    try:
