import os
import time
import json
import subprocess
import tempfile
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

import yt_dlp
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

WORK_DIR = tempfile.gettempdir()
HISTORY_FILE = "posted_history.json"

TIKTOK_COOKIES_FILE = os.environ.get("TIKTOK_COOKIES_FILE", "")
MIN_VALID_FILE_BYTES = int(os.environ.get("MIN_VALID_FILE_BYTES", "300000"))
MIN_VALID_DURATION_SECONDS = float(os.environ.get("MIN_VALID_DURATION_SECONDS", "1.0"))
POLL_INTERVAL_HOURS = float(os.environ.get("POLL_INTERVAL_HOURS", "1"))
LOOKBACK_COUNT = int(os.environ.get("LOOKBACK_COUNT", "5"))
STARTUP_WAIT_HOURS = float(os.environ.get("STARTUP_WAIT_HOURS", "0.05"))

_seed_usernames = [
    u.strip().lstrip("@") for u in os.environ.get("TIKTOK_USERNAMES", "").split(",") if u.strip()
]

pipeline_log = ["TikTok -> YouTube bot ready. Waiting for the startup window or a manual trigger."]
log_lock = threading.Lock()

pipeline_state_lock = threading.Lock()
pipeline_running = False

trigger_event = threading.Event()
next_run_at = [time.time() + STARTUP_WAIT_HOURS * 3600]


def log(msg):
    print(msg, flush=True)
    with log_lock:
        pipeline_log.append(msg)
        if len(pipeline_log) > 100:
            pipeline_log.pop(0)


def load_youtube_accounts():
    raw = os.environ.get("YOUTUBE_ACCOUNTS_JSON", "")
    accounts = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                accounts = [a for a in parsed if a.get("id") and a.get("refresh_token")]
        except json.JSONDecodeError:
            log("WARNING: YOUTUBE_ACCOUNTS_JSON is not valid JSON - ignoring it.")

    legacy_token = os.environ.get("YOUTUBE_REFRESH_TOKEN")
    if legacy_token and not any(a["id"] == "default" for a in accounts):
        accounts.insert(0, {"id": "default", "label": "Default Channel", "refresh_token": legacy_token})

    default_client_id = os.environ.get("YOUTUBE_CLIENT_ID", "")
    default_client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "")

    for i, acc in enumerate(accounts):
        suffix = "" if i == 0 else str(i + 1)
        env_client_id = os.environ.get(f"YOUTUBE_CLIENT_ID{suffix}", "")
        env_client_secret = os.environ.get(f"YOUTUBE_CLIENT_SECRET{suffix}", "")

        acc["client_id"] = acc.get("client_id") or env_client_id or default_client_id
        acc["client_secret"] = acc.get("client_secret") or env_client_secret or default_client_secret

    return accounts


YOUTUBE_ACCOUNTS = load_youtube_accounts()
YOUTUBE_ACCOUNTS_BY_ID = {a["id"]: a for a in YOUTUBE_ACCOUNTS}

config_lock = threading.Lock()
_default_youtube_id = YOUTUBE_ACCOUNTS[0]["id"] if YOUTUBE_ACCOUNTS else ""
CONFIG = {
    "accounts": [{"tiktok": u, "youtube": _default_youtube_id} for u in _seed_usernames]
}


def get_config():
    with config_lock:
        return dict(CONFIG)


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {}


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def get_posted_ids(history, history_key):
    val = history.get(history_key, [])
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        return val
    return []


def get_google_creds(scopes, refresh_token, client_id, client_secret):
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TikTok -> YouTube Bot</title>
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
  .wrap { max-width: 880px; margin: 0 auto; }
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
  textarea { width: 100%; background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 12px; color: var(--text); font-size: 14px; font-family: inherit; resize: vertical; }
  textarea:focus { outline: none; border-color: var(--accent-2); }
  input[type=text], select { background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 10px; color: var(--text); font-size: 13.5px; font-family: inherit; }
  input[type=text]:focus, select:focus { outline: none; border-color: var(--accent-2); }
  .account-row { display: flex; gap: 8px; margin-bottom: 8px; align-items: center; }
  .account-row input[type=text] { flex: 1; }
  .account-row select { flex: 1; }
  .account-row button { width: auto; margin-top: 0; padding: 8px 12px; }
  button { width: 100%; margin-top: 18px; background: linear-gradient(135deg, var(--accent), var(--accent-2));
    color: #fff; border: none; padding: 13px; border-radius: 10px; font-size: 14.5px; font-weight: 600;
    cursor: pointer; letter-spacing: 0.01em; }
  button:hover { filter: brightness(1.08); }
  button.secondary { background: var(--panel-2); border: 1px solid var(--border); color: var(--text); }
  .log { background: #08080d; border: 1px solid var(--border); border-radius: 10px; padding: 14px;
    font-size: 12px; color: #8fe3a8; font-family: "SF Mono", Menlo, Consolas, monospace;
    max-height: 360px; overflow-y: auto; white-space: pre-wrap; line-height: 1.5; }
  .hint { font-size: 11.5px; color: var(--muted); margin-top: 8px; line-height: 1.5; }
  footer { text-align: center; color: var(--muted); font-size: 11.5px; margin-top: 26px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">TT&gt;YT</div>
    <div>
      <h1>TikTok -&gt; YouTube Bot</h1>
      <div class="sub">Watches TikTok accounts, reposts new videos to YouTube automatically</div>
    </div>
  </header>

  <div class="grid">
    <div class="card">
      <h2>Status</h2>
      <div class="badges">
        <div class="badge">Posted <b>@@DONE_COUNT@@</b></div>
        <div class="badge">Checking every <b>@@INTERVAL@@h</b></div>
        <div class="badge @@YT_WARN_CLASS@@">YouTube channels <b>@@YT_COUNT@@</b></div>
        <div class="badge @@RUNNING_CLASS@@">@@RUNNING_TEXT@@</div>
      </div>
      <div class="log">@@LOG_CONTENT@@</div>
      <form method="POST" action="/trigger">
        <button type="submit">Check All Now</button>
      </form>
      <div class="hint">Checks every monitored account immediately instead of waiting for the next scheduled check.</div>
    </div>

    <div class="card">
      <h2>Monitored Accounts</h2>
      <form method="POST" action="/configure">
        <label>Each row is one profile: a TikTok account paired with the YouTube channel it posts to.</label>
        <div id="accountRows">
@@ACCOUNT_ROWS@@
        </div>
        <button type="button" class="secondary" onclick="addRow()">+ Add Account</button>
        <button type="submit">Save Accounts</button>
      </form>
      <div class="hint">
        Every check cycle, the bot looks at each TikTok account's last @@LOOKBACK@@ videos. Any
        that aren't already posted for that profile get queued up, oldest first, one upload per
        cycle, to the YouTube channel selected for that row - so nothing gets silently skipped
        even if several videos land between checks.
        @@YT_HINT@@
      </div>
    </div>
  </div>

  <footer>First check runs @@STARTUP_WAIT@@h after boot &middot; next run in @@NEXT_RUN_IN@@</footer>
</div>

<script>
let rowIndex = @@ROW_COUNT@@;
function addRow() {
  const div = document.createElement('div');
  div.className = 'account-row';
  div.innerHTML = `
    <input type="text" name="tiktok_${rowIndex}" placeholder="tiktok username">
    <select name="youtube_${rowIndex}">@@YOUTUBE_OPTIONS_JS@@</select>
    <button type="button" class="secondary" onclick="this.parentElement.remove()">&times;</button>
  `;
  document.getElementById('accountRows').appendChild(div);
  rowIndex++;
}
</script>
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


def esc(s):
    return (s or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def youtube_options_html(selected=""):
    if not YOUTUBE_ACCOUNTS:
        return '<option value="">No YouTube accounts configured</option>'
    opts = []
    for acc in YOUTUBE_ACCOUNTS:
        sel = " selected" if acc["id"] == selected else ""
        opts.append(f'<option value="{esc(acc["id"])}"{sel}>{esc(acc["label"])}</option>')
    return "\n".join(opts)


def render_account_rows(accounts):
    rows_source = accounts if accounts else [{"tiktok": "", "youtube": ""}]
    rows = []
    for i, acc in enumerate(rows_source):
        rows.append(
            f'<div class="account-row">\n'
            f'  <input type="text" name="tiktok_{i}" value="{esc(acc.get("tiktok", ""))}" placeholder="tiktok username">\n'
            f'  <select name="youtube_{i}">{youtube_options_html(acc.get("youtube", ""))}</select>\n'
            f'  <button type="button" class="secondary" onclick="this.parentElement.remove()">&times;</button>\n'
            f'</div>'
        )
    return "\n".join(rows), len(rows_source)


def render_page():
    history = load_history()
    done_count = sum(len(get_posted_ids(history, k)) for k in history)
    with log_lock:
        log_text = "\n".join(pipeline_log[-40:])
    with pipeline_state_lock:
        running = pipeline_running

    cfg = get_config()
    rows_html, row_count = render_account_rows(cfg["accounts"])

    yt_hint = ""
    if not YOUTUBE_ACCOUNTS:
        yt_hint = " No YouTube accounts are configured yet - set YOUTUBE_ACCOUNTS_JSON (or the legacy YOUTUBE_REFRESH_TOKEN) before saving accounts."

    html = PAGE_TEMPLATE
    html = html.replace("@@DONE_COUNT@@", str(done_count))
    html = html.replace("@@INTERVAL@@", str(POLL_INTERVAL_HOURS))
    html = html.replace("@@RUNNING_CLASS@@", "running" if running else "")
    html = html.replace("@@RUNNING_TEXT@@", "Checking now" if running else "Idle")
    html = html.replace("@@YT_COUNT@@", str(len(YOUTUBE_ACCOUNTS)))
    html = html.replace("@@YT_WARN_CLASS@@", "warn" if not YOUTUBE_ACCOUNTS else "")
    html = html.replace("@@YT_HINT@@", yt_hint)
    html = html.replace("@@LOG_CONTENT@@", log_text)
    html = html.replace("@@ACCOUNT_ROWS@@", rows_html)
    html = html.replace("@@ROW_COUNT@@", str(row_count))
    html = html.replace("@@YOUTUBE_OPTIONS_JS@@", youtube_options_html().replace("`", "\\`"))
    html = html.replace("@@STARTUP_WAIT@@", str(STARTUP_WAIT_HOURS))
    html = html.replace("@@NEXT_RUN_IN@@", format_countdown(next_run_at[0]))
    html = html.replace("@@LOOKBACK@@", str(LOOKBACK_COUNT))
    return html


def parse_accounts_from_form(fields_multi):
    indices = set()
    for key in fields_multi:
        if key.startswith("tiktok_"):
            suffix = key[len("tiktok_"):]
            if suffix.isdigit():
                indices.add(int(suffix))

    accounts = []
    for i in sorted(indices):
        tiktok = fields_multi.get(f"tiktok_{i}", [""])[0].strip().lstrip("@")
        youtube = fields_multi.get(f"youtube_{i}", [""])[0].strip()
        if not tiktok:
            continue
        if not youtube and YOUTUBE_ACCOUNTS:
            youtube = YOUTUBE_ACCOUNTS[0]["id"]
        accounts.append({"tiktok": tiktok, "youtube": youtube})
    return accounts


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
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
            fields_multi = urllib.parse.parse_qs(body)
            accounts = parse_accounts_from_form(fields_multi)
            with config_lock:
                CONFIG["accounts"] = accounts
            summary = ", ".join(f"{a['tiktok']} -> {YOUTUBE_ACCOUNTS_BY_ID.get(a['youtube'], {}).get('label', a['youtube'])}" for a in accounts)
            log(f"Accounts updated -> {summary if accounts else '(none)'}")
        elif self.path == "/trigger":
            log("Manual trigger received - checking all accounts now.")
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


def get_recent_tiktok_videos(username, limit=5):
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
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                filepath,
            ],
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
        "quiet": True,
        "no_warnings": True,
        "outtmpl": filepath,
        "format": "mp4/best",
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
        raise RuntimeError(
            f"Downloaded file too small ({size} bytes) - likely blocked by TikTok "
            f"or got an error response instead of the real video. "
            f"Check TIKTOK_COOKIES_FILE is set to a fresh, TikTok-only cookies.txt."
        )

    duration = probe_duration_seconds(filepath)
    if duration is None:
        os.remove(filepath)
        raise RuntimeError(
            f"File downloaded ({size / 1_000_000:.1f} MB) but ffprobe couldn't read it "
            f"as a valid video - it's corrupt or not actually a video file. "
            f"Likely a blocked/error response from TikTok, not a real download."
        )
    if duration < MIN_VALID_DURATION_SECONDS:
        os.remove(filepath)
        raise RuntimeError(
            f"File downloaded ({size / 1_000_000:.1f} MB) but duration is only "
            f"{duration:.2f}s - too short to be real, likely corrupt/blocked."
        )

    log(f"  Download OK ({size / 1_000_000:.1f} MB, {duration:.1f}s)")


def upload_to_youtube(file_path, title, description, youtube_account_id):
    account = YOUTUBE_ACCOUNTS_BY_ID.get(youtube_account_id)
    if not account:
        log(f"  Upload failed: no YouTube account configured for id '{youtube_account_id}'")
        return None

    client_id = account.get("client_id")
    client_secret = account.get("client_secret")
    if not client_id or not client_secret:
        log(f"  Upload failed: no OAuth client_id/client_secret resolved for "
            f"'{account['label']}'. Set YOUTUBE_CLIENT_ID/SECRET (default) or a "
            f"numbered pair like YOUTUBE_CLIENT_ID2/YOUTUBE_CLIENT_SECRET2 for "
            f"this account's position in YOUTUBE_ACCOUNTS_JSON.")
        return None

    log(f"  Uploading to YouTube ({account['label']})...")
    log(f"  [DEBUG] Using client_id: {client_id[:20]}...")
    try:
        log(f"  [DEBUG] Creating credentials object...")
        creds = get_google_creds(
            ["https://www.googleapis.com/auth/youtube.upload"],
            account["refresh_token"],
            client_id,
            client_secret,
        )
        log(f"  [DEBUG] Refreshing access token...")
        creds.refresh(Request())
        log(f"  [DEBUG] Building YouTube API client...")
        youtube = build("youtube", "v3", credentials=creds)
        
        body = {
            "snippet": {"title": title[:100], "description": description, "categoryId": "24"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True)
        log(f"  [DEBUG] Inserting video to YouTube...")
        req = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
        response = None
        while response is None:
            status, response = req.next_chunk()
        vid = response.get("id")
        log(f"  Live -> https://youtube.com/watch?v={vid}")
        return vid
    except Exception as e:
        import traceback
        log(f"  ❌ Upload failed: {type(e).__name__}: {e}")
        log(f"  [FULL TRACEBACK]")
        for line in traceback.format_exc().split('\n'):
            log(f"  {line}")
        return None


def check_account(account, history):
    username = account["tiktok"]
    youtube_id = account.get("youtube") or (YOUTUBE_ACCOUNTS[0]["id"] if YOUTUBE_ACCOUNTS else "")
    yt_label = YOUTUBE_ACCOUNTS_BY_ID.get(youtube_id, {}).get("label", youtube_id or "no channel set")
    history_key = f"{username}::{youtube_id}"

    log(f"Checking @{username} (-> {yt_label})...")

    if not youtube_id or youtube_id not in YOUTUBE_ACCOUNTS_BY_ID:
        log(f"  Skipping: no valid YouTube channel selected for this profile.")
        return

    try:
        videos = get_recent_tiktok_videos(username, limit=LOOKBACK_COUNT)
    except Exception as e:
        log(f"  Failed to check TikTok: {e}")
        return

    if not videos:
        log("  No videos found on this profile.")
        return

    posted_ids = get_posted_ids(history, history_key)

    unposted = [v for v in videos if v["id"] not in posted_ids]
    if not unposted:
        log("  No new video since last check.")
        return

    if len(unposted) > 1:
        log(f"  {len(unposted)} unposted videos found within the last {len(videos)} - "
            f"posting the oldest of them now, the rest next cycle(s).")

    video = unposted[-1]

    log(f"  New video found ({video['id']}) - downloading...")
    filepath = os.path.join(WORK_DIR, f"{video['id']}.mp4")
    try:
        download_tiktok_video(video["url"], filepath)
    except Exception as e:
        log(f"  Download failed: {e}")
        return

    title = video["title"][:95] + " #Shorts"
    description = f"{video['title']}\n\nOriginally posted on TikTok by @{username}\n{video['url']}"
    vid = upload_to_youtube(filepath, title, description, youtube_id)

    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception:
        pass

    if vid:
        posted_ids.append(video["id"])
        history[history_key] = posted_ids[-50:]
        save_history(history)
        log(f"  Done: @{username} -> {video['id']} posted to {yt_label}.")


def run_pipeline():
    global pipeline_running
    with pipeline_state_lock:
        if pipeline_running:
            log("Check already running - ignoring this trigger.")
            return
        pipeline_running = True

    try:
        cfg = get_config()
        accounts = cfg["accounts"]
        if not accounts:
            log("No accounts configured yet - add accounts on the dashboard.")
            return
        if not YOUTUBE_ACCOUNTS:
            log("No YouTube accounts configured - set YOUTUBE_ACCOUNTS_JSON before running.")
            return
        history = load_history()
        for account in accounts:
            check_account(account, history)
        log("Check cycle complete.")
    except Exception as e:
        log(f"Pipeline error: {e}")
    finally:
        with pipeline_state_lock:
            pipeline_running = False


def autopilot_loop():
    wait_seconds = STARTUP_WAIT_HOURS * 3600
    log(f"Startup window: waiting {STARTUP_WAIT_HOURS}h before the first check. "
        f"Visit the dashboard to add TikTok accounts, or click 'Check All Now' to skip the wait.")
    while True:
        next_run_at[0] = time.time() + wait_seconds
        trigger_event.wait(timeout=wait_seconds)
        trigger_event.clear()
        run_pipeline()
        wait_seconds = POLL_INTERVAL_HOURS * 3600
        log(f"Sleeping {POLL_INTERVAL_HOURS}h until next check (or trigger manually anytime)...")


def main():
    if TIKTOK_COOKIES_FILE:
        if os.path.exists(TIKTOK_COOKIES_FILE):
            n_lines = sum(1 for _ in open(TIKTOK_COOKIES_FILE))
            log(f"Cookies file found at '{TIKTOK_COOKIES_FILE}' ({n_lines} lines).")
        else:
            log(f"WARNING: TIKTOK_COOKIES_FILE is set to '{TIKTOK_COOKIES_FILE}' but that "
                f"path doesn't exist - cookies will NOT be used. Check the path/env var.")
    else:
        log("WARNING: No TIKTOK_COOKIES_FILE set - requests are unauthenticated and "
            "much more likely to get blocked by TikTok.")

    if YOUTUBE_ACCOUNTS:
        for acc in YOUTUBE_ACCOUNTS:
            has_client = bool(acc.get("client_id") and acc.get("client_secret"))
            client_note = "OK" if has_client else "MISSING client_id/client_secret!"
            log(f"YouTube account loaded: {acc['label']} (id={acc['id']}) - OAuth client: {client_note}")
            log(f"  [DEBUG] Account {acc['id']}: client_id={acc.get('client_id', 'NOT SET')[:20]}...")
    else:
        log("WARNING: No YouTube accounts configured - set YOUTUBE_ACCOUNTS_JSON "
            "(or the legacy YOUTUBE_REFRESH_TOKEN) or uploads will fail.")

    threading.Thread(target=start_server, daemon=True).start()
    threading.Thread(target=autopilot_loop, daemon=True).start()
    log("Bot started.")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
