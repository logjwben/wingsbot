This repo powers the Wings → Bluesky mirror bot.
# Dallas Wings Twitter → Bluesky Mirror

Automatically mirrors the Dallas Wings Twitter/X account to Bluesky,
including images and videos, running every 30 minutes via GitHub Actions — completely free.

---

## Setup (one-time, ~20 minutes)

### Step 1 — Create a Bluesky bot account

1. Go to [bsky.app](https://bsky.app) and sign up for a new account.
   - Suggested handle: `dallaswinsupdates.bsky.social` or similar
2. Once logged in, go to **Settings → Privacy and Security → App Passwords**
3. Click **Add App Password**, name it `mirror-bot`, and copy the password.
   - This is your `BSKY_APP_PASSWORD` — save it somewhere safe.

---

### Step 2 — Generate an RSS feed for the Wings' Twitter

1. Go to [rss.app](https://rss.app) and sign up for a free account
2. Click **Create RSS Feed → Twitter/X**
3. Enter: `https://twitter.com/DallasWings`
4. After it generates, go to the feed's **Output settings** and make sure:
   - ✅ **Include images in the media tag** — enabled
   - ✅ **Include images in the enclosure** — enabled
5. Copy the generated RSS feed URL (looks like `https://rss.app/feeds/xxxxxxxxxx.xml`)
   - This is your `RSS_FEED_URL`

---

### Step 3 — Create the GitHub repository

1. Go to [github.com](https://github.com) and create a new **public** repository
   - Name it: `wings-bluesky-mirror`
2. Upload all four files from this project:
   - `bot.py`
   - `seen_ids.json`
   - `.github/workflows/mirror.yml`
   - `README.md`

   The easiest way: use GitHub's "Add file → Upload files" for the top-level files,
   then use "Create new file" and type `.github/workflows/mirror.yml` as the path
   to create the workflow file.

---

### Step 4 — Add secrets to the repository

1. In your GitHub repo, go to **Settings → Secrets and variables → Actions**
2. Click **New repository secret** for each of the following:

| Secret name         | Value                                                              |
|---------------------|--------------------------------------------------------------------|
| `RSS_FEED_URL`      | Your RSS.app feed URL from Step 2                                  |
| `BSKY_HANDLE`       | Your bot's Bluesky handle (e.g. `dallaswinsupdates.bsky.social`)  |
| `BSKY_APP_PASSWORD` | The app password from Step 1                                       |

---

### Step 5 — Test it manually

1. In your repo, go to **Actions → Wings Twitter → Bluesky Mirror**
2. Click **Run workflow → Run workflow**
3. Watch the logs — if everything's green, check your Bluesky bot account!

---

## How it works

- GitHub Actions runs `bot.py` every 30 minutes (free on public repos)
- The script fetches your RSS.app feed of the Wings' Twitter account
- New tweets (not in `seen_ids.json`) are posted to Bluesky with media:
  - **Images**: up to 4 per post, downloaded and uploaded to Bluesky automatically
  - **Videos**: first video in a tweet uploaded to Bluesky's video service (MP4 only, up to 100 MB)
- `seen_ids.json` is committed back to the repo after each run to track state
- A max of 5 posts per run prevents flooding if something goes wrong

## Notes

- **RSS.app free tier** gives you 1 feed with a slight delay (~15 min). Paid tiers
  are faster but free is fine for a fan mirror.
- **Images**: each must be under 1 MB — oversized images are skipped with a log message.
- **Videos**: the bot polls Bluesky's video processing service for up to 2 minutes.
  If a tweet has multiple videos, only the first is posted (Bluesky's one-video-per-post limit).
- **Bluesky posts are capped at 300 graphemes** — longer tweets are truncated with `…`
- To stop the bot, disable the workflow in the Actions tab.
