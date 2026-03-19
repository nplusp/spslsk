import asyncio
import json
import os
import re
import logging
from pathlib import Path
from dataclasses import dataclass, field
from app.slskd_client import SlskdClient

DOWNLOADS_DIR = Path("/app/downloads")
MANIFEST_FILE = DOWNLOADS_DIR / ".manifest.json"

logger = logging.getLogger(__name__)

# Audio file extensions we care about
AUDIO_EXTENSIONS = {".flac", ".wav", ".mp3", ".ogg", ".aac", ".m4a", ".wma", ".alac"}

# Quality priority: lower number = higher quality
FORMAT_PRIORITY = {
    ".flac": 1,
    ".alac": 2,
    ".wav": 3,
    ".m4a": 4,  # could be ALAC or AAC
    ".ogg": 5,
    ".aac": 6,
    ".mp3": 7,
    ".wma": 8,
}


@dataclass
class TrackStatus:
    artist: str
    title: str
    track_id: str = ""  # Spotify track ID for dedup
    status: str = "pending"  # pending, searching, found, downloading, completed, not_found, error
    quality: str = ""
    filename: str = ""
    error: str = ""


@dataclass
class DownloadSession:
    playlist_name: str = ""
    tracks: list[TrackStatus] = field(default_factory=list)
    active: bool = False


# Global session state (simple for prototype)
session = DownloadSession()


def _get_file_extension(filename: str) -> str:
    """Extract lowercase file extension."""
    match = re.search(r"\.[a-zA-Z0-9]+$", filename)
    return match.group(0).lower() if match else ""


def _get_bitrate_from_attrs(file_info: dict) -> int:
    """Extract bitrate from file attributes if available."""
    bitrate = file_info.get("bitRate", 0)
    if bitrate:
        return bitrate
    # Sometimes in attributes list
    for attr in file_info.get("attributes", []):
        if attr.get("type") == 0:  # BitRate attribute
            return attr.get("value", 0)
    return 0


def _score_file(file_info: dict) -> tuple[int, int, int]:
    """Score a file for sorting. Lower score = better quality.

    Returns (format_priority, -bitrate, file_size_inverse) for sorting.
    """
    filename = file_info.get("filename", "")
    ext = _get_file_extension(filename)
    format_score = FORMAT_PRIORITY.get(ext, 99)
    bitrate = _get_bitrate_from_attrs(file_info)
    size = file_info.get("size", 0)

    # For lossless formats, bitrate doesn't matter much — prefer by size
    if ext in {".flac", ".wav", ".alac"}:
        return (format_score, 0, -size)

    # For lossy, prefer higher bitrate
    return (format_score, -bitrate, -size)


def _matches_track(filename: str, artist: str, title: str) -> bool:
    """Check if a filename roughly matches the expected track.

    Lenient matching: at least one significant title word must appear.
    Short filler words are ignored.
    """
    fname_lower = filename.lower()
    filler_words = {"the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "is"}

    clean_title = _clean_query(title).lower()
    title_words = [w for w in clean_title.split() if w not in filler_words and len(w) > 2]

    if not title_words:
        return True

    matches = sum(1 for w in title_words if w in fname_lower)
    return matches >= 1


def _describe_quality(file_info: dict) -> str:
    """Human-readable quality description."""
    filename = file_info.get("filename", "")
    ext = _get_file_extension(filename)
    bitrate = _get_bitrate_from_attrs(file_info)

    if ext == ".flac":
        return "FLAC (lossless)"
    elif ext == ".wav":
        return "WAV (lossless)"
    elif ext == ".alac":
        return "ALAC (lossless)"
    elif ext == ".mp3" and bitrate >= 320:
        return f"MP3 {bitrate}kbps"
    elif ext == ".mp3" and bitrate > 0:
        return f"MP3 {bitrate}kbps"
    elif ext == ".mp3":
        return "MP3"
    else:
        label = ext.replace(".", "").upper()
        return f"{label} {bitrate}kbps" if bitrate else label


def _load_manifest() -> dict:
    """Load download manifest mapping Spotify track IDs to filenames."""
    if MANIFEST_FILE.exists():
        try:
            return json.loads(MANIFEST_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_manifest(manifest: dict) -> None:
    """Save download manifest to disk."""
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))


def _record_download(track_id: str, filename: str, quality: str) -> None:
    """Record a successful download in the manifest."""
    manifest = _load_manifest()
    manifest[track_id] = {"filename": filename, "quality": quality}
    _save_manifest(manifest)


def _is_already_downloaded(track_id: str) -> str | None:
    """Check if a track was already downloaded using the manifest.

    Returns the filename if found, None otherwise.
    """
    manifest = _load_manifest()
    entry = manifest.get(track_id)
    if entry:
        # Verify file still exists on disk
        for f in DOWNLOADS_DIR.rglob(entry["filename"]):
            if f.is_file():
                return entry["filename"]
        # File gone from disk — remove stale entry
        del manifest[track_id]
        _save_manifest(manifest)
    return None


def _clean_query(text: str) -> str:
    """Clean up search query for better Soulseek results.

    Removes remaster tags, year tags, special editions, and other noise
    that prevents matching on Soulseek.
    """
    # Remove common noise patterns
    noise_patterns = [
        r"\(?\d{4}\)?\s*",                    # year like (2015) or 2015
        r"\[?remaster(ed)?\]?",                # [Remastered] or Remastered
        r"\[?deluxe(\s+edition)?\]?",          # Deluxe Edition
        r"\[?bonus\s+track(s)?\]?",            # Bonus Tracks
        r"\[?expanded(\s+edition)?\]?",        # Expanded Edition
        r"\[?special(\s+edition)?\]?",         # Special Edition
        r"\(feat\.?\s+[^)]+\)",                # (feat. Someone)
        r"\(ft\.?\s+[^)]+\)",                  # (ft. Someone)
        r"-\s*(long|short|extended|radio)\s*(version|mix|edit)?",  # - Long Version
    ]
    result = text
    for pattern in noise_patterns:
        result = re.sub(pattern, " ", result, flags=re.IGNORECASE)

    # Remove extra whitespace
    result = re.sub(r"\s+", " ", result).strip()
    # Remove trailing/leading punctuation junk
    result = result.strip(" -,.")
    return result


def _build_search_queries(artist: str, title: str) -> list[str]:
    """Build search queries: broad first, then filter results on our side.

    Soulseek matches ALL words against file paths.
    Strategy: search by artist only (gets many results), then we filter
    for the specific track on our side.
    """
    clean_artist = _clean_query(artist)
    first_artist = clean_artist.split(",")[0].strip()

    # Remove apostrophes (Can't -> Cant) — Soulseek doesn't handle them well
    first_artist = first_artist.replace("'", "")

    queries = []
    seen = set()

    def _add(q: str):
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            queries.append(q)

    # Query 1: just the artist name (broad, most results)
    _add(first_artist)

    # Query 2: artist + first significant title word (narrower)
    filler = {"the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "is"}
    clean_title = _clean_query(title).replace("'", "")
    title_words = [w for w in clean_title.split() if w.lower() not in filler and len(w) > 2]
    if title_words:
        _add(f"{first_artist} {title_words[0]}")

    return queries


async def process_playlist(tracks: list[dict], playlist_name: str) -> None:
    """Search and download all tracks from a playlist."""
    global session

    session = DownloadSession(
        playlist_name=playlist_name,
        tracks=[
            TrackStatus(
                artist=t["artist"],
                title=t["title"],
                track_id=t.get("id", ""),
            )
            for t in tracks
        ],
        active=True,
    )

    client = SlskdClient()

    for i, track_status in enumerate(session.tracks):
        if not session.active:
            break

        # Skip already downloaded tracks (by Spotify track ID)
        if track_status.track_id:
            existing = _is_already_downloaded(track_status.track_id)
            if existing:
                track_status.status = "completed"
                track_status.quality = "already downloaded"
                track_status.filename = existing
                logger.info(f"Skipped (exists): {existing}")
                continue

        # Ensure slskd is connected before searching
        if not await client.health_check():
            logger.warning("slskd disconnected, waiting 15s for reconnect...")
            await asyncio.sleep(15)
            if not await client.health_check():
                track_status.status = "error"
                track_status.error = "slskd disconnected"
                continue

        queries = _build_search_queries(track_status.artist, track_status.title)
        track_status.status = "searching"

        try:
            responses = []
            for query in queries:
                logger.info(f"Searching: {query}")
                search_id = await client.search(query)
                responses = await client.wait_for_search(search_id)
                await client.delete_search(search_id)
                if responses:
                    break
                # Pause between query attempts to avoid flood
                await asyncio.sleep(2)

            # Collect all audio files from all responses
            candidates = []
            for resp in responses:
                username = resp.get("username", "")
                free_upload = resp.get("freeUploadSlots", 0) > 0
                for f in resp.get("files", []):
                    ext = _get_file_extension(f.get("filename", ""))
                    if ext not in AUDIO_EXTENSIONS:
                        continue
                    if not _matches_track(
                        f.get("filename", ""), track_status.artist, track_status.title
                    ):
                        continue
                    candidates.append({
                        "username": username,
                        "free_upload": free_upload,
                        "file": f,
                    })

            if not candidates:
                track_status.status = "not_found"
                logger.info(f"Not found: {track_status.artist} - {track_status.title}")
                continue

            # Sort: free slots first, then by quality score
            candidates.sort(key=lambda c: (
                0 if c["free_upload"] else 1,
                _score_file(c["file"]),
            ))

            best = candidates[0]
            track_status.status = "downloading"
            track_status.quality = _describe_quality(best["file"])
            track_status.filename = best["file"].get("filename", "").split("\\")[-1]
            logger.info(
                f"Downloading: {track_status.filename} from {best['username']} ({track_status.quality})"
            )

            await client.download_file(best["username"], best["file"])
            track_status.status = "completed"

            # Record in manifest so we don't re-download next time
            if track_status.track_id:
                _record_download(
                    track_status.track_id,
                    track_status.filename,
                    track_status.quality,
                )

        except Exception as e:
            track_status.status = "error"
            track_status.error = str(e)

        # Delay between tracks to avoid Soulseek server flood-kick
        await asyncio.sleep(5)

    session.active = False


def get_session_status() -> dict:
    """Return current download session status."""
    total = len(session.tracks)
    completed = sum(1 for t in session.tracks if t.status == "completed")
    not_found = sum(1 for t in session.tracks if t.status == "not_found")
    errors = sum(1 for t in session.tracks if t.status == "error")

    return {
        "playlist_name": session.playlist_name,
        "active": session.active,
        "total": total,
        "completed": completed,
        "not_found": not_found,
        "errors": errors,
        "tracks": [
            {
                "artist": t.artist,
                "title": t.title,
                "status": t.status,
                "quality": t.quality,
                "filename": t.filename,
                "error": t.error,
            }
            for t in session.tracks
        ],
    }


def stop_session() -> None:
    """Stop the current download session."""
    session.active = False
