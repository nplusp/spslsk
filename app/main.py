import asyncio
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import HOST_DOWNLOADS_DIR, HOST_HELPER_URL
from app.parser import parse_input
from app.slskd_client import SlskdClient
from app.downloader import (
    process_playlist,
    get_session_status,
    stop_session,
    _load_manifest,
    _sanitize_dirname,
    DOWNLOADS_DIR,
    AUDIO_EXTENSIONS,
)

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


def host_path_for(relative: str = "") -> str:
    """Render a downloads-relative path as the user would see it on the host.

    Falls back to the container path when HOST_DOWNLOADS_DIR is unset (backend
    started outside docker-compose) — a wrong-but-honest path beats an empty
    string, since the UI shows this verbatim.
    """
    root = HOST_DOWNLOADS_DIR or str(DOWNLOADS_DIR)
    return f"{root.rstrip('/')}/{relative}".rstrip("/") if relative else root


def resolve_within_downloads(relative: str) -> Path:
    """Resolve a user-supplied relative path against DOWNLOADS_DIR safely.

    Both sides are fully resolved before comparison so symlinks and ``..``
    segments cannot escape the downloads tree. Raises HTTPException(400) on
    an escape attempt — the frontend only ever sends paths we handed it, so
    a violation means a hand-crafted request.
    """
    root = DOWNLOADS_DIR.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=400, detail="Path escapes downloads directory")
    return candidate


@app.get("/api/paths")
async def downloads_paths():
    """Expose where files physically land, plus how to reach the host helper.

    The UI cannot derive either value on its own: it runs in a browser that
    only knows about localhost:8000, while the files live on the host
    filesystem and the "open in Finder" helper is a separate process.
    """
    return {
        "host_downloads": host_path_for(),
        "container_downloads": str(DOWNLOADS_DIR),
        "helper_url": HOST_HELPER_URL,
        "is_host_path_known": bool(HOST_DOWNLOADS_DIR),
    }


@app.get("/api/files")
async def list_downloaded_files():
    """List all downloaded files, each tagged with its containing folder.

    ``folder`` is the top-level directory under downloads/ (the sanitized
    list name) or "" for loose files sitting in the root. The UI groups by
    it, so a user can see which list a track ended up in without guessing.
    """
    files = []
    if DOWNLOADS_DIR.exists():
        for f in sorted(DOWNLOADS_DIR.rglob("*")):
            if not f.is_file() or f.name.startswith("."):
                continue
            rel = f.relative_to(DOWNLOADS_DIR)
            folder = rel.parts[0] if len(rel.parts) > 1 else ""
            files.append({
                "name": f.name,
                "path": str(rel),
                "folder": folder,
                "size_mb": round(f.stat().st_size / 1024 / 1024, 1),
                "host_path": host_path_for(str(rel)),
            })
    return {"files": files, "total": len(files)}


@app.get("/api/folders")
async def list_folders():
    """Summarize downloads/ as a list of folders for the UI's grouped view.

    Loose files in the downloads root are reported under the "" folder so
    they stay visible instead of silently vanishing from a folder-only view.
    Sorted newest-first by mtime: the folder a user just filled is the one
    they want to open.
    """
    folders: dict[str, dict] = {}
    if DOWNLOADS_DIR.exists():
        for f in DOWNLOADS_DIR.rglob("*"):
            if not f.is_file() or f.name.startswith("."):
                continue
            rel = f.relative_to(DOWNLOADS_DIR)
            name = rel.parts[0] if len(rel.parts) > 1 else ""
            entry = folders.setdefault(name, {
                "name": name,
                "path": host_path_for(name),
                "relative_path": name,
                "files": 0,
                "audio_files": 0,
                "size_mb": 0.0,
                "modified": 0.0,
            })
            stat = f.stat()
            entry["files"] += 1
            if f.suffix.lower() in AUDIO_EXTENSIONS:
                entry["audio_files"] += 1
            entry["size_mb"] += stat.st_size / 1024 / 1024
            entry["modified"] = max(entry["modified"], stat.st_mtime)

    result = sorted(folders.values(), key=lambda e: e["modified"], reverse=True)
    for entry in result:
        entry["size_mb"] = round(entry["size_mb"], 1)
    return {"folders": result, "total": len(result)}


@app.get("/api/file")
async def download_file(path: str):
    """Stream a single downloaded file to the browser.

    The escape hatch for when the host helper isn't running: a user can still
    retrieve what was downloaded without touching a file manager at all.
    """
    target = resolve_within_downloads(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target, filename=target.name)


@app.get("/api/resolve-folder")
async def resolve_folder(name: str):
    """Map a user-typed list name to the folder it actually occupies on disk.

    History entries store the display name; downloads land in
    ``_sanitize_dirname(name)``. Letting the frontend (or the host helper)
    re-implement that transform is exactly how the two drifted apart, so the
    single authoritative implementation answers here.
    """
    folder = _sanitize_dirname(name) if name else ""
    target = DOWNLOADS_DIR / folder if folder else DOWNLOADS_DIR
    return {
        "name": name,
        "folder": folder,
        "exists": target.is_dir(),
        "path": host_path_for(folder),
        "relative_path": folder,
    }


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
    """Report where downloads live so the caller can open or copy the path.

    The backend runs in a container and cannot spawn a file manager; that is
    the host helper's job (see host_helper.py). Kept as a POST for backward
    compatibility with older frontends that call it.
    """
    path = host_path_for()
    return {
        "path": path,
        "helper_url": HOST_HELPER_URL,
        "message": f"Downloads are in {path}",
    }


# Serve static files (CSS, JS if needed)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
