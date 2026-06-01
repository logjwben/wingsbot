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
        auth_resp = await client.get(
            f"{BSKY_API}/com.atproto.server.getServiceAuth",
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
            params={
                "aud": "did:web:video.bsky.app",
                "lxm": "app.bsky.video.uploadVideo",
                "exp": int(time.time()) + 1800,
            },
            timeout=15,
        )
        auth_resp.raise_for_status()
        return auth_resp.json()["token"]
    except Exception as e:
        log.warning("    Could not get video service token: %s", e)
        return None

async def upload_video(
    client: httpx.AsyncClient,
    session: Dict[str, Any],
    video_bytes: bytes,
) -> Optional[Dict[str, Any]]:
    """
    Upload video to Bluesky's video service, poll until processed,
    return blob ref dict. Videos must be MP4.
    """
    did = session["did"]
    service_token = await get_video_service_token(client, session)
    if not service_token:
        return None

    # Upload to video service
    try:
        up_resp = await client.post(
            f"{VIDEO_API}/app.bsky.video.uploadVideo",
            headers={
                "Authorization": f"Bearer {service_token}",
                "Content-Type": "video/mp4",
            },
            params={"did": did, "name": "video.mp4"},
            content=video_bytes,
            timeout=120,
        )
        up_resp.raise_for_status()
        data = up_resp.json()
        job_id = data.get("jobId")
        blob   = data.get("blob")  # may already be ready
    except Exception as e:
        log.warning("    Video upload failed: %s", e)
        return None

    if blob:
        return blob

    # Poll for processing completion (up to ~2 minutes)
    if job_id:
        for _ in range(24):
            await asyncio.sleep(5)
            try:
                status_resp = await client.get(
                    f"{VIDEO_API}/app.bsky.video.getJobStatus",
                    headers={"Authorization": f"Bearer {service_token}"},
                    params={"jobId": job_id},
                    timeout=15,
                )
                status_resp.raise_for_status()
                status_data = status_resp.json().get("jobStatus", {})
                state = status_data.get("state")
                blob  = status_data.get("blob")
                log.info("    Video processing: %s", state)
                if blob:
                    return blob
                if state == "JOB_STATE_FAILED":
                    log.warning("    Video processing failed.")
                    return None
            except Exception as e:
                log.warning("    Error checking video status: %s", e)

    log.warning("    Video processing timed out.")
    return None

# ── RSS media extraction ──────────────────────────────────────────────────────

def extract_media_urls(entry: Any) -> Tuple[List[str], List[str]]:
    """
    Return (image_urls, video_urls) found in an RSS entry.
    Checks media_content, enclosures, and description HTML.
    """
    image_urls: List[str] = []
    video_urls: List[str] = []

    # feedparser normalises media:content into entry.media_content
    for m in getattr(entry, "media_content", []):
        url  = m.get("url", "")
        mime = m.get("type", "")
        if not url:
            continue
        if "video" in mime or is_video_url(url):
            video_urls.append(url)
        elif "image" in mime or is_image_url(url):
            image_urls.append(url)

    # enclosures
    for enc in getattr(entry, "enclosures", []):
        url  = enc.get("href") or enc.get("url", "")
        mime = enc.get("type", "")
        if not url:
            continue
        if "video" in mime or is_video_url(url):
            if url not in video_urls:
                video_urls.append(url)
        elif "image" in mime or is_image_url(url):
            if url not in image_urls:
                image_urls.append(url)

    # Scrape HTML description as fallback (RSS.app puts media here too)
    html = entry.get("summary") or entry.get("description") or ""
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src and is_image_url(src) and src not in image_urls:
                image_urls.append(src)
        for vid in soup.find_all(["video", "source"]):
            src = vid.get("src", "")
            if src and is_video_url(src) and src not in video_urls:
                video_urls.append(src)

    return image_urls, video_urls

# ── Text extraction ───────────────────────────────────────────────────────────

def entry_to_text(entry: Any) -> str:
    """Extract clean tweet text from an RSS entry title."""
    text = entry.get("title") or ""
    # Strip bare URLs that RSS.app sometimes appends
    lines = text.strip().splitlines()
    cleaned = "\n".join(
        l for l in lines if not re.match(r"^https?://\S+$", l.strip())
    )
    result = cleaned.strip() or text.strip()
    # Bluesky 300-grapheme limit (approximate by characters)
    if len(result) > 300:
        result = result[:297] + "…"
    return result

# ── Post builder ──────────────────────────────────────────────────────────────

async def bsky_post(
    client: httpx.AsyncClient,
    session: Dict[str, Any],
    text: str,
    embed: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a Bluesky post, optionally with an image or video embed."""
    record: Dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if embed:
        record["embed"] = embed

    resp = await client.post(
        f"{BSKY_API}/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {session['accessJwt']}"},
        json={
            "repo": session["did"],
            "collection": "app.bsky.feed.post",
            "record": record,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

async def build_image_embed(
    client: httpx.AsyncClient,
    session: Dict[str, Any],
    image_urls: List[str],
    media_semaphore: asyncio.Semaphore,
) -> Optional[Dict[str, Any]]:
    """Download, upload, and build an app.bsky.embed.images embed."""
    images: List[Dict[str, Any]] = []

    async def handle_one(url: str) -> None:
        async with media_semaphore:
            mime = guess_mime(url, "image/jpeg")
            data = await download_media(client, url, IMAGE_MAX_BYTES)
            if not data:
                return
            blob = await upload_image(client, session, data, mime)
            if blob:
                images.append({"image": blob, "alt": ""})
                log.info("    ✓ Image attached")

    tasks = [handle_one(url) for url in image_urls[:MAX_IMAGES]]
    await asyncio.gather(*tasks)

    if not images:
        return None
    return {"$type": "app.bsky.embed.images", "images": images}

async def build_video_embed(
    client: httpx.AsyncClient,
    session: Dict[str, Any],
    video_url: str,
    media_semaphore: asyncio.Semaphore,
) -> Optional[Dict[str, Any]]:
    """Download, upload, and build an app.bsky.embed.video embed."""
    async with media_semaphore:
        data = await download_media(client, video_url, VIDEO_MAX_BYTES)
        if not data:
            return None
        blob = await upload_video(client, session, data)
        if not blob:
            return None
        log.info("    ✓ Video attached")
        return {
            "$type": "app.bsky.embed.video",
            "video": blob,
        }

# ── Per-entry processing ──────────────────────────────────────────────────────

async def process_entry(
    client: httpx.AsyncClient,
    session: Dict[str, Any],
    entry: Any,
    media_semaphore: asyncio.Semaphore,
) -> bool:
    """Process one RSS entry. Returns True if posted successfully."""
    text = entry_to_text(entry)
    img_urls, vid_urls = extract_media_urls(entry)

    log.info("  Text: %s%s", text[:80], "…" if len(text) > 80 else "")
    log.info("  Media: %d image(s), %d video(s)", len(img_urls), len(vid_urls))

    embed: Optional[Dict[str, Any]] = None

    # Videos take priority (Bluesky only allows one embed per post)
    if vid_urls:
        embed = await build_video_embed(client, session, vid_urls[0], media_semaphore)
        if len(vid_urls) > 1:
            log.info("  Note: only first video attached (Bluesky limit)")

    # Fall back to images if no video (or video upload failed)
    if embed is None and img_urls:
        embed = await build_image_embed(client, session, img_urls, media_semaphore)

    await bsky_post(client, session, text, embed)
    return True

# ── Main ──────────────────────────────────────────────────────────────────────

async def run_bot() -> None:
    seen = load_seen()
    feed = feedparser.parse(RSS_FEED_URL)
    new_entries: List[Any] = []

    for entry in feed.entries:
        uid = entry.get("id") or entry.get("link")
        if uid and uid not in seen:
            new_entries.append(entry)

    new_entries.reverse()          # oldest first
    new_entries = new_entries[:MAX_NEW_POSTS]

    if not new_entries:
        log.info("No new tweets. Done.")
        return

    log.info("Found %d new tweet(s). Logging into Bluesky…", len(new_entries))

    async with httpx.AsyncClient() as client:
        session = await bsky_login(client)
        media_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MEDIA)
        post_semaphore = asyncio.Semaphore(MAX_CONCURRENT_POSTS)

        async def handle_entry(entry: Any) -> None:
            uid = entry.get("id") or entry.get("link")
            if not uid:
                return
            log.info("Processing: %s", uid)
            async with post_semaphore:
                try:
                    await process_entry(client, session, entry, media_semaphore)
                    log.info("  ✓ Posted")
                except Exception as e:
                    log.warning("  ✗ Failed: %s", e)
                finally:
                    seen.add(uid)

        await asyncio.gather(*(handle_entry(e) for e in new_entries))

    save_seen(seen)
    log.info("Done. %d/%d post(s) processed.", len(new_entries), len(new_entries))

def main() -> None:
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
