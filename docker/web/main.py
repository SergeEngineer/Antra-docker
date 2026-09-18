from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path
from threading import Thread
from datetime import datetime
import subprocess
import os
import json
import uuid
import re


app = FastAPI(
    title="Antra",
    description="Antra Web API",
    version="1.0.1",
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
#
# These are the download settings chosen in the Settings tab (output format,
# filename templates, folder structure). They're stored as JSON inside
# CONFIG_DIR so they survive container restarts/recreations as long as
# /config is mounted to a persistent volume, regardless of which browser or
# device is used to reach the web UI.

SETTINGS_FILE = CONFIG_DIR / "web_settings.json"

DEFAULT_SETTINGS = {
    "output_format": "flac",
    "single_track_filename": "{artist} - {title}",
    "album_track_filename": "{track} - {title}",
    "folder_structure": "{album_artist}/{year} - {album}",
}

ALLOWED_FORMATS = {"auto", "flac", "alac", "aac", "mp3", "atmos"}


class SettingsRequest(BaseModel):
    output_format: str = DEFAULT_SETTINGS["output_format"]
    single_track_filename: str = DEFAULT_SETTINGS["single_track_filename"]
    album_track_filename: str = DEFAULT_SETTINGS["album_track_filename"]
    folder_structure: str = DEFAULT_SETTINGS["folder_structure"]


def load_settings() -> dict:
    """Read persisted settings from CONFIG_DIR, falling back to defaults."""

    settings = DEFAULT_SETTINGS.copy()

    if not SETTINGS_FILE.exists():
        return settings

    try:

        with open(SETTINGS_FILE, "r") as f:
            saved = json.load(f)

        for key in DEFAULT_SETTINGS:

            if key in saved and isinstance(saved[key], str) and saved[key].strip():
                settings[key] = saved[key]

    except (json.JSONDecodeError, OSError):
        # Corrupt or unreadable settings file: fall back to defaults
        # rather than failing the whole app.
        pass

    return settings


def save_settings(settings: dict) -> None:
    """Persist settings to CONFIG_DIR so they survive container restarts."""

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


# ---------------------------------------------------------------------------
# Download jobs
# ---------------------------------------------------------------------------

jobs = {}


class AddRequest(BaseModel):
    url: str
    output_format: str = "flac"
    single_track_filename: str = "{artist} - {title}"
    album_track_filename: str = "{track} - {title}"
    folder_structure: str = "{album_artist}/{year} - {album}"


def normalize_url(url: str) -> str:
    """
    Convert normal YouTube URLs to the YouTube Music equivalent.

    Antra's built-in YouTube metadata fetcher currently recognizes
    music.youtube.com URLs.
    """

    url = url.strip()

    # https://www.youtube.com/watch?v=xxxxx
    # https://youtube.com/watch?v=xxxxx
    if re.match(r"https?://(www\.)?youtube\.com/watch\?", url):
        return url.replace(
            "https://www.youtube.com/",
            "https://music.youtube.com/"
        ).replace(
            "https://youtube.com/",
            "https://music.youtube.com/"
        )

    # https://youtu.be/xxxxx
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


def run_download(job_id: str, url: str, settings: dict):

    jobs[job_id]["status"] = "running"
    jobs[job_id]["message"] = "Starting Antra..."

    normalized_url = normalize_url(url)

    jobs[job_id]["url"] = normalized_url

    env = os.environ.copy()

    # Make absolutely sure Antra writes to our mounted music directory.
    env["OUTPUT_DIR"] = str(MUSIC_DIR)

    # Keep Antra configuration persistent.
    env["ANTRA_CONFIG_DIR"] = str(CONFIG_DIR)

    # Apply settings for this download only. Antra already reads the filename
    # templates from these environment variables.
    env["OUTPUT_FORMAT"] = settings.get("output_format", "flac")
    env["SINGLE_TRACK_FILENAME_TEMPLATE"] = settings.get(
        "single_track_filename", "{artist} - {title}"
    )
    env["ALBUM_TRACK_FILENAME_TEMPLATE"] = settings.get(
        "album_track_filename", "{track} - {title}"
    )

    # Antra versions that support custom folder layouts use FOLDER_STRUCTURE.
    # The additional ALBUM_FOLDER_STRUCTURE variable is harmless on versions
    # that do not use it and supports versions exposing album-specific layout.
    folder_structure = settings.get(
        "folder_structure", "{album_artist}/{year} - {album}"
    )
    env["FOLDER_STRUCTURE"] = folder_structure
    env["ALBUM_FOLDER_STRUCTURE"] = folder_structure

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

        for line in process.stdout:

            line = line.strip()

            if not line:
                continue

            jobs[job_id]["logs"].append(line)

            try:

                event = json.loads(line)

                event_type = event.get("type")

                if event_type == "log":

                    jobs[job_id]["message"] = event.get(
                        "message",
                        ""
                    )

                elif event_type == "playlist_loaded":

                    jobs[job_id]["title"] = event.get(
                        "title",
                        ""
                    )

                    jobs[job_id]["track_count"] = event.get(
                        "track_count",
                        0
                    )

                    jobs[job_id]["message"] = (
                        f"Found {jobs[job_id]['track_count']} track(s)"
                    )

                elif event_type == "event":

                    payload = event.get("payload", {})

                    jobs[job_id]["message"] = (
                        payload.get("message")
                        or event.get("name")
                        or ""
                    )

                    jobs[job_id]["current_track"] = (
                        payload.get("track")
                    )

                    jobs[job_id]["current_artist"] = (
                        payload.get("artist")
                    )

                elif event_type == "playlist_summary":

                    jobs[job_id]["downloaded"] = event.get(
                        "downloaded",
                        0
                    )

                    jobs[job_id]["failed"] = event.get(
                        "failed",
                        0
                    )

                    jobs[job_id]["skipped"] = event.get(
                        "skipped",
                        0
                    )

                    if event.get("error"):

                        jobs[job_id]["message"] = (
                            event["error"]
                        )

                    else:

                        jobs[job_id]["message"] = (
                            f"Completed: "
                            f"{event.get('downloaded', 0)} downloaded, "
                            f"{event.get('failed', 0)} failed, "
                            f"{event.get('skipped', 0)} skipped"
                        )

            except json.JSONDecodeError:

                jobs[job_id]["message"] = line

        return_code = process.wait()

        jobs[job_id]["return_code"] = return_code

        if return_code == 0:

            jobs[job_id]["status"] = "completed"

        else:

            jobs[job_id]["status"] = "failed"

            jobs[job_id]["message"] = (
                f"Antra exited with code {return_code}"
            )

    except Exception as exc:

        jobs[job_id]["status"] = "failed"

        jobs[job_id]["message"] = str(exc)

        jobs[job_id]["logs"].append(
            json.dumps({
                "error": str(exc)
            })
        )


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
            .format-buttons {
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                margin-bottom: 8px;
            }
            .format-button {
                padding: 10px 18px;
                font-size: 14px;
                font-weight: 600;
                background: #111;
                border: 1px solid #444;
                border-radius: 8px;
                color: #ccc;
            }
            .format-button:hover {
                background: #2a2a2a;
                border-color: #666;
            }
            .format-button.active {
                background: #f5c518;
                border-color: #f5c518;
                color: #000;
            }
            .format-note {
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

            #addButton:hover {
                background-color: #ffd43b;
            }
            .header {
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 20px;
                text-align: left;
            }

            .header h2 {
                margin-bottom: 5px;
            }

            .logo {
                width: 96px;
                height: 96px;
                flex-shrink: 0;
            }

            .header .subtitle {
                margin-bottom: 0;
                font-size: 34px;
                font-weight: 700;
                color: #eee;
                text-align: left;
            }

            .subtitle {
                text-align: center;
            }

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
            .tab-button:hover {
                background: #1c1c1c;
                color: #eee;
            }
            .tab-button.active {
                background: #1c1c1c;
                color: #f5c518;
            }
            .tab-panel {
                display: none;
            }
            .tab-panel.active {
                display: block;
            }
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
                            placeholder="Paste music URL here... One per line or comma-separated." 
                            oninput="updateExample()"
                        >
                    </div>

                    <br>
                    <button id="addButton" onclick="addUrl()">Add to Library</button>

                    <div id="progress" class="progress" style="display:none">
                        <div id="status"></div>
                        <div id="track"></div>
                    </div>
                </div>

                <div class="card">
                    <h2>Download Log</h2>
                    <pre id="logs">Waiting for download...</pre>
                </div>
            </div>

            <div id="tabSettings" class="tab-panel">
                <div class="card">
                    <div class="box">
                        <h2>Download Settings</h2>

                        <label for="outputFormat">Output format</label>
                        <div class="format-buttons" id="outputFormat"></div>
                        <div class="format-note" id="formatNote">Lossless FLAC</div>

                        <label for="singleTrack">Single track filename</label>
                        <input id="singleTrack" type="text"
                               value="{artist} - {title}" oninput="updateExample()">
                            <div class="hint">Edit a tag to your preferred filename.</div>

                        <label for="albumTrack">Album track filename</label>
                        <input id="albumTrack" type="text"
                               value="{track} - {title}" oninput="updateExample()">
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
            let currentJob = null;

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

            let selectedFormat = "flac";

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
                        button.classList.toggle(
                            "active",
                            button.dataset.value === format
                        );
                    });

                updateFormatNote();
                saveSettings();
            }

            function updateFormatNote() {
                const format = selectedFormat;
                const note = document.getElementById("formatNote");

                if (format === "auto") {
                    note.innerText =
                        "Automatically picks the best available quality";
                } else if (format === "flac") {
                    note.innerText = "Lossless FLAC";
                } else if (format === "alac") {
                    note.innerText = "Lossless ALAC";
                } else if (format === "aac") {
                    note.innerText = "Lossy AAC";
                } else if (format === "atmos") {
                    note.innerText =
                        "Dolby Atmos spatial audio, when available";
                } else {
                    note.innerText =
                        "MP3 — prefers 360 kbps when available, then lower quality";
                }

                updateExample();
            }

            function updateExample() {
                const format = selectedFormat;
                const folder =
                    document.getElementById("folderStructure").value ||
                    "{album_artist}/{year} - {album}";
                const albumTrack =
                    document.getElementById("albumTrack").value ||
                    "{track} - {title}";

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

                // Keep a local cache too, so the form has something to
                // restore from instantly even if the request below is
                // still in flight or the server is briefly unreachable.
                try {
                    localStorage.setItem(
                        "antraSettings",
                        JSON.stringify(settings)
                    );
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
                        console.error(
                            "Could not save settings to /config:",
                            data.message
                        );
                    }

                } catch (error) {
                    console.error(
                        "Could not reach server to save settings", error
                    );
                }
            }

            function applySettings(settings) {
                if (!settings) return;

                if (settings.output_format)
                    setSelectedFormat(settings.output_format);

                if (settings.single_track_filename)
                    document.getElementById("singleTrack").value =
                        settings.single_track_filename;

                if (settings.album_track_filename)
                    document.getElementById("albumTrack").value =
                        settings.album_track_filename;

                if (settings.folder_structure)
                    document.getElementById("folderStructure").value =
                        settings.folder_structure;
            }

            async function loadSettings() {
                // Settings persisted in /config (survives container
                // restarts, shared across any browser/device) are the
                // source of truth.
                try {
                    const response = await fetch("/api/settings");

                    if (response.ok) {
                        applySettings(await response.json());
                        return;
                    }

                } catch (error) {
                    console.error(
                        "Could not load settings from server", error
                    );
                }

                // Fall back to whatever this browser last cached locally
                // if the server couldn't be reached.
                try {
                    const cached =
                        JSON.parse(localStorage.getItem("antraSettings"));
                    applySettings(cached);
                } catch (error) {
                    console.error("Could not load cached settings", error);
                }
            }

            async function addUrl() {
                const url = document.getElementById("url").value.trim();
                const button = document.getElementById("addButton");
                const progress = document.getElementById("progress");
                const status = document.getElementById("status");
                const logs = document.getElementById("logs");

                if (!url) {
                    status.innerText = "Please enter a URL.";
                    status.className = "error";
                    progress.style.display = "block";
                    return;
                }

                const settings = currentSettingsPayload();

                saveSettings();

                button.disabled = true;
                progress.style.display = "block";
                status.className = "status";
                status.innerText = "Starting download...";
                logs.innerText = "";

                try {
                    const response = await fetch("/api/add", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({url: url, ...settings})
                    });

                    const data = await response.json();

                    if (!data.job_id) {
                        throw new Error(
                            data.message || "Failed to start download."
                        );
                    }

                    currentJob = data.job_id;
                    pollJob();

                } catch (error) {
                    status.innerText = error.message;
                    status.className = "error";
                    button.disabled = false;
                }
            }

            async function pollJob() {
                if (!currentJob) return;

                try {
                    const response =
                        await fetch("/api/jobs/" + currentJob);
                    const job = await response.json();

                    const status = document.getElementById("status");
                    const track = document.getElementById("track");
                    const logs = document.getElementById("logs");

                    status.innerText = job.message || job.status;
                    status.className =
                        job.status === "failed" ? "error" : "status";

                    if (job.current_artist || job.current_track) {
                        track.innerText =
                            (job.current_artist || "") +
                            " - " +
                            (job.current_track || "");
                    }

                    logs.innerText = (job.logs || []).join("\\n");
                    logs.scrollTop = logs.scrollHeight;

                    if (
                        job.status === "running" ||
                        job.status === "queued"
                    ) {
                        setTimeout(pollJob, 1000);
                    } else {
                        document.getElementById("addButton").disabled = false;
                    }

                } catch (error) {
                    console.error(error);
                    setTimeout(pollJob, 2000);
                }
            }

            document.querySelectorAll(
                "#singleTrack, #albumTrack, #folderStructure"
            ).forEach(input => {
                input.addEventListener("change", saveSettings);
            });

            (async function init() {
                trackFocusedField();
                createFormatButtons();
                createTagButtons();
                await loadSettings();
                updateFormatNote();
                updateExample();
            })();
        </script>
    </body>
    </html>
    """

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():

    return {
        "status": "ok",
        "music_directory": str(MUSIC_DIR),
        "music_directory_exists": MUSIC_DIR.exists(),
        "config_directory": str(CONFIG_DIR),
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

    settings = {
        "output_format": output_format,
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


@app.post("/api/add")
async def add_music(data: AddRequest):
    url = data.url.strip()

    if not url:
        return {"success": False, "message": "URL is required."}

    output_format = data.output_format.strip().lower()

    if output_format not in ALLOWED_FORMATS:
        return {
            "success": False,
            "message": "Invalid output format. Choose Auto, FLAC, ALAC, AAC, MP3 or Atmos."
        }

    settings = {
        "output_format": output_format,
        "single_track_filename": data.single_track_filename.strip()
            or DEFAULT_SETTINGS["single_track_filename"],
        "album_track_filename": data.album_track_filename.strip()
            or DEFAULT_SETTINGS["album_track_filename"],
        "folder_structure": data.folder_structure.strip()
            or DEFAULT_SETTINGS["folder_structure"],
    }

    # Persist these as the new defaults for next time, so the container
    # remembers the settings used for this download even if the browser
    # never explicitly hit /api/settings.
    try:
        save_settings(settings)
    except OSError:
        # Non-fatal: the download can still proceed even if we couldn't
        # write to CONFIG_DIR for some reason (e.g. read-only mount).
        pass

    job_id = str(uuid.uuid4())

    jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "url": url,
        "message": "Queued",
        "title": "",
        "track_count": 0,
        "downloaded": 0,
        "failed": 0,
        "skipped": 0,
        "current_artist": "",
        "current_track": "",
        "logs": [],
        "created": datetime.now().isoformat(),
        "settings": settings,
    }

    thread = Thread(
        target=run_download,
        args=(job_id, url, settings),
        daemon=True,
    )
    thread.start()

    return {
        "success": True,
        "job_id": job_id,
        "message": "Download started."
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):

    job = jobs.get(job_id)

    if not job:

        return {
            "success": False,
            "message": "Job not found."
        }


    return job


@app.get("/api/library")
async def library():

    if not MUSIC_DIR.exists():

        return {
            "files": []
        }


    files = []


    for file in MUSIC_DIR.rglob("*"):

        if file.is_file():

            files.append(
                str(file.relative_to(MUSIC_DIR))
            )


    return {
        "files": files
    }