"""UGC clip bot.

- Upload clips into sections on the dashboard.
- Each run picks one random clip per section (in order), MUTES them, puts a
  random text overlay on each one (about 30% from the top), stitches them into
  one vertical 1080x1920 video, and lays a background track over it.
- The music crossfades into itself when it loops (no hard stop/restart), fades
  in at the start and fades out at the end.
- Music comes from uploaded audio files OR a TikTok / Instagram Reel link
  (the bot pulls that post's sound).
- Posts through Zernio and PUBLISHES straight away by default (no drafts to edit).
- Clips, music, links, settings and history are committed to a GitHub repo so
  everything survives redeploys.

Railway env vars:
  ZERNIO_API_KEY      required for posting
  GITHUB_TOKEN        token with read/write Contents access to the repo
  GITHUB_REPO         owner/name of a PRIVATE repo, e.g. icom/ugc-store
  GITHUB_BRANCH       optional, default main
  DASHBOARD_PASSWORD  optional
  TIKTOK_COOKIES_FILE / INSTAGRAM_COOKIES_FILE   optional cookies.txt paths
                      (Instagram links usually need cookies)
  FFMPEG_THREADS      optional, default 2
  FONT_FILE           optional path to a .ttf font for the text overlays
                      (if not set, the bot looks for a system font, then
                      downloads DejaVu Sans Bold once)
Needs: pip install requests yt-dlp   and ffmpeg (with drawtext) + git installed.
"""
import os
import re
import json
import math
import time
import hmac
import base64
import random
import shutil
import hashlib
import textwrap
import mimetypes
import tempfile
import threading
import subprocess
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests

# ===========================================================================
# EDIT THIS: sections in the order they appear in the finished video.
# (id used for the folder, label shown on the dashboard)
# ===========================================================================
SECTIONS = [
    ("intro", "Intro"),
    ("open_claude", "Open Claude"),
    ("paste_prompt", "Paste this prompt"),
    ("copy_prompt", "Copy this prompt"),
    ("go_krea", "Go to krea.ai"),
    ("paste_cook", "Paste the prompt & let it cook"),
    ("result", "Result"),
    ("rewards", "Rewards proof"),
]

# ===========================================================================
# EDIT THIS: text shown over each section's clip. One variation is picked at
# random per run. Add as many lines as you like. Use [] (or delete the key)
# for no text on that section. Keep emojis out (the font can't draw them).
# ===========================================================================
OVERLAY_TEXT = {
    "intro": [
        "",
        "",
        "",
        " ",
    ],
    "open_claude": [
        "Open Claude",
        "First, open Claude",
        "Start by opening Claude",
        "Open up Claude",
    ],
    "paste_prompt": [
        "Paste this prompt",
        "Now paste this prompt",
        "Paste this prompt in",
        "Drop this prompt in",
    ],
    "copy_prompt": [
        "Copy this prompt",
        "Copy this prompt first",
        "Grab this prompt",
        "Copy the prompt",
    ],
    "go_krea": [
        "Go to krea.ai",
        "Now head to krea.ai",
        "Open krea.ai",
        "Next, go to krea.ai",
    ],
    "paste_cook": [
        "Paste the prompt and let it cook",
        "Paste it in and let it cook",
        "Now paste the prompt and let it cook",
        "Paste the prompt, then let it cook",
    ],
    "result": [
        "Now you have a ready-to-post video for YouTube",
        "Boom. A ready-to-post video for YouTube",
        "Ready to post on YouTube",
        "That's a ready-to-post video for YouTube",
    ],
    "rewards": [
        "Comment KREA and I'll send it to you",
        "Rewards proof. Comment KREA and I'll send it to you",
        "Want this? Comment KREA and I'll send it to you",
        "Comment KREA and I'll send it over",
    ],
}

OVERLAY_ON = True          # set False to turn all text overlays off
OVERLAY_Y_PCT = 20         # vertical centre of the text, % from the top
OVERLAY_FONT_SIZE = 64     # pixels (video is 1080 wide)
OVERLAY_WRAP = 20          # max characters per line before wrapping

# Music loop / fade lengths (seconds)
MUSIC_FADE_IN = 1.0
MUSIC_FADE_OUT = 1.5
MUSIC_CROSSFADE = 2.0      # overlap between the end of a song and its restart
# ===========================================================================

SECTION_IDS = [s[0] for s in SECTIONS]
SECTION_LABELS = dict(SECTIONS)
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac", ".mp4", ".mov")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/aac", ".aac")
mimetypes.add_type("audio/opus", ".opus")

ZERNIO_API_KEY = os.environ.get("ZERNIO_API_KEY", "")
ZERNIO_BASE = "https://zernio.com/api/v1"
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "2")
STARTUP_WAIT_HOURS = float(os.environ.get("STARTUP_WAIT_HOURS", "0.05"))
TIKTOK_COOKIES_FILE = os.environ.get("TIKTOK_COOKIES_FILE", "")
INSTAGRAM_COOKIES_FILE = os.environ.get("INSTAGRAM_COOKIES_FILE", "")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip().strip("/")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
REMOTE_URL = os.environ.get("GIT_REMOTE_URL") or (
    f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git" if GITHUB_TOKEN and GITHUB_REPO else ""
)
SYNC_ON = bool(REMOTE_URL)
# GitHub rejects files over 100 MB, so cap uploads when syncing.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "95" if SYNC_ON else "500"))


def _pick_data_dir():
    for d in (os.environ.get("DATA_DIR"), "/data"):
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            return d, True
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(d, exist_ok=True)
    return d, False


DATA_DIR, HAS_VOLUME = _pick_data_dir()
STORE_DIR = os.path.join(DATA_DIR, "store")          # <- this folder is the git repo
CLIPS_DIR = os.path.join(STORE_DIR, "clips")
MUSIC_DIR = os.path.join(STORE_DIR, "music")
LINK_AUDIO_DIR = os.path.join(MUSIC_DIR, "links")
MUSIC_LINKS_FILE = os.path.join(STORE_DIR, "music_links.json")
CONFIG_FILE = os.path.join(STORE_DIR, "ugc_config.json")
HISTORY_FILE = os.path.join(STORE_DIR, "ugc_history.json")
OUT_DIR = os.path.join(DATA_DIR, "out")               # built videos (not synced)
FONT_CACHE_FILE = os.path.join(DATA_DIR, "overlay_font.ttf")   # downloaded font (not synced)


def ensure_dirs():
    for sid in SECTION_IDS:
        os.makedirs(os.path.join(CLIPS_DIR, sid), exist_ok=True)
    os.makedirs(LINK_AUDIO_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)


ensure_dirs()

# ---------------------------------------------------------------------------
# Logging: log() = Railway logs, status() = dashboard activity feed
# ---------------------------------------------------------------------------
status_lock = threading.Lock()
status_feed = []


def log(msg):
    print(msg, flush=True)


def status(msg):
    print(f"[status] {msg}", flush=True)
    with status_lock:
        status_feed.append(msg)
        if len(status_feed) > 60:
            status_feed.pop(0)


def esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def scrub(s):
    return s.replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN else s


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# GitHub sync: STORE_DIR is a git repo. Changes are committed + pushed a few
# seconds after the last change. On boot the repo is pulled back down.
# ---------------------------------------------------------------------------
sync_info = {"state": "off" if not SYNC_ON else "restoring", "msg": ""}
restore_done = threading.Event()
sync_dirty = [0.0]
sync_reason = ["update"]
if not SYNC_ON:
    restore_done.set()


def git(*args, timeout=900):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git", "-C", STORE_DIR, *args], capture_output=True, text=True, timeout=timeout, env=env)
    return r.returncode, scrub((r.stdout + r.stderr).strip())


def request_sync(reason="update"):
    if SYNC_ON:
        sync_reason[0] = reason
        sync_dirty[0] = time.time()


def sync_restore():
    if not SYNC_ON:
        return
    if not shutil.which("git"):
        sync_info.update(state="error", msg="git is not installed on this server")
        return
    existed = os.path.isdir(os.path.join(STORE_DIR, ".git"))
    if not existed:
        git("init")
        git("symbolic-ref", "HEAD", f"refs/heads/{GITHUB_BRANCH}")
    git("remote", "remove", "origin")
    git("remote", "add", "origin", REMOTE_URL)
    git("config", "user.email", "ugc-bot@local")
    git("config", "user.name", "ugc-bot")
    git("config", "pack.threads", "1")
    gi = os.path.join(STORE_DIR, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write("*.part\n*.tmp\n")

    code, out = git("fetch", "origin", GITHUB_BRANCH)
    if code == 0:
        if existed:
            git("add", "-A")
            git("commit", "-m", "local changes before restore")
            code, out = git("pull", "--rebase", "origin", GITHUB_BRANCH)
        else:
            code, out = git("checkout", "-f", "-B", GITHUB_BRANCH, f"origin/{GITHUB_BRANCH}")
        if code != 0:
            sync_info.update(state="error", msg=f"restore failed: {out[:200]}")
            return
        ensure_dirs()
        n_clips = sum(len(list_clips(s)) for s in SECTION_IDS)
        n_music = len(music_pool())
        sync_info.update(state="ok", msg="restored")
        status(f"☁️ Restored from GitHub: {n_clips} clips, {n_music} music tracks")
    elif "find remote ref" in out.lower() or "empty" in out.lower():
        git("checkout", "-B", GITHUB_BRANCH)
        sync_info.update(state="ok", msg="repo empty - will push on first change")
        status("☁️ GitHub repo is empty - it will fill as you upload.")
        request_sync("initial commit")
    else:
        sync_info.update(state="error", msg=f"fetch failed: {out[:200]}")


def sync_push(reason):
    git("add", "-A")
    code, out = git("commit", "-m", reason)
    if code != 0 and "nothing to commit" in out:
        return True, "nothing to push"
    code, out = git("push", "origin", GITHUB_BRANCH)
    if code != 0:
        git("pull", "--rebase", "origin", GITHUB_BRANCH)
        code, out = git("push", "origin", GITHUB_BRANCH)
    return code == 0, out[:300]


def sync_worker():
    while True:
        time.sleep(3)
        if not SYNC_ON or not restore_done.is_set() or sync_info["state"] == "error" and not sync_dirty[0]:
            continue
        t = sync_dirty[0]
        if t and time.time() - t >= 8:
            sync_dirty[0] = 0.0
            try:
                ok, msg = sync_push(sync_reason[0])
            except Exception as e:
                ok, msg = False, scrub(str(e))
            if ok:
                sync_info.update(state="ok", msg="synced " + time.strftime("%H:%M:%S"))
            else:
                sync_info.update(state="error", msg=msg)
                status(f"☁️❌ GitHub sync failed: {esc(msg[:200])}")
                log(f"GitHub sync failed: {msg}")


def restore_and_reload():
    try:
        sync_restore()
    except Exception as e:
        sync_info.update(state="error", msg=scrub(str(e))[:200])
        log(f"Restore failed: {scrub(str(e))}")
    finally:
        with config_lock:
            CONFIG.update(load_config())
        restore_done.set()


# ---------------------------------------------------------------------------
# Config + history
# ---------------------------------------------------------------------------
CFG_VERSION = 2   # v2: TikTok posts publish immediately instead of going to drafts
DEFAULT_CONFIG = {
    "cfg_version": CFG_VERSION,
    "auto": False,                 # auto-post on a schedule
    "posts_per_day": 2,
    "tiktok_draft": False,         # False = publish straight away, True = TikTok drafts
    "music_volume": 100,           # percent
    "destinations": [],            # Zernio account ids; empty = first TikTok account
    "captions": [
        "Make UGC videos with AI in minutes #ugc #ai #claude",
        "This AI workflow makes UGC for you #ugc #aitools #krea",
    ],
}
config_lock = threading.Lock()


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    loaded = {}
    try:
        with open(CONFIG_FILE) as f:
            loaded = json.load(f)
        cfg.update(loaded)
    except Exception:
        pass
    # One-time upgrade: older saved settings defaulted to TikTok drafts.
    if int(loaded.get("cfg_version", 1)) < CFG_VERSION:
        cfg["tiktok_draft"] = False
    cfg["cfg_version"] = CFG_VERSION
    return cfg


CONFIG = load_config()


def get_config():
    with config_lock:
        return json.loads(json.dumps(CONFIG))


def save_config(new):
    with config_lock:
        CONFIG.update(new)
        try:
            _write_json(CONFIG_FILE, CONFIG)
        except Exception as e:
            log(f"WARNING: could not save config: {e}")
    request_sync("settings")


def load_history():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return {"combos": []}


def save_history(h):
    try:
        _write_json(HISTORY_FILE, h)
    except Exception as e:
        log(f"WARNING: could not save history: {e}")
    request_sync("history")


# ---------------------------------------------------------------------------
# Clip + music library
# ---------------------------------------------------------------------------
def clip_dir(section):
    return os.path.join(CLIPS_DIR, section)


def list_clips(section):
    try:
        names = [n for n in os.listdir(clip_dir(section)) if n.lower().endswith(VIDEO_EXT)]
    except FileNotFoundError:
        return []
    return sorted(names)


def safe_name(name):
    name = os.path.basename(name or "")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .") or "file"
    return name


links_lock = threading.Lock()
link_state = {}   # link id -> "downloading"


def load_links():
    try:
        with open(MUSIC_LINKS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_links(links):
    _write_json(MUSIC_LINKS_FILE, links)
    request_sync("music links")


def list_music_files():
    try:
        return sorted(n for n in os.listdir(MUSIC_DIR)
                      if os.path.isfile(os.path.join(MUSIC_DIR, n)) and n.lower().endswith(AUDIO_EXT))
    except FileNotFoundError:
        return []


def music_pool():
    """[(label, path)] of every usable background track."""
    pool = [(n, os.path.join(MUSIC_DIR, n)) for n in list_music_files()]
    for rec in load_links():
        if rec.get("file"):
            p = os.path.join(MUSIC_DIR, rec["file"])
            if os.path.exists(p):
                pool.append((rec.get("title") or rec["url"], p))
    return pool


def download_audio(url, dest):
    """Pull the sound of a TikTok / Instagram post into dest (m4a). Returns title."""
    import yt_dlp
    tmp = tempfile.mkdtemp(prefix="aud_")
    try:
        cookies = INSTAGRAM_COOKIES_FILE if "instagram.com" in url else TIKTOK_COOKIES_FILE
        opts = {"quiet": True, "no_warnings": True, "format": "bestaudio/best",
                "outtmpl": os.path.join(tmp, "src.%(ext)s")}
        if cookies and os.path.exists(cookies):
            opts["cookiefile"] = cookies
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        src = next((os.path.join(tmp, n) for n in os.listdir(tmp) if n.startswith("src.")), None)
        if not src:
            raise RuntimeError("nothing was downloaded")
        run_ffmpeg(["ffmpeg", "-y", "-i", src, "-vn", "-c:a", "aac", "-b:a", "192k",
                    "-ar", "44100", "-ac", "2", dest], "extract audio")
        return (info or {}).get("track") or (info or {}).get("title") or url
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def add_music_link(url):
    url = url.strip()
    if not re.match(r"^https?://", url):
        status("❌ That doesn't look like a link (needs to start with http).")
        return
    with links_lock:
        links = load_links()
        if any(l["url"] == url for l in links):
            status("ℹ️ That link is already in the music list.")
            return
        lid = hashlib.sha1(url.encode()).hexdigest()[:10]
        link_state[lid] = "downloading"
    status(f"🎵 Getting the sound from {esc(url[:80])} ...")

    def work():
        try:
            dest_rel = f"links/{lid}.m4a"
            title = download_audio(url, os.path.join(MUSIC_DIR, dest_rel))
            with links_lock:
                links = load_links()
                links.append({"id": lid, "url": url, "title": str(title)[:120], "file": dest_rel,
                              "added": int(time.time())})
                save_links(links)
            status(f"✅ Music added: {esc(str(title)[:80])}")
        except Exception as e:
            log(f"Music link failed: {type(e).__name__}: {e}")
            hint = " (Instagram usually needs INSTAGRAM_COOKIES_FILE)" if "instagram.com" in url else ""
            status(f"❌ Couldn't get audio from that link: {esc(str(e)[:160])}{hint}")
        finally:
            link_state.pop(lid, None)

    threading.Thread(target=work, daemon=True).start()


def delete_music_link(lid):
    with links_lock:
        links = load_links()
        keep = []
        for l in links:
            if l["id"] == lid:
                try:
                    os.remove(os.path.join(MUSIC_DIR, l.get("file", "")))
                except OSError:
                    pass
            else:
                keep.append(l)
        save_links(keep)


# ---------------------------------------------------------------------------
# Text overlay helpers
# ---------------------------------------------------------------------------
FONT_URL = "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans-Bold.ttf"
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
]
_font_cache = [None]


def find_font():
    """Path to a usable bold .ttf, or '' if none can be found/downloaded."""
    if _font_cache[0] is not None:
        return _font_cache[0]
    found = ""
    for p in [os.environ.get("FONT_FILE", "")] + FONT_PATHS + [FONT_CACHE_FILE]:
        if p and os.path.isfile(p):
            found = p
            break
    if not found:
        try:
            r = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True, text=True, timeout=10)
            p = r.stdout.strip()
            if p and os.path.isfile(p):
                found = p
        except Exception:
            pass
    if not found:
        try:
            r = requests.get(FONT_URL, timeout=30)
            if r.status_code == 200 and len(r.content) > 50_000:
                with open(FONT_CACHE_FILE, "wb") as f:
                    f.write(r.content)
                found = FONT_CACHE_FILE
                log(f"Downloaded overlay font to {FONT_CACHE_FILE}")
        except Exception as e:
            log(f"Could not download overlay font: {e}")
    _font_cache[0] = found
    return found


def overlay_for(section):
    options = [t for t in OVERLAY_TEXT.get(section, []) if t and t.strip()]
    return random.choice(options) if options else ""


def text_filters(text, workdir, idx, font):
    """ffmpeg drawtext filters for `text`, each line centred, block centred
    OVERLAY_Y_PCT% from the top of the 1080x1920 frame."""
    lines = textwrap.wrap(text, width=OVERLAY_WRAP) or []
    if not lines:
        return ""
    line_h = int(OVERLAY_FONT_SIZE * 1.6)
    top = int(1920 * OVERLAY_Y_PCT / 100 - len(lines) * line_h / 2)
    filters = []
    for j, line in enumerate(lines):
        path = os.path.join(workdir, f"t{idx}_{j}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(line)
        filters.append(
            f"drawtext=fontfile='{font}':textfile='{path}':expansion=none:"
            f"fontsize={OVERLAY_FONT_SIZE}:fontcolor=white:borderw=3:bordercolor=black:"
            f"box=1:boxcolor=black@0.45:boxborderw=14:x=(w-text_w)/2:y={top + j * line_h}"
        )
    return ",".join(filters)


# ---------------------------------------------------------------------------
# Video building (ffmpeg)
# ---------------------------------------------------------------------------
def run_ffmpeg(cmd, what):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        if r.returncode < 0:
            raise RuntimeError(f"{what}: ffmpeg killed by signal {-r.returncode} "
                               f"(likely out of memory; lower FFMPEG_THREADS)")
        tail = "\n".join([l for l in r.stderr.splitlines() if l.strip()][-6:])
        raise RuntimeError(f"{what}: ffmpeg failed: {tail}")


def probe(path, entries, stream=None):
    cmd = ["ffprobe", "-v", "error"]
    if stream:
        cmd += ["-select_streams", stream]
    cmd += ["-show_entries", entries, "-of", "csv=p=0", path]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def has_audio(path):
    return "audio" in probe(path, "stream=codec_type", "a")


def duration(path):
    try:
        return float(probe(path, "format=duration"))
    except ValueError:
        return 0.0


def normalize(src, dst, overlay=""):
    """Any clip -> 1080x1920, 30fps, h264, NO audio (clips are muted).
    Whole frame is kept; a blurred copy fills the bars. `overlay` is an
    optional string of drawtext filters applied on top."""
    vf = (
        "[0:v]split[a][b];"
        "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        "boxblur=10:2,scale=1080:1920,setsar=1[bg];"
        "[b]scale=1080:1920:force_original_aspect_ratio=decrease,setsar=1[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30"
        + ("," + overlay if overlay else "")
        + ",format=yuv420p[v]"
    )
    run_ffmpeg(["ffmpeg", "-y", "-i", src, "-filter_complex", vf, "-map", "[v]", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-threads", FFMPEG_THREADS,
                "-movflags", "+faststart", dst], f"normalize {os.path.basename(src)}")


def concat(parts, out_path, workdir):
    lst = os.path.join(workdir, "list.txt")
    with open(lst, "w") as f:
        for p in parts:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                "-c", "copy", "-movflags", "+faststart", out_path], "concat")


def add_music(video, track_path, out_path, volume_pct):
    """Lay the track under the video.
    - Shorter than the video: copies of the song are crossfaded into each other,
      so the loop point is a smooth blend instead of a hard cut.
    - Longer than the video: it is simply cut.
    - Always fades in at the start and fades out at the end."""
    dur = duration(video)
    tdur = duration(track_path)
    vol = max(0.0, min(2.0, volume_pct / 100.0))
    fade_in = min(MUSIC_FADE_IN, max(0.0, dur / 4))
    fade_out = min(MUSIC_FADE_OUT, max(0.0, dur / 4))

    inputs = ["-i", video]
    graph = []
    if tdur <= 0 or tdur >= dur:
        inputs += ["-i", track_path]
        last = "1:a"
    else:
        xf = max(0.1, min(MUSIC_CROSSFADE, tdur / 3))
        step = tdur - xf
        copies = max(2, min(int(math.ceil((dur - tdur) / step)) + 1, 60))
        for _ in range(copies):
            inputs += ["-i", track_path]
        last = "1:a"
        for i in range(2, copies + 1):
            out = f"x{i}"
            graph.append(f"[{last}][{i}:a]acrossfade=d={xf:.2f}:c1=qsin:c2=qsin[{out}]")
            last = out
    graph.append(
        f"[{last}]volume={vol:.2f},"
        f"afade=t=in:st=0:d={fade_in:.2f},"
        f"afade=t=out:st={max(0.0, dur - fade_out):.2f}:d={fade_out:.2f}[a]"
    )
    run_ffmpeg(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(graph),
                "-map", "0:v:0", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-t", f"{dur:.3f}", "-movflags", "+faststart", out_path], "add music")


def pick_clips(history):
    pools = [(sid, list_clips(sid)) for sid in SECTION_IDS]
    pools = [(sid, files) for sid, files in pools if files]
    if not pools:
        raise RuntimeError("no clips uploaded yet - add clips to at least one section")
    used = set(history.get("combos", []))
    picks, key = [], ""
    for _ in range(80):  # try to avoid a combination we've already posted
        picks = [(sid, random.choice(files)) for sid, files in pools]
        key = "|".join(f"{s}/{f}" for s, f in picks)
        if key not in used:
            break
    return picks, key


def pick_track():
    pool = music_pool()
    random.shuffle(pool)
    for label, path in pool:
        if has_audio(path):
            return label, path
    return None, None


def build_video(history, cfg):
    picks, key = pick_clips(history)
    work = tempfile.mkdtemp(prefix="ugc_")
    try:
        font = find_font() if OVERLAY_ON else ""
        if OVERLAY_ON and not font:
            status("⚠️ No font found for text overlays - building without text. "
                   "Set FONT_FILE to a .ttf path or install a font.")
        parts = []
        for i, (sid, fname) in enumerate(picks):
            text = overlay_for(sid) if font else ""
            flt = text_filters(text, work, i, font) if text else ""
            shown = f' - "{esc(text)}"' if text else ""
            status(f"🎞️ {esc(SECTION_LABELS[sid])}: {esc(fname)}{shown}")
            dst = os.path.join(work, f"{i:02d}.mp4")
            src = os.path.join(clip_dir(sid), fname)
            try:
                normalize(src, dst, flt)
            except RuntimeError as e:
                if not flt:
                    raise
                log(f"Overlay failed, retrying without text: {e}")
                status("⚠️ Text overlay failed on this clip (ffmpeg may lack drawtext) - using it without text.")
                normalize(src, dst, "")
            parts.append(dst)
        silent = os.path.join(work, "silent.mp4")
        concat(parts, silent, work)

        out = os.path.join(OUT_DIR, "latest.mp4")
        tmp_out = os.path.join(work, "final.mp4")
        label, track = pick_track()
        if track:
            status(f"🎵 Music: {esc(str(label)[:80])}")
            add_music(silent, track, tmp_out, cfg["music_volume"])
        else:
            status("⚠️ No music added yet - this video is silent. Add a track or a TikTok/Instagram link below.")
            shutil.move(silent, tmp_out)
        shutil.move(tmp_out, out)
        log(f"  Built {out} ({os.path.getsize(out) / 1_000_000:.1f} MB) from {len(parts)} clips")
        return out, key
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Zernio posting
# ---------------------------------------------------------------------------
def _zh(js=False):
    h = {"Authorization": f"Bearer {ZERNIO_API_KEY}"}
    if js:
        h["Content-Type"] = "application/json"
    return h


_acc_cache = {"t": 0, "data": [], "error": ""}


def zernio_accounts(force=False):
    if not ZERNIO_API_KEY:
        _acc_cache["error"] = "ZERNIO_API_KEY not set"
        return []
    if not force and time.time() - _acc_cache["t"] < 60:
        return _acc_cache["data"]
    try:
        r = requests.get(f"{ZERNIO_BASE}/accounts", headers=_zh(), timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
        _acc_cache.update(t=time.time(), data=r.json().get("accounts", []), error="")
    except Exception as e:
        _acc_cache.update(t=time.time(), error=str(e))
    return _acc_cache["data"]


def post_via_zernio(path, cfg):
    accounts = zernio_accounts(force=True)
    chosen = [a for a in accounts if a.get("_id") in cfg["destinations"]]
    if not chosen:
        chosen = [a for a in accounts if a.get("platform") == "tiktok"][:1]
    if not chosen:
        raise RuntimeError("no destination: connect a TikTok account in Zernio and pick it on the dashboard")

    caption = random.choice(cfg["captions"]) if cfg["captions"] else "New video"
    title = caption.split("#")[0].strip()[:100] or "New video"

    p = requests.post(f"{ZERNIO_BASE}/media/presign", headers=_zh(True), timeout=30,
                      json={"filename": "ugc.mp4", "contentType": "video/mp4"})
    if p.status_code >= 300:
        raise RuntimeError(f"presign failed: {p.status_code} {p.text[:200]}")
    info = p.json()
    with open(path, "rb") as f:
        put = requests.put(info["uploadUrl"], headers={"Content-Type": "video/mp4"}, data=f, timeout=900)
    if put.status_code >= 300:
        raise RuntimeError(f"media upload failed: {put.status_code} {put.text[:200]}")

    body = {
        "content": caption[:2200],
        "title": title,
        "publishNow": True,
        "visibility": "public",
        "platforms": [{"platform": a["platform"], "accountId": a["_id"]} for a in chosen],
        "mediaItems": [{"type": "video", "url": info["publicUrl"]}],
    }
    if any(a["platform"] == "tiktok" for a in chosen):
        body["tiktokSettings"] = {
            "draft": bool(cfg["tiktok_draft"]),
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "allow_comment": True, "allow_duet": True, "allow_stitch": True,
            "commercial_content_type": "none",
            "content_preview_confirmed": True, "express_consent_given": True,
            "media_type": "video", "auto_add_music": False, "video_made_with_ai": False,
        }
    r = requests.post(f"{ZERNIO_BASE}/posts", headers=_zh(True), json=body, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"create post failed: {r.status_code} {r.text[:300]}")
    post = r.json().get("post", {})
    names = ", ".join(f"{a['platform']}:{a.get('username', '')}" for a in chosen)
    log(f"  Zernio accepted post {post.get('_id')} -> {names} ({post.get('status')})")
    return post.get("_id") or "sent", names


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------
job_lock = threading.Lock()
job_running = False
next_run = [time.time() + STARTUP_WAIT_HOURS * 3600]


def run_job(do_post):
    global job_running
    if not restore_done.is_set():
        status("⏳ Still restoring your clips from GitHub - try again in a moment.")
        return
    if not job_lock.acquire(blocking=False):
        status("⏳ A video is already being built - wait for it to finish.")
        return
    job_running = True
    try:
        cfg = get_config()
        history = load_history()
        status("🎬 Building a new video to post..." if do_post else "🎬 Building a preview video...")
        out, key = build_video(history, cfg)
        status('✅ Video built - <a href="/out/latest.mp4" target="_blank">watch it</a>')
        if do_post:
            status("⬆️ Posting via Zernio...")
            post_id, names = post_via_zernio(out, cfg)
            history.setdefault("combos", []).append(key)
            save_history(history)
            mode = "as a TikTok draft" if cfg["tiktok_draft"] and "tiktok" in names else "live"
            status(f"🚀 Posted ({esc(names)}) {mode} - id {esc(post_id)}")
    except Exception as e:
        log(f"Job failed: {type(e).__name__}: {e}")
        status(f"❌ {esc(str(e)[:300])}")
    finally:
        job_running = False
        job_lock.release()


def start_job(do_post):
    threading.Thread(target=run_job, args=(do_post,), daemon=True).start()


def interval_seconds(cfg):
    return 24 * 3600 / max(1, int(cfg.get("posts_per_day", 2)))


def autopilot():
    while True:
        time.sleep(15)
        cfg = get_config()
        if cfg["auto"] and time.time() >= next_run[0] and not job_running and restore_done.is_set():
            next_run[0] = time.time() + interval_seconds(cfg)
            log("Autopilot: starting a scheduled post.")
            start_job(True)


def countdown(t):
    s = int(t - time.time())
    if s <= 0:
        return "any moment"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m}m" if h else (f"{m}m {sec}s" if m else f"{sec}s")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>UGC Bot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0b0b10;--panel:#14141c;--p2:#1b1b26;--border:#26263a;--text:#eaeaf2;--muted:#8a8aa0;--a:#ff3b5c;--a2:#7c5cff;--ok:#35d488}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;background:radial-gradient(1200px 600px at 10% -10%,rgba(124,92,255,.18),transparent 60%),radial-gradient(1000px 500px at 100% 0,rgba(255,59,92,.14),transparent 55%),var(--bg);color:var(--text);padding:28px 18px 60px}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);font-size:13px;margin-bottom:22px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:20px;margin-bottom:16px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 14px}
.grid{display:grid;grid-template-columns:1fr;gap:16px}@media(min-width:760px){.grid{grid-template-columns:1fr 1fr}}
.grid .card{margin-bottom:0}.grid{margin-bottom:16px}
.badges{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.badge{background:var(--p2);border:1px solid var(--border);border-radius:999px;padding:6px 12px;font-size:12.5px;color:var(--muted)}
.badge b{color:var(--text)}.badge.on{color:var(--ok);border-color:rgba(53,212,136,.35)}.badge.warn{color:var(--a);border-color:rgba(255,59,92,.4)}
button,.btn{background:linear-gradient(135deg,var(--a),var(--a2));color:#fff;border:0;padding:11px 16px;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer}
button.sec{background:var(--p2);border:1px solid var(--border);color:var(--text)}
.row{display:flex;gap:10px;flex-wrap:wrap}.row form{margin:0}
label{display:block;font-size:12.5px;color:var(--muted);margin:12px 0 6px}
input[type=number],input[type=text],select,textarea{background:var(--p2);border:1px solid var(--border);border-radius:8px;padding:8px 10px;color:var(--text);font:inherit;font-size:13.5px}
input[type=text]{width:100%}
textarea{width:100%;min-height:90px}
.feed{display:flex;flex-direction:column;gap:7px;max-height:340px;overflow-y:auto}
.fi{background:#08080d;border:1px solid var(--border);border-radius:10px;padding:9px 12px;font-size:13px;line-height:1.5}
.fi a,.clip a{color:var(--a2);text-decoration:none}
.empty{color:var(--muted);font-size:13px}
.sec-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px}
.sec-head b{font-size:15px}.cnt{font-size:12px;color:var(--muted)}.cnt.zero{color:var(--a)}
.clip{display:flex;justify-content:space-between;gap:8px;font-size:12.5px;padding:5px 0;border-top:1px solid var(--border)}
.clip>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.x{background:none;border:0;color:var(--muted);padding:0 4px;cursor:pointer;font-size:16px}
.hint{font-size:11.5px;color:var(--muted);margin-top:8px;line-height:1.5}
.chk{display:flex;gap:8px;align-items:center;font-size:13px;margin:6px 0;color:var(--text)}
.up{margin-top:10px;font-size:12px;color:var(--muted)}
.inline{display:flex;gap:8px;margin-top:10px}.inline button{white-space:nowrap}
</style></head><body><div class="wrap">
<h1>UGC Bot</h1>
<div class="sub">Random clip per section, muted, with text overlays and background music, posted for you.</div>
@@WARNINGS@@
<div class="grid">
 <div class="card">
  <h2>Run</h2>
  <div class="badges">
   <div class="badge @@RUNCLS@@">@@RUNTXT@@</div>
   <div class="badge @@AUTOCLS@@">Auto-post <b>@@AUTO@@</b></div>
   <div class="badge">Posts/day <b>@@PPD@@</b></div>
   <div class="badge">Posted <b>@@POSTED@@</b></div>
   <div class="badge @@SYNCCLS@@">GitHub sync <b>@@SYNC@@</b></div>
  </div>
  <div class="row">
   <form method="POST" action="/preview"><button class="sec" type="submit">Build preview only</button></form>
   <form method="POST" action="/run"><button type="submit">Build &amp; post now</button></form>
  </div>
  <div class="hint">Preview builds a video from random clips without posting, so you can check it. Next scheduled run in @@NEXT@@ (only if auto-post is on).@@SYNCMSG@@</div>
  <h2 style="margin-top:18px">Activity</h2>
  <div class="feed">@@FEED@@</div>
 </div>
 <div class="card">
  <h2>Settings</h2>
  <form method="POST" action="/save">
   <div class="chk"><input type="checkbox" id="auto" name="auto" value="1" @@AUTOCHK@@><label for="auto" style="margin:0;color:var(--text)">Auto-post on a schedule</label></div>
   <label>Posts per day</label><input type="number" name="posts_per_day" min="1" max="15" value="@@PPD@@">
   <label>Music volume (%)</label><input type="number" name="music_volume" min="0" max="200" value="@@VOL@@">
   <label>TikTok mode</label>
   <select name="tiktok_mode"><option value="public" @@PUBSEL@@>Publish immediately (no editing needed)</option><option value="draft" @@DRAFTSEL@@>Draft (you publish in the TikTok app)</option></select>
   <label>Post to (accounts connected in Zernio)</label>
   @@DESTS@@
   <label>Captions (one per line, a random one is used)</label>
   <textarea name="captions">@@CAPTIONS@@</textarea>
   <button type="submit" style="margin-top:14px">Save settings</button>
  </form>
 </div>
</div>
<div class="card">
 <div class="sec-head"><b>Background music</b><span class="cnt @@MZERO@@">@@MCOUNT@@</span></div>
 <div class="hint" style="margin:0 0 8px">Clips are always muted. Each video gets one random track from this list. If the song is shorter than the video it crossfades into itself so the loop is smooth, if longer it is cut. It fades in at the start and out at the end.</div>
 @@MUSIC@@
 <div class="up"><input type="file" accept="audio/*,video/mp4" multiple onchange="upload('music',this)"> <span id="up_music"></span></div>
 <div class="inline"><input type="text" id="murl" placeholder="Paste a TikTok or Instagram Reel link to use its sound"><button type="button" onclick="addLink()">Add sound</button></div>
</div>
<div class="card"><h2>Clip sections (in video order)</h2>
 <div class="hint" style="margin:0">Each run uses one random clip from every section that has clips. Empty sections are skipped. Any size or orientation works. Each section also gets a random text overlay (edit the wording in OVERLAY_TEXT at the top of the script).</div>
</div>
@@SECTIONS@@
<div class="hint">Data folder: @@DATA@@ &middot; max upload @@MAXMB@@ MB per file</div>
</div>
<script>
async function upload(sec,input){
 const files=[...input.files],out=document.getElementById('up_'+sec);
 for(let i=0;i<files.length;i++){
  out.textContent='Uploading '+(i+1)+'/'+files.length+': '+files[i].name+' ...';
  try{
   const r=await fetch('/upload?section='+sec+'&name='+encodeURIComponent(files[i].name),{method:'PUT',body:files[i]});
   if(!r.ok){out.textContent='Failed: '+files[i].name+' (HTTP '+r.status+': '+(await r.text())+')';return;}
  }catch(e){out.textContent='Failed: '+files[i].name+' ('+e+')';return;}
 }
 location.reload();
}
async function del(btn){
 const sec=btn.dataset.sec,name=btn.dataset.name;
 if(!confirm('Delete '+name+'?'))return;
 await fetch('/delete?section='+sec+'&name='+encodeURIComponent(name),{method:'POST'});
 location.reload();
}
async function delm(btn){
 if(!confirm('Remove this sound?'))return;
 await fetch('/music/delete?id='+encodeURIComponent(btn.dataset.id),{method:'POST'});
 location.reload();
}
async function addLink(){
 const v=document.getElementById('murl').value.trim();
 if(!v)return;
 await fetch('/music/add',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'url='+encodeURIComponent(v)});
 location.reload();
}
@@RELOAD@@
</script></body></html>"""


def render_sections():
    out = []
    for sid, label in SECTIONS:
        files = list_clips(sid)
        rows = []
        for n in files:
            try:
                mb = os.path.getsize(os.path.join(clip_dir(sid), n)) / 1_000_000
            except OSError:
                mb = 0
            q = urllib.parse.quote(n)
            rows.append(
                f'<div class="clip"><span><a href="/clip/{sid}/{q}" target="_blank">{esc(n)}</a> '
                f'<span style="color:var(--muted)">{mb:.1f} MB</span></span>'
                f'<button class="x" title="Delete" data-sec="{sid}" data-name="{esc(n)}" onclick="del(this)">&times;</button></div>'
            )
        zero = "zero" if not files else ""
        plural = "s" if len(files) != 1 else ""
        body = "".join(rows) or '<div class="empty">No clips yet.</div>'
        out.append(
            f'<div class="card"><div class="sec-head"><b>{esc(label)}</b>'
            f'<span class="cnt {zero}">{len(files)} clip{plural}</span></div>{body}'
            f'<div class="up"><input type="file" accept="video/*" multiple onchange="upload(\'{sid}\',this)"> '
            f'<span id="up_{sid}"></span></div></div>'
        )
    return "\n".join(out)


def render_music():
    rows = []
    for n in list_music_files():
        q = urllib.parse.quote(n)
        rows.append(
            f'<div class="clip"><span><a href="/music/file/{q}" target="_blank">{esc(n)}</a> '
            f'<span style="color:var(--muted)">uploaded</span></span>'
            f'<button class="x" title="Delete" data-sec="music" data-name="{esc(n)}" onclick="del(this)">&times;</button></div>'
        )
    for rec in load_links():
        ok = rec.get("file") and os.path.exists(os.path.join(MUSIC_DIR, rec["file"]))
        tag = "link" if ok else "audio missing - remove and re-add"
        rows.append(
            f'<div class="clip"><span><a href="/music/link/{esc(rec["id"])}" target="_blank">{esc(rec.get("title") or rec["url"])}</a> '
            f'<span style="color:var(--muted)">{tag} &middot; {esc(rec["url"][:60])}</span></span>'
            f'<button class="x" title="Remove" data-id="{esc(rec["id"])}" onclick="delm(this)">&times;</button></div>'
        )
    for lid in list(link_state):
        rows.append('<div class="clip"><span style="color:var(--muted)">Getting sound from a link...</span></div>')
    return "".join(rows) or '<div class="empty">No music yet - clips will be silent.</div>'


def render_dests(cfg):
    accounts = zernio_accounts()
    if not accounts:
        err = _acc_cache["error"] or "none found"
        return f'<div class="empty">No Zernio accounts loaded ({esc(err)}).</div>'
    sel = set(cfg["destinations"])
    if not sel:
        first = next((a for a in accounts if a.get("platform") == "tiktok"), None)
        sel = {first["_id"]} if first else set()
    rows = []
    for a in accounts:
        chk = "checked" if a["_id"] in sel else ""
        rows.append(
            f'<div class="chk"><input type="checkbox" name="dest" value="{esc(a["_id"])}" {chk}>'
            f'{esc(a.get("platform"))} &middot; {esc(a.get("username") or a.get("displayName"))}</div>'
        )
    return "\n".join(rows)


def render_page():
    cfg = get_config()
    history = load_history()
    warnings = []
    if not HAS_VOLUME and not SYNC_ON:
        warnings.append("No volume and no GitHub sync - uploaded clips are lost on every redeploy. "
                        "Set GITHUB_TOKEN + GITHUB_REPO (or add a Railway volume at /data).")
    if not ZERNIO_API_KEY:
        warnings.append("ZERNIO_API_KEY is not set - posting is disabled.")
    if SYNC_ON and sync_info["state"] == "error":
        warnings.append("GitHub sync problem: " + sync_info["msg"])
    warn_html = "".join(
        f'<div class="card" style="border-color:rgba(255,59,92,.4);color:#ffb3c0;font-size:13px">&#9888; {esc(w)}</div>'
        for w in warnings)

    with status_lock:
        feed_items = list(status_feed[-25:])
    feed = "\n".join(f'<div class="fi">{i}</div>' for i in reversed(feed_items)) or '<div class="empty">Nothing yet.</div>'

    n_music = len(music_pool())
    st = sync_info["state"]
    sync_txt = {"off": "OFF", "restoring": "restoring...", "ok": "OK", "error": "ERROR"}[st]
    busy = job_running or bool(link_state) or not restore_done.is_set()

    rep = {
        "@@WARNINGS@@": warn_html,
        "@@RUNCLS@@": "on" if job_running else "",
        "@@RUNTXT@@": "Building..." if job_running else "Idle",
        "@@AUTOCLS@@": "on" if cfg["auto"] else "",
        "@@AUTO@@": "ON" if cfg["auto"] else "OFF",
        "@@AUTOCHK@@": "checked" if cfg["auto"] else "",
        "@@PPD@@": str(cfg["posts_per_day"]),
        "@@VOL@@": str(cfg["music_volume"]),
        "@@POSTED@@": str(len(history.get("combos", []))),
        "@@SYNCCLS@@": {"ok": "on", "error": "warn"}.get(st, ""),
        "@@SYNC@@": sync_txt,
        "@@SYNCMSG@@": (" Sync: " + esc(sync_info["msg"]) + ".") if sync_info["msg"] and st != "off" else "",
        "@@NEXT@@": countdown(next_run[0]),
        "@@FEED@@": feed,
        "@@DRAFTSEL@@": "selected" if cfg["tiktok_draft"] else "",
        "@@PUBSEL@@": "" if cfg["tiktok_draft"] else "selected",
        "@@DESTS@@": render_dests(cfg),
        "@@CAPTIONS@@": esc("\n".join(cfg["captions"])),
        "@@MUSIC@@": render_music(),
        "@@MCOUNT@@": f"{n_music} track{'s' if n_music != 1 else ''}",
        "@@MZERO@@": "zero" if n_music == 0 else "",
        "@@SECTIONS@@": render_sections(),
        "@@DATA@@": esc(DATA_DIR),
        "@@MAXMB@@": str(MAX_UPLOAD_MB),
        "@@RELOAD@@": "setTimeout(()=>location.reload(),4000);" if busy else "",
    }
    html = PAGE
    for k, v in rep.items():
        html = html.replace(k, v)
    return html


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _authorized(self):
        if not DASHBOARD_PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                return hmac.compare_digest(pw, DASHBOARD_PASSWORD)
            except Exception:
                return False
        return False

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="UGC Bot"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self):
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_file(self, path):
        if not os.path.isfile(path):
            return self._send(404, b"not found")
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            s, _, e = rng[6:].partition("-")
            try:
                start = int(s) if s else max(0, size - int(e))
                end = int(e) if (e and s) else size - 1
                end = min(end, size - 1)
                code = 206
            except ValueError:
                start, end, code = 0, size - 1, 200
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1024 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, b"ok")
        if not self._authorized():
            return self._deny()
        if path == "/":
            return self._send(200, render_page().encode(), "text/html; charset=utf-8")
        if path == "/out/latest.mp4":
            return self._serve_file(os.path.join(OUT_DIR, "latest.mp4"))
        m = re.match(r"^/clip/([a-z_]+)/(.+)$", path)
        if m and m.group(1) in SECTION_IDS:
            return self._serve_file(os.path.join(clip_dir(m.group(1)), safe_name(urllib.parse.unquote(m.group(2)))))
        m = re.match(r"^/music/file/(.+)$", path)
        if m:
            return self._serve_file(os.path.join(MUSIC_DIR, safe_name(urllib.parse.unquote(m.group(1)))))
        m = re.match(r"^/music/link/([0-9a-f]+)$", path)
        if m:
            return self._serve_file(os.path.join(LINK_AUDIO_DIR, m.group(1) + ".m4a"))
        self._send(404, b"not found")

    def do_PUT(self):
        if not self._authorized():
            return self._deny()
        if urllib.parse.urlparse(self.path).path != "/upload":
            return self._send(404, b"not found")
        if not restore_done.is_set():
            return self._send(503, b"still restoring from GitHub, try again in a moment")
        q = self._query()
        section = (q.get("section") or [""])[0]
        name = safe_name((q.get("name") or [""])[0])
        if section == "music":
            folder, exts = MUSIC_DIR, AUDIO_EXT
        elif section in SECTION_IDS:
            folder, exts = clip_dir(section), VIDEO_EXT
        else:
            return self._send(400, b"bad section")
        if not name.lower().endswith(exts):
            return self._send(400, ("bad file type, use: " + " ".join(exts)).encode())
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_UPLOAD_MB * 1024 * 1024:
            return self._send(413, f"file too large (max {MAX_UPLOAD_MB} MB)".encode())
        dest = os.path.join(folder, name)
        if os.path.exists(dest):
            base, ext = os.path.splitext(name)
            dest = os.path.join(folder, f"{base}_{int(time.time())}{ext}")
        part = dest + ".part"
        try:
            with open(part, "wb") as f:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(1024 * 1024, left))
                    if not chunk:
                        raise IOError("upload interrupted")
                    f.write(chunk)
                    left -= len(chunk)
            os.replace(part, dest)
            log(f"Uploaded {section}/{os.path.basename(dest)} ({length / 1_000_000:.1f} MB)")
            request_sync(f"add {section}/{os.path.basename(dest)}")
            self._send(200, b"ok")
        except Exception as e:
            try:
                os.remove(part)
            except OSError:
                pass
            log(f"Upload failed: {e}")
            self._send(500, b"upload failed")

    def do_POST(self):
        if not self._authorized():
            return self._deny()
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode() if length else ""
        busy = not restore_done.is_set()

        if path == "/delete":
            q = self._query()
            section = (q.get("section") or [""])[0]
            name = safe_name((q.get("name") or [""])[0])
            folder = MUSIC_DIR if section == "music" else clip_dir(section) if section in SECTION_IDS else None
            if folder and not busy:
                try:
                    os.remove(os.path.join(folder, name))
                    log(f"Deleted {section}/{name}")
                    request_sync(f"delete {section}/{name}")
                except OSError:
                    pass
            return self._send(200, b"ok")

        if path == "/music/add":
            if busy:
                status("⏳ Still restoring from GitHub - try again in a moment.")
            else:
                add_music_link(urllib.parse.parse_qs(body).get("url", [""])[0])
            return self._send(200, b"ok")

        if path == "/music/delete":
            lid = (self._query().get("id") or [""])[0]
            if re.match(r"^[0-9a-f]+$", lid) and not busy:
                delete_music_link(lid)
            return self._send(200, b"ok")

        if path == "/save":
            f = urllib.parse.parse_qs(body)
            old = get_config()
            try:
                ppd = max(1, min(15, int(f.get("posts_per_day", ["2"])[0])))
            except ValueError:
                ppd = old["posts_per_day"]
            try:
                vol = max(0, min(200, int(f.get("music_volume", ["100"])[0])))
            except ValueError:
                vol = old["music_volume"]
            captions = [l.strip() for l in f.get("captions", [""])[0].splitlines() if l.strip()]
            new = {
                "auto": "auto" in f,
                "posts_per_day": ppd,
                "music_volume": vol,
                "tiktok_draft": f.get("tiktok_mode", ["public"])[0] == "draft",
                "destinations": f.get("dest", []),
                "captions": captions or old["captions"],
            }
            save_config(new)
            if new["auto"] and not old["auto"]:
                next_run[0] = time.time() + 60
            elif ppd != old["posts_per_day"]:
                next_run[0] = time.time() + interval_seconds(new)
            log(f"Settings saved: auto={new['auto']} ppd={ppd} draft={new['tiktok_draft']} dests={len(new['destinations'])}")
            return self._redirect()

        if path == "/run":
            status("▶️ Manual run started.")
            start_job(True)
            return self._redirect()
        if path == "/preview":
            status("▶️ Preview build started.")
            start_job(False)
            return self._redirect()
        self._send(404, b"not found")


def main():
    log(f"Data folder: {DATA_DIR} (volume: {HAS_VOLUME}) | store repo: {STORE_DIR}")
    if SYNC_ON:
        log(f"GitHub sync ON -> {GITHUB_REPO or 'custom remote'} ({GITHUB_BRANCH})")
    else:
        log("GitHub sync OFF - set GITHUB_TOKEN and GITHUB_REPO so clips survive redeploys.")
    if not ZERNIO_API_KEY:
        log("WARNING: ZERNIO_API_KEY not set - posting disabled (preview still works).")
    for tool in ("ffmpeg", "ffprobe") + (("git",) if SYNC_ON else ()):
        if not shutil.which(tool):
            log(f"WARNING: {tool} not found on PATH.")
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever, daemon=True).start()
    threading.Thread(target=restore_and_reload, daemon=True).start()
    threading.Thread(target=sync_worker, daemon=True).start()
    threading.Thread(target=autopilot, daemon=True).start()
    log(f"UGC bot running on port {port}.")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
