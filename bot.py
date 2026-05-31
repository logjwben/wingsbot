"""
Dallas Wings Twitter → Bluesky Mirror Bot
Polls an RSS.app feed of the Wings' Twitter account and
mirrors new posts to a Bluesky account, including images and videos.
"""

import os
import re
import json
import time
import mimetypes
import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
RSS_FEED_URL   = os.environ["RSS_FEED_URL"]       # RSS.app feed URL
BSKY_HANDLE    = os.environ["BSKY_HANDLE"]        # e.g. wingsupdates.bsky.social
BSKY_PASSWORD  = os.environ["BSKY_APP_PASSWORD"]  # Bluesky app password
STATE_FILE     = Path("seen_ids.json")
MAX_NEW_POSTS  = 5    # safety cap per run to prevent flooding
MAX_IMAGES     = 4    # Bluesky limit
IMAGE_MAX_BYTES = 999_000   # just under Bluesky's 1 MB image limit
VIDEO_MAX_BYTES = 100_000_000  # 100 MB Bluesky video limit
BSKY_API       = "https://bsky.social/xrpc"
VIDEO_API      = "https://video.bsky.app/xrpc"

# ── State helpers ─────────────────────────────────────────────────────────────

def load_seen() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()

def save_seen(seen: set):
    STATE_FILE.write_text(json.dumps(list(seen)))

# ── Bluesky auth ──────────────────────────────────────────────────────────────

def bsky_login() -> dict:
    resp = requests.post(
        f"{BSKY_API}/com.atproto.server.createSession",
        json={"identifier": BSKY_HANDLE, "password": BSKY_PASSWORD},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()   # contains accessJwt, refreshJwt, did

# ── Media helpers ─────────────────────────────────────────────────────────────

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

def download_media(url: str, max_bytes: int) -> bytes | None:
    """Download media bytes, returning None if too large or failed."""
    try:
        resp = requests.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > max_bytes:
                print(f"    Media too large (>{max_bytes//1_000_000}MB), skipping: {url}")
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    except Exception as e:
        print(f"    Failed to download media {url}: {e}")
        return None

def upload_image(session: dict, image_bytes: bytes, mime: str) -> dict | None:
    """Upload image bytes to Bluesky blob store, return blob ref dict."""
    try:
        resp = requests.post(
            f"{BSKY_API}/com.atproto.repo.uploadBlob",
            headers={
                "Authorization": f"Bearer {session['accessJwt']}",
                "Content-Type": mime,
            },
            data=image_bytes,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["blob"]
    except Exception as e:
        print(f"    Image upload failed: {e}")
        return None

def upload_video(session: dict, video_bytes: bytes) -> dict | None:
    """
    Upload video to Bluesky's video service, poll until processed,
    return blob ref dict. Videos must be MP4.
    """
    did = session["did"]

    # Get a short-lived service token scoped to video upload
    try:
        auth_resp = requests.get(
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
        service_token = auth_resp.json()["token"]
    except Exception as e:
        print(f"    Could not get video service token: {e}")
        return None

    # Upload to video service
    try:
        up_resp = requests.post(
            f"{VIDEO_API}/app.bsky.video.uploadVideo",
            headers={
                "Authorization": f"Bearer {service_token}",
                "Content-Type": "video/mp4",
            },
            params={"did": did, "name": "video.mp4"},
            data=video_bytes,
            timeout=120,
        )
        up_resp.raise_for_status()
        job_id = up_resp.json().get("jobId")
        blob   = up_resp.json().get("blob")  # may already be ready
    except Exception as e:
        print(f"    Video upload failed: {e}")
        return None

    if blob:
        return blob

    # Poll for processing completion (up to ~2 minutes)
    if job_id:
        for _ in range(24):
            time.sleep(5)
            try:
                status_resp = requests.get(
                    f"{VIDEO_API}/app.bsky.video.getJobStatus",
                    headers={"Authorization": f"Bearer {service_token}"},
                    params={"jobId": job_id},
                    timeout=15,
                )
                status_resp.raise_for_status()
                status_data = status_resp.json().get("jobStatus", {})
                state = status_data.get("state")
                blob  = status_data.get("blob")
                print(f"    Video processing: {state}")
                if blob:
                    return blob
                if state == "JOB_STATE_FAILED":
                    print("    Video processing failed.")
                    return None
            except Exception as e:
                print(f"    Error checking video status: {e}")

    print("    Video processing timed out.")
    return None

# ── RSS media extraction ──────────────────────────────────────────────────────

def extract_media_urls(entry) -> tuple[list[str], list[str]]:
    """
    Return (image_urls, video_urls) found in an RSS entry.
    Checks media_content, enclosures, and description HTML.
    """
    image_urls = []
    video_urls = []

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

def entry_to_text(entry) -> str:
    """Extract clean tweet text from an RSS entry title."""
    text = entry.get("title") or ""
    # Strip bare URLs that RSS.app sometimes appends
    lines = text.strip().splitlines()
    cleaned = "\n".join(l for l in lines if not re.match(r"^https?://\S+$", l.strip()))
    result = cleaned.strip() or text.strip()
    # Bluesky 300-grapheme limit
    if len(result) > 300:
        result = result[:297] + "…"
    return result

# ── Post builder ──────────────────────────────────────────────────────────────

def bsky_post(session: dict, text: str, embed: dict | None = None):
    """Create a Bluesky post, optionally with an image or video embed."""
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if embed:
        record["embed"] = embed

    resp = requests.post(
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

def build_image_embed(session: dict, image_urls: list[str]) -> dict | None:
    """Download, upload, and build an app.bsky.embed.images embed."""
    images = []
    for url in image_urls[:MAX_IMAGES]:
        mime  = guess_mime(url, "image/jpeg")
        data  = download_media(url, IMAGE_MAX_BYTES)
        if not data:
            continue
        blob = upload_image(session, data, mime)
        if blob:
            images.append({"image": blob, "alt": ""})
            print(f"    ✓ Image attached")

    if not images:
        return None
    return {"$type": "app.bsky.embed.images", "images": images}

def build_video_embed(session: dict, video_url: str) -> dict | None:
    """Download, upload, and build an app.bsky.embed.video embed."""
    data = download_media(video_url, VIDEO_MAX_BYTES)
    if not data:
        return None
    blob = upload_video(session, data)
    if not blob:
        return None
    print(f"    ✓ Video attached")
    return {
        "$type": "app.bsky.embed.video",
        "video": blob,
        # aspect ratio unknown from RSS; Bluesky will use the video's native ratio
    }

# ── Main ──────────────────────────────────────────────────────────────────────

def process_entry(session: dict, entry) -> bool:
    """Process one RSS entry. Returns True if posted successfully."""
    text        = entry_to_text(entry)
    img_urls, vid_urls = extract_media_urls(entry)

    print(f"  Text: {text[:80]}{'…' if len(text) > 80 else ''}")
    print(f"  Media: {len(img_urls)} image(s), {len(vid_urls)} video(s)")

    embed = None

    # Videos take priority (Bluesky only allows one embed per post)
    if vid_urls:
        embed = build_video_embed(session, vid_urls[0])
        if len(vid_urls) > 1:
            print(f"  Note: only first video attached (Bluesky limit)")

    # Fall back to images if no video (or video upload failed)
    if embed is None and img_urls:
        embed = build_image_embed(session, img_urls)

    bsky_post(session, text, embed)
    return True


def main():
    seen        = load_seen()
    feed        = feedparser.parse(RSS_FEED_URL)
    new_entries = []

    for entry in feed.entries:
        uid = entry.get("id") or entry.get("link")
        if uid and uid not in seen:
            new_entries.append(entry)

    new_entries.reverse()          # oldest first
    new_entries = new_entries[:MAX_NEW_POSTS]

    if not new_entries:
        print("No new tweets. Done.")
        return

    print(f"Found {len(new_entries)} new tweet(s). Logging into Bluesky…")
    session = bsky_login()

    posted = 0
    for entry in new_entries:
        uid = entry.get("id") or entry.get("link")
        print(f"\nProcessing: {uid}")
        try:
            process_entry(session, entry)
            posted += 1
        except Exception as e:
            print(f"  ✗ Failed: {e}")
        finally:
            seen.add(uid)

    save_seen(seen)
    print(f"\nDone. {posted}/{len(new_entries)} post(s) sent to Bluesky.")


if __name__ == "__main__":
    main()
