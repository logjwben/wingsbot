"""
Dallas Wings Twitter → Bluesky Mirror Bot (Async + yt-dlp Edition)

Polls an RSS/Nitter feed of the Wings' Twitter account and
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
import yt_dlp
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
RSS_FEED_URL    = os.environ["RSS_FEED_URL"]
BSKY_HANDLE     = os.environ["BSKY_HANDLE"]
BSKY_PASSWORD   = os.environ["BSKY_APP_PASSWORD"]
STATE_FILE      = Path("seen_ids.json")

MAX_NEW_POSTS   = 5
MAX_IMAGES      = 4
IMAGE_MAX_BYTES = 999_000
VIDEO_MAX_BYTES = 100_000_000

BSKY_API        = "https://bsky.social/xrpc"
VIDEO_API       = "https://video.bsky.app/xrpc"

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
        return set(str(x) for x in data)
    except Exception as e:
        log.warning("Failed to read seen_ids.json, starting fresh: %s", e)
        return set()

def save_seen(seen: Set[str]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(list(seen))))
    tmp.replace(STATE_FILE)

# ── Utility helpers ───────────────────────────────────────────────────────────

def is_video_url(url: str) -> bool:
    return any(url.lower().split("?")[0].endswith(ext)
               for ext in (".mp4", ".mov", ".m4v", ".webm"))

def is_image_url(url: str) -> bool:
    return any(url.lower().split("?")[0].endswith(ext)
               for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"))

def guess_mime(url: str, default: str = "image/jpeg") -> str:
    clean = url.split("?")[0]
    mime, _ = mimetypes.guess_type(clean)
    return mime or default

async def download_media(client: httpx.AsyncClient, url: str, max_bytes: int) -> Optional[bytes]:
    try:
        async with client.stream("GET", url, timeout=30) as resp:
            resp.raise_for_status()
            chunks = []
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

# ── yt-dlp Twitter video extraction ───────────────────────────────────────────

async def extract_twitter_video_with_ytdlp(tweet_url: str) -> Optional[str]:
    """
    Use yt-dlp to extract the best MP4 URL from a Twitter/X post.
    Returns a direct MP4 URL or None.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "forceurl": True,
        "format": "mp4[height<=1080]/mp4/best",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(tweet_url, download=False)
            if "url" in info:
                return info["url"]
            for f in info.get("formats", []):
                if f.get("ext") == "mp4" and "url" in f:
                    return f["url"]
    except Exception as e:
        log.warning("    yt-dlp failed to extract video: %s", e)
        return None

    return None

# ── Bluesky auth ──────────────────────────────────────────────────────────────

async def bsky_login(client: httpx.AsyncClient) -> Dict[str, Any]:
    resp = await client.post(
        f"{BSKY_API}/com.atproto.server.createSession",
        json={"identifier": BSKY_HANDLE, "password": BSKY_PASSWORD},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

# ── Media upload helpers ──────────────────────────────────────────────────────

async def upload_image(client: httpx.AsyncClient, session: Dict[str, Any],
                       image_bytes: bytes, mime: str) -> Optional[Dict[str, Any]]:
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

async def get_video_service_token(client: httpx.AsyncClient, session: Dict[str, Any]) -> Optional[str]:
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

async def upload_video(client: httpx.AsyncClient, session: Dict[str, Any],
                       video_bytes: bytes) -> Optional[Dict[str, Any]]:
    did = session["did"]
    service_token = await get_video_service_token(client, session)
    if not service_token:
        return None

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
        blob   = data.get("blob")
    except Exception as e:
        log.warning("    Video upload failed: %s", e)
        return None

    if blob:
        return blob

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
    image_urls = []
    video_urls = []

    for m in getattr(entry, "media_content", []):
        url  = m.get("url", "")
        mime = m.get("type", "")
        if not url:
            continue
        if "video" in mime or is_video_url(url):
            video_urls.append(url)
        elif "image" in mime or is_image_url(url):
            image_urls.append(url)

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
    text = entry.get("title") or ""
    lines = text.strip().splitlines()
    cleaned = "\n".join(
        l for l in lines if not re.match(r"^https?://\S+$", l.strip())
    )
    result = cleaned.strip() or text.strip()
    if len(result) > 300:
        result = result[:297] + "…"
    return result

# ── Post builder ──────────────────────────────────────────────────────────────

async def bsky_post(client: httpx.AsyncClient, session: Dict[str, Any],
                    text: str, embed: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    record = {
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

async def build_image_embed(client: httpx.AsyncClient, session: Dict[str, Any],
                            image_urls: List[str], media_semaphore: asyncio.Semaphore
) -> Optional[Dict[str, Any]]:
    images = []

    async def handle_one(url: str):
        async with media_semaphore:
            mime = guess_mime(url)
            data = await download_media(client, url, IMAGE_MAX_BYTES)
            if not data:
                return
            blob = await upload_image(client, session, data, mime)
            if blob:
                images.append({"image": blob, "alt": ""})
                log.info("    ✓ Image attached")

    await asyncio.gather(*(handle_one(url) for url in image_urls[:MAX_IMAGES]))

    if not images:
        return None
    return {"$type": "app.bsky.embed.images", "images": images}

async def build_video_embed(client: httpx.AsyncClient, session: Dict[str, Any],
                            video_url: str, media_semaphore: asyncio.Semaphore
) -> Optional[Dict[str, Any]]:
    async with media_semaphore:
        data = await download_media(client, video_url, VIDEO_MAX_BYTES)
        if not data:
            return None
        blob = await upload_video(client, session, data)
        if not blob:
            return None
        log.info("    ✓ Video attached")
        return {"$type": "app.bsky.embed.video", "video": blob}

# ── Per-entry processing ──────────────────────────────────────────────────────

async def process_entry(client: httpx.AsyncClient, session: Dict[str, Any],
                        entry: Any, media_semaphore: asyncio.Semaphore) -> bool:
    text = entry_to_text(entry)
    img_urls, vid_urls = extract_media_urls(entry)

    log.info("  Text: %s%s", text[:80], "…" if len(text) > 80 else "")
    log.info("  Media: %d image(s), %d video(s)", len(img_urls), len(vid_urls))

    embed = None

    if vid_urls:
        original_url = vid_urls[0]

        real_mp4 = await extract_twitter_video_with_ytdlp(original_url)

        if real_mp4:
            log.info("    ✓ yt-dlp extracted real MP4")
            embed = await build_video_embed(client, session, real_mp4, media_semaphore)
        else:
            log.info("    yt-dlp failed, falling back to RSS video URL")
            embed = await build_video_embed(client, session, original_url, media_semaphore)

    if embed is None and img_urls:
        embed = await build_image_embed(client, session, img_urls, media_semaphore)

    await bsky_post(client, session, text, embed)
    return True

# ── Main ──────────────────────────────────────────────────────────────────────

async def run_bot() -> None:
    seen = load_seen()
    feed = feedparser.parse(RSS_FEED_URL)
    new_entries = []

    for entry in feed.entries:
        uid = entry.get("id") or entry.get("link")
        if uid and uid not in seen:
            new_entries.append(entry)

    new_entries.reverse()
    new_entries = new_entries[:MAX_NEW_POSTS]

    if not new_entries:
        log.info("No new tweets. Done.")
        return

    log.info("Found %d new tweet(s). Logging into Bluesky…", len(new_entries))

    async with httpx.AsyncClient() as client:
        session = await bsky_login(client)
        media_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MEDIA)
        post_semaphore = asyncio.Semaphore(MAX_CONCURRENT_POSTS)

        async def handle_entry(entry: Any):
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

def main():
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
