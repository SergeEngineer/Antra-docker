from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path
from threading import Thread, Lock
from datetime import datetime, timedelta, timezone
import subprocess
import os
import json
import uuid
import re
import time

# test 

app = FastAPI(
    title="Antra",
    description="Antra Web API",
    version="1.2.0",
)


MUSIC_DIR = Path(
    os.getenv("OUTPUT_DIR", "/music")
)

CONFIG_DIR = Path(
    os.getenv("ANTRA_CONFIG_DIR", "/config")
)

# ---------------------------------------------------------------------------
# Persisted web UI settings
# ---------------------------------------------------------------------------

SETTINGS_FILE = CONFIG_DIR / "web_settings.json"

DEFAULT_SETTINGS = {
    "output_format": "auto",
    "download_quality": "lossless",
    "single_track_filename": "{artist} - {title}",
    "album_track_filename": "{artist} - {title}",
    "folder_structure": "{album_artist}/{year} - {album}",
}

ALLOWED_FORMATS = {"auto", "flac", "alac", "aac", "mp3", "atmos"}
ALLOWED_QUALITIES = {"highest", "lossless", "320", "256", "192", "128"}


class SettingsRequest(BaseModel):
    output_format: str = DEFAULT_SETTINGS["output_format"]
    download_quality: str = DEFAULT_SETTINGS["download_quality"]
    single_track_filename: str = DEFAULT_SETTINGS["single_track_filename"]
    album_track_filename: str = DEFAULT_SETTINGS["album_track_filename"]
    folder_structure: str = DEFAULT_SETTINGS["folder_structure"]


def load_settings() -> dict:
    """Read persisted settings from CONFIG_DIR, falling back to defaults.

    Explicitly checks whether the file exists first, and logs every
    outcome (found/missing/corrupt) so a broken /config mount or a
    permissions problem is visible in `docker logs` instead of silently
    falling back to defaults with no trace.
    """

    settings = DEFAULT_SETTINGS.copy()

    if not SETTINGS_FILE.exists():
        print(
            f"[settings] {SETTINGS_FILE} does not exist yet -- "
            f"using built-in defaults: {settings}"
        )
        return settings

    try:
        with open(SETTINGS_FILE, "r") as f:
            saved = json.load(f)

        for key in DEFAULT_SETTINGS:
            if key in saved and isinstance(saved[key], str) and saved[key].strip():
                settings[key] = saved[key]

        print(f"[settings] loaded from {SETTINGS_FILE}: {settings}")

    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"[settings] could not read {SETTINGS_FILE} ({exc}); "
            f"using built-in defaults: {settings}"
        )

    return settings


def save_settings(settings: dict) -> None:
    """Persist settings to CONFIG_DIR so they survive container restarts."""

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)

    print(f"[settings] saved to {SETTINGS_FILE}: {settings}")


# ---------------------------------------------------------------------------
# Persisted download queue
# ---------------------------------------------------------------------------
#
# Every URL added in the Add Music tab becomes a queue entry stored in
# CONFIG_DIR/queue.json. Entries survive container restarts. A background
# thread wakes up once a minute and re-triggers any entry whose schedule
# (daily/weekly/monthly) is due.

QUEUE_FILE = CONFIG_DIR / "queue.json"

ALLOWED_SCHEDULES = {"none", "daily", "weekly", "monthly"}

SCHEDULE_SECONDS = {
    "daily": 24 * 60 * 60,
    "weekly": 7 * 24 * 60 * 60,
    "monthly": 30 * 24 * 60 * 60,
}

queue_lock = Lock()

# Transient, in-memory per-run job state (detailed live logs). Keyed by
# job_id. Does NOT survive a restart -- that's fine, it's just the log
# feed for whichever download is currently/most-recently running for a
# queue item.
jobs: dict = {}

# job_id -> subprocess.Popen, so the Stop button can terminate a running
# download. Also transient/in-memory only.
running_processes: dict = {}

# job_id -> True once a Stop request has been issued for that job, so the
# code that observes the process exiting can tell "stopped on purpose"
# apart from "crashed".
stop_flags: dict = {}

# Exactly one Antra process may download at a time. Additional queue items remain
# pending until the active process finishes or is stopped.
download_state_lock = Lock()
active_job_id: str | None = None


def load_queue() -> list:
    if not QUEUE_FILE.exists():
        print(f"[queue] {QUEUE_FILE} does not exist yet -- starting empty")
        return []

    try:
        with open(QUEUE_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[queue] could not read {QUEUE_FILE} ({exc}); starting empty")
        return []


def save_queue(items: list) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    with open(QUEUE_FILE, "w") as f:
        json.dump(items, f, indent=2)


def find_queue_item(items: list, item_id: str):
    for item in items:
        if item.get("id") == item_id:
            return item
    return None


def update_queue_item(item_id: str, **fields) -> None:
    """Read-modify-write a single queue item under a lock, so concurrent
    download-progress updates and API requests never clobber each other."""

    with queue_lock:
        items = load_queue()
        item = find_queue_item(items, item_id)

        if item is None:
            return

        item.update(fields)
        save_queue(items)


def safe_playlist_name(name: str) -> str:
    """Return a filesystem-safe playlist folder/file name."""
    name = (name or "Untitled Playlist").strip()
    # Remove characters that are invalid on Windows/Linux shared libraries and
    # collapse whitespace so the folder name stays predictable.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "Untitled Playlist"


def playlist_folder_path(playlist_name: str) -> Path:
    """Antra's playlist 'flat' layout is /music/<Playlist Name>."""
    return MUSIC_DIR / safe_playlist_name(playlist_name)


def new_queue_item(url: str, schedule: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()

    return {
        "id": str(uuid.uuid4()),
        "url": url,
        "playlist_name": "",
        "folder_path": "",
        "added_at": now,
        "schedule": schedule if schedule in ALLOWED_SCHEDULES else "none",
        "last_sync": None,
        "status": "queued",
        "message": "Queued",
        "progress": {
            "track_count": 0,
            "downloaded": 0,
            "failed": 0,
            "skipped": 0,
            "current_artist": "",
            "current_track": "",
        },
        "job_id": None,
    }


def reset_stale_downloading_items() -> None:
    """On startup, any item still marked 'downloading' or 'queued' belongs
    to a process that no longer exists (the container just restarted)."""

    with queue_lock:
        items = load_queue()
        changed = False

        for item in items:
            if item.get("status") in ("downloading", "queued", "pending"):
                item["status"] = "idle"
                item["message"] = "Interrupted by restart"
                changed = True

        if changed:
            save_queue(items)
            print("[queue] reset stale downloading/queued items after restart")
def normalize_url(url: str) -> str:
    """
    Convert normal YouTube URLs to the YouTube Music equivalent.

    Antra's built-in YouTube metadata fetcher currently recognizes
    music.youtube.com URLs.
    """

    url = url.strip()

    if re.match(r"https?://(www\.)?youtube\.com/watch\?", url):
        return url.replace(
            "https://www.youtube.com/",
            "https://music.youtube.com/"
        ).replace(
            "https://youtube.com/",
            "https://music.youtube.com/"
        )

    match = re.match(
        r"https?://youtu\.be/([^?&/]+)(.*)",
        url
    )

    if match:
        video_id = match.group(1)
        query = match.group(2)

        return (
            f"https://music.youtube.com/watch"
            f"?v={video_id}"
            f"{query}"
        )

    return url


def _progress_snapshot(job: dict) -> dict:
    return {
        "track_count": job.get("track_count", 0),
        "downloaded": job.get("downloaded", 0),
        "failed": job.get("failed", 0),
        "skipped": job.get("skipped", 0),
        "current_index": job.get("current_index", 0),
        "current_total": job.get("current_total", job.get("track_count", 0)),
        "current_artist": job.get("current_artist", ""),
        "current_track": job.get("current_track", ""),
    }


def _update_track_progress(
    job: dict,
    message: str = "",
    payload: dict | None = None,
    event_name: str = "",
) -> None:
    """Extract live track position from Antra JSON events/log messages."""
    payload = payload or {}
    index = payload.get("track_index") or payload.get("index") or payload.get("current_index")
    total = payload.get("track_total") or payload.get("total") or payload.get("current_total")

    if isinstance(index, str) and index.isdigit():
        index = int(index)
    if isinstance(total, str) and total.isdigit():
        total = int(total)

    if not (isinstance(index, int) and isinstance(total, int) and total > 0):
        match = re.search(r"\[(?:Downloading|download(?:ing)?|track_started|track_start)[^\]]*\]?\s*\[(\d+)\s*/\s*(\d+)\]", message or "", re.I)
        if not match:
            match = re.search(r"\[(\d+)\s*/\s*(\d+)\]", message or "")
        if match:
            index, total = int(match.group(1)), int(match.group(2))

    if isinstance(total, int) and total > 0:
        job["track_count"] = total
        job["current_total"] = total

    if isinstance(index, int) and index > 0:
        job["current_index"] = index
        if event_name.lower() in {"track_completed", "track_finished", "track_downloaded", "track_skipped"}:
            job["downloaded"] = max(job.get("downloaded", 0), index)
        else:
            job["downloaded"] = max(job.get("downloaded", 0), index - 1)

    track = payload.get("track") or payload.get("title")
    artist = payload.get("artist") or payload.get("track_artist")
    if track:
        job["current_track"] = str(track)
    if artist:
        job["current_artist"] = str(artist)

    if message and (not job.get("current_track") or not job.get("current_artist")):
        m = re.search(
            r"\[Downloading\]\s*\[\d+\s*/\s*\d+\]\s*(.*?)\s+by\s+(.*?)(?:\s+\([^)]*\))?$",
            message,
        )
        if m:
            job["current_track"] = m.group(1).strip()
            job["current_artist"] = m.group(2).strip()


def run_download(job_id: str, item_id: str, url: str, settings: dict):

    jobs[job_id]["status"] = "running"
    jobs[job_id]["message"] = "Starting Antra..."
    update_queue_item(item_id, status="downloading", message="Starting Antra...")

    normalized_url = normalize_url(url)
    jobs[job_id]["url"] = normalized_url

    env = os.environ.copy()

    # Make absolutely sure Antra writes to our mounted music directory.
    env["OUTPUT_DIR"] = str(MUSIC_DIR)

    # Keep Antra configuration persistent.
    env["ANTRA_CONFIG_DIR"] = str(CONFIG_DIR)

    # Apply the current global settings to this download.
    env["OUTPUT_FORMAT"] = settings.get("output_format", "auto")
    env["DOWNLOAD_QUALITY"] = settings.get("download_quality", "lossless")
    env["SINGLE_TRACK_FILENAME_TEMPLATE"] = settings.get(
        "single_track_filename", "{artist} - {title}"
    )
    env["ALBUM_TRACK_FILENAME_TEMPLATE"] = settings.get(
        "album_track_filename", "{artist} - {title}"
    )

    folder_structure = settings.get(
        "folder_structure", "{album_artist}/{year} - {album}"
    )
    env["FOLDER_STRUCTURE"] = folder_structure
    env["ALBUM_FOLDER_STRUCTURE"] = folder_structure

    # Playlists are kept in their own top-level folder named after the playlist.
    # Antra's current playlist layout supports: /music/<Playlist Name>/tracks.
    env["PLAYLIST_FOLDER_STRUCTURE"] = "flat"

    # Never create "file (2).ext" or other conflict copies during resync.
    env["FILENAME_CONFLICT_BEHAVIOR"] = "skip"

    jobs[job_id]["settings"] = settings

    try:
        process = subprocess.Popen(
            [
                "python",
                "-m",
                "antra.json_cli",
                normalized_url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )

        jobs[job_id]["pid"] = process.pid
        running_processes[job_id] = process

        for line in process.stdout:

            line = line.strip()

            if not line:
                continue

            jobs[job_id]["logs"].append(line)

            try:
                event = json.loads(line)
                event_type = event.get("type")

                if event_type == "log":
                    message = event.get("message", "")
                    jobs[job_id]["message"] = message
                    _update_track_progress(jobs[job_id], message=message)
                    update_queue_item(
                        item_id,
                        message=message,
                        progress=_progress_snapshot(jobs[job_id]),
                    )

                elif event_type == "playlist_loaded":
                    jobs[job_id]["title"] = event.get("title", "")
                    jobs[job_id]["track_count"] = event.get("track_count", 0)
                    jobs[job_id]["current_total"] = event.get("track_count", 0)
                    playlist_name = safe_playlist_name(jobs[job_id]["title"])
                    folder = playlist_folder_path(playlist_name)
                    jobs[job_id]["playlist_name"] = playlist_name
                    jobs[job_id]["folder_path"] = str(folder)
                    jobs[job_id]["message"] = (
                        f"Ready — {jobs[job_id]['track_count']} track(s) queued for download"
                    )
                    update_queue_item(
                        item_id,
                        playlist_name=playlist_name,
                        folder_path=str(folder),
                        message=jobs[job_id]["message"],
                        progress=_progress_snapshot(jobs[job_id]),
                    )

                elif event_type == "event":
                    payload = event.get("payload") or {}
                    event_name = str(event.get("name") or payload.get("event") or "")
                    message = payload.get("message") or event_name or ""
                    jobs[job_id]["message"] = message
                    _update_track_progress(
                        jobs[job_id],
                        message=message,
                        payload=payload,
                        event_name=event_name,
                    )
                    update_queue_item(
                        item_id,
                        message=message,
                        progress=_progress_snapshot(jobs[job_id]),
                    )

                elif event_type == "playlist_summary":
                    jobs[job_id]["downloaded"] = event.get("downloaded", 0)
                    jobs[job_id]["failed"] = event.get("failed", 0)
                    jobs[job_id]["skipped"] = event.get("skipped", 0)
                    jobs[job_id]["current_index"] = jobs[job_id]["track_count"]
                    jobs[job_id]["current_total"] = jobs[job_id]["track_count"]

                    if event.get("error"):
                        jobs[job_id]["message"] = event["error"]
                    else:
                        jobs[job_id]["message"] = (
                            f"Finished — {event.get('downloaded', 0)} downloaded, "
                            f"{event.get('failed', 0)} failed, "
                            f"{event.get('skipped', 0)} skipped"
                        )

                    update_queue_item(
                        item_id,
                        message=jobs[job_id]["message"],
                        progress=_progress_snapshot(jobs[job_id]),
                    )

            except json.JSONDecodeError:
                jobs[job_id]["message"] = line

        return_code = process.wait()
        jobs[job_id]["return_code"] = return_code

        stopped = stop_flags.pop(job_id, False)

        if stopped:
            jobs[job_id]["status"] = "stopped"
            update_queue_item(item_id, status="stopped", message="Stopped by user")

        elif return_code == 0:
            jobs[job_id]["status"] = "completed"
            now = datetime.now(timezone.utc).isoformat()

            update_queue_item(
                item_id,
                status="completed",
                message=jobs[job_id]["message"] or "Completed",
                last_sync=now,
                progress=_progress_snapshot(jobs[job_id]),
            )

        else:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["message"] = f"Antra exited with code {return_code}"
            update_queue_item(
                item_id, status="failed", message=jobs[job_id]["message"]
            )

    except Exception as exc:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["message"] = str(exc)
        jobs[job_id]["logs"].append(json.dumps({"error": str(exc)}))
        update_queue_item(item_id, status="failed", message=str(exc))

    finally:
        running_processes.pop(job_id, None)
        global active_job_id
        with download_state_lock:
            if active_job_id == job_id:
                active_job_id = None
        start_next_pending_download()


def start_download_for_item(item_id: str) -> str | None:
    """Start one queue item, or leave it pending behind the active download."""
    global active_job_id

    items = load_queue()
    item = find_queue_item(items, item_id)
    if item is None:
        return None

    with download_state_lock:
        if active_job_id is not None:
            job_id = str(uuid.uuid4())
            jobs[job_id] = {
                "job_id": job_id,
                "queue_item_id": item_id,
                "status": "pending",
                "message": "Pending — waiting for the current download to finish",
                "title": "",
                "playlist_name": item.get("playlist_name", ""),
                "folder_path": item.get("folder_path", ""),
                "track_count": 0,
                "current_index": 0,
                "current_total": 0,
                "downloaded": 0,
                "failed": 0,
                "skipped": 0,
                "current_artist": "",
                "current_track": "",
                "logs": [],
                "created": datetime.now().isoformat(),
            }
            update_queue_item(
                item_id,
                status="pending",
                message="Pending — waiting for the current download to finish",
                job_id=job_id,
            )
            return job_id

        job_id = str(uuid.uuid4())
        active_job_id = job_id

    url = item["url"]
    jobs[job_id] = {
        "job_id": job_id,
        "queue_item_id": item_id,
        "status": "queued",
        "message": "Waiting for Antra to start...",
        "title": "",
        "playlist_name": item.get("playlist_name", ""),
        "folder_path": item.get("folder_path", ""),
        "track_count": 0,
        "current_index": 0,
        "current_total": 0,
        "downloaded": 0,
        "failed": 0,
        "skipped": 0,
        "current_artist": "",
        "current_track": "",
        "logs": [],
        "created": datetime.now().isoformat(),
    }

    update_queue_item(item_id, status="downloading", message="Starting download...", job_id=job_id)

    settings = load_settings()
    thread = Thread(
        target=run_download,
        args=(job_id, item_id, url, settings),
        daemon=True,
    )
    thread.start()
    return job_id


def start_next_pending_download() -> None:
    """Start the oldest pending item after the active download finishes."""
    items = load_queue()
    pending = [i for i in items if i.get("status") == "pending"]
    if not pending:
        return
    pending.sort(key=lambda i: i.get("added_at", ""))
    start_download_for_item(pending[0]["id"])

def scheduler_loop():
    """Background thread: once a minute, check every queue item with a
    recurring schedule and re-trigger it if it's due."""

    while True:
        try:
            items = load_queue()
            now = datetime.now(timezone.utc)

            for item in items:
                schedule = item.get("schedule", "none")

                if schedule not in SCHEDULE_SECONDS:
                    continue

                if item.get("status") in ("downloading", "queued", "pending"):
                    continue

                reference = item.get("last_sync") or item.get("added_at")

                try:
                    reference_dt = datetime.fromisoformat(reference)
                except (TypeError, ValueError):
                    continue

                if reference_dt.tzinfo is None:
                    reference_dt = reference_dt.replace(tzinfo=timezone.utc)

                due_at = reference_dt + timedelta(seconds=SCHEDULE_SECONDS[schedule])

                if now >= due_at:
                    print(
                        f"[scheduler] {item['id']} is due for a {schedule} "
                        f"resync -- starting"
                    )
                    start_download_for_item(item["id"])

        except Exception as exc:
            print(f"[scheduler] error: {exc}")

        time.sleep(60)


# ---------------------------------------------------------------------------
# Web interface
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Antra</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #111;
                color: #eee;
                margin: 0;
                padding: 40px;
            }
            .container { max-width: 900px; margin: auto; }
            h1 { font-size: 42px; margin-bottom: 5px; }
            h2 { margin-top: 0; }
            .subtitle { color: #999; margin-bottom: 40px; }
            .card {
                background: #1c1c1c;
                border-radius: 12px;
                padding: 25px;
                margin-bottom: 20px;
            }
            label {
                display: block;
                margin-top: 14px;
                margin-bottom: 6px;
                color: #ccc;
                font-weight: 600;
            }
            input, select {
                width: 100%;
                box-sizing: border-box;
                padding: 12px;
                margin-bottom: 12px;
                border-radius: 8px;
                border: 1px solid #444;
                background: #111;
                color: white;
                font-size: 15px;
            }
            select { cursor: pointer; }
            button {
                padding: 12px 20px;
                border: 0;
                border-radius: 8px;
                background: #444;
                color: white;
                cursor: pointer;
                font-size: 15px;
            }
            button:hover { background: #666; }
            button:disabled { opacity: 0.5; cursor: not-allowed; }
            .tag-buttons {
                display: flex;
                flex-wrap: wrap;
                gap: 6px;
                margin: 2px 0 14px;
            }
            .tag-button {
                padding: 7px 10px;
                font-size: 12px;
                background: #303030;
                border: 1px solid #4a4a4a;
            }
            .tag-button:hover { background: #555; }
            .hint {
                color: #888;
                font-size: 12px;
                margin-top: -4px;
                margin-bottom: 12px;
            }
            .format-buttons, .quality-buttons, .schedule-buttons {
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                margin-bottom: 8px;
            }
            .format-button, .quality-button, .schedule-button {
                padding: 10px 18px;
                font-size: 14px;
                font-weight: 600;
                background: #111;
                border: 1px solid #444;
                border-radius: 8px;
                color: #ccc;
            }
            .format-button:hover, .quality-button:hover, .schedule-button:hover {
                background: #2a2a2a;
                border-color: #666;
            }
            .format-button.active, .quality-button.active, .schedule-button.active {
                background: #f5c518;
                border-color: #f5c518;
                color: #000;
            }
            .schedule-button {
                padding: 6px 12px;
                font-size: 12px;
            }
            .format-note, .quality-note {
                color: #999;
                font-size: 13px;
                margin-bottom: 12px;
            }
            .status { color: #7ddf7d; }
            .error { color: #ff7777; }
            .progress {
                margin-top: 20px;
                padding: 15px;
                background: #111;
                border-radius: 8px;
            }
            .example {
                padding: 10px 12px;
                background: #111;
                border-radius: 8px;
                color: #aaa;
                font-family: monospace;
                font-size: 13px;
                overflow-wrap: anywhere;
            }
            .section {
                border-top: 1px solid #333;
                padding-top: 20px;
                margin-top: 20px;
            }
            pre {
                white-space: pre-wrap;
                word-wrap: break-word;
                max-height: 400px;
                overflow: auto;
                background: #080808;
                padding: 15px;
                border-radius: 8px;
            }
            .box {
                border: 1px solid #444;
                border-radius: 12px;
                padding: 16px;
                background: #1e1e1e;
            }
            #addButton {
                display: block;
                margin-left: auto;
                margin-top: 10px;
                background-color: #f5c518;
                color: #000;
                border: none;
                border-radius: 8px;
                padding: 10px 18px;
                font-weight: 600;
                cursor: pointer;
            }
            #addButton:hover { background-color: #ffd43b; }
            .header {
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 20px;
                text-align: left;
            }
            .header h2 { margin-bottom: 5px; }
            .logo { width: 96px; height: 96px; flex-shrink: 0; }
            .header .subtitle {
                margin-bottom: 0;
                font-size: 34px;
                font-weight: 700;
                color: #eee;
                text-align: left;
            }
            .subtitle { text-align: center; }
            .tabs {
                display: flex;
                gap: 6px;
                margin-bottom: 20px;
                border-bottom: 1px solid #333;
            }
            .tab-button {
                background: none;
                border: none;
                border-radius: 8px 8px 0 0;
                padding: 12px 22px;
                font-size: 15px;
                font-weight: 600;
                color: #999;
                cursor: pointer;
            }
            .tab-button:hover { background: #1c1c1c; color: #eee; }
            .tab-button.active { background: #1c1c1c; color: #f5c518; }
            .tab-panel { display: none; }
            .tab-panel.active { display: block; }

            /* --- Download queue --- */
            .queue-empty {
                color: #888;
                font-size: 14px;
                padding: 10px 0;
            }
            .queue-item {
                border: 1px solid #333;
                border-radius: 10px;
                padding: 16px;
                background: #171717;
                margin-bottom: 14px;
            }
            .queue-item-top {
                display: flex;
                justify-content: space-between;
                align-items: baseline;
                gap: 12px;
                flex-wrap: wrap;
            }
            .queue-item-url {
                font-family: monospace;
                font-size: 13px;
                color: #999;
                word-break: break-all;
                text-align: right;
            }
            .queue-item-playlist {
                font-size: 18px;
                font-weight: 700;
                color: #f5c518;
            }
            .queue-divider {
                border: none;
                border-top: 1px solid #333;
                margin: 12px 0 0;
            }
            .queue-item-folder {
                margin-top: 12px;
                padding: 8px 10px;
                background: #101010;
                border-radius: 6px;
                color: #aaa;
                font-family: monospace;
                font-size: 12px;
                word-break: break-all;
            }
            .queue-item-folder strong {
                color: #ccc;
                font-family: Arial, sans-serif;
            }
            .queue-badge {
                display: inline-block;
                padding: 3px 9px;
                border-radius: 999px;
                font-size: 11px;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.03em;
                margin-right: 6px;
            }
            .queue-badge-idle, .queue-badge-queued { background: #333; color: #ccc; }
            .queue-badge-pending { background: #4a4a4a; color: #ddd; }
            .queue-badge-downloading { background: #f5c518; color: #000; }
            .queue-badge-completed { background: #b9b27a; color: #161616; }
            .queue-badge-failed { background: #7d2e2e; color: #fff; }
            .queue-badge-stopped { background: #555; color: #fff; }
            .queue-progress-bar {
                margin-top: 12px;
                height: 8px;
                border-radius: 4px;
                background: #0a0a0a;
                overflow: hidden;
            }
            .queue-progress-fill {
                height: 100%;
                background: #f5c518;
                border-radius: 4px;
                transition: width 0.4s ease, background 0.5s ease;
                width: 0%;
            }
            .queue-progress-fill.completed {
                background: linear-gradient(90deg, #f5c518, #d8d3a0, #777);
            }
            .queue-progress-fill.pending {
                width: 100% !important;
                background: repeating-linear-gradient(
                    45deg, #343434, #343434 10px, #4a4a4a 10px, #4a4a4a 20px
                );
                opacity: 0.65;
            }
            .queue-progress-fill.indeterminate {
                width: 35%;
                background: repeating-linear-gradient(
                    45deg, #f5c518, #f5c518 10px, #b5900f 10px, #b5900f 20px
                );
                animation: queue-stripes 1s linear infinite;
            }
            @keyframes queue-stripes {
                0% { margin-left: 0; }
                100% { margin-left: 28px; }
            }

            .queue-item-message {
                margin-top: 10px;
                font-size: 13px;
                color: #ccc;
            }
            .queue-item-meta {
                margin-top: 12px;
                display: flex;
                justify-content: space-between;
                align-items: center;
                flex-wrap: wrap;
                gap: 10px;
            }
            .queue-item-meta-left {
                font-size: 12px;
                color: #999;
            }
            .queue-item-actions {
                display: flex;
                gap: 8px;
            }
            .queue-item-actions button {
                padding: 7px 14px;
                font-size: 13px;
            }
            .btn-stop {
                background: #7d2e2e !important;
            }
            .btn-stop:hover { background: #9a3a3a !important; }
            .btn-delete {
                background: #2a2a2a !important;
                border: 1px solid #555;
            }
            .btn-delete:hover { background: #3a3a3a !important; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <img
                    src="https://raw.githubusercontent.com/SergeEngineer/Antra-docker/refs/heads/main/assets/antra-128.png"
                    alt="Antra"
                    class="logo"
                >
                <div class="subtitle">Music Library Builder</div>
            </div>

            <div class="tabs">
                <button class="tab-button active" id="tabBtnAdd" onclick="switchTab('add')">Add Music</button>
                <button class="tab-button" id="tabBtnSettings" onclick="switchTab('settings')">Settings</button>
            </div>

            <div id="tabAdd" class="tab-panel active">
                <div class="card">
                    <div class="box">
                        <h2>Add Music</h2>

                        <label>
                            URL: YouTube, YouTube Music, Spotify, Apple Music, SoundCloud, Amazon Music, Tidal, Qobuz
                        </label>
                        <input
                            id="url"
                            type="text"
                            placeholder="Paste music URL here..."
                        >

                        <label>Sync schedule for this URL</label>
                        <div class="schedule-buttons" id="addScheduleButtons"></div>
                        <div class="hint">
                            Recurring schedules automatically re-check this URL for new
                            tracks in the background.
                        </div>
                    </div>

                    <br>
                    <button id="addButton" onclick="addUrl()">Add to Queue</button>
                </div>

                <div class="card">
                    <h2>Download Queue</h2>
                    <div id="queueList">Loading queue...</div>
                </div>
            </div>

            <div id="tabSettings" class="tab-panel">
                <div class="card">
                    <div class="box">
                        <h2>Download Settings</h2>

                        <label for="outputFormat">Output format</label>
                        <div class="format-buttons" id="outputFormat"></div>
                        <div class="format-note" id="formatNote">Lossless FLAC</div>

                        <label for="downloadQuality">Download quality</label>
                        <div class="quality-buttons" id="downloadQuality"></div>
                        <div class="quality-note" id="qualityNote"></div>

                        <label for="singleTrack">Single track filename</label>
                        <input id="singleTrack" type="text"
                               value="{artist} - {title}" oninput="updateExample()">
                            <div class="hint">Edit a tag to your preferred filename.</div>

                        <label for="albumTrack">Album track filename</label>
                        <input id="albumTrack" type="text"
                               value="{artist} - {title}" oninput="updateExample()">
                        <div class="hint">Used for tracks inside an album folder.</div>

                        <label for="folderStructure">Folder structure</label>
                        <input id="folderStructure" type="text"
                               value="{album_artist}/{year} - {album}"
                               oninput="updateExample()">
                        <div class="hint">Use "/" between tags to create nested folders.</div>

                        <label for="outputFormat">Tags:</label>
                        <div class="hint">Click a tag to insert it into whichever field you clicked/edited last.</div>
                        <div class="tag-buttons"></div>

                        <label>Example output path</label>
                        <div class="example" id="examplePath"></div>
                    </div>
                </div>

                <div class="card">
                    <h2>Configuration</h2>
                    <p>Music directory: <strong>/music</strong></p>
                    <p>Configuration: <strong>/config</strong></p>
                    <p class="status">● Antra Web API is running</p>
                </div>
            </div>
        </div>

        <script>
            function switchTab(tab) {
                const isAdd = tab === "add";

                document.getElementById("tabAdd").classList.toggle("active", isAdd);
                document.getElementById("tabSettings").classList.toggle("active", !isAdd);

                document.getElementById("tabBtnAdd").classList.toggle("active", isAdd);
                document.getElementById("tabBtnSettings").classList.toggle("active", !isAdd);
            }

            const TAGS = [
                ["{title}", "Title"],
                ["{artist}", "Artist"],
                ["{album_artist}", "Album Artist"],
                ["{album}", "Album"],
                ["{year}", "Year"],
                ["{track}", "Track"],
                ["{disk}", "Disk"],
                ["{genre}", "Genre"],
                ["{composer}", "Composer"],
                ["{isrc}", "ISRC"],
                ["{codec}", "Codec"],
                ["{bitrate}", "Bitrate"],
                ["{quality}", "Quality"]
            ];

            const TAG_TARGET_FIELDS = ["singleTrack", "albumTrack", "folderStructure"];
            let lastFocusedField = "folderStructure";

            function trackFocusedField() {
                TAG_TARGET_FIELDS.forEach(id => {
                    const input = document.getElementById(id);
                    input.addEventListener("focus", () => {
                        lastFocusedField = id;
                    });
                });
            }

            function createTagButtons() {
                document.querySelectorAll(".tag-buttons").forEach(container => {
                    TAGS.forEach(([tag, label]) => {
                        const button = document.createElement("button");
                        button.type = "button";
                        button.className = "tag-button";
                        button.textContent = tag;
                        button.title = label;
                        button.onclick = () => insertTag(lastFocusedField, tag);
                        container.appendChild(button);
                    });
                });
            }

            function insertTag(targetId, tag) {
                const input = document.getElementById(targetId);
                const start = input.selectionStart ?? input.value.length;
                const end = input.selectionEnd ?? start;

                input.value =
                    input.value.substring(0, start) +
                    tag +
                    input.value.substring(end);

                input.focus();

                const position = start + tag.length;
                input.setSelectionRange(position, position);
                updateExample();
            }

            const FORMATS = [
                ["auto", "AUTO"],
                ["flac", "FLAC"],
                ["alac", "ALAC"],
                ["aac", "AAC"],
                ["mp3", "MP3"],
                ["atmos", "ATMOS"]
            ];

            const QUALITIES = [
                ["highest", "Highest"],
                ["lossless", "Lossless"],
                ["320", "320"],
                ["256", "256"],
                ["192", "192"],
                ["128", "128"]
            ];

            const SCHEDULES = [
                ["none", "None"],
                ["daily", "Daily"],
                ["weekly", "Weekly"],
                ["monthly", "Monthly"]
            ];

            let selectedFormat = "auto";
            let selectedQuality = "lossless";
            let selectedAddSchedule = "none";

            function createFormatButtons() {
                const container = document.getElementById("outputFormat");
                container.innerHTML = "";

                FORMATS.forEach(([value, label]) => {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "format-button";
                    button.textContent = label;
                    button.dataset.value = value;
                    button.classList.toggle("active", value === selectedFormat);
                    button.onclick = () => setSelectedFormat(value);
                    container.appendChild(button);
                });
            }

            function setSelectedFormat(format) {
                selectedFormat = format;

                document
                    .querySelectorAll("#outputFormat .format-button")
                    .forEach(button => {
                        button.classList.toggle("active", button.dataset.value === format);
                    });

                updateFormatNote();
                saveSettings();
            }

            function createQualityButtons() {
                const container = document.getElementById("downloadQuality");
                container.innerHTML = "";

                QUALITIES.forEach(([value, label]) => {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "quality-button";
                    button.textContent = label;
                    button.dataset.value = value;
                    button.classList.toggle("active", value === selectedQuality);
                    button.onclick = () => setSelectedQuality(value);
                    container.appendChild(button);
                });
            }

            function setSelectedQuality(quality) {
                selectedQuality = quality;

                document
                    .querySelectorAll("#downloadQuality .quality-button")
                    .forEach(button => {
                        button.classList.toggle("active", button.dataset.value === quality);
                    });

                updateQualityNote();
                saveSettings();
            }

            function createAddScheduleButtons() {
                const container = document.getElementById("addScheduleButtons");
                container.innerHTML = "";

                SCHEDULES.forEach(([value, label]) => {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "schedule-button";
                    button.textContent = label;
                    button.dataset.value = value;
                    button.classList.toggle("active", value === selectedAddSchedule);
                    button.onclick = () => {
                        selectedAddSchedule = value;
                        document
                            .querySelectorAll("#addScheduleButtons .schedule-button")
                            .forEach(b => b.classList.toggle("active", b.dataset.value === value));
                    };
                    container.appendChild(button);
                });
            }

            function updateFormatNote() {
                const format = selectedFormat;
                const note = document.getElementById("formatNote");

                if (format === "auto") {
                    note.innerText = "Automatically picks the best available quality";
                } else if (format === "flac") {
                    note.innerText = "Lossless FLAC";
                } else if (format === "alac") {
                    note.innerText = "Lossless ALAC";
                } else if (format === "aac") {
                    note.innerText = "Lossy AAC";
                } else if (format === "atmos") {
                    note.innerText = "Dolby Atmos spatial audio, when available";
                } else {
                    note.innerText = "MP3 — prefers 360 kbps when available, then lower quality";
                }

                updateExample();
            }

            function updateQualityNote() {
                const note = document.getElementById("qualityNote");
                const formatLabel = selectedFormat.toUpperCase();

                const qualityLabels = {
                    highest: "the highest quality available",
                    lossless: "lossless, when the source supports it",
                    "320": "320 kbps",
                    "256": "256 kbps",
                    "192": "192 kbps",
                    "128": "128 kbps"
                };

                const qualityText = qualityLabels[selectedQuality] || selectedQuality;
                note.innerText = `Best available source → ${formatLabel} ${qualityText}`;
            }

            function updateExample() {
                const format = selectedFormat;
                const folder =
                    document.getElementById("folderStructure").value ||
                    "{album_artist}/{year} - {album}";
                const albumTrack =
                    document.getElementById("albumTrack").value ||
                    "{artist} - {title}";

                const sample = {
                    title: "Give Life Back to Music",
                    artist: "Daft Punk",
                    album_artist: "Daft Punk",
                    album: "Random Access Memories",
                    year: "2013",
                    track: "01",
                    disk: "1",
                    genre: "Electronic",
                    composer: "Daft Punk",
                    isrc: "USQX91300105",
                    codec: format === "flac" ? "FLAC" :
                           format === "alac" ? "ALAC" :
                           format === "aac" ? "AAC" :
                           format === "atmos" ? "ATMOS" :
                           format === "auto" ? "AUTO" : "MP3",
                    bitrate: format === "mp3" ? "360" : "—",
                    quality: format === "flac" || format === "alac" || format === "atmos"
                        ? "Lossless"
                        : "High Quality"
                };

                function render(template) {
                    return template.replace(
                        /\\{(title|artist|album_artist|album|year|track|disk|genre|composer|isrc|codec|bitrate|quality)\\}/g,
                        (match, key) => sample[key] ?? ""
                    );
                }

                const renderedFolder = render(folder)
                    .replace(/\\\\/g, "/")
                    .replace(/^\\/+|\\/+$/g, "");

                const renderedTrack = render(albumTrack);

                document.getElementById("examplePath").innerText =
                    renderedFolder + "/" + renderedTrack + "." + format;
            }

            function currentSettingsPayload() {
                return {
                    output_format: selectedFormat,
                    download_quality: selectedQuality,
                    single_track_filename:
                        document.getElementById("singleTrack").value,
                    album_track_filename:
                        document.getElementById("albumTrack").value,
                    folder_structure:
                        document.getElementById("folderStructure").value
                };
            }

            async function saveSettings() {
                const settings = currentSettingsPayload();

                try {
                    localStorage.setItem("antraSettings", JSON.stringify(settings));
                } catch (error) {
                    console.error("Could not cache settings locally", error);
                }

                try {
                    const response = await fetch("/api/settings", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify(settings)
                    });

                    const data = await response.json();

                    if (!data.success) {
                        console.error("Could not save settings to /config:", data.message);
                    }

                } catch (error) {
                    console.error("Could not reach server to save settings", error);
                }
            }

            function applySettings(settings) {
                if (!settings) return;

                if (settings.output_format) setSelectedFormat(settings.output_format);
                if (settings.download_quality) setSelectedQuality(settings.download_quality);

                if (settings.single_track_filename)
                    document.getElementById("singleTrack").value = settings.single_track_filename;

                if (settings.album_track_filename)
                    document.getElementById("albumTrack").value = settings.album_track_filename;

                if (settings.folder_structure)
                    document.getElementById("folderStructure").value = settings.folder_structure;
            }

            async function loadSettings() {
                try {
                    const response = await fetch("/api/settings");
                    if (response.ok) {
                        applySettings(await response.json());
                        return;
                    }
                } catch (error) {
                    console.error("Could not load settings from server", error);
                }

                try {
                    const cached = JSON.parse(localStorage.getItem("antraSettings"));
                    applySettings(cached);
                } catch (error) {
                    console.error("Could not load cached settings", error);
                }
            }

            // --- Download queue ---

            let queuePollTimer = null;

            function fmtDate(iso) {
                if (!iso) return "Never";
                const d = new Date(iso);
                if (isNaN(d.getTime())) return "Never";
                return d.toLocaleDateString(undefined, {
                    year: "numeric", month: "short", day: "numeric",
                    hour: "2-digit", minute: "2-digit"
                });
            }

            function statusLabel(status) {
                const labels = {
                    idle: "Idle",
                    queued: "Queued",
                    pending: "Pending",
                    downloading: "Downloading",
                    completed: "Completed",
                    failed: "Failed",
                    stopped: "Stopped"
                };
                return labels[status] || status;
            }

            function renderQueueItem(item) {
                const progress = item.progress || {};
                const total = progress.current_total || progress.track_count || 0;
                const index = progress.current_index || 0;
                const completed = progress.downloaded || 0;
                const failed = progress.failed || 0;
                const skipped = progress.skipped || 0;
                const isDownloading = item.status === "downloading";
                const isPending = item.status === "pending";
                const percent = total > 0
                    ? Math.min(100, Math.round(((completed + failed + skipped) / total) * 100))
                    : 0;

                let fillClass = "queue-progress-fill";
                let fillStyle = `width:${percent}%;`;
                if (isPending) {
                    fillClass += " pending";
                    fillStyle = "";
                } else if (item.status === "completed") {
                    fillClass += " completed";
                    fillStyle = "width:100%;";
                }

                const scheduleButtons = SCHEDULES.map(([value, label]) => {
                    const active = item.schedule === value ? "active" : "";
                    return `<button type="button" class="schedule-button ${active}" `
                        + `onclick="setItemSchedule('${item.id}','${value}')">${label}</button>`;
                }).join("");

                const canStop = isDownloading || isPending;
                const actionButton = canStop
                    ? `<button class="btn-stop" onclick="stopQueueItem('${item.id}')">${isPending ? "Cancel" : "Stop"}</button>`
                    : `<button onclick="startQueueItem('${item.id}')">Sync Now</button>`;

                let description = "";
                if (isDownloading && total > 0) {
                    description = `Track ${Math.min(index || 1, total)} of ${total} — ${item.message || "Downloading..."}`;
                } else if (isDownloading) {
                    description = item.message || "Downloading...";
                } else if (isPending) {
                    description = "Waiting for the current download to finish";
                } else if (item.status === "completed") {
                    description = `${completed} downloaded · ${failed} failed · ${skipped} skipped`;
                } else {
                    description = item.message || "";
                }

                return `
                    <div class="queue-item">
                        <div class="queue-item-top">
                            <div class="queue-item-playlist">
                                ${escapeHtml(item.playlist_name || "Waiting for playlist name...")}
                            </div>
                            <div class="queue-item-url">${escapeHtml(item.url)}</div>
                        </div>

                        <hr class="queue-divider">

                        <div class="queue-progress-bar">
                            <div class="${fillClass}" style="${fillStyle}"></div>
                        </div>

                        <div class="queue-item-message">
                            <span class="queue-badge queue-badge-${item.status}">${statusLabel(item.status)}</span>
                            ${escapeHtml(description)}
                        </div>

                        <div class="queue-item-folder">
                            <strong>Saved to:</strong>
                            ${escapeHtml(item.folder_path || "Waiting for playlist metadata...")}
                        </div>

                        <div class="queue-item-meta">
                            <div class="queue-item-meta-left">
                                Last synced: ${fmtDate(item.last_sync)}
                            </div>
                            <div class="queue-item-actions">
                                ${actionButton}
                                <button class="btn-delete" onclick="deleteQueueItem('${item.id}')">Delete</button>
                            </div>
                        </div>

                        <label style="margin-top:12px;">Sync schedule</label>
                        <div class="schedule-buttons">${scheduleButtons}</div>
                    </div>
                `;
            }

            function escapeHtml(text) {
                const div = document.createElement("div");
                div.textContent = text ?? "";
                return div.innerHTML;
            }

            async function refreshQueue() {
                try {
                    const response = await fetch("/api/queue");
                    const data = await response.json();
                    const items = data.items || [];

                    const container = document.getElementById("queueList");

                    if (items.length === 0) {
                        container.innerHTML =
                            '<div class="queue-empty">No URLs in the queue yet. Add one above.</div>';
                        return;
                    }

                    container.innerHTML = items.map(renderQueueItem).join("");

                } catch (error) {
                    console.error("Could not refresh queue", error);
                }
            }

            async function addUrl() {
                const urlInput = document.getElementById("url");
                const url = urlInput.value.trim();

                if (!url) {
                    alert("Please enter a URL.");
                    return;
                }

                const button = document.getElementById("addButton");
                button.disabled = true;

                try {
                    const response = await fetch("/api/queue", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({url: url, schedule: selectedAddSchedule})
                    });

                    const data = await response.json();

                    if (!data.success) {
                        alert(data.message || "Failed to add URL.");
                    } else {
                        urlInput.value = "";
                        await refreshQueue();
                    }

                } catch (error) {
                    alert("Could not reach the server: " + error.message);
                } finally {
                    button.disabled = false;
                }
            }

            async function startQueueItem(itemId) {
                try {
                    const response = await fetch(`/api/queue/${itemId}/start`, {method: "POST"});
                    const data = await response.json();
                    if (!data.success) {
                        alert(data.message || "Could not start download.");
                    }
                } catch (error) {
                    alert("Could not reach the server: " + error.message);
                }
                await refreshQueue();
            }

            async function stopQueueItem(itemId) {
                try {
                    const response = await fetch(`/api/queue/${itemId}/stop`, {method: "POST"});
                    const data = await response.json();
                    if (!data.success) {
                        alert(data.message || "Could not stop download.");
                    }
                } catch (error) {
                    alert("Could not reach the server: " + error.message);
                }
                await refreshQueue();
            }

            async function deleteQueueItem(itemId) {
                if (!confirm("Remove this URL from the queue?")) return;

                try {
                    const response = await fetch(`/api/queue/${itemId}`, {method: "DELETE"});
                    const data = await response.json();
                    if (!data.success) {
                        alert(data.message || "Could not delete item.");
                    }
                } catch (error) {
                    alert("Could not reach the server: " + error.message);
                }
                await refreshQueue();
            }

            async function setItemSchedule(itemId, schedule) {
                try {
                    const response = await fetch(`/api/queue/${itemId}`, {
                        method: "PATCH",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({schedule: schedule})
                    });
                    const data = await response.json();
                    if (!data.success) {
                        alert(data.message || "Could not update schedule.");
                    }
                } catch (error) {
                    alert("Could not reach the server: " + error.message);
                }
                await refreshQueue();
            }

            document.querySelectorAll(
                "#singleTrack, #albumTrack, #folderStructure"
            ).forEach(input => {
                input.addEventListener("change", saveSettings);
            });

            (async function init() {
                trackFocusedField();
                createFormatButtons();
                createQualityButtons();
                createAddScheduleButtons();
                createTagButtons();
                await loadSettings();
                updateFormatNote();
                updateQualityNote();
                updateExample();

                await refreshQueue();
                queuePollTimer = setInterval(refreshQueue, 2000);
            })();
        </script>
    </body>
    </html>
    """

class QueueAddRequest(BaseModel):
    url: str
    schedule: str = "none"


class QueueScheduleRequest(BaseModel):
    schedule: str


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "music_directory": str(MUSIC_DIR),
        "music_directory_exists": MUSIC_DIR.exists(),
        "config_directory": str(CONFIG_DIR),
        "settings_file": str(SETTINGS_FILE),
        "settings_file_exists": SETTINGS_FILE.exists(),
        "queue_file": str(QUEUE_FILE),
        "queue_file_exists": QUEUE_FILE.exists(),
    }


@app.get("/api/settings")
async def get_settings():
    return load_settings()


@app.post("/api/settings")
async def update_settings(data: SettingsRequest):

    output_format = data.output_format.strip().lower()

    if output_format not in ALLOWED_FORMATS:
        return {
            "success": False,
            "message": "Invalid output format. Choose Auto, FLAC, ALAC, AAC, MP3 or Atmos."
        }

    download_quality = data.download_quality.strip().lower()

    if download_quality not in ALLOWED_QUALITIES:
        return {
            "success": False,
            "message": "Invalid download quality. Choose Highest, Lossless, 320, 256, 192 or 128."
        }

    settings = {
        "output_format": output_format,
        "download_quality": download_quality,
        "single_track_filename": data.single_track_filename.strip()
            or DEFAULT_SETTINGS["single_track_filename"],
        "album_track_filename": data.album_track_filename.strip()
            or DEFAULT_SETTINGS["album_track_filename"],
        "folder_structure": data.folder_structure.strip()
            or DEFAULT_SETTINGS["folder_structure"],
    }

    try:
        save_settings(settings)
    except OSError as exc:
        return {
            "success": False,
            "message": f"Could not save settings to {CONFIG_DIR}: {exc}"
        }

    return {"success": True, "settings": settings}


@app.get("/api/queue")
async def get_queue():
    items = load_queue()
    items.sort(key=lambda i: i.get("added_at", ""), reverse=True)
    return {"items": items}


@app.post("/api/queue")
async def add_to_queue(data: QueueAddRequest):
    url = data.url.strip()

    if not url:
        return {"success": False, "message": "URL is required."}

    schedule = data.schedule.strip().lower()

    if schedule not in ALLOWED_SCHEDULES:
        schedule = "none"

    normalized_url = normalize_url(url)
    duplicate_key = normalized_url.rstrip("/").lower()

    with queue_lock:
        items = load_queue()
        for existing in items:
            existing_url = normalize_url(str(existing.get("url", ""))).rstrip("/").lower()
            if existing_url == duplicate_key:
                return {
                    "success": False,
                    "message": "This URL already exists in the download queue."
                }

        item = new_queue_item(url, schedule)
        items.append(item)
        save_queue(items)

    job_id = start_download_for_item(item["id"])

    return {"success": True, "item": item, "job_id": job_id}


@app.post("/api/queue/{item_id}/start")
async def start_queue_item(item_id: str):
    items = load_queue()
    item = find_queue_item(items, item_id)

    if item is None:
        return {"success": False, "message": "Queue item not found."}

    if item.get("status") == "downloading":
        return {"success": False, "message": "This URL is already downloading."}

    if item.get("status") == "pending":
        return {"success": False, "message": "This URL is already pending in the download queue."}

    job_id = start_download_for_item(item_id)

    return {"success": True, "job_id": job_id}


@app.post("/api/queue/{item_id}/stop")
async def stop_queue_item(item_id: str):
    items = load_queue()
    item = find_queue_item(items, item_id)

    if item is None:
        return {"success": False, "message": "Queue item not found."}

    if item.get("status") == "pending":
        update_queue_item(item_id, status="stopped", message="Pending download cancelled.")
        return {"success": True}

    job_id = item.get("job_id")
    process = running_processes.get(job_id) if job_id else None

    if process is None or process.poll() is not None:
        return {"success": False, "message": "This item isn't currently downloading."}

    stop_flags[job_id] = True

    try:
        process.terminate()
    except Exception:
        pass

    update_queue_item(item_id, status="stopped", message="Stopping...")

    return {"success": True}


@app.patch("/api/queue/{item_id}")
async def update_queue_schedule(item_id: str, data: QueueScheduleRequest):
    schedule = data.schedule.strip().lower()

    if schedule not in ALLOWED_SCHEDULES:
        return {"success": False, "message": "Invalid schedule."}

    items = load_queue()
    item = find_queue_item(items, item_id)

    if item is None:
        return {"success": False, "message": "Queue item not found."}

    update_queue_item(item_id, schedule=schedule)

    return {"success": True}


@app.delete("/api/queue/{item_id}")
async def delete_queue_item(item_id: str):
    with queue_lock:
        items = load_queue()
        item = find_queue_item(items, item_id)

        if item is None:
            return {"success": False, "message": "Queue item not found."}

        job_id = item.get("job_id")
        process = running_processes.get(job_id) if job_id else None

        if process is not None and process.poll() is None:
            stop_flags[job_id] = True
            try:
                process.terminate()
            except Exception:
                pass

        items = [i for i in items if i.get("id") != item_id]
        save_queue(items)

    return {"success": True}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)

    if not job:
        return {"success": False, "message": "Job not found."}

    return job


@app.get("/api/library")
async def library():
    if not MUSIC_DIR.exists():
        return {"files": []}

    files = []

    for file in MUSIC_DIR.rglob("*"):
        if file.is_file():
            files.append(str(file.relative_to(MUSIC_DIR)))

    return {"files": files}


# Reset any queue items that were mid-download when the container last
# stopped, and start the recurring-schedule watcher.
reset_stale_downloading_items()
Thread(target=scheduler_loop, daemon=True).start()