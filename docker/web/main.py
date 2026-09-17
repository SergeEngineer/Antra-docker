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
    version="1.0.0",
)


MUSIC_DIR = Path(
    os.getenv("OUTPUT_DIR", "/music")
)

CONFIG_DIR = Path(
    os.getenv("ANTRA_CONFIG_DIR", "/config")
)


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
            .format-row {
                display: flex;
                align-items: center;
                gap: 12px;
            }
            .format-row select {
                max-width: 220px;
                margin-bottom: 0;
            }
            .format-note { color: #999; font-size: 13px; }
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
                text-align: center;
            }

            .header h2 {
                margin-bottom: 5px;
            }

            .subtitle {
                text-align: center;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2>🎵 Antra</h2>
                <div class="subtitle">Music Library Builder</div>
            </div>

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
                <div class="box">
                    <h2>Download Settings</h2>

                    <label for="outputFormat">Output format</label>
                    <div class="format-row">
                        <select id="outputFormat">
                            <option value="flac">FLAC</option>
                            <option value="alac">ALAC</option>
                            <option value="aac">AAC</option>
                            <option value="mp3">MP3</option>
                        </select>
                        <span class="format-note" id="formatNote">Lossless FLAC</span>
                    </div>

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
                    <div class="tag-buttons" data-target="folderStructure"></div>

                </div>

                <br>
                <button id="addButton" onclick="addUrl()">Add to Library</button>

                <div id="progress" class="progress" style="display:none">
                    <div id="status"></div>
                    <div id="track"></div>
                </div>
            </div>

            <div class="card">
                <h2>Configuration</h2>
                <p>Music directory: <strong>/music</strong></p>
                <p>Configuration: <strong>/config</strong></p>
                <p class="status">● Antra Web API is running</p>
            </div>

            <div class="card">
                <h2>Download Log</h2>
                <pre id="logs">Waiting for download...</pre>
            </div>
        </div>

        <script>
            let currentJob = null;

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

            function createTagButtons() {
                document.querySelectorAll(".tag-buttons").forEach(container => {
                    const targetId = container.dataset.target;

                    TAGS.forEach(([tag, label]) => {
                        const button = document.createElement("button");
                        button.type = "button";
                        button.className = "tag-button";
                        button.textContent = tag;
                        button.title = label;
                        button.onclick = () => insertTag(targetId, tag);
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

            function updateFormatNote() {
                const format = document.getElementById("outputFormat").value;
                const note = document.getElementById("formatNote");

                if (format === "flac") {
                    note.innerText = "Lossless FLAC";
                } else if (format === "alac") {
                    note.innerText = "Lossless ALAC";
                } else if (format === "aac") {
                    note.innerText = "Lossy AAC";
                } else {
                    note.innerText =
                        "MP3 — prefers 360 kbps when available, then lower quality";
                }

                updateExample();
            }

            function updateExample() {
                const format = document.getElementById("outputFormat").value;
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
                           format === "aac" ? "AAC" : "MP3",
                    bitrate: format === "mp3" ? "360" : "—",
                    quality: format === "flac" || format === "alac"
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

            function saveSettings() {
                localStorage.setItem("antraSettings", JSON.stringify({
                    outputFormat:
                        document.getElementById("outputFormat").value,
                    singleTrack:
                        document.getElementById("singleTrack").value,
                    albumTrack:
                        document.getElementById("albumTrack").value,
                    folderStructure:
                        document.getElementById("folderStructure").value
                }));
            }

            function loadSettings() {
                try {
                    const saved =
                        JSON.parse(localStorage.getItem("antraSettings"));

                    if (!saved) return;

                    if (saved.outputFormat)
                        document.getElementById("outputFormat").value =
                            saved.outputFormat;

                    if (saved.singleTrack)
                        document.getElementById("singleTrack").value =
                            saved.singleTrack;

                    if (saved.albumTrack)
                        document.getElementById("albumTrack").value =
                            saved.albumTrack;

                    if (saved.folderStructure)
                        document.getElementById("folderStructure").value =
                            saved.folderStructure;

                } catch (error) {
                    console.error("Could not load saved settings", error);
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

                const settings = {
                    output_format:
                        document.getElementById("outputFormat").value,
                    single_track_filename:
                        document.getElementById("singleTrack").value,
                    album_track_filename:
                        document.getElementById("albumTrack").value,
                    folder_structure:
                        document.getElementById("folderStructure").value
                };

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

            document.getElementById("outputFormat")
                .addEventListener("change", () => {
                    updateFormatNote();
                    saveSettings();
                });

            document.querySelectorAll(
                "#singleTrack, #albumTrack, #folderStructure"
            ).forEach(input => {
                input.addEventListener("change", saveSettings);
            });

            createTagButtons();
            loadSettings();
            updateFormatNote();
            updateExample();
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


@app.post("/api/add")
async def add_music(data: AddRequest):
    url = data.url.strip()

    if not url:
        return {"success": False, "message": "URL is required."}

    allowed_formats = {"flac", "alac", "aac", "mp3"}
    output_format = data.output_format.strip().lower()

    if output_format not in allowed_formats:
        return {
            "success": False,
            "message": "Invalid output format. Choose FLAC, ALAC, AAC or MP3."
        }

    settings = {
        "output_format": output_format,
        "single_track_filename": data.single_track_filename.strip()
            or "{artist} - {title}",
        "album_track_filename": data.album_track_filename.strip()
            or "{track} - {title}",
        "folder_structure": data.folder_structure.strip()
            or "{album_artist}/{year} - {album}",
    }

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