import asyncio
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


# Serve static files (CSS, JS if needed)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
