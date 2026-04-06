import asyncio
import json
import math
import os
import re
import logging
import unicodedata
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
    # Lifecycle states:
    #   pending → searching → (queued | not_found | error | completed)
    #   queued → downloading → (completed | error)
    # 'queued' is the new intermediate state introduced by the search/download
    # decouple: search has resolved candidates and the track is waiting for a
    # download worker to pick it up.
    status: str = "pending"
    quality: str = ""
    filename: str = ""
    error: str = ""
    # Resolved download candidates from the search phase. Populated by
    # _search_for_candidates, drained by _download_one_track. Cleared after
    # the download attempt completes (regardless of success) to free memory
    # for large playlists.
    candidates: list = field(default_factory=list)


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


def _score_file(file_info: dict, artist: str = "", title: str = "") -> tuple:
    """Score a file for sorting. Lower tuple = better candidate.

    Sort key shape:
        (phrase_rank, neg_match_count, format_priority, neg_bitrate, neg_size)

    Where:
      - phrase_rank: 0 if the cleaned title appears as a contiguous substring
        anywhere in the path, else 1. **Phrase match outranks format tier**
        (R7) — a correctly-identified MP3 beats a wrongly-named FLAC.
      - neg_match_count: negative count of significant title words present
        in the path. More matches sort earlier (smaller negative number).
        This is the tiebreaker between two non-phrase-match candidates (R8).
      - format_priority: existing FORMAT_PRIORITY table (FLAC=1, ALAC=2, ...)
      - neg_bitrate: 0 for lossless formats, else -bitrate. Matches the
        existing two-branch logic from before this change.
      - neg_size: -size as final tiebreaker.

    Calling _score_file without artist/title still works (returns the
    pre-improvement scoring) — the new sort keys evaluate to (1, 0, ...)
    which is identical to "no phrase, no matches, fall through to format".
    """
    filename = file_info.get("filename", "")
    path_lower = filename.lower()
    ext = _get_file_extension(filename)
    format_score = FORMAT_PRIORITY.get(ext, 99)
    bitrate = _get_bitrate_from_attrs(file_info)
    size = file_info.get("size", 0)

    # Match-strength keys
    if title:
        clean_title_text = _clean_query(title)
        title_lower = clean_title_text.lower().strip()
        # Full phrase match: cleaned title as contiguous substring of path.
        phrase_rank = 0 if title_lower and title_lower in path_lower else 1
        # Partial match count: how many significant title words are present.
        title_words = [w.lower() for w in _significant_words(clean_title_text)]
        match_count = sum(1 for w in title_words if w in path_lower)
    else:
        phrase_rank = 1
        match_count = 0

    # Format / bitrate / size keys (preserve historical behavior)
    if ext in {".flac", ".wav", ".alac"}:
        return (phrase_rank, -match_count, format_score, 0, -size)
    return (phrase_rank, -match_count, format_score, -bitrate, -size)


def _matches_track(filename: str, artist: str, title: str) -> bool:
    """Check whether a candidate file matches the expected track.

    Three rules, all required (unless the short-artist fallback triggers):

    R1 — Artist check: at least one significant artist word must appear
         somewhere in the full file path. Many peers store files as
         `Artist/Album/Track.ext`; the artist often lives in a parent
         directory, not the basename, so we match against the full path
         string.

    R2 — Scaled title threshold: at least `ceil(N/2)` of N significant
         title words must appear in the path. For 2-word titles → 1
         match. For 6-word titles → 3 matches. This closes the bug where
         long titles made the threshold trivially satisfiable.

    R3 — Short-artist fallback: if the artist has zero significant words
         after the filler/length filter (U2, M83, X, AC/DC if split, etc.),
         skip the artist check and instead require a contiguous phrase
         match of the cleaned title in the path.

    The `filename` parameter is the FULL path string from slskd (e.g.,
    "Artist\\Album\\Track.flac"). Backslash separators are treated as
    regular characters by substring matching, which works fine for our
    purpose. Matching is case-insensitive.
    """
    path_lower = filename.lower()

    clean_artist_text = _clean_query(artist)
    clean_title_text = _clean_query(title)

    artist_words = [w.lower() for w in _significant_words(clean_artist_text)]
    title_words = [w.lower() for w in _significant_words(clean_title_text)]

    # R3 fallback: if no significant artist words, require a contiguous
    # phrase match of the cleaned title.
    if not artist_words:
        if not title_words:
            # Both degenerate (e.g., "X - Y"). Conservative true — let the
            # scorer/sorter filter the rest. Matches the historical
            # behavior for pathological inputs.
            return True
        phrase = clean_title_text.lower().strip()
        return bool(phrase) and phrase in path_lower

    # R1: at least one artist word in the full path.
    if not any(w in path_lower for w in artist_words):
        return False

    # R2: scaled title threshold. Empty significant title words → conservative
    # true (all-filler title; rare in practice).
    if not title_words:
        return True
    required = max(1, math.ceil(len(title_words) / 2))
    matches = sum(1 for w in title_words if w in path_lower)
    return matches >= required


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


# Indivisible Latin glyphs that NFKD does NOT decompose. Mapped explicitly
# so non-ASCII artist/title characters become slskd-safe AND match more peer
# files (peers mostly use ASCII filenames).
_ASCII_FOLD_TABLE = str.maketrans({
    "Æ": "AE", "æ": "ae",
    "Ø": "O", "ø": "o",
    "ß": "ss",
    "Ð": "D", "ð": "d",
    "Þ": "Th", "þ": "th",
    "Ł": "L", "ł": "l",
    "Œ": "OE", "œ": "oe",
})


def _ascii_fold(text: str) -> str:
    """Fold Unicode characters to ASCII for slskd-safe queries.

    Two-step transform: (1) explicit table for indivisible Latin glyphs
    that NFKD does not split (Æ, Ø, ß, Ł, Œ); (2) NFKD normalization +
    drop-on-encode for accented characters whose NFKD decomposition
    produces a base letter plus combining marks (é → e + ́, ü → u + ̈).
    """
    text = text.translate(_ASCII_FOLD_TABLE)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _collapse_single_char_runs(text: str) -> str:
    """Collapse runs of >=2 consecutive single-character whitespace-separated
    tokens into one concatenated token.

    Examples:
        "S L F" -> "SLF"           (full collapse)
        "S L F Merkin" -> "SLF Merkin"  (partial)
        "Mr X Foo" -> "Mr X Foo"    (single isolated 1-char left alone)
        "Mr X Y Foo" -> "Mr XY Foo" (run in middle collapses)

    Rationale: slskd's underlying Soulseek client throws ArgumentException
    for queries that consist solely of single-char tokens because they would
    flood the network. Users typing "S L F" almost always mean the
    abbreviation "SLF".
    """
    tokens = text.split()
    result = []
    i = 0
    while i < len(tokens):
        if len(tokens[i]) == 1:
            run_start = i
            while i < len(tokens) and len(tokens[i]) == 1:
                i += 1
            run = tokens[run_start:i]
            if len(run) >= 2:
                result.append("".join(run))
            else:
                result.append(run[0])
        else:
            result.append(tokens[i])
            i += 1
    return " ".join(result)


# Filler words that shouldn't count as significant title/artist tokens.
# Both `_build_search_queries` and `_matches_track` use this set so the
# definition of "significant word" stays consistent across query construction
# and result filtering.
_FILLER_WORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "is",
})


def _significant_words(text: str) -> list[str]:
    """Split text into significant words: longer than 2 chars, not filler."""
    return [w for w in text.split() if w.lower() not in _FILLER_WORDS and len(w) > 2]


def _build_search_queries(artist: str, title: str) -> list[str]:
    """Build search queries: broad first, then filter results on our side.

    Soulseek matches ALL words against file paths. Strategy: search by
    artist only (gets many results), then refine with the longest significant
    title word as a second query if the first is empty.

    Three defensive transforms applied to the artist before queries are built:
      1. Apostrophe stripping (Soulseek does not handle them well).
      2. Single-char token run collapse — "S L F" -> "SLF". slskd rejects
         queries with only single-char tokens with HTTP 400.
      3. ASCII-folding — "Björk" -> "Bjork". slskd 0.24 has a database
         concurrency bug on some non-ASCII queries (DHÆÜR diagnostic case),
         and peer file paths are mostly ASCII anyway, so this also boosts
         recall.

    The narrowing query picks the LONGEST significant title word (length is
    a cheap proxy for specificity) instead of the first.
    """
    clean_artist = _clean_query(artist)
    first_artist = clean_artist.split(",")[0].strip()
    first_artist = first_artist.replace("'", "")
    first_artist = _collapse_single_char_runs(first_artist)
    first_artist = _ascii_fold(first_artist)

    queries = []
    seen = set()

    def _add(q: str):
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            queries.append(q)

    _add(first_artist)

    clean_title = _ascii_fold(_clean_query(title).replace("'", ""))
    title_words = _significant_words(clean_title)
    if title_words:
        # Longest significant word — better specificity proxy than "first".
        # Python's max() returns the first occurrence on length ties.
        _add(f"{first_artist} {max(title_words, key=len)}")

    return queries


def _sanitize_dirname(name: str) -> str:
    """Make a string safe for use as a directory/file name."""
    # Remove characters not allowed in filenames
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    # Limit length
    return name[:100] if name else "Unknown"


def _move_to_playlist_folder(filename: str, playlist_name: str) -> str | None:
    """Move a downloaded file into a playlist-named subfolder.

    Searches for the file in downloads dir, moves it to
    downloads/{playlist_name}/{filename}. Returns new filename or None.
    """
    safe_name = _sanitize_dirname(playlist_name)
    target_dir = DOWNLOADS_DIR / safe_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # Find the file anywhere in downloads
    for f in DOWNLOADS_DIR.rglob(filename):
        if f.is_file() and f.parent != target_dir:
            dest = target_dir / f.name
            # Avoid overwriting
            if dest.exists():
                return f.name
            try:
                import shutil
                shutil.move(str(f), str(dest))
                logger.info(f"Moved: {f.name} → {safe_name}/")
                return f.name
            except OSError as e:
                logger.warning(f"Failed to move {f.name}: {e}")
                return f.name
    return filename


# Search concurrency: how many search calls can be in flight at once.
# slskd's REST API rate-limits searches with HTTP 429; the Soulseek protocol
# itself flood-kicks aggressive query bursts. Keep this conservative.
SEARCH_CONCURRENCY = 1
# Download concurrency: how many download workers drain the queue in
# parallel. Downloads do NOT trigger slskd rate limits — each is a per-peer
# transfer — so we can be much more aggressive here than for searches.
DOWNLOAD_CONCURRENCY = 4
# Pause between successive search calls (kindness to slskd's rate limiter).
SEARCH_DELAY = 3
# Maximum candidates to try per track before giving up. Real-world peer
# failures (mid-transfer drops, queue full, transient rejections) are common
# enough that one-shot download attempts silently mark perfectly findable
# tracks as `error`. Three attempts cover the typical flaky-peer case while
# bounding worst-case latency to 3 * download_file timeout per track.
MAX_DOWNLOAD_ATTEMPTS = 3


async def _search_for_candidates(
    client: SlskdClient, track_status: TrackStatus
) -> None:
    """Phase 1 of the pipeline — search slskd and populate the candidate list.

    Sets `track_status.status` to one of:
      - 'completed' (manifest skip — already downloaded, no work needed)
      - 'queued' (candidates resolved, ready for the download phase)
      - 'not_found' (search returned no acceptable matches)
      - 'error' (search itself raised — slskd 5xx, network, etc.)

    Does NOT call download_file. The download is performed by the separate
    `_download_one_track` function in the download phase.
    """
    # Skip already downloaded tracks (by Spotify track ID)
    if track_status.track_id:
        existing = _is_already_downloaded(track_status.track_id)
        if existing:
            track_status.status = "completed"
            track_status.quality = "already downloaded"
            track_status.filename = existing
            logger.info(f"Skipped (exists): {existing}")
            return

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
            await asyncio.sleep(2)

        # Collect all matching audio files into candidates
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
                candidates.append(
                    {
                        "username": username,
                        "free_upload": free_upload,
                        "file": f,
                    }
                )

        if not candidates:
            track_status.status = "not_found"
            logger.info(
                f"Not found: {track_status.artist} - {track_status.title}"
            )
            return

        # Sort: free slots first, then by quality score (match strength
        # outranks format tier — see _score_file docstring).
        candidates.sort(
            key=lambda c: (
                0 if c["free_upload"] else 1,
                _score_file(c["file"], track_status.artist, track_status.title),
            )
        )

        track_status.candidates = candidates
        track_status.status = "queued"

    except Exception as e:
        track_status.status = "error"
        track_status.error = str(e)


async def _download_one_track(
    client: SlskdClient,
    track_status: TrackStatus,
    playlist_name: str = "",
) -> None:
    """Phase 2 of the pipeline — download from a pre-resolved candidate list.

    Iterates `track_status.candidates` (already sorted by `_score_file`) up
    to MAX_DOWNLOAD_ATTEMPTS times, falling back to the next candidate when
    a download attempt fails. On success, records to the manifest and moves
    the file into the playlist folder. On exhaustion, marks the track as
    'error' with a message naming the attempt count and final exception.

    Tracks not in 'queued' state are skipped (no-op). This makes the
    function safe to call from a worker that processes a mix of statuses
    coming out of the search phase.
    """
    # Skip tracks that aren't ready for download (already completed via
    # manifest, or marked not_found / error during the search phase).
    if track_status.status != "queued":
        return

    candidates = track_status.candidates
    if not candidates:
        # Defensive — shouldn't happen if status is 'queued', but handle
        # gracefully if it does.
        track_status.status = "not_found"
        return

    attempted = candidates[:MAX_DOWNLOAD_ATTEMPTS]
    last_error: Exception | None = None
    try:
        for attempt_idx, candidate in enumerate(attempted, start=1):
            track_status.status = "downloading"
            track_status.quality = _describe_quality(candidate["file"])
            track_status.filename = (
                candidate["file"].get("filename", "").split("\\")[-1]
            )
            logger.info(
                f"Downloading (attempt {attempt_idx}/{len(attempted)}): "
                f"{track_status.filename} from {candidate['username']} "
                f"({track_status.quality})"
            )
            try:
                await client.download_file(
                    candidate["username"], candidate["file"]
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    f"Attempt {attempt_idx}/{len(attempted)} failed for "
                    f"{track_status.artist} - {track_status.title}: {e}"
                )
                continue
            # Success on this candidate.
            track_status.status = "completed"
            break
        else:
            # for/else: every attempt failed
            track_status.status = "error"
            track_status.error = (
                f"All {len(attempted)} candidates failed: {last_error}"
            )
            return

        # Post-download success steps (only on break from loop)
        if playlist_name:
            _move_to_playlist_folder(track_status.filename, playlist_name)
        if track_status.track_id:
            _record_download(
                track_status.track_id,
                track_status.filename,
                track_status.quality,
            )
    finally:
        # Free the candidates list. For large playlists, holding hundreds
        # of file dicts per track adds up quickly.
        track_status.candidates = []


async def _search_and_download_track(
    client: SlskdClient, track_status: TrackStatus, playlist_name: str = ""
) -> None:
    """Backwards-compatible wrapper that runs search + download sequentially
    for a single track. Preserves the pre-pipeline call surface so existing
    tests and any external callers keep working unchanged.

    The new pipeline in `process_playlist` does not use this wrapper — it
    calls `_search_for_candidates` and `_download_one_track` directly so
    that search and download can run in parallel through a queue.
    """
    await _search_for_candidates(client, track_status)
    await _download_one_track(client, track_status, playlist_name)


async def process_playlist(tracks: list[dict], playlist_name: str) -> None:
    """Search and download all tracks from a playlist via a two-phase pipeline.

    Architecture: search and download are decoupled into separate worker
    pools that communicate through an asyncio.Queue.

      ┌─ search worker (1) ──────┐
      │ for each track:          │
      │   _search_for_candidates │      ┌─ download workers (4) ───┐
      │   if 'queued': enqueue ──┼─────►│ pull from queue          │
      │   sleep SEARCH_DELAY     │      │ _download_one_track      │
      └──────────────────────────┘      │ (3-attempt fallback)     │
                                        └──────────────────────────┘

    Why decouple: search and download have very different rate-limit
    profiles. Searches trigger slskd's REST 429 and Soulseek's protocol
    flood-kick if bursted; downloads do not (they're per-peer transfers).
    The old wave-based design coupled them through one parallelism number,
    forcing a compromise between "search slow enough not to 429" and
    "download fast enough to feel snappy". Decoupling lets each phase use
    its own concurrency budget.

    Pipeline benefits:
      - Search runs sequentially (SEARCH_CONCURRENCY=1) with SEARCH_DELAY
        between calls — kind to slskd's rate limiter, no more 429.
      - Downloads run with DOWNLOAD_CONCURRENCY=4 — much faster than the
        old PARALLEL_SEARCHES=2 because the parallelism is now applied
        where it matters (the 10-min-per-attempt download polling, not
        the few-seconds search step).
      - Search and download interleave: as soon as the first track has
        candidates, download workers start pulling while the search worker
        is still resolving track 2, 3, 4. Total time ≈ max(search_total,
        download_total) instead of sum.
    """
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

    # Health check before doing anything. If slskd is disconnected, mark
    # everything as error and bail — same behavior as the old wave loop.
    if not await client.health_check():
        logger.warning("slskd disconnected, waiting 15s for reconnect...")
        await asyncio.sleep(15)
        if not await client.health_check():
            for t in session.tracks:
                t.status = "error"
                t.error = "slskd disconnected"
            session.active = False
            return

    # Sentinel to tell download workers there are no more tracks coming.
    DOWNLOAD_SENTINEL: object = object()
    download_queue: asyncio.Queue = asyncio.Queue()

    async def search_worker() -> None:
        """Resolve candidates for each track and enqueue ready ones."""
        for track in session.tracks:
            if not session.active:
                break
            await _search_for_candidates(client, track)
            # Only enqueue tracks that actually have something to download.
            # Manifest-skipped (status=completed), not_found, and error
            # tracks are terminal and skip the download phase entirely.
            if track.status == "queued":
                await download_queue.put(track)
            # Pace search calls to stay below slskd's REST rate limit.
            await asyncio.sleep(SEARCH_DELAY)
        # Signal end-of-stream to every download worker.
        for _ in range(DOWNLOAD_CONCURRENCY):
            await download_queue.put(DOWNLOAD_SENTINEL)

    async def download_worker() -> None:
        """Drain the queue and download until the sentinel is seen."""
        while True:
            track = await download_queue.get()
            if track is DOWNLOAD_SENTINEL:
                return
            if not session.active:
                # Drain remaining items quickly to unblock the search worker
                continue
            await _download_one_track(client, track, playlist_name)

    # Run search and downloads as concurrent coroutines. asyncio.gather
    # propagates exceptions; we don't expect any to escape because the
    # helpers catch their own errors and surface them as track.status='error'.
    await asyncio.gather(
        search_worker(),
        *[download_worker() for _ in range(DOWNLOAD_CONCURRENCY)],
    )

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
                "track_id": t.track_id,
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
