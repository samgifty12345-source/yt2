"""Zernio test. Not part of the bot.

Usage:
  set ZERNIO_API_KEY=your_key            (Windows cmd)
  export ZERNIO_API_KEY=your_key         (Mac/Linux)

  python test_zernio.py                  -> read-only: lists your connected accounts
  python test_zernio.py video.mp4        -> uploads video, sends it to TikTok DRAFTS
"""
import os
import sys
import json
import requests

KEY = os.environ.get("ZERNIO_API_KEY")
if not KEY:
    sys.exit("Set ZERNIO_API_KEY first.")

BASE = "https://zernio.com/api/v1"
H = {"Authorization": f"Bearer {KEY}"}


def show(label, r):
    print(f"\n== {label}: HTTP {r.status_code}")
    try:
        print(json.dumps(r.json(), indent=2)[:1500])
    except Exception:
        print(r.text[:1500])


# 1. Read-only auth check
r = requests.get(f"{BASE}/accounts", headers=H, timeout=30)
show("list accounts", r)
if r.status_code != 200:
    sys.exit("Auth/connectivity failed.")

accounts = r.json().get("accounts", [])
tt = [a for a in accounts if a.get("platform") == "tiktok"]
if not tt:
    sys.exit("No TikTok account connected in Zernio.")
account_id = tt[0]["_id"]
print(f"\nTikTok accountId: {account_id}")

if len(sys.argv) < 2:
    sys.exit("Auth OK. Pass a video path to test a draft upload.")

path = sys.argv[1]

# 2. Upload the video
p = requests.post(f"{BASE}/media/presign", headers=H, timeout=30,
                  json={"filename": os.path.basename(path), "contentType": "video/mp4"})
show("presign", p)
p.raise_for_status()
info = p.json()
with open(path, "rb") as f:
    put = requests.put(info["uploadUrl"], headers={"Content-Type": "video/mp4"}, data=f, timeout=600)
print(f"\n== upload PUT: HTTP {put.status_code}")
put.raise_for_status()

# 3. Send to TikTok drafts (tiktokSettings.draft = true)
body = {
    "content": "test draft",
    "publishNow": True,
    "platforms": [{"platform": "tiktok", "accountId": account_id}],
    "mediaItems": [{"type": "video", "url": info["publicUrl"]}],
    "tiktokSettings": {
        "draft": True,
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
show("create draft post", r)
print("\nNow open the TikTok app and check your inbox/drafts.")
