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


def run_download(job_id: str, url: str):

    jobs[job_id]["status"] = "running"
    jobs[job_id]["message"] = "Starting Antra..."

    normalized_url = normalize_url(url)

    jobs[job_id]["url"] = normalized_url

    env = os.environ.copy()

    # Make absolutely sure Antra writes to our mounted music directory.
    env["OUTPUT_DIR"] = str(MUSIC_DIR)

    # Keep Antra configuration persistent.
    env["ANTRA_CONFIG_DIR"] = str(CONFIG_DIR)

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

        <meta
            name="viewport"
            content="width=device-width, initial-scale=1.0"
        >

        <style>

            body {
                font-family: Arial, sans-serif;
                background: #111;
                color: #eee;
                margin: 0;
                padding: 40px;
            }

            .container {
                max-width: 900px;
                margin: auto;
            }

            h1 {
                font-size: 42px;
                margin-bottom: 5px;
            }

            .subtitle {
                color: #999;
                margin-bottom: 40px;
            }

            .card {
                background: #1c1c1c;
                border-radius: 12px;
                padding: 25px;
                margin-bottom: 20px;
            }

            input {
                width: 100%;
                box-sizing: border-box;
                padding: 14px;
                margin-top: 10px;
                margin-bottom: 15px;
                border-radius: 8px;
                border: 1px solid #444;
                background: #111;
                color: white;
            }

            button {
                padding: 12px 20px;
                border: 0;
                border-radius: 8px;
                background: #444;
                color: white;
                cursor: pointer;
            }

            button:hover {
                background: #666;
            }

            button:disabled {
                opacity: 0.5;
                cursor: not-allowed;
            }

            .status {
                color: #7ddf7d;
            }

            .error {
                color: #ff7777;
            }

            .progress {
                margin-top: 20px;
                padding: 15px;
                background: #111;
                border-radius: 8px;
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

        </style>

    </head>

    <body>

        <div class="container">

            <h1>🎵 Antra</h1>

            <div class="subtitle">
                Music Library Builder
            </div>

            <div class="card">

                <h2>Add Music</h2>

                <label>
                    YouTube / YouTube Music / Spotify / Tidal / Qobuz URL
                </label>

                <input
                    id="url"
                    type="text"
                    placeholder="Paste music URL here..."
                >

                <button
                    id="addButton"
                    onclick="addUrl()"
                >
                    Add to Library
                </button>

                <div
                    id="progress"
                    class="progress"
                    style="display:none"
                >

                    <div id="status"></div>

                    <div id="track"></div>

                </div>

            </div>


            <div class="card">

                <h2>Configuration</h2>

                <p>
                    Music directory:
                    <strong>/music</strong>
                </p>

                <p>
                    Configuration:
                    <strong>/config</strong>
                </p>

                <p class="status">
                    ● Antra Web API is running
                </p>

            </div>


            <div class="card">

                <h2>Download Log</h2>

                <pre id="logs">Waiting for download...</pre>

            </div>

        </div>


        <script>

            let currentJob = null;


            async function addUrl() {

                const url =
                    document.getElementById("url").value.trim();

                const button =
                    document.getElementById("addButton");

                const progress =
                    document.getElementById("progress");

                const status =
                    document.getElementById("status");

                const logs =
                    document.getElementById("logs");


                if (!url) {

                    status.innerText =
                        "Please enter a URL.";

                    progress.style.display =
                        "block";

                    return;
                }


                button.disabled = true;

                progress.style.display =
                    "block";

                status.innerText =
                    "Starting download...";

                logs.innerText =
                    "";


                try {

                    const response =
                        await fetch("/api/add", {

                            method: "POST",

                            headers: {
                                "Content-Type":
                                    "application/json"
                            },

                            body: JSON.stringify({
                                url: url
                            })

                        });


                    const data =
                        await response.json();


                    if (!data.job_id) {

                        throw new Error(
                            data.message ||
                            "Failed to start download."
                        );

                    }


                    currentJob =
                        data.job_id;


                    pollJob();

                }

                catch (error) {

                    status.innerText =
                        error.message;

                    status.className =
                        "error";

                    button.disabled =
                        false;

                }

            }


            async function pollJob() {

                if (!currentJob)
                    return;


                try {

                    const response =
                        await fetch(
                            "/api/jobs/" +
                            currentJob
                        );


                    const job =
                        await response.json();


                    const status =
                        document.getElementById(
                            "status"
                        );

                    const track =
                        document.getElementById(
                            "track"
                        );

                    const logs =
                        document.getElementById(
                            "logs"
                        );


                    status.innerText =
                        job.message || job.status;


                    if (
                        job.current_artist ||
                        job.current_track
                    ) {

                        track.innerText =
                            (
                                job.current_artist ||
                                ""
                            ) +
                            " - " +
                            (
                                job.current_track ||
                                ""
                            );

                    }


                    logs.innerText =
                        (job.logs || []).join("\\n");


                    logs.scrollTop =
                        logs.scrollHeight;


                    if (
                        job.status === "running" ||
                        job.status === "queued"
                    ) {

                        setTimeout(
                            pollJob,
                            1000
                        );

                    }

                    else {

                        document.getElementById(
                            "addButton"
                        ).disabled = false;

                    }

                }

                catch (error) {

                    console.error(error);

                    setTimeout(
                        pollJob,
                        2000
                    );

                }

            }

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

        return {
            "success": False,
            "message": "URL is required."
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

    }


    thread = Thread(
        target=run_download,
        args=(job_id, url),
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