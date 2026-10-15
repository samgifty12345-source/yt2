"""Take one TikTok video from an old account and post it to your new TikTok via Zernio.
No YouTube, no scheduler. Just this.

Setup:
  pip install yt-dlp requests
  set ZERNIO_API_KEY=your_key          (Windows cmd)   |   export ZERNIO_API_KEY=your_key   (Mac/Linux)
  optional: TIKTOK_COOKIES_FILE=path/to/cookies.txt  (if TikTok blocks the download)

Usage:
  python repost_test.py sky_raiding                 -> newest video of @sky_raiding, sent to DRAFTS
  python repost_test.py https://www.tiktok.com/@x/video/123   -> that exact video
  python repost_test.py sky_raiding --public        -> publish immediately instead of draft
  python repost_test.py sky_raiding --to someuser   -> different Zernio TikTok account
                                                       (default: iconpage47)
"""
import os
import sys
import json
import tempfile

import requests
import yt_dlp

KEY = os.environ.get("ZERNIO_API_KEY")
if not KEY:
    sys.exit("Set ZERNIO_API_KEY first.")
COOKIES = os.environ.get("TIKTOK_COOKIES_FILE", "")
BASE = "https://zernio.com/api/v1"
H = {"Authorization": f"Bearer {KEY}"}

args = sys.argv[1:]
if not args:
    sys.exit(__doc__)
source = args[0]
public = "--public" in args
dest = "iconpage47"
if "--to" in args:
    dest = args[args.index("--to") + 1]


def step(msg):
    print(f"\n>> {msg}", flush=True)


def fail(msg):
    sys.exit(f"\nFAILED: {msg}")


# 1. Pick the video ----------------------------------------------------------
if source.startswith("http"):
    video_url, title = source, "Reposted video"
else:
    step(f"Reading @{source.lstrip('@')}'s video list...")
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist"}
    if COOKIES:
        opts["cookiefile"] = COOKIES
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.tiktok.com/@{source.lstrip('@')}", download=False)
    entries = [e for e in (info or {}).get("entries", []) if e.get("id")]
    if not entries:
        fail("No videos found (TikTok may be blocking you; try TIKTOK_COOKIES_FILE).")
    newest = max(entries, key=lambda e: int(e["id"]) if str(e["id"]).isdigit() else 0)
    video_url = newest.get("url") or f"https://www.tiktok.com/@{source.lstrip('@')}/video/{newest['id']}"
    title = newest.get("title") or "Reposted video"
print(f"Video: {video_url}\nTitle: {title}")

# 2. Download ----------------------------------------------------------------
step("Downloading...")
path = os.path.join(tempfile.gettempdir(), "repost_test.mp4")
if os.path.exists(path):
    os.remove(path)
dl = {"quiet": True, "no_warnings": True, "outtmpl": path, "format": "mp4/best"}
if COOKIES:
    dl["cookiefile"] = COOKIES
try:
    with yt_dlp.YoutubeDL(dl) as ydl:
        ydl.download([video_url])
except Exception as e:
    fail(f"download: {e}")
if not os.path.exists(path) or os.path.getsize(path) < 100_000:
    fail("download produced a missing/tiny file (TikTok blocked it; try cookies).")
print(f"Downloaded {os.path.getsize(path) / 1_000_000:.1f} MB")

# 3. Find the destination account in Zernio -----------------------------------
step(f"Finding '{dest}' in Zernio...")
r = requests.get(f"{BASE}/accounts", headers=H, timeout=30)
if r.status_code != 200:
    fail(f"accounts: HTTP {r.status_code} {r.text[:300]}")
account_id = None
for a in r.json().get("accounts", []):
    if a.get("platform") == "tiktok" and (a.get("username") or "").lstrip("@").lower() == dest.lstrip("@").lower():
        account_id = a["_id"]
if not account_id:
    fail(f"no TikTok account '{dest}' connected in Zernio.")
print(f"accountId: {account_id}")

# 4. Upload to Zernio ----------------------------------------------------------
step("Uploading video to Zernio...")
p = requests.post(f"{BASE}/media/presign", headers={**H, "Content-Type": "application/json"}, timeout=30,
                  json={"filename": "repost_test.mp4", "contentType": "video/mp4"})
if p.status_code >= 300:
    fail(f"presign: HTTP {p.status_code} {p.text[:300]}")
info = p.json()
with open(path, "rb") as f:
    put = requests.put(info["uploadUrl"], headers={"Content-Type": "video/mp4"}, data=f, timeout=900)
if put.status_code >= 300:
    fail(f"upload: HTTP {put.status_code} {put.text[:300]}")
print("Uploaded.")

# 5. Post to the new TikTok ----------------------------------------------------
step(f"Posting to TikTok ({dest}) as {'PUBLIC' if public else 'DRAFT'}...")
body = {
    "content": title[:2200],
    "publishNow": True,
    "platforms": [{"platform": "tiktok", "accountId": account_id}],
    "mediaItems": [{"type": "video", "url": info["publicUrl"]}],
    "tiktokSettings": {
        "draft": not public,
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "allow_comment": True,
        "allow_duet": True,
        "allow_stitch": True,
        "commercial_content_type": "none",
        "content_preview_confirmed": True,
        "express_consent_given": True,
        "media_type": "video",
        "auto_add_music": False,
        "video_made_with_ai": False,
    },
}
r = requests.post(f"{BASE}/posts", headers={**H, "Content-Type": "application/json"}, json=body, timeout=60)
print(f"HTTP {r.status_code}")
try:
    print(json.dumps(r.json(), indent=2)[:1500])
except Exception:
    print(r.text[:1500])
if r.status_code >= 300:
    fail("Zernio rejected the post (see above).")

os.remove(path)
print("\nDONE. Check your TikTok app: " + ("the new post." if public else "inbox/drafts."))
