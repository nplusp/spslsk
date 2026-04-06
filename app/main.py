import asyncio
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.parser import parse_input
from app.slskd_client import SlskdClient
from app.downloader import process_playlist, get_session_status, stop_session, _load_manifest

app = FastAPI(title="Spotify → Soulseek Downloader")


class ParseInputRequest(BaseModel):
    text: str


class DownloadRequest(BaseModel):
    # Tracks come from the frontend after user edits in the preview, so the
    # backend no longer re-fetches by URL. raw_text is preserved for History
    # so Reload can repopulate the textarea exactly as typed.
    tracks: list[dict]
    name: str
    raw_text: str = ""


class CheckDownloadedRequest(BaseModel):
    track_ids: list[str]


@app.get("/")
async def index():
    return FileResponse("app/static/index.html")


@app.get("/api/health")
async def health():
    """Check if slskd is reachable."""
    client = SlskdClient()
    ok = await client.health_check()
    return {"slskd": ok}


@app.post("/api/parse-input")
async def parse_input_endpoint(req: ParseInputRequest):
    """Parse a raw textarea blob into a ParsedInput.

    Accepts any mix of Spotify URLs (playlist/album/track) and plain text
    track lines. Returns the full resolved track list plus a suggested
    name and optional thumbnail for the preview UI. Per-line errors are
    captured inside the parser as needs_review rows; only unexpected
    exceptions reach the HTTP layer as 5xx.
    """
    try:
        return parse_input(req.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Parse error: {e}")


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    """Start downloading a user-curated, already-resolved track list.

    The frontend is responsible for editing the preview and sending only
    the tracks the user wants. We still filter to 'ready' state as a
    defensive measure — frontend should already block needs_review rows
    via the disabled Start button, but backend trusts nothing.
    """
    ready_tracks = [t for t in req.tracks if t.get("state") == "ready"]

    # Launch download in background. process_playlist signature is unchanged.
    asyncio.create_task(process_playlist(ready_tracks, req.name))

    return {"message": "Download started", "total": len(ready_tracks)}


@app.get("/api/status")
async def download_status():
    """Get current download session status."""
    return get_session_status()


@app.post("/api/stop")
async def stop_download():
    """Stop the current download session."""
    stop_session()
    return {"message": "Stopped"}


@app.get("/api/files")
async def list_downloaded_files():
    """List all downloaded files."""
    downloads_dir = Path("/app/downloads")
    files = []
    if downloads_dir.exists():
        for f in sorted(downloads_dir.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                files.append({
                    "name": f.name,
                    "path": str(f.relative_to(downloads_dir)),
                    "size_mb": round(f.stat().st_size / 1024 / 1024, 1),
                })
    return {"files": files, "total": len(files)}


@app.get("/api/manifest")
async def get_manifest():
    """Return the download manifest with verified file existence.

    Each entry includes 'exists' flag — if file was moved/deleted,
    the track shows as needing re-download.
    """
    from app.downloader import DOWNLOADS_DIR
    manifest = _load_manifest()
    verified = {}
    for track_id, entry in manifest.items():
        filename = entry.get("filename", "")
        file_found = any(
            f.is_file() for f in DOWNLOADS_DIR.rglob(filename)
        ) if filename else False
        verified[track_id] = {**entry, "exists": file_found}
    return verified


@app.post("/api/check-downloaded")
async def check_downloaded(req: CheckDownloadedRequest):
    """Check which track IDs are already downloaded via manifest."""
    manifest = _load_manifest()
    downloaded = {}
    for tid in req.track_ids:
        entry = manifest.get(tid)
        if entry:
            downloaded[tid] = {
                "filename": entry["filename"],
                "quality": entry.get("quality", "downloaded"),
            }
    return {"downloaded": downloaded}


@app.post("/api/open-downloads")
async def open_downloads_folder():
    """Open the downloads folder in the host file manager.

    This works because the downloads volume is mapped to ./downloads/ on the host.
    """
    # Since we're in Docker, we can't open Finder directly.
    # Return the path so the frontend can show it.
    return {"path": "./downloads/", "message": "Open ./downloads/ in your file manager"}


# Serve static files (CSS, JS if needed)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
