"""UGC clip bot.

Upload clips into sections on the dashboard. Each run picks one random clip per
section (in order), stitches them into one vertical 1080x1920 video with ffmpeg,
and posts it through Zernio (TikTok drafts by default, YouTube optional).

Railway env vars:
  ZERNIO_API_KEY      required
  DASHBOARD_PASSWORD  strongly recommended (the page can upload files and post)
  DATA_DIR            optional, defaults to /data if a volume is mounted there
  FFMPEG_THREADS      optional, default 2
Needs: pip install requests   and ffmpeg/ffprobe installed.
"""
import os
import re
import json
import time
import hmac
import base64
import random
import shutil
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

SECTION_IDS = [s[0] for s in SECTIONS]
SECTION_LABELS = dict(SECTIONS)
ALLOWED_EXT = (".mp4", ".mov", ".m4v", ".webm", ".mkv")

ZERNIO_API_KEY = os.environ.get("ZERNIO_API_KEY", "")
ZERNIO_BASE = "https://zernio.com/api/v1"
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "2")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
STARTUP_WAIT_HOURS = float(os.environ.get("STARTUP_WAIT_HOURS", "0.05"))


def _pick_data_dir():
    for d in (os.environ.get("DATA_DIR"), "/data"):
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            return d, True
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(d, exist_ok=True)
    return d, False


DATA_DIR, HAS_VOLUME = _pick_data_dir()
CLIPS_DIR = os.path.join(DATA_DIR, "clips")
OUT_DIR = os.path.join(DATA_DIR, "out")
CONFIG_FILE = os.path.join(DATA_DIR, "ugc_config.json")
HISTORY_FILE = os.path.join(DATA_DIR, "ugc_history.json")
for _sid in SECTION_IDS:
    os.makedirs(os.path.join(CLIPS_DIR, _sid), exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

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


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Config + history
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "auto": False,                 # auto-post on a schedule
    "posts_per_day": 2,
    "tiktok_draft": True,          # True = TikTok drafts, False = publish now
    "destinations": [],            # Zernio account ids; empty = first TikTok account
    "captions": [
        "Make UGC videos with AI in minutes #ugc #ai #claude",
        "This AI workflow makes UGC for you #ugc #aitools #krea",
    ],
}
config_lock = threading.Lock()


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
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


# ---------------------------------------------------------------------------
# Clip library
# ---------------------------------------------------------------------------
def clip_dir(section):
    return os.path.join(CLIPS_DIR, section)


def list_clips(section):
    d = clip_dir(section)
    try:
        names = [n for n in os.listdir(d) if n.lower().endswith(ALLOWED_EXT) and not n.endswith(".part")]
    except FileNotFoundError:
        return []
    return sorted(names)


def safe_name(name):
    name = os.path.basename(name or "")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .") or "clip.mp4"
    return name


# ---------------------------------------------------------------------------
# Video building (ffmpeg)
# ---------------------------------------------------------------------------
def has_audio(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        return "audio" in r.stdout
    except Exception:
        return False


def run_ffmpeg(cmd, what):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        if r.returncode < 0:
            raise RuntimeError(f"{what}: ffmpeg killed by signal {-r.returncode} "
                               f"(likely out of memory; lower FFMPEG_THREADS)")
        tail = "\n".join([l for l in r.stderr.splitlines() if l.strip()][-8:])
        raise RuntimeError(f"{what}: ffmpeg failed:\n{tail}")


def normalize(src, dst):
    """Any clip -> 1080x1920, 30fps, h264 + stereo aac 44.1k. Whole frame kept,
    blurred copy fills the bars (works for landscape, square or vertical)."""
    audio = has_audio(src)
    vf = (
        "[0:v]split[a][b];"
        "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        "boxblur=10:2,scale=1080:1920,setsar=1[bg];"
        "[b]scale=1080:1920:force_original_aspect_ratio=decrease,setsar=1[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p[v]"
    )
    cmd = ["ffmpeg", "-y", "-i", src]
    if not audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    cmd += ["-filter_complex", vf, "-map", "[v]", "-map", "0:a:0" if audio else "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-threads", FFMPEG_THREADS,
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    if not audio:
        cmd += ["-shortest"]
    cmd += ["-movflags", "+faststart", dst]
    run_ffmpeg(cmd, f"normalize {os.path.basename(src)}")


def concat(parts, out_path, workdir):
    lst = os.path.join(workdir, "list.txt")
    with open(lst, "w") as f:
        for p in parts:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                "-c", "copy", "-movflags", "+faststart", out_path], "concat")


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


def build_video(history):
    picks, key = pick_clips(history)
    work = tempfile.mkdtemp(prefix="ugc_")
    try:
        parts = []
        for i, (sid, fname) in enumerate(picks):
            status(f"🎞️ {esc(SECTION_LABELS[sid])}: {esc(fname)}")
            dst = os.path.join(work, f"{i:02d}.mp4")
            normalize(os.path.join(clip_dir(sid), fname), dst)
            parts.append(dst)
        out = os.path.join(OUT_DIR, "latest.mp4")
        tmp_out = os.path.join(work, "final.mp4")
        concat(parts, tmp_out, work)
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
    if not force and time.time() - _acc_cache["t"] < 60 and _acc_cache["data"]:
        return _acc_cache["data"]
    try:
        r = requests.get(f"{ZERNIO_BASE}/accounts", headers=_zh(), timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
        _acc_cache.update(t=time.time(), data=r.json().get("accounts", []), error="")
    except Exception as e:
        _acc_cache["error"] = str(e)
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
    if not job_lock.acquire(blocking=False):
        status("⏳ A video is already being built - wait for it to finish.")
        return
    job_running = True
    try:
        cfg = get_config()
        history = load_history()
        status("🎬 Building a new video..." if not do_post else "🎬 Building a new video to post...")
        out, key = build_video(history)
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
        if cfg["auto"] and time.time() >= next_run[0] and not job_running:
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
.badges{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.badge{background:var(--p2);border:1px solid var(--border);border-radius:999px;padding:6px 12px;font-size:12.5px;color:var(--muted)}
.badge b{color:var(--text)}.badge.on{color:var(--ok);border-color:rgba(53,212,136,.35)}.badge.warn{color:var(--a);border-color:rgba(255,59,92,.4)}
button,.btn{background:linear-gradient(135deg,var(--a),var(--a2));color:#fff;border:0;padding:11px 16px;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer}
button.sec{background:var(--p2);border:1px solid var(--border);color:var(--text)}
.row{display:flex;gap:10px;flex-wrap:wrap}.row form{margin:0}
label{display:block;font-size:12.5px;color:var(--muted);margin:12px 0 6px}
input[type=number],select,textarea{background:var(--p2);border:1px solid var(--border);border-radius:8px;padding:8px 10px;color:var(--text);font:inherit;font-size:13.5px}
textarea{width:100%;min-height:90px}
.feed{display:flex;flex-direction:column;gap:7px;max-height:340px;overflow-y:auto}
.fi{background:#08080d;border:1px solid var(--border);border-radius:10px;padding:9px 12px;font-size:13px;line-height:1.5}
.fi a,.clip a{color:var(--a2);text-decoration:none}
.empty{color:var(--muted);font-size:13px}
.sec-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px}
.sec-head b{font-size:15px}.cnt{font-size:12px;color:var(--muted)}.cnt.zero{color:var(--a)}
.clip{display:flex;justify-content:space-between;gap:8px;font-size:12.5px;padding:5px 0;border-top:1px solid var(--border)}
.clip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.x{background:none;border:0;color:var(--muted);padding:0 4px;cursor:pointer;font-size:16px}
.hint{font-size:11.5px;color:var(--muted);margin-top:8px;line-height:1.5}
.chk{display:flex;gap:8px;align-items:center;font-size:13px;margin:6px 0;color:var(--text)}
.up{margin-top:10px;font-size:12px;color:var(--muted)}
</style></head><body><div class="wrap">
<h1>UGC Bot</h1>
<div class="sub">Random clip per section, stitched into one video, posted for you.</div>
@@WARNINGS@@
<div class="grid">
 <div class="card">
  <h2>Run</h2>
  <div class="badges">
   <div class="badge @@RUNCLS@@">@@RUNTXT@@</div>
   <div class="badge @@AUTOCLS@@">Auto-post <b>@@AUTO@@</b></div>
   <div class="badge">Posts/day <b>@@PPD@@</b></div>
   <div class="badge">Posted <b>@@POSTED@@</b></div>
  </div>
  <div class="row">
   <form method="POST" action="/preview"><button class="sec" type="submit">Build preview only</button></form>
   <form method="POST" action="/run"><button type="submit">Build &amp; post now</button></form>
  </div>
  <div class="hint">Preview builds a video from random clips without posting, so you can check it. Next scheduled run in @@NEXT@@ (only if auto-post is on).</div>
  <h2 style="margin-top:18px">Activity</h2>
  <div class="feed">@@FEED@@</div>
 </div>
 <div class="card">
  <h2>Settings</h2>
  <form method="POST" action="/save">
   <div class="chk"><input type="checkbox" id="auto" name="auto" value="1" @@AUTOCHK@@><label for="auto" style="margin:0;color:var(--text)">Auto-post on a schedule</label></div>
   <label>Posts per day</label><input type="number" name="posts_per_day" min="1" max="15" value="@@PPD@@">
   <label>TikTok mode</label>
   <select name="tiktok_mode"><option value="draft" @@DRAFTSEL@@>Draft (you publish in the TikTok app)</option><option value="public" @@PUBSEL@@>Publish immediately</option></select>
   <label>Post to (accounts connected in Zernio)</label>
   @@DESTS@@
   <label>Captions (one per line, a random one is used)</label>
   <textarea name="captions">@@CAPTIONS@@</textarea>
   <button type="submit" style="margin-top:14px">Save settings</button>
  </form>
 </div>
</div>
<div class="card"><h2>Clip sections (in video order)</h2>
 <div class="hint" style="margin:0 0 6px">Each run uses one random clip from every section that has clips. Empty sections are skipped. Any size or orientation works.</div>
</div>
@@SECTIONS@@
<div class="hint">Data folder: @@DATA@@</div>
</div>
<script>
async function upload(sec,input){
 const files=[...input.files],out=document.getElementById('up_'+sec);
 for(let i=0;i<files.length;i++){
  out.textContent='Uploading '+(i+1)+'/'+files.length+': '+files[i].name+' ...';
  try{
   const r=await fetch('/upload?section='+sec+'&name='+encodeURIComponent(files[i].name),{method:'PUT',body:files[i]});
   if(!r.ok){out.textContent='Failed: '+files[i].name+' (HTTP '+r.status+')';return;}
  }catch(e){out.textContent='Failed: '+files[i].name+' ('+e+')';return;}
 }
 location.reload();
}
async function del(sec,name){
 if(!confirm('Delete '+name+'?'))return;
 await fetch('/delete?section='+sec+'&name='+encodeURIComponent(name),{method:'POST'});
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
                f'<button class="x" title="Delete" onclick="del(\'{sid}\',decodeURIComponent(\'{q}\'))">&times;</button></div>'
            )
        cnt = f'<span class="cnt {"zero" if not files else ""}">{len(files)} clip{"s" if len(files) != 1 else ""}</span>'
        out.append(
            f'<div class="card"><div class="sec-head"><b>{esc(label)}</b>{cnt}</div>'
            f'{"".join(rows) or "<div class=empty>No clips yet.</div>"}'
            f'<div class="up"><input type="file" accept="video/*" multiple onchange="upload(\'{sid}\',this)"> '
            f'<span id="up_{sid}"></span></div></div>'
        )
    return "\n".join(out)


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
    if not DASHBOARD_PASSWORD:
        warnings.append("No DASHBOARD_PASSWORD set - anyone with this URL can upload and post. Set it in Railway.")
    if not HAS_VOLUME:
        warnings.append("No persistent volume at /data - uploaded clips are lost on every redeploy. Add a Railway volume mounted at /data.")
    if not ZERNIO_API_KEY:
        warnings.append("ZERNIO_API_KEY is not set - posting is disabled.")
    warn_html = "".join(f'<div class="card" style="border-color:rgba(255,59,92,.4);color:#ffb3c0;font-size:13px">&#9888; {esc(w)}</div>' for w in warnings)

    with status_lock:
        feed_items = list(status_feed[-25:])
    feed = "\n".join(f'<div class="fi">{i}</div>' for i in reversed(feed_items)) or '<div class="empty">Nothing yet.</div>'

    html = PAGE
    rep = {
        "@@WARNINGS@@": warn_html,
        "@@RUNCLS@@": "on" if job_running else "",
        "@@RUNTXT@@": "Building..." if job_running else "Idle",
        "@@AUTOCLS@@": "on" if cfg["auto"] else "",
        "@@AUTO@@": "ON" if cfg["auto"] else "OFF",
        "@@AUTOCHK@@": "checked" if cfg["auto"] else "",
        "@@PPD@@": str(cfg["posts_per_day"]),
        "@@POSTED@@": str(len(history.get("combos", []))),
        "@@NEXT@@": countdown(next_run[0]),
        "@@FEED@@": feed,
        "@@DRAFTSEL@@": "selected" if cfg["tiktok_draft"] else "",
        "@@PUBSEL@@": "" if cfg["tiktok_draft"] else "selected",
        "@@DESTS@@": render_dests(cfg),
        "@@CAPTIONS@@": esc("\n".join(cfg["captions"])),
        "@@SECTIONS@@": render_sections(),
        "@@DATA@@": esc(DATA_DIR),
        "@@RELOAD@@": "setTimeout(()=>location.reload(),4000);" if job_running else "",
    }
    for k, v in rep.items():
        html = html.replace(k, v)
    return html


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # --- helpers ---
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
        self.send_header("Content-Type", "video/mp4" if not path.endswith(".webm") else "video/webm")
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

    # --- routes ---
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
            name = safe_name(urllib.parse.unquote(m.group(2)))
            return self._serve_file(os.path.join(clip_dir(m.group(1)), name))
        self._send(404, b"not found")

    def do_PUT(self):
        if not self._authorized():
            return self._deny()
        path = urllib.parse.urlparse(self.path).path
        if path != "/upload":
            return self._send(404, b"not found")
        q = self._query()
        section = (q.get("section") or [""])[0]
        name = safe_name((q.get("name") or [""])[0])
        if section not in SECTION_IDS or not name.lower().endswith(ALLOWED_EXT):
            return self._send(400, b"bad section or file type (use mp4/mov/m4v/webm/mkv)")
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_UPLOAD_MB * 1024 * 1024:
            return self._send(413, f"file too large (max {MAX_UPLOAD_MB} MB)".encode())
        dest = os.path.join(clip_dir(section), name)
        if os.path.exists(dest):
            base, ext = os.path.splitext(name)
            dest = os.path.join(clip_dir(section), f"{base}_{int(time.time())}{ext}")
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

        if path == "/delete":
            q = self._query()
            section = (q.get("section") or [""])[0]
            name = safe_name((q.get("name") or [""])[0])
            if section in SECTION_IDS:
                try:
                    os.remove(os.path.join(clip_dir(section), name))
                    log(f"Deleted {section}/{name}")
                except OSError:
                    pass
            return self._send(200, b"ok")

        if path == "/save":
            f = urllib.parse.parse_qs(body)
            old = get_config()
            try:
                ppd = max(1, min(15, int(f.get("posts_per_day", ["2"])[0])))
            except ValueError:
                ppd = old["posts_per_day"]
            captions = [l.strip() for l in f.get("captions", [""])[0].splitlines() if l.strip()]
            new = {
                "auto": "auto" in f,
                "posts_per_day": ppd,
                "tiktok_draft": f.get("tiktok_mode", ["draft"])[0] != "public",
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
    log(f"Data folder: {DATA_DIR} (volume: {HAS_VOLUME})")
    if not DASHBOARD_PASSWORD:
        log("WARNING: DASHBOARD_PASSWORD not set - dashboard is open to anyone with the URL.")
    if not ZERNIO_API_KEY:
        log("WARNING: ZERNIO_API_KEY not set - posting disabled (preview still works).")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            log(f"WARNING: {tool} not found on PATH - video building will fail.")
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever, daemon=True).start()
    threading.Thread(target=autopilot, daemon=True).start()
    log(f"UGC bot running on port {port}.")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
