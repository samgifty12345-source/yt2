import os
import time
import json
import subprocess
import tempfile
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

import yt_dlp
import requests

WORK_DIR = tempfile.gettempdir()
PREVIEW_DIR = os.path.join(WORK_DIR, "ugc_previews")
os.makedirs(PREVIEW_DIR, exist_ok=True)

USED_VIDEOS_FILE = "used_videos.json"       # {"base": [id,id,...], "reaction": [id,id,...]}
PREVIEWS_FILE = "previews.json"             # list of finished preview metadata, newest first

TIKTOK_COOKIES_FILE = os.environ.get("TIKTOK_COOKIES_FILE", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

MIN_VALID_FILE_BYTES = int(os.environ.get("MIN_VALID_FILE_BYTES", "300000"))
LOOKBACK_COUNT = int(os.environ.get("LOOKBACK_COUNT", "20"))  # how many recent posts to scan per handle

# Canvas is fixed to vertical 9:16 (TikTok/Reels/Shorts).
CANVAS_W = int(os.environ.get("CANVAS_W", "1080"))
CANVAS_H = int(os.environ.get("CANVAS_H", "1920"))

# --- Timeline knobs -----------------------------------------------------
# Phase 1: reaction clip plays alone for this many seconds - bottom-anchored,
# roughly half the screen height ("watch this" hook).
HOOK_SECONDS = float(os.environ.get("HOOK_SECONDS", "3"))
HOOK_HEIGHT_PCT = float(os.environ.get("HOOK_HEIGHT_PCT", "0.5"))  # fraction of canvas height

# Phase 2: reaction shrinks to a small box, vertically centered, right side,
# while the base video plays fullscreen behind it. Runs exactly as long as
# the base video does.
PIP_WIDTH_PCT = float(os.environ.get("PIP_WIDTH_PCT", "0.36"))    # fraction of canvas width
PIP_RIGHT_MARGIN = int(os.environ.get("PIP_RIGHT_MARGIN", "24"))  # px gap from right edge

FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "2")

log_lock = threading.Lock()
status_lock = threading.Lock()
status_feed = []

pipeline_state_lock = threading.Lock()
pipeline_running = False


def log(msg):
    print(msg, flush=True)


def status(msg):
    print(f"[status] {msg}", flush=True)
    with status_lock:
        status_feed.append(msg)
        if len(status_feed) > 60:
            status_feed.pop(0)


config_lock = threading.Lock()
CONFIG = {
    "base_username": os.environ.get("BASE_TIKTOK_USERNAME", "").strip().lstrip("@"),
    "reaction_username": os.environ.get("REACTION_TIKTOK_USERNAME", "").strip().lstrip("@"),
}


def get_config():
    with config_lock:
        return dict(CONFIG)


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return default
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_used_videos():
    return load_json(USED_VIDEOS_FILE, {"base": [], "reaction": []})


def mark_used(pool, video_id):
    used = get_used_videos()
    used.setdefault(pool, [])
    if video_id not in used[pool]:
        used[pool].append(video_id)
    save_json(USED_VIDEOS_FILE, used)


def get_previews():
    return load_json(PREVIEWS_FILE, [])


def add_preview(entry):
    previews = get_previews()
    previews.insert(0, entry)
    previews = previews[:30]  # keep the gallery light
    save_json(PREVIEWS_FILE, previews)


# ---------------------------------------------------------------------------
# TikTok fetching (same approach as the TikTok->YouTube reposter bot)
# ---------------------------------------------------------------------------

def get_recent_tiktok_videos(username, limit=LOOKBACK_COUNT):
    profile_url = f"https://www.tiktok.com/@{username}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": limit,
    }
    if TIKTOK_COOKIES_FILE:
        ydl_opts["cookiefile"] = TIKTOK_COOKIES_FILE

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(profile_url, download=False)

    entries = info.get("entries") if info else None
    if not entries:
        return []

    videos = []
    for entry in entries:
        video_id = entry.get("id")
        if not video_id:
            continue
        videos.append({
            "id": video_id,
            "title": entry.get("title") or f"TikTok video {video_id}",
            "url": entry.get("url") or f"https://www.tiktok.com/@{username}/video/{video_id}",
        })
    return videos


def probe_duration_seconds(filepath):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        return float(result.stdout.strip())
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
        return None


def download_tiktok_video(video_url, filepath):
    log(f"  Fetching: {video_url}")
    dl_opts = {
        "quiet": True, "no_warnings": True,
        "outtmpl": filepath, "format": "mp4/best",
    }
    if TIKTOK_COOKIES_FILE:
        dl_opts["cookiefile"] = TIKTOK_COOKIES_FILE
    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.download([video_url])

    if not os.path.exists(filepath):
        raise RuntimeError("Download reported success but no file was written")
    size = os.path.getsize(filepath)
    if size < MIN_VALID_FILE_BYTES:
        os.remove(filepath)
        raise RuntimeError(f"Downloaded file too small ({size} bytes) - likely blocked by TikTok.")
    duration = probe_duration_seconds(filepath)
    if duration is None or duration < 1.0:
        if os.path.exists(filepath):
            os.remove(filepath)
        raise RuntimeError("Downloaded file isn't a valid/complete video - likely blocked by TikTok.")
    log(f"  Download OK ({size / 1_000_000:.1f} MB, {duration:.1f}s)")
    return duration


def pick_unused_video(username, pool):
    """Random unused pick from the given handle's recent videos, for the given pool
    ('base' or 'reaction'). Returns None if everything recent has already been used."""
    used = get_used_videos().get(pool, [])
    videos = get_recent_tiktok_videos(username, limit=LOOKBACK_COUNT)
    candidates = [v for v in videos if v["id"] not in used]
    if not candidates:
        return None
    import random
    return random.choice(candidates)


# ---------------------------------------------------------------------------
# Caption / hashtag generation (Groq - same provider as the AI history bot)
# ---------------------------------------------------------------------------

def generate_caption_and_hashtags(base_title):
    if not GROQ_API_KEY:
        return "New one dropped 👀", "#fyi #factcheck #ai"
    prompt = f"""You write short, punchy captions for a UGC-style reaction video on TikTok/Instagram.
The reaction is a "let's fact-check this claim" format. The base clip being reacted to is
titled/described: "{base_title}"

Return ONLY valid JSON, no markdown fences:
{{"caption": "one short punchy caption line, under 100 chars", "hashtags": "5-8 space-separated lowercase hashtags with # symbol"}}"""
    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.8,
                "max_tokens": 300,
                "reasoning_effort": "low",
            },
            timeout=30,
        )
        res.raise_for_status()
        content = res.json()["choices"][0]["message"]["content"].strip()
        content = content.replace("```json", "").replace("```", "").strip()
        data = json.loads(content)
        return data.get("caption", ""), data.get("hashtags", "")
    except Exception as e:
        log(f"  Caption generation failed, using fallback: {e}")
        return "New one dropped 👀", "#fyi #factcheck #ai"


# ---------------------------------------------------------------------------
# Video composition
# ---------------------------------------------------------------------------

def build_reaction_ad(base_path, base_dur, reaction_path, reaction_dur, output_path):
    """
    Timeline:
      Phase 1 (0 -> hook):            reaction only, bottom-anchored, ~half screen height.
      Phase 2 (hook -> hook+pip_dur):  base fullscreen behind; reaction shrunk to a small
                                       box, vertically centered, right side. Audio = base only.
      Phase 3 (hook+pip_dur -> end):  reaction fullscreen for the rest of its own runtime
                                       (this is where your baked-in reveal line + CTA live).

    pip_dur is normally == base_dur (base plays out in full during the PiP phase). If the
    reaction clip isn't long enough to cover hook + base_dur, pip_dur is clamped down to
    whatever reaction footage is actually available, and the base video gets cut short to
    match (logged so it's obvious this happened).
    """
    hook = HOOK_SECONDS
    if reaction_dur <= hook + 0.5:
        # Reaction clip too short for a real hook+reveal split - shrink the hook instead
        # of failing outright.
        hook = max(0.5, reaction_dur * 0.25)
        log(f"  Reaction clip is short ({reaction_dur:.1f}s) - reducing hook to {hook:.1f}s")

    available_for_pip = max(0.5, reaction_dur - hook)
    pip_dur = min(base_dur, available_for_pip)
    if pip_dur < base_dur:
        log(f"  Base video ({base_dur:.1f}s) longer than available reaction footage - "
            f"trimming base to {pip_dur:.1f}s for the PiP phase.")

    hook_h = int(CANVAS_H * HOOK_HEIGHT_PCT)
    pip_w = int(CANVAS_W * PIP_WIDTH_PCT)

    filter_complex = (
        # Phase 1: reaction clip, bottom-anchored, half height, on a black canvas
        f"color=c=black:s={CANVAS_W}x{CANVAS_H}:d={hook}[bg1];"
        f"[1:v]trim=0:{hook},setpts=PTS-STARTPTS,scale=-2:{hook_h}[r1];"
        f"[bg1][r1]overlay=(main_w-overlay_w)/2:main_h-overlay_h[p1v];"
        f"[1:a]atrim=0:{hook},asetpts=PTS-STARTPTS[p1a];"

        # Phase 2: base fullscreen (cropped to fill canvas) + reaction PiP, right-center
        f"[0:v]trim=0:{pip_dur},setpts=PTS-STARTPTS,"
        f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase,"
        f"crop={CANVAS_W}:{CANVAS_H}[p2bg];"
        f"[1:v]trim={hook}:{hook + pip_dur},setpts=PTS-STARTPTS,scale={pip_w}:-2[p2pip];"
        f"[p2bg][p2pip]overlay=main_w-overlay_w-{PIP_RIGHT_MARGIN}:(main_h-overlay_h)/2[p2v];"
        f"[0:a]atrim=0:{pip_dur},asetpts=PTS-STARTPTS[p2a];"

        # Phase 3: reaction fullscreen for the remainder of its own runtime
        f"[1:v]trim={hook + pip_dur}:{reaction_dur},setpts=PTS-STARTPTS,"
        f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase,"
        f"crop={CANVAS_W}:{CANVAS_H}[p3v];"
        f"[1:a]atrim={hook + pip_dur}:{reaction_dur},asetpts=PTS-STARTPTS[p3a];"

        # Stitch all three phases together
        f"[p1v][p1a][p2v][p2a][p3v][p3a]concat=n=3:v=1:a=1[outv][outa]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", base_path,
        "-i", reaction_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-threads", FFMPEG_THREADS,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", FFMPEG_THREADS,
        "-c:a", "aac", "-b:a", "128k",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if result.returncode != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg compose failed (exit {result.returncode}):\n{result.stderr[-3000:]}")
    log(f"  Composed ad ready ({os.path.getsize(output_path) / 1_000_000:.1f} MB)")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline():
    global pipeline_running
    with pipeline_state_lock:
        if pipeline_running:
            log("Generation already running - ignoring this trigger.")
            status("⏳ Already generating one - hang tight.")
            return
        pipeline_running = True

    base_path = reaction_path = output_path = None
    try:
        cfg = get_config()
        base_username = cfg["base_username"]
        reaction_username = cfg["reaction_username"]
        if not base_username or not reaction_username:
            status("❌ Set both TikTok handles (base clips + reaction clips) before generating.")
            return

        status(f"🔍 Picking a fresh base clip from @{base_username}...")
        base_video = pick_unused_video(base_username, "base")
        if not base_video:
            status(f"❌ No unused base clips left from @{base_username} in the last {LOOKBACK_COUNT} posts.")
            return

        status(f"🔍 Picking a fresh reaction clip from @{reaction_username}...")
        reaction_video = pick_unused_video(reaction_username, "reaction")
        if not reaction_video:
            status(f"❌ No unused reaction clips left from @{reaction_username} in the last {LOOKBACK_COUNT} posts.")
            return

        status(f"📥 Downloading base clip ({base_video['id']})...")
        base_path = os.path.join(WORK_DIR, f"base_{base_video['id']}.mp4")
        base_dur = download_tiktok_video(base_video["url"], base_path)

        status(f"📥 Downloading reaction clip ({reaction_video['id']})...")
        reaction_path = os.path.join(WORK_DIR, f"reaction_{reaction_video['id']}.mp4")
        reaction_dur = download_tiktok_video(reaction_video["url"], reaction_path)

        status("🎬 Compositing hook -> PiP -> reveal...")
        output_name = f"ad_{base_video['id']}_{reaction_video['id']}.mp4"
        output_path = os.path.join(PREVIEW_DIR, output_name)
        build_reaction_ad(base_path, base_dur, reaction_path, reaction_dur, output_path)

        status("✍️ Writing caption + hashtags...")
        caption, hashtags = generate_caption_and_hashtags(base_video["title"])

        mark_used("base", base_video["id"])
        mark_used("reaction", reaction_video["id"])

        add_preview({
            "file": output_name,
            "created_at": time.time(),
            "caption": caption,
            "hashtags": hashtags,
            "base_title": base_video["title"],
            "base_url": base_video["url"],
            "reaction_url": reaction_video["url"],
        })
        status(f"✅ Ready to review - new preview added below.")

    except Exception as e:
        log(f"Pipeline error: {e}")
        status(f"❌ Generation failed: {type(e).__name__}: {e}")
    finally:
        for p in (base_path, reaction_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        with pipeline_state_lock:
            pipeline_running = False


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>UGC Reaction Ad Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0b0b10; --panel: #14141c; --panel-2: #1b1b26; --border: #26263a;
    --text: #eaeaf2; --muted: #8a8aa0; --accent: #ff3b5c; --accent-2: #7c5cff; --ok: #35d488;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
    background: radial-gradient(1200px 600px at 10% -10%, rgba(124,92,255,0.18), transparent 60%),
      radial-gradient(1000px 500px at 100% 0%, rgba(255,59,92,0.14), transparent 55%), var(--bg);
    color: var(--text); padding: 32px 20px 60px;
  }
  .wrap { max-width: 1000px; margin: 0 auto; }
  header { display: flex; align-items: center; gap: 14px; margin-bottom: 28px; }
  .logo { width: 42px; height: 42px; border-radius: 12px;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 16px; flex-shrink: 0; }
  h1 { font-size: 22px; margin: 0; letter-spacing: -0.02em; }
  .sub { color: var(--muted); font-size: 13px; margin-top: 2px; }
  .grid { display: grid; grid-template-columns: 1fr; gap: 18px; }
  @media (min-width: 720px) { .grid { grid-template-columns: 1fr 1fr; } }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 16px; padding: 22px; }
  .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin: 0 0 16px; }
  .badges { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  .badge { background: var(--panel-2); border: 1px solid var(--border); border-radius: 999px;
    padding: 6px 12px; font-size: 12.5px; color: var(--muted); }
  .badge b { color: var(--text); }
  .badge.running { color: var(--ok); border-color: rgba(53,212,136,0.35); background: rgba(53,212,136,0.08); }
  .badge.warn { color: var(--accent); border-color: rgba(255,59,92,0.35); background: rgba(255,59,92,0.08); }
  label { display: block; font-size: 12.5px; color: var(--muted); margin: 14px 0 6px; }
  label:first-of-type { margin-top: 0; }
  input[type=text] {
    width: 100%; background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px; color: var(--text); font-size: 14px; font-family: inherit;
  }
  input[type=text]:focus { outline: none; border-color: var(--accent-2); }
  button { width: 100%; margin-top: 18px; background: linear-gradient(135deg, var(--accent), var(--accent-2));
    color: #fff; border: none; padding: 13px; border-radius: 10px; font-size: 14.5px; font-weight: 600;
    cursor: pointer; letter-spacing: 0.01em; }
  button:hover { filter: brightness(1.08); }
  button.secondary { background: var(--panel-2); border: 1px solid var(--border); color: var(--text); }
  .feed { display: flex; flex-direction: column; gap: 8px; max-height: 220px; overflow-y: auto; margin-bottom: 4px; }
  .feed-item { background: #08080d; border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 12px; font-size: 13px; line-height: 1.5; }
  .feed-empty { color: var(--muted); font-size: 13px; padding: 8px 2px; }
  .hint { font-size: 11.5px; color: var(--muted); margin-top: 8px; line-height: 1.5; }
  footer { text-align: center; color: var(--muted); font-size: 11.5px; margin-top: 26px; }
  .previews { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; margin-top: 18px; }
  .preview-card { background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 12px; }
  .preview-card video { width: 100%; border-radius: 10px; background: #000; aspect-ratio: 9/16; }
  .preview-card .cap { font-size: 12.5px; margin-top: 8px; color: var(--text); }
  .preview-card .tags { font-size: 11.5px; color: var(--accent-2); margin-top: 4px; }
  .preview-card .srcs { font-size: 10.5px; color: var(--muted); margin-top: 6px; }
  .preview-card .srcs a { color: var(--muted); }
  .preview-empty { color: var(--muted); font-size: 13px; grid-column: 1/-1; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">UGC</div>
    <div>
      <h1>UGC Reaction Ad Bot</h1>
      <div class="sub">Random base clip + random reaction clip -&gt; auto-composited ad, for your review</div>
    </div>
  </header>

  <div class="grid">
    <div class="card">
      <h2>Status</h2>
      <div class="badges">
        <div class="badge">Rendered <b>@@PREVIEW_COUNT@@</b></div>
        <div class="badge @@RUNNING_CLASS@@">@@RUNNING_TEXT@@</div>
      </div>
      <div class="feed">@@FEED_CONTENT@@</div>
      <form method="POST" action="/generate">
        <button type="submit">Generate New Ad</button>
      </form>
      <div class="hint">
        Posting to TikTok/Instagram is intentionally OFF for now - this only renders the video
        so you can review the cuts before we wire up real accounts.
      </div>
    </div>

    <div class="card">
      <h2>Source Handles</h2>
      <form method="POST" action="/configure">
        <label>Base clips TikTok handle (professor/claim videos)</label>
        <input type="text" name="base_username" value="@@BASE_USERNAME@@" placeholder="username, no @">
        <label>Reaction clips TikTok handle (you)</label>
        <input type="text" name="reaction_username" value="@@REACTION_USERNAME@@" placeholder="username, no @">
        <button type="submit" class="secondary">Save Handles</button>
      </form>
      <div class="hint">
        Each generation picks one unused video from each handle at random and never reuses it.
        Once a handle's recent videos are all used up, add newer posts to that account or raise
        LOOKBACK_COUNT.
      </div>
    </div>
  </div>

  <h2 style="margin-top:28px;">Rendered Previews</h2>
  <div class="previews">
@@PREVIEW_CARDS@@
  </div>

  <footer>Timeline: hook (@@HOOK_SECONDS@@s) &rarr; PiP over base video &rarr; fullscreen reveal</footer>
</div>
</body>
</html>"""


def esc(s):
    return (s or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def render_feed_html():
    with status_lock:
        items = list(status_feed[-20:])
    if not items:
        return '<div class="feed-empty">Nothing yet - click "Generate New Ad".</div>'
    return "\n".join(f'<div class="feed-item">{esc(item)}</div>' for item in reversed(items))


def render_preview_cards():
    previews = get_previews()
    if not previews:
        return '<div class="preview-empty">No renders yet - click "Generate New Ad" to make your first one.</div>'
    cards = []
    for p in previews:
        cards.append(
            f'<div class="preview-card">'
            f'<video controls preload="metadata" src="/previews/{esc(p["file"])}"></video>'
            f'<div class="cap">{esc(p.get("caption", ""))}</div>'
            f'<div class="tags">{esc(p.get("hashtags", ""))}</div>'
            f'<div class="srcs">Base: {esc(p.get("base_title", ""))}<br>'
            f'<a href="{esc(p.get("base_url",""))}" target="_blank">base source</a> &middot; '
            f'<a href="{esc(p.get("reaction_url",""))}" target="_blank">reaction source</a></div>'
            f'</div>'
        )
    return "\n".join(cards)


def render_page():
    cfg = get_config()
    with pipeline_state_lock:
        running = pipeline_running
    previews = get_previews()

    html = PAGE_TEMPLATE
    html = html.replace("@@PREVIEW_COUNT@@", str(len(previews)))
    html = html.replace("@@RUNNING_CLASS@@", "running" if running else "")
    html = html.replace("@@RUNNING_TEXT@@", "Generating..." if running else "Idle")
    html = html.replace("@@FEED_CONTENT@@", render_feed_html())
    html = html.replace("@@BASE_USERNAME@@", esc(cfg["base_username"]))
    html = html.replace("@@REACTION_USERNAME@@", esc(cfg["reaction_username"]))
    html = html.replace("@@PREVIEW_CARDS@@", render_preview_cards())
    html = html.replace("@@HOOK_SECONDS@@", str(HOOK_SECONDS))
    return html


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/previews/"):
            filename = os.path.basename(urllib.parse.unquote(self.path[len("/previews/"):]))
            filepath = os.path.join(PREVIEW_DIR, filename)
            if os.path.exists(filepath):
                with open(filepath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()
            return

        html = render_page()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        body = html.encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/configure":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode() if length else ""
            fields = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}
            with config_lock:
                CONFIG["base_username"] = fields.get("base_username", "").strip().lstrip("@")
                CONFIG["reaction_username"] = fields.get("reaction_username", "").strip().lstrip("@")
            log(f"Handles updated -> base=@{CONFIG['base_username']}, reaction=@{CONFIG['reaction_username']}")
        elif self.path == "/generate":
            log("Manual generate triggered.")
            threading.Thread(target=run_pipeline, daemon=True).start()

        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def start_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


def main():
    if TIKTOK_COOKIES_FILE:
        if os.path.exists(TIKTOK_COOKIES_FILE):
            log(f"Cookies file found at '{TIKTOK_COOKIES_FILE}'.")
        else:
            log(f"WARNING: TIKTOK_COOKIES_FILE set to '{TIKTOK_COOKIES_FILE}' but missing.")
    else:
        log("WARNING: No TIKTOK_COOKIES_FILE set - requests are unauthenticated and more likely blocked.")

    if not GROQ_API_KEY:
        log("WARNING: No GROQ_API_KEY set - captions/hashtags will use a generic fallback.")

    threading.Thread(target=start_server, daemon=True).start()
    log("UGC reaction ad bot started. Set your two TikTok handles on the dashboard, then click Generate.")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
