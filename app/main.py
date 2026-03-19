import asyncio
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.spotify import get_playlist_tracks
from app.slskd_client import SlskdClient
from app.downloader import process_playlist, get_session_status, stop_session

app = FastAPI(title="Spotify → Soulseek Downloader")


class PlaylistRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str


@app.get("/")
async def index():
    return FileResponse("app/static/index.html")


@app.get("/api/health")
async def health():
    """Check if slskd is reachable."""
    client = SlskdClient()
    ok = await client.health_check()
    return {"slskd": ok}


@app.post("/api/playlist")
async def fetch_playlist(req: PlaylistRequest):
    """Parse a Spotify playlist URL and return track list."""
    try:
        data = get_playlist_tracks(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Spotify error: {e}")
    return data


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    """Start searching and downloading all tracks from a playlist."""
    try:
        data = get_playlist_tracks(req.url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Launch download in background
    asyncio.create_task(process_playlist(data["tracks"], data["name"]))

    return {"message": "Download started", "total": data["total"]}


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
