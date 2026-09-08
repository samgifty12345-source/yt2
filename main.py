import os
import time
import json
import random
import base64
import tempfile
import requests
import subprocess
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from huggingface_hub import InferenceClient

WORK_DIR = tempfile.gettempdir()
DONE_FILE = "done_history.txt"
REFERENCE_IMAGE_PATH_FILE = "reference_image_path.txt"  # persists chosen ref image across restarts

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "")

# Stability AI (https://platform.stability.ai) - fallback #2 for image gen,
# used if all HF providers fail. Uses SDXL 1.0 - Stability's cheapest model
# (from ~0.9 credits/gen, vs 3 for Stable Image Core), since credits are tight.
STABILITY_API_KEY = os.environ.get("STABILITY_API_KEY", "")
STABILITY_SDXL_ENGINE = os.environ.get("STABILITY_SDXL_ENGINE", "stable-diffusion-xl-1024-v1-0")
STABILITY_SDXL_ENDPOINT = f"https://api.stability.ai/v1/generation/{STABILITY_SDXL_ENGINE}/text-to-image"

# The style-reference control tool (used to keep a consistent character/style
# across scenes) is a separate, pricier Stability product from SDXL - there is
# no "cheap" version of it, so it's left as-is and only kicks in when a
# reference image is actually available.
STABILITY_STYLE_ENDPOINT = "https://api.stability.ai/v2beta/stable-image/control/style"

# SDXL 1.0 only accepts a fixed set of width/height pairs. Pick the closest
# match to each orientation's aspect ratio.
SDXL_DIMS = {
    "shorts": (640, 1536),   # closest to 9:16
    "long":   (1344, 768),   # closest to 16:9
}

# Fish Audio TTS (https://docs.fish.audio) - primary narration voice.
FISH_AUDIO_API_KEY = os.environ.get("FISH_AUDIO_API_KEY", "")
FISH_AUDIO_VOICE_ID = os.environ.get("FISH_AUDIO_VOICE_ID", "")
FISH_AUDIO_MODEL = os.environ.get("FISH_AUDIO_MODEL", "s2.1-pro-free")

DEFAULT_NICHE = os.environ.get(
    "NICHE_PROMPT",
    "Ancient history and forgotten historical events, told dramatically ."
)

STYLE_SUFFIX = os.environ.get(
    "STYLE_SUFFIX",
    "flat 2D cartoon illustration style, muted earth tones, thick black outlines. "
    "Only the named recurring character(s) described in the prompt should keep a consistent "
    "face/appearance across scenes - every other person shown (background characters, crowds, "
    "unnamed figures) must have a distinct, varied face and appearance, not reused from other scenes."
)

# How often autopilot posts once it's running on its own schedule.
AUTOPILOT_INTERVAL_HOURS = float(os.environ.get("AUTOPILOT_INTERVAL_HOURS", "24"))

# How long the bot waits after boot before its FIRST unattended run, so you
# have a window to open the site and set topic/duration/orientation first.
# Visiting the site and hitting "Generate Now" at any point cancels the wait.
STARTUP_WAIT_HOURS = float(os.environ.get("STARTUP_WAIT_HOURS", "2"))

HF_IMAGE_MODEL = os.environ.get("HF_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell")

HF_IMAGE_PROVIDERS = [
    p.strip() for p in os.environ.get("HF_IMAGE_PROVIDERS", "nscale,fal-ai,hf-inference").split(",")
    if p.strip()
]

POLLINATIONS_TOKEN = os.environ.get("POLLINATIONS_TOKEN", "")

# Point this at a direct URL to an mp3 you actually own/have rights to use
# (e.g. a file hosted in your own GitHub repo - NOT a signed/expiring link
# from a site like audio.com, and NOT a track marked "All Rights Reserved").
BACKGROUND_MUSIC_URL = os.environ.get("BACKGROUND_MUSIC_URL", "")
BACKGROUND_MUSIC_VOLUME = float(os.environ.get("BACKGROUND_MUSIC_VOLUME", "0.32"))

# How strongly generated scenes should match the reference image's style
# (0 = ignore reference, 1 = match it very closely). 0.5 is a good default:
# strong enough for a consistent look, loose enough to still vary per scene.
STYLE_FIDELITY = float(os.environ.get("STYLE_FIDELITY", "0.5"))

SCENE_SECONDS = 10  # each scene/image is on screen this long

# Orientation presets: (width, height, description used in prompts)
# stability_ar is the closest supported Stability aspect_ratio for that shape
# (used by the style-reference control endpoint, which does support arbitrary
# aspect ratios - unlike SDXL below).
ORIENTATIONS = {
    "shorts": {"w": 1080, "h": 1920, "aspect_text": "vertical 9:16", "max_seconds": 180, "stability_ar": "9:16"},
    "long":   {"w": 1920, "h": 1080, "aspect_text": "horizontal 16:9", "max_seconds": 600, "stability_ar": "16:9"},
}

pipeline_log = ["History bot ready. Waiting for the startup window or a manual trigger."]
log_lock = threading.Lock()

pipeline_state_lock = threading.Lock()
pipeline_running = False

trigger_event = threading.Event()
next_run_at = [time.time() + STARTUP_WAIT_HOURS * 3600]  # mutable box for cross-thread read

config_lock = threading.Lock()
CONFIG = {
    "topic": DEFAULT_NICHE,
    "duration_seconds": 60,
    "orientation": "shorts",
    "reference_image_path": None,  # user-uploaded character/style reference, or None
}

# Load a persisted reference image path (if the process restarted) so uploads survive reboots.
if os.path.exists(REFERENCE_IMAGE_PATH_FILE):
    with open(REFERENCE_IMAGE_PATH_FILE) as f:
        _saved_ref = f.read().strip()
    if _saved_ref and os.path.exists(_saved_ref):
        CONFIG["reference_image_path"] = _saved_ref


def log(msg):
    print(msg, flush=True)
    with log_lock:
        pipeline_log.append(msg)
        if len(pipeline_log) > 80:
            pipeline_log.pop(0)


def get_config():
    with config_lock:
        return dict(CONFIG)


def get_google_creds(scopes):
    return Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=scopes,
    )


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AI History Shorts Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0b0b10;
    --panel: #14141c;
    --panel-2: #1b1b26;
    --border: #26263a;
    --text: #eaeaf2;
    --muted: #8a8aa0;
    --accent: #ff3b5c;
    --accent-2: #7c5cff;
    --ok: #35d488;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
    background:
      radial-gradient(1200px 600px at 10% -10%, rgba(124,92,255,0.18), transparent 60%),
      radial-gradient(1000px 500px at 100% 0%, rgba(255,59,92,0.14), transparent 55%),
      var(--bg);
    color: var(--text);
    padding: 32px 20px 60px;
  }
  .wrap { max-width: 880px; margin: 0 auto; }
  header { display: flex; align-items: center; gap: 14px; margin-bottom: 28px; }
  .logo {
    width: 42px; height: 42px; border-radius: 12px;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 18px; flex-shrink: 0;
  }
  h1 { font-size: 22px; margin: 0; letter-spacing: -0.02em; }
  .sub { color: var(--muted); font-size: 13px; margin-top: 2px; }
  .grid { display: grid; grid-template-columns: 1fr; gap: 18px; }
  @media (min-width: 720px) { .grid { grid-template-columns: 1fr 1fr; } }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 22px;
  }
  .card h2 {
    font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--muted); margin: 0 0 16px;
  }
  .badges { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  .badge {
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 999px; padding: 6px 12px; font-size: 12.5px; color: var(--muted);
  }
  .badge b { color: var(--text); }
  .badge.running { color: var(--ok); border-color: rgba(53,212,136,0.35); background: rgba(53,212,136,0.08); }
  label { display: block; font-size: 12.5px; color: var(--muted); margin: 14px 0 6px; }
  label:first-of-type { margin-top: 0; }
  textarea, input[type=number] {
    width: 100%; background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px; color: var(--text); font-size: 14px;
    font-family: inherit; resize: vertical;
  }
  textarea:focus, input:focus { outline: none; border-color: var(--accent-2); }
  .row { display: flex; gap: 10px; align-items: center; }
  .row input[type=number] { width: 90px; }
  .toggle { display: flex; gap: 8px; margin-top: 4px; }
  .toggle label {
    flex: 1; margin: 0; text-align: center; cursor: pointer;
    background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px; font-size: 13px; color: var(--muted); transition: all .15s;
  }
  .toggle input { display: none; }
  .toggle input:checked + label {
    color: #fff; border-color: var(--accent-2);
    background: linear-gradient(135deg, rgba(124,92,255,0.25), rgba(255,59,92,0.2));
  }
  button {
    width: 100%; margin-top: 18px;
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    color: #fff; border: none; padding: 13px; border-radius: 10px;
    font-size: 14.5px; font-weight: 600; cursor: pointer; letter-spacing: 0.01em;
  }
  button:hover { filter: brightness(1.08); }
  button.secondary {
    background: var(--panel-2); border: 1px solid var(--border); color: var(--text);
  }
  .log {
    background: #08080d; border: 1px solid var(--border); border-radius: 10px;
    padding: 14px; font-size: 12px; color: #8fe3a8; font-family: "SF Mono", Menlo, Consolas, monospace;
    max-height: 360px; overflow-y: auto; white-space: pre-wrap; line-height: 1.5;
  }
  .hint { font-size: 11.5px; color: var(--muted); margin-top: 8px; line-height: 1.5; }
  footer { text-align: center; color: var(--muted); font-size: 11.5px; margin-top: 26px; }
  .refbox {
    display: flex; align-items: center; gap: 12px; background: var(--panel-2);
    border: 1px solid var(--border); border-radius: 10px; padding: 10px; margin-top: 6px;
  }
  .refbox img {
    width: 54px; height: 54px; object-fit: cover; border-radius: 8px; border: 1px solid var(--border);
  }
  .refbox .refstate { font-size: 12.5px; color: var(--muted); flex: 1; }
  .refbox .refstate b { color: var(--text); }
  input[type=file] {
    width: 100%; background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 10px; padding: 8px; color: var(--muted); font-size: 12.5px;
  }
  .checkline { display: flex; align-items: center; gap: 8px; margin-top: 8px; font-size: 12.5px; color: var(--muted); }
  .checkline input { width: auto; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">AI</div>
    <div>
      <h1>History Shorts Bot</h1>
      <div class="sub">Unattended AI research &rarr; script &rarr; voice &rarr; video &rarr; YouTube pipeline</div>
    </div>
  </header>

  <div class="grid">
    <div class="card">
      <h2>Status</h2>
      <div class="badges">
        <div class="badge">Posted <b>@@DONE_COUNT@@</b></div>
        <div class="badge">Autopilot every <b>@@INTERVAL@@h</b></div>
        <div class="badge @@RUNNING_CLASS@@">@@RUNNING_TEXT@@</div>
      </div>
      <div class="log">@@LOG_CONTENT@@</div>
      <form method="POST" action="/trigger">
        <button type="submit">Generate &amp; Post Now</button>
      </form>
      <div class="hint">Triggering now cancels any wait/schedule and starts immediately with the settings on the right.</div>
    </div>

    <div class="card">
      <h2>Configure Next Video</h2>
      <form method="POST" action="/configure" enctype="multipart/form-data">
        <label>Topic / niche</label>
        <textarea name="topic" rows="3" placeholder="e.g. The Bronze Age Collapse">@@TOPIC@@</textarea>

        <label>Orientation</label>
        <div class="toggle">
          <input type="radio" id="o_shorts" name="orientation" value="shorts" @@SHORTS_CHECKED@@>
          <label for="o_shorts">Short (vertical)</label>
          <input type="radio" id="o_long" name="orientation" value="long" @@LONG_CHECKED@@>
          <label for="o_long">Long-form (horizontal)</label>
        </div>

        <label>Duration</label>
        <div class="row">
          <input type="number" name="duration_minutes" min="0.25" max="10" step="0.25" value="@@DURATION_MINUTES@@">
          <span class="sub">minutes (~@@SCENE_COUNT@@ scenes)</span>
        </div>

        <label>Character / style reference image (optional)</label>
        <div class="refbox">
          @@REF_THUMB@@
          <div class="refstate">@@REF_STATE@@</div>
        </div>
        <input type="file" name="reference_image" accept="image/*">
        <div class="checkline">
          <input type="checkbox" id="remove_ref" name="remove_reference" value="1">
          <label for="remove_ref" style="margin:0;">Remove current reference image</label>
        </div>

        <button type="submit" class="secondary">Save Settings</button>
      </form>
      <div class="hint">
        Shorts are capped at 3 min, long-form at 10 min. Settings persist until you change them again,
        and apply to both manual and autopilot runs.
        <br><br>
        The reference image is used to keep the art style (and, loosely, a recurring character's look)
        consistent across scenes. If you don't upload one, the bot auto-generates the first scene, then
        uses that as the style reference for the rest of the video - so scenes still stay visually
        consistent with each other, just not pinned to a specific look you chose. Requires a Stability
        API key; without one, only the text prompts help keep things consistent.
      </div>
    </div>
  </div>

  <footer>First unattended run waits @@STARTUP_WAIT@@h after boot &middot; next run in @@NEXT_RUN_IN@@</footer>
</div>
</body>
</html>"""


def format_countdown(target_epoch):
    remaining = int(target_epoch - time.time())
    if remaining <= 0:
        return "any moment"
    h, rem = divmod(remaining, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def render_page():
    done_count = 0
    if os.path.exists(DONE_FILE):
        with open(DONE_FILE) as f:
            done_count = len([l for l in f if l.strip()])
    with log_lock:
        log_text = "\n".join(pipeline_log[-40:])
    with pipeline_state_lock:
        running = pipeline_running

    cfg = get_config()
    duration_minutes = round(cfg["duration_seconds"] / 60, 2)
    scene_count = max(1, round(cfg["duration_seconds"] / SCENE_SECONDS))

    ref_path = cfg.get("reference_image_path")
    if ref_path and os.path.exists(ref_path):
        ref_state = "<b>Set</b> - used as the style/character reference for every scene."
        ref_thumb = '<img src="/reference_image" alt="reference">'
    else:
        ref_state = "None set - first generated scene becomes the auto-reference (if Stability key is configured)."
        ref_thumb = '<img src="" style="visibility:hidden">'

    html = PAGE_TEMPLATE
    html = html.replace("@@DONE_COUNT@@", str(done_count))
    html = html.replace("@@INTERVAL@@", str(AUTOPILOT_INTERVAL_HOURS))
    html = html.replace("@@RUNNING_CLASS@@", "running" if running else "")
    html = html.replace("@@RUNNING_TEXT@@", "Running now" if running else "Idle")
    html = html.replace("@@LOG_CONTENT@@", log_text)
    html = html.replace("@@TOPIC@@", cfg["topic"])
    html = html.replace("@@SHORTS_CHECKED@@", "checked" if cfg["orientation"] == "shorts" else "")
    html = html.replace("@@LONG_CHECKED@@", "checked" if cfg["orientation"] == "long" else "")
    html = html.replace("@@DURATION_MINUTES@@", str(duration_minutes))
    html = html.replace("@@SCENE_COUNT@@", str(scene_count))
    html = html.replace("@@STARTUP_WAIT@@", str(STARTUP_WAIT_HOURS))
    html = html.replace("@@NEXT_RUN_IN@@", format_countdown(next_run_at[0]))
    html = html.replace("@@REF_THUMB@@", ref_thumb)
    html = html.replace("@@REF_STATE@@", ref_state)
    return html


def parse_post_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(length).decode() if length else ""
    return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}


def parse_multipart(handler):
    """Minimal multipart/form-data parser (no external deps; cgi is removed in 3.13+).
    Returns (fields: dict[str,str], files: dict[str, {"filename": str, "data": bytes}])."""
    content_type = handler.headers.get("Content-Type", "")
    if "boundary=" not in content_type:
        return {}, {}
    boundary = content_type.split("boundary=", 1)[1].strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]
    boundary_bytes = ("--" + boundary).encode()

    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length) if length else b""

    fields, files = {}, {}
    parts = raw.split(boundary_bytes)
    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        headers_blob, content = part.split(b"\r\n\r\n", 1)
        content = content.rstrip(b"\r\n")
        headers_text = headers_blob.decode(errors="ignore")
        disp_line = next((h for h in headers_text.split("\r\n") if h.lower().startswith("content-disposition")), "")
        name = None
        filename = None
        for piece in disp_line.split(";"):
            piece = piece.strip()
            if piece.startswith("name="):
                name = piece.split("=", 1)[1].strip('"')
            elif piece.startswith("filename="):
                filename = piece.split("=", 1)[1].strip('"')
        if name is None:
            continue
        if filename is not None:
            if filename:  # empty filename => no file chosen
                files[name] = {"filename": filename, "data": content}
        else:
            fields[name] = content.decode(errors="ignore")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/reference_image":
            cfg = get_config()
            ref_path = cfg.get("reference_image_path")
            if ref_path and os.path.exists(ref_path):
                with open(ref_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
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
            content_type = self.headers.get("Content-Type", "")
            if content_type.startswith("multipart/form-data"):
                fields, files = parse_multipart(self)
            else:
                fields, files = parse_post_body(self), {}

            with config_lock:
                if fields.get("topic", "").strip():
                    CONFIG["topic"] = fields["topic"].strip()
                orientation = fields.get("orientation", CONFIG["orientation"])
                if orientation not in ORIENTATIONS:
                    orientation = CONFIG["orientation"]
                CONFIG["orientation"] = orientation
                try:
                    minutes = float(fields.get("duration_minutes", 1))
                except ValueError:
                    minutes = 1.0
                seconds = minutes * 60
                cap = ORIENTATIONS[orientation]["max_seconds"]
                seconds = max(SCENE_SECONDS, min(seconds, cap))
                CONFIG["duration_seconds"] = seconds

                if fields.get("remove_reference") == "1":
                    old = CONFIG.get("reference_image_path")
                    if old and os.path.exists(old):
                        try:
                            os.remove(old)
                        except Exception:
                            pass
                    CONFIG["reference_image_path"] = None
                    if os.path.exists(REFERENCE_IMAGE_PATH_FILE):
                        os.remove(REFERENCE_IMAGE_PATH_FILE)
                    log("Reference image removed.")

                ref_file = files.get("reference_image")
                if ref_file and ref_file.get("data"):
                    ext = os.path.splitext(ref_file["filename"])[1].lower() or ".png"
                    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                        ext = ".png"
                    ref_path = os.path.join(WORK_DIR, f"user_reference{ext}")
                    with open(ref_path, "wb") as f:
                        f.write(ref_file["data"])
                    CONFIG["reference_image_path"] = ref_path
                    with open(REFERENCE_IMAGE_PATH_FILE, "w") as f:
                        f.write(ref_path)
                    log(f"Reference image uploaded ({ref_file['filename']}) - will be used for style/character consistency.")

            log(f"Settings updated -> orientation={orientation}, "
                f"duration={seconds/60:.2f}min, topic='{CONFIG['topic'][:60]}'")
        elif self.path == "/trigger":
            log("Manual trigger received - starting now.")
            trigger_event.set()

        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def start_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ---------------------------------------------------------------------------
# Content pipeline
# ---------------------------------------------------------------------------

def groq_chat(prompt, temperature=0.8, max_tokens=3000):
    res = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "openai/gpt-oss-120b",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "reasoning_effort": "low",
        },
        timeout=60,
    )
    res.raise_for_status()
    body = res.json()
    choice = body["choices"][0]
    content = (choice.get("message", {}).get("content") or "").strip()
    if not content:
        # Reasoning models can burn the whole token budget on hidden reasoning
        # and return empty content, or the API can return a finish_reason
        # like "length" or "content_filter" instead of real text.
        finish_reason = choice.get("finish_reason")
        raise RuntimeError(
            f"Groq returned empty content (finish_reason={finish_reason}). "
            f"Full response: {json.dumps(body)[:800]}"
        )
    return content


def generate_story(topic, num_scenes):
    total_seconds = num_scenes * SCENE_SECONDS
    log(f"Picking a topic and writing a factual script (~{total_seconds}s, {num_scenes} scenes)...")
    prompt = f"""You are a history channel scriptwriter. Your channel's niche/topic focus: {topic}

Pick ONE specific, genuinely interesting REAL historical topic or event within that focus.
Every fact you include must be historically accurate - do not invent or exaggerate details.
Write a punchy, cinematic narration script about it, split into exactly {num_scenes} scenes
of roughly equal length (about {SCENE_SECONDS} seconds of spoken narration each, so aim for
roughly {int(total_seconds * 2.3)} words total across all scenes combined).

If the same named person or persons recur across multiple scenes, first invent a short, fixed
physical-description tag for each of them (age, hair, face shape, clothing colors/style, any
distinguishing features) and reuse that EXACT phrase, word-for-word, inside the image_prompt of
every single scene that person appears in. This is critical for keeping their look consistent
across images - do not vary or paraphrase the description between scenes.

For each scene also write a short visual description (image_prompt) of what should be drawn to
illustrate that part of the narration - concrete, vivid, specific (people, setting, action), and
including the fixed character-description tag(s) above wherever that person appears.

IMPORTANT: the physical-description tags (hair, clothing, build, etc.) belong ONLY inside
image_prompt. The "narration" field is spoken voiceover - it must read like natural spoken
storytelling and must NEVER contain physical-description phrases like "a boy with white hair
and a blue shirt" or "a tall man". Refer to people in narration by name, title, or role only.

Also return 5-8 relevant hashtags for the video (no # symbol, no spaces, lowercase).

Return ONLY valid JSON, no markdown fences, in this exact shape:
{{
  "title": "viral YouTube title under 60 chars, no clickbait lies",
  "description": "2-3 sentence accurate description",
  "hashtags": ["ancienthistory", "..."],
  "scenes": [
    {{"narration": "...", "image_prompt": "..."}},
    ... exactly {num_scenes} of these ...
  ]
}}"""
    raw = groq_chat(prompt)
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Groq response wasn't valid JSON: {e}\nRaw content: {raw[:800]}") from e
    if len(data.get("scenes", [])) != num_scenes:
        raise ValueError(f"Expected {num_scenes} scenes, got {len(data.get('scenes', []))}")
    log(f"  Topic: {data['title']}")
    return data


def _try_huggingface_image(full_prompt, img_path):
    """Try each configured HF provider in order. Returns True on success."""
    last_err = None
    for provider in HF_IMAGE_PROVIDERS:
        try:
            client = InferenceClient(provider=provider, api_key=HF_TOKEN)
            image = client.text_to_image(full_prompt, model=HF_IMAGE_MODEL)
            image.save(img_path)
            return True
        except Exception as e:
            last_err = e
            log(f"    provider '{provider}' failed ({e}), trying next...")
    if last_err:
        log(f"    all HF providers exhausted, last error: {last_err}")
    return False


def _try_stability_style_reference(full_prompt, img_path, orientation, reference_path):
    """Use Stability AI's style-reference control endpoint to condition a new image
    on an existing reference image, for style/character consistency. Returns True on success.
    Note: this is a separate, pricier Stability product from SDXL - there is no cheap
    equivalent for style-conditioned generation, so it's only used when a reference
    image actually exists (uploaded, or auto-anchor from this video's own scene 0)."""
    if not STABILITY_API_KEY:
        return False
    if not reference_path or not os.path.exists(reference_path):
        return False
    try:
        log(f"    trying Stability AI style-reference (from {os.path.basename(reference_path)})...")
        aspect_ratio = ORIENTATIONS[orientation]["stability_ar"]
        with open(reference_path, "rb") as ref_f:
            res = requests.post(
                STABILITY_STYLE_ENDPOINT,
                headers={
                    "authorization": f"Bearer {STABILITY_API_KEY}",
                    "accept": "image/*",
                },
                files={"image": ref_f},
                data={
                    "prompt": full_prompt,
                    "aspect_ratio": aspect_ratio,
                    "fidelity": str(STYLE_FIDELITY),
                    "output_format": "png",
                },
                timeout=60,
            )
        if res.status_code == 200:
            with open(img_path, "wb") as f:
                f.write(res.content)
            return True
        log(f"    Stability AI style-reference error {res.status_code}: {res.text[:300]}")
        return False
    except Exception as e:
        log(f"    Stability AI style-reference failed: {e}")
        return False


def _try_stability_image(full_prompt, img_path, orientation):
    """Fallback #2: SDXL 1.0 - Stability's cheapest model (from ~0.9 credits/gen),
    used instead of Stable Image Core to stretch a small credit balance as far as
    possible. No reference-image conditioning here (that's the pricier style
    endpoint above); this is plain text-to-image. Returns True on success."""
    if not STABILITY_API_KEY:
        log("    no STABILITY_API_KEY set - skipping Stability AI fallback")
        return False
    try:
        log("    trying Stability AI (SDXL 1.0)...")
        width, height = SDXL_DIMS[orientation]
        res = requests.post(
            STABILITY_SDXL_ENDPOINT,
            headers={
                "authorization": f"Bearer {STABILITY_API_KEY}",
                "content-type": "application/json",
                "accept": "application/json",
            },
            json={
                "text_prompts": [{"text": full_prompt, "weight": 1}],
                "cfg_scale": 7,
                "height": height,
                "width": width,
                "samples": 1,
                "steps": 30,
            },
            timeout=60,
        )
        if res.status_code == 200:
            artifacts = res.json().get("artifacts", [])
            if not artifacts:
                log("    Stability AI (SDXL) returned no image artifacts")
                return False
            img_bytes = base64.b64decode(artifacts[0]["base64"])
            with open(img_path, "wb") as f:
                f.write(img_bytes)
            return True
        log(f"    Stability AI (SDXL) error {res.status_code}: {res.text[:300]}")
        return False
    except Exception as e:
        log(f"    Stability AI (SDXL) failed: {e}")
        return False


def _try_pollinations_image(full_prompt, img_path, orientation):
    """Fallback #3: Pollinations (free, no key needed)."""
    try:
        log("    trying Pollinations...")
        dims = ORIENTATIONS[orientation]
        url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(full_prompt)}"
        params = {"width": dims["w"], "height": dims["h"], "nologo": "true", "model": "flux"}
        if POLLINATIONS_TOKEN:
            params["token"] = POLLINATIONS_TOKEN
        res = requests.get(url, params=params, timeout=90)
        res.raise_for_status()
        with open(img_path, "wb") as f:
            f.write(res.content)
        return True
    except Exception as e:
        log(f"    Pollinations failed: {e}")
        return False


def generate_scene_image(image_prompt, index, num_scenes, orientation, reference_path=None):
    """reference_path: either the user-uploaded reference, or an auto-anchor image from
    scene 0 of this same video, used to keep style/character consistent across scenes."""
    log(f"  Generating image {index + 1}/{num_scenes}...")
    full_prompt = (
        f"A cinematic illustration, {ORIENTATIONS[orientation]['aspect_text']} composition, "
        f"no text or watermarks, {STYLE_SUFFIX}: {image_prompt}"
    )
    img_path = os.path.join(WORK_DIR, f"scene_{index}.png")

    # If we have a reference image (user-provided or auto-anchor from scene 0), lead with
    # Stability's style-reference endpoint so this scene matches it.
    if reference_path and _try_stability_style_reference(full_prompt, img_path, orientation, reference_path):
        return img_path

    # Fallback chain: Hugging Face providers -> Stability AI SDXL (cheapest) -> Pollinations
    if _try_huggingface_image(full_prompt, img_path):
        return img_path

    if _try_stability_image(full_prompt, img_path, orientation):
        return img_path

    if _try_pollinations_image(full_prompt, img_path, orientation):
        return img_path

    raise RuntimeError("All image providers failed (Hugging Face, Stability AI, Pollinations).")


def image_to_clip(img_path, index, seconds, orientation):
    dims = ORIENTATIONS[orientation]
    w, h = dims["w"], dims["h"]
    clip_path = os.path.join(WORK_DIR, f"clip_{index}.mp4")
    frames = int(25 * seconds)

    # Randomize zoom direction (in/out) and pan direction (which way it drifts)
    # per clip so scenes don't all move the same way.
    zoom_in = random.choice([True, False])
    if zoom_in:
        z_expr = "min(zoom+0.0018,1.4)"
    else:
        z_expr = "if(eq(on,0),1.4,max(zoom-0.0018,1.0))"

    pan = random.choice(["left", "right", "up", "down", "center"])
    if pan == "left":
        x_expr, y_expr = "(iw-iw/zoom)*(1-on/{f})", "(ih-ih/zoom)/2"
    elif pan == "right":
        x_expr, y_expr = "(iw-iw/zoom)*on/{f}", "(ih-ih/zoom)/2"
    elif pan == "up":
        x_expr, y_expr = "(iw-iw/zoom)/2", "(ih-ih/zoom)*(1-on/{f})"
    elif pan == "down":
        x_expr, y_expr = "(iw-iw/zoom)/2", "(ih-ih/zoom)*on/{f}"
    else:
        x_expr, y_expr = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    x_expr = x_expr.format(f=frames)
    y_expr = y_expr.format(f=frames)

    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", img_path,
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
               f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d={frames}:s={w}x{h}:fps=25",
        "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-pix_fmt", "yuv420p", clip_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return clip_path


def concat_clips(clip_paths):
    log("Stitching scenes together...")
    list_path = os.path.join(WORK_DIR, "concat_list.txt")
    with open(list_path, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")
    combined_path = os.path.join(WORK_DIR, "combined.mp4")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", combined_path]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return combined_path


def generate_narration(full_script):
    log("Generating narration voiceover (Fish Audio)...")
    if not FISH_AUDIO_API_KEY:
        log("  No FISH_AUDIO_API_KEY set - video will have no narration")
        return None

    payload = {
        "text": full_script,
        "format": "mp3",
        "mp3_bitrate": 128,
        "normalize": True,
        "chunk_length": 300,
    }
    if FISH_AUDIO_VOICE_ID:
        payload["reference_id"] = FISH_AUDIO_VOICE_ID

    try:
        res = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={
                "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
                "Content-Type": "application/json",
                "model": FISH_AUDIO_MODEL,
            },
            json=payload,
            timeout=120,
        )
        if res.status_code == 200:
            audio_path = os.path.join(WORK_DIR, "narration.mp3")
            with open(audio_path, "wb") as f:
                f.write(res.content)
            return audio_path
        log(f"  Fish Audio error {res.status_code}: {res.text[:300]}")
        return None
    except Exception as e:
        log(f"  Fish Audio failed: {e}")
        return None


def get_background_music():
    if not BACKGROUND_MUSIC_URL:
        return None
    try:
        log("Downloading background music...")
        res = requests.get(BACKGROUND_MUSIC_URL, timeout=60)
        res.raise_for_status()
        music_path = os.path.join(WORK_DIR, "music.mp3")
        with open(music_path, "wb") as f:
            f.write(res.content)
        return music_path
    except Exception as e:
        log(f"  Background music download failed: {e}")
        return None


def mux_narration(video_path, audio_path, output_path):
    log("Adding narration to video...")
    music_path = get_background_music()

    if not audio_path and not music_path:
        cmd = ["ffmpeg", "-y", "-i", video_path, "-c", "copy", output_path]
    elif audio_path and not music_path:
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", audio_path,
            "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest", output_path,
        ]
    elif music_path and not audio_path:
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-stream_loop", "-1", "-i", music_path,
            "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
            "-filter:a", f"volume={BACKGROUND_MUSIC_VOLUME}", "-shortest", output_path,
        ]
    else:
        log(f"  Mixing narration + background music (music at {BACKGROUND_MUSIC_VOLUME}x volume)...")
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", audio_path,
            "-stream_loop", "-1", "-i", music_path,
            "-filter_complex",
            f"[1:a]volume=1.0[narr];[2:a]volume={BACKGROUND_MUSIC_VOLUME}[music];"
            f"[narr][music]amix=inputs=2:duration=first:dropout_transition=0[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-shortest", output_path,
        ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    return output_path


def build_hashtags(story, orientation):
    tags = story.get("hashtags", []) or []
    clean = []
    for t in tags:
        t = "".join(ch for ch in t if ch.isalnum())
        if t:
            clean.append(f"#{t}")
    if orientation == "shorts":
        clean.append("#Shorts")
    # de-dupe, keep order
    seen = set()
    out = []
    for t in clean:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return " ".join(out)


def upload_to_youtube(file_path, title, description, orientation):
    log("Uploading to YouTube...")
    try:
        creds = get_google_creds(["https://www.googleapis.com/auth/youtube.upload"])
        creds.refresh(Request())
        youtube = build("youtube", "v3", credentials=creds)
        body = {
            "snippet": {"title": title[:100], "description": description, "categoryId": "27"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True)
        req = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
        response = None
        while response is None:
            status, response = req.next_chunk()
        vid = response.get("id")
        log(f"  Live -> https://youtube.com/watch?v={vid}")
        return vid
    except Exception as e:
        log(f"  Upload failed: {e}")
        return None


def run_pipeline():
    global pipeline_running
    with pipeline_state_lock:
        if pipeline_running:
            log("Pipeline already running - ignoring this trigger.")
            return
        pipeline_running = True

    log("\nStarting new history video...")
    clip_paths, combined, narration_path, final_path = [], None, None, None
    auto_anchor_path = None  # scene-0 image, used as a style reference for later scenes
    # if the user didn't upload one of their own
    try:
        cfg = get_config()
        orientation = cfg["orientation"]
        num_scenes = max(1, round(cfg["duration_seconds"] / SCENE_SECONDS))
        user_reference = cfg.get("reference_image_path")
        if user_reference:
            log("Using your uploaded reference image for style/character consistency.")

        story = generate_story(cfg["topic"], num_scenes)
        full_narration = " ".join(s["narration"] for s in story["scenes"])

        for i, scene in enumerate(story["scenes"]):
            # Reference priority: user-uploaded image > auto-anchor from this video's own scene 0.
            reference_for_scene = user_reference or auto_anchor_path
            img_path = generate_scene_image(
                scene["image_prompt"], i, num_scenes, orientation,
                reference_path=reference_for_scene,
            )
            if i == 0 and not user_reference:
                auto_anchor_path = img_path
                log("  Using this first scene as the style anchor for the rest of the video.")
            clip_path = image_to_clip(img_path, i, SCENE_SECONDS, orientation)
            clip_paths.append(clip_path)

        combined = concat_clips(clip_paths)
        narration_path = generate_narration(full_narration)
        final_path = os.path.join(WORK_DIR, "final_history.mp4")
        mux_narration(combined, narration_path, final_path)

        hashtags = build_hashtags(story, orientation)
        description = f"{story['description']}\n\n{hashtags}".strip()

        vid = upload_to_youtube(final_path, story["title"], description, orientation)
        if vid:
            with open(DONE_FILE, "a") as f:
                f.write(f"{time.time()} | {story['title']}\n")
            log("Pipeline complete!")
        else:
            log("Upload failed - pipeline stopped")

    except Exception as e:
        log(f"Pipeline error: {e}")
    finally:
        for p in clip_paths + [combined, narration_path, final_path]:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        with pipeline_state_lock:
            pipeline_running = False


def autopilot_loop():
    wait_seconds = STARTUP_WAIT_HOURS * 3600
    log(f"Startup window: waiting {STARTUP_WAIT_HOURS}h before the first unattended run. "
        f"Visit the site to configure the topic/duration/orientation, or click "
        f"'Generate & Post Now' to skip the wait.")
    while True:
        next_run_at[0] = time.time() + wait_seconds
        triggered = trigger_event.wait(timeout=wait_seconds)
        trigger_event.clear()
        run_pipeline()
        wait_seconds = AUTOPILOT_INTERVAL_HOURS * 3600
        log(f"Sleeping {AUTOPILOT_INTERVAL_HOURS}h until next autopilot run "
            f"(or trigger manually anytime)...")


def main():
    threading.Thread(target=start_server, daemon=True).start()
    threading.Thread(target=autopilot_loop, daemon=True).start()
    log("Bot started.")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
