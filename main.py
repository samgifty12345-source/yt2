import os
import time
import json
import re
import requests
import threading
import subprocess
import tempfile
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote

# API Keys & Credentials
TIKTOK_ACCOUNT = os.environ.get("TIKTOK_ACCOUNT", "@iconspage1")
INSTAGRAM_ACCESS_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
INSTAGRAM_BUSINESS_ACCOUNT_ID = os.environ.get("INSTAGRAM_BUSINESS_ACCOUNT_ID", "")
FACEBOOK_ACCESS_TOKEN = os.environ.get("FACEBOOK_ACCESS_TOKEN", "")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID", "")
SNAPCHAT_ACCESS_TOKEN = os.environ.get("SNAPCHAT_ACCESS_TOKEN", "")
SNAPCHAT_PIXEL_ID = os.environ.get("SNAPCHAT_PIXEL_ID", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")
YOUTUBE_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "")  # jason_animation channel

# Polling interval (check TikTok every N seconds)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))  # 5 minutes

WORK_DIR = tempfile.gettempdir()
HISTORY_FILE = "posted_tiktoks.txt"  # Track which TikToks we've already posted

pipeline_log = ["TikTok repost bot ready. Monitoring @iconspage1..."]
log_lock = threading.Lock()

def log(msg):
    print(msg, flush=True)
    with log_lock:
        pipeline_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        if len(pipeline_log) > 100:
            pipeline_log.pop(0)

def get_posted_videos():
    """Load list of already-posted TikTok video IDs to avoid duplicates."""
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE) as f:
        return set(line.strip() for line in f if line.strip())

def mark_as_posted(video_id):
    """Record that we've posted this video."""
    with open(HISTORY_FILE, "a") as f:
        f.write(f"{video_id}\n")

# ============================================================================
# TikTok Download (using yt-dlp - most reliable)
# ============================================================================

def download_tiktok_video(video_url):
    """Download TikTok video at highest quality using yt-dlp.
    Returns: (video_path, audio_path, title, hashtags)"""
    try:
        log(f"  Downloading TikTok video: {video_url}")
        
        # Use yt-dlp to get highest quality
        output_template = os.path.join(WORK_DIR, "tiktok_%(id)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "-f", "best[ext=mp4]",  # Highest quality MP4
            "--write-info-json",
            "-o", output_template,
            video_url
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            log(f"    yt-dlp error: {result.stderr}")
            return None, None, None, None
        
        # Find the downloaded file
        video_files = sorted(Path(WORK_DIR).glob("tiktok_*.mp4"))
        if not video_files:
            log("    No video file found after download")
            return None, None, None, None
        
        video_path = str(video_files[-1])
        
        # Try to extract metadata from info JSON
        info_path = video_path.replace(".mp4", ".info.json")
        title, hashtags = "TikTok Video", []
        
        if os.path.exists(info_path):
            with open(info_path) as f:
                info = json.load(f)
                title = info.get("title", "TikTok Video")[:100]
                # Extract hashtags from description
                desc = info.get("description", "")
                hashtags = re.findall(r"#\w+", desc)
        
        log(f"    Downloaded: {video_path}")
        log(f"    Title: {title}")
        log(f"    Hashtags: {', '.join(hashtags)}")
        
        return video_path, title, hashtags, info_path
        
    except Exception as e:
        log(f"    Download failed: {e}")
        return None, None, None, None

# ============================================================================
# Upload Functions
# ============================================================================

def upload_to_instagram(video_path, title, hashtags):
    """Upload to Instagram using Meta Graph API."""
    if not INSTAGRAM_ACCESS_TOKEN or not INSTAGRAM_BUSINESS_ACCOUNT_ID:
        log("  Skipping Instagram (no credentials)")
        return False
    
    try:
        log("  Uploading to Instagram...")
        
        caption = f"{title}\n\n{' '.join(hashtags)}"
        
        # Upload video to Instagram
        url = f"https://graph.instagram.com/{INSTAGRAM_BUSINESS_ACCOUNT_ID}/media"
        
        with open(video_path, "rb") as f:
            files = {"media_source": f}
            data = {
                "caption": caption,
                "media_type": "VIDEO",
                "access_token": INSTAGRAM_ACCESS_TOKEN
            }
            res = requests.post(url, files=files, data=data, timeout=120)
        
        if res.status_code in (200, 201):
            media_id = res.json().get("id")
            log(f"    Posted to Instagram (ID: {media_id})")
            return True
        else:
            log(f"    Instagram error {res.status_code}: {res.text[:300]}")
            return False
    except Exception as e:
        log(f"    Instagram upload failed: {e}")
        return False

def upload_to_facebook(video_path, title, hashtags):
    """Upload to Facebook using Meta Graph API."""
    if not FACEBOOK_ACCESS_TOKEN or not FACEBOOK_PAGE_ID:
        log("  Skipping Facebook (no credentials)")
        return False
    
    try:
        log("  Uploading to Facebook...")
        
        caption = f"{title}\n\n{' '.join(hashtags)}"
        
        url = f"https://graph.facebook.com/{FACEBOOK_PAGE_ID}/videos"
        
        with open(video_path, "rb") as f:
            files = {"source": f}
            data = {
                "title": title,
                "description": caption,
                "access_token": FACEBOOK_ACCESS_TOKEN
            }
            res = requests.post(url, files=files, data=data, timeout=120)
        
        if res.status_code in (200, 201):
            video_id = res.json().get("id")
            log(f"    Posted to Facebook (ID: {video_id})")
            return True
        else:
            log(f"    Facebook error {res.status_code}: {res.text[:300]}")
            return False
    except Exception as e:
        log(f"    Facebook upload failed: {e}")
        return False

def upload_to_snapchat(video_path, title, hashtags):
    """Upload to Snapchat using Snapchat Marketing API."""
    if not SNAPCHAT_ACCESS_TOKEN or not SNAPCHAT_PIXEL_ID:
        log("  Skipping Snapchat (no credentials)")
        return False
    
    try:
        log("  Uploading to Snapchat...")
        
        caption = f"{title}\n\n{' '.join(hashtags)}"
        
        # Snapchat's Media Library API
        url = "https://adsapi.snapchat.com/v1/media/upload"
        
        with open(video_path, "rb") as f:
            files = {"file": f}
            headers = {
                "Authorization": f"Bearer {SNAPCHAT_ACCESS_TOKEN}"
            }
            data = {
                "name": title
            }
            res = requests.post(url, files=files, headers=headers, data=data, timeout=120)
        
        if res.status_code in (200, 201):
            media_id = res.json().get("media", {}).get("id")
            log(f"    Posted to Snapchat (ID: {media_id})")
            return True
        else:
            log(f"    Snapchat error {res.status_code}: {res.text[:300]}")
            return False
    except Exception as e:
        log(f"    Snapchat upload failed: {e}")
        return False

def upload_to_youtube(video_path, title, hashtags):
    """Upload to YouTube using Google API."""
    if not YOUTUBE_REFRESH_TOKEN or not YOUTUBE_CLIENT_ID or not YOUTUBE_CLIENT_SECRET:
        log("  Skipping YouTube (no credentials)")
        return False
    
    try:
        log("  Uploading to YouTube...")
        
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        
        creds = Credentials(
            token=None,
            refresh_token=YOUTUBE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=YOUTUBE_CLIENT_ID,
            client_secret=YOUTUBE_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/youtube.upload"]
        )
        creds.refresh(Request())
        
        youtube = build("youtube", "v3", credentials=creds)
        
        description = f"{title}\n\n{' '.join(hashtags)}"
        
        body = {
            "snippet": {
                "title": title[:100],
                "description": description,
                "categoryId": "20"  # Shorts category
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False
            }
        }
        
        media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        
        response = None
        while response is None:
            status, response = req.next_chunk()
        
        video_id = response.get("id")
        log(f"    Posted to YouTube: https://youtube.com/watch?v={video_id}")
        return True
        
    except Exception as e:
        log(f"    YouTube upload failed: {e}")
        return False

# ============================================================================
# TikTok Monitoring
# ============================================================================

def get_tiktok_video_urls():
    """Fetch recent videos from TikTok account using web scraping.
    Returns list of (video_url, video_id) tuples."""
    try:
        log(f"Checking TikTok account {TIKTOK_ACCOUNT}...")
        
        # Using unofficial TikTok API / scraper
        # For production, use: https://github.com/davidteather/TikTok-Api
        # or install: pip install TikTokApi
        
        # Fallback: Use yt-dlp to list videos from profile
        account_url = f"https://www.tiktok.com/{TIKTOK_ACCOUNT}"
        
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "-j",
            account_url
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log(f"  Error fetching TikTok profile: {result.stderr[:200]}")
            return []
        
        videos = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                try:
                    entry = json.loads(line)
                    url = entry.get("url") or f"https://www.tiktok.com/@{TIKTOK_ACCOUNT}/video/{entry.get('id')}"
                    video_id = entry.get("id", "").split("_")[-1]
                    videos.append((url, video_id))
                except:
                    pass
        
        log(f"  Found {len(videos)} videos")
        return videos
        
    except Exception as e:
        log(f"  Error fetching TikTok videos: {e}")
        return []

def process_video(video_url, video_id):
    """Download and repost a single TikTok video to all platforms."""
    log(f"\nProcessing TikTok video {video_id}...")
    
    video_path = None
    try:
        # Download
        video_path, title, hashtags, info_path = download_tiktok_video(video_url)
        if not video_path:
            return False
        
        log(f"  Reposting to all platforms...")
        
        # Upload to each platform
        upload_to_instagram(video_path, title, hashtags)
        upload_to_facebook(video_path, title, hashtags)
        upload_to_snapchat(video_path, title, hashtags)
        upload_to_youtube(video_path, title, hashtags)
        
        # Mark as done
        mark_as_posted(video_id)
        log(f"  ✓ Completed: {title}")
        return True
        
    except Exception as e:
        log(f"  Error processing video: {e}")
        return False
    finally:
        # Cleanup
        for f in [video_path, info_path]:
            try:
                if f and os.path.exists(f):
                    os.remove(f)
            except:
                pass

def monitoring_loop():
    """Main loop: periodically check TikTok and repost new videos."""
    posted = get_posted_videos()
    log(f"Loaded history: {len(posted)} videos already posted")
    
    while True:
        try:
            videos = get_tiktok_video_urls()
            
            for url, video_id in videos:
                if video_id not in posted:
                    if process_video(url, video_id):
                        posted.add(video_id)
            
            log(f"Waiting {POLL_INTERVAL}s until next check...")
            time.sleep(POLL_INTERVAL)
            
        except Exception as e:
            log(f"Monitoring loop error: {e}")
            time.sleep(60)

# ============================================================================
# Web Dashboard
# ============================================================================

PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TikTok Repost Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: -apple-system, sans-serif; background: #0b0b10; color: #eaeaf2; padding: 20px; }
  .wrap { max-width: 900px; margin: 0 auto; }
  h1 { margin-top: 0; }
  .card { background: #14141c; border: 1px solid #26263a; border-radius: 12px; padding: 20px; margin: 20px 0; }
  .log { background: #08080d; border: 1px solid #26263a; border-radius: 8px; padding: 12px; 
         font-family: monospace; font-size: 12px; color: #8fe3a8; max-height: 400px; 
         overflow-y: auto; white-space: pre-wrap; line-height: 1.5; }
  .status { color: #35d488; font-weight: bold; }
  .error { color: #ff3b5c; }
</style>
</head>
<body>
<div class="wrap">
  <h1>🎵 TikTok → All Platforms Repost Bot</h1>
  
  <div class="card">
    <h2>Status</h2>
    <p><span class="status">✓ Monitoring:</span> @@ACCOUNT@@</p>
    <p>Polling interval: @@INTERVAL@@s | Posted: @@POSTED_COUNT@@</p>
  </div>
  
  <div class="card">
    <h2>Platforms Connected</h2>
    <p>✓ Instagram: @@INSTAGRAM@@</p>
    <p>✓ Facebook: @@FACEBOOK@@</p>
    <p>✓ Snapchat: @@SNAPCHAT@@</p>
    <p>✓ YouTube: @@YOUTUBE@@</p>
  </div>
  
  <div class="card">
    <h2>Activity Log</h2>
    <div class="log">@@LOG@@</div>
  </div>
</div>
</body>
</html>"""

def render_dashboard():
    with log_lock:
        log_text = "\n".join(pipeline_log[-50:])
    
    posted = len(get_posted_videos())
    
    html = PAGE_TEMPLATE
    html = html.replace("@@ACCOUNT@@", TIKTOK_ACCOUNT)
    html = html.replace("@@INTERVAL@@", str(POLL_INTERVAL))
    html = html.replace("@@POSTED_COUNT@@", str(posted))
    html = html.replace("@@INSTAGRAM@@", "Configured" if INSTAGRAM_ACCESS_TOKEN else "Not set up")
    html = html.replace("@@FACEBOOK@@", "Configured" if FACEBOOK_ACCESS_TOKEN else "Not set up")
    html = html.replace("@@SNAPCHAT@@", "Configured" if SNAPCHAT_ACCESS_TOKEN else "Not set up")
    html = html.replace("@@YOUTUBE@@", "Configured" if YOUTUBE_REFRESH_TOKEN else "Not set up")
    html = html.replace("@@LOG@@", log_text)
    
    return html

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        html = render_dashboard()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        body = html.encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    
    def log_message(self, *args):
        pass

def start_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

# ============================================================================
# Main
# ============================================================================

def main():
    threading.Thread(target=start_server, daemon=True).start()
    threading.Thread(target=monitoring_loop, daemon=True).start()
    
    log("TikTok Repost Bot started")
    log(f"Monitoring: {TIKTOK_ACCOUNT}")
    log(f"Posting to: Instagram, Facebook, Snapchat, YouTube")
    
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
