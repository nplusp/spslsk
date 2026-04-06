"""Unified input parser.

Takes a raw textarea blob from the user and produces a canonical track list
that the existing download pipeline can consume unchanged. Handles four line
kinds in one sweep:

  1. Spotify playlist URLs  -> reuse get_playlist_tracks
  2. Spotify album URLs     -> resolve_album
  3. Spotify track URLs     -> resolve_track_ids (batched at 50 per call)
  4. Plain text lines       -> separator-based artist/title split

Empty lines and ``#`` comments are silently dropped. Lines that cannot be
classified or resolved become ``needs_review`` rows so the user can fix or
delete them in the preview.

All Spotify noise cleaning (feat. X, [Remastered], year markers, apostrophes)
is delegated to the existing ``_clean_query`` helper in ``app.downloader`` so
that preview values equal eventual search values — one source of truth.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Optional

from spotipy.exceptions import SpotifyException

from app.downloader import _clean_query, _load_manifest
from app.spotify import (
    get_playlist_tracks,
    parse_spotify_url,
    resolve_album,
    resolve_track_ids,
)


# Separator priority for text line splitting. First match wins. The hyphen
# entry intentionally requires spaces on both sides to preserve artist names
# like "Jay-Z", "t-A-T-u", and "Crosby, Stills, Nash & Young".
_SEPARATORS = (
    "—",   # em-dash
    "–",   # en-dash
    " - ",  # hyphen with surrounding spaces
    ":",
    " / ",
)


def manual_track_id(artist: str, title: str) -> str:
    """Synthetic track ID for text-sourced entries.

    Case-insensitive SHA1 of ``{artist}|{title}`` truncated to 16 hex chars.
    Prefixed with ``manual:`` so the downstream manifest can distinguish
    text entries from real Spotify IDs in the same keyspace. The rest of
    the download pipeline treats this as an opaque dict key — see
    ``app.downloader._record_download`` and ``_is_already_downloaded``.
    """
    normalized = f"{artist.strip().lower()}|{title.strip().lower()}"
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"manual:{digest}"


def _split_text_line(line: str) -> Optional[tuple]:
    """Split a text line into (artist, title) using the leftmost separator.

    Returns None if no separator is found — the caller marks the row as
    needs_review. We pick the *earliest* occurrence across all allowed
    separators so that a line like ``"Artist - Title — Version"`` splits
    at the first hyphen (position-based), not at the em-dash the priority
    list happens to check first. For ``"A - B - C"`` this still gives
    ``("A", "B - C")`` since the leftmost ` - ` wins.
    """
    best_idx = -1
    best_sep = ""
    for sep in _SEPARATORS:
        idx = line.find(sep)
        if idx == -1:
            continue
        if best_idx == -1 or idx < best_idx:
            best_idx = idx
            best_sep = sep
    if best_idx == -1:
        return None
    left = line[:best_idx].strip()
    right = line[best_idx + len(best_sep):].strip()
    if not left or not right:
        return None
    return (left, right)


def _make_text_track(line: str, manifest: dict) -> dict:
    """Build a track dict from a plain text line."""
    split = _split_text_line(line)
    if split is None:
        return {
            "id": "",
            "artist": "",
            "title": "",
            "album": "",
            "duration_ms": 0,
            "source": "text",
            "state": "needs_review",
            "error": "No separator found — add ' - ' between artist and title, or delete",
            "raw_line": line,
        }
    raw_artist, raw_title = split
    artist = _clean_query(raw_artist)
    title = _clean_query(raw_title)
    track_id = manual_track_id(artist, title)
    state = "already_downloaded" if track_id in manifest else "ready"
    return {
        "id": track_id,
        "artist": artist,
        "title": title,
        "album": "",
        "duration_ms": 0,
        "source": "text",
        "state": state,
        "error": None,
        "raw_line": line,
    }


def _apply_clean_and_state(
    normalized: dict,
    source: str,
    raw_line: str,
    manifest: dict,
) -> dict:
    """Attach source, state, and cleaned fields to a Spotify-sourced track."""
    artist = _clean_query(normalized.get("artist", ""))
    title = _clean_query(normalized.get("title", ""))
    track_id = normalized.get("id", "")
    state = "already_downloaded" if track_id and track_id in manifest else "ready"
    return {
        "id": track_id,
        "artist": artist,
        "title": title,
        "album": normalized.get("album", ""),
        "duration_ms": normalized.get("duration_ms", 0),
        "source": source,
        "state": state,
        "error": None,
        "raw_line": raw_line,
    }


def _needs_review_row(source: str, raw_line: str, error: str) -> dict:
    """Build a needs_review placeholder row for a failed Spotify resolution."""
    return {
        "id": "",
        "artist": "",
        "title": "",
        "album": "",
        "duration_ms": 0,
        "source": source,
        "state": "needs_review",
        "error": error,
        "raw_line": raw_line,
    }


def _mixed_list_name() -> str:
    """Timestamped fallback name for mixed or text-only input."""
    return f"Mixed list {datetime.now().strftime('%Y-%m-%d %H:%M')}"


def parse_input(text: str) -> dict:
    """Parse a raw textarea blob into a canonical ParsedInput dict.

    Return shape::

        {
            "tracks": [ {id, artist, title, album, duration_ms,
                         source, state, error, raw_line}, ... ],
            "suggested_name": str,
            "suggested_image": str | None,
            "raw_text": str,       # original text preserved for History
        }

    Per-URL errors are isolated: one failing Spotify call becomes one
    needs_review row, other rows continue to resolve. Unexpected exceptions
    bubble up — the FastAPI layer turns them into 5xx.
    """
    raw_text = text
    lines = text.splitlines()

    # Phase 1 — line classification
    # entries is a flat list of {kind, payload, raw_line} describing what each
    # non-skipped input line is. We'll resolve them in phase 2 and flatten
    # into the final tracks list in phase 3.
    entries: list = []
    track_url_ids: list = []
    track_url_raw_lines: list = []
    track_url_entry_indexes: list = []

    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        parsed_url = parse_spotify_url(stripped)
        if parsed_url is not None:
            kind, spid = parsed_url
            if kind == "track":
                entries.append(
                    {"kind": "track_url_pending", "id": spid, "raw": raw}
                )
                track_url_ids.append(spid)
                track_url_raw_lines.append(raw)
                track_url_entry_indexes.append(len(entries) - 1)
            elif kind == "album":
                entries.append({"kind": "album_url", "id": spid, "raw": raw})
            elif kind == "playlist":
                entries.append({"kind": "playlist_url", "id": spid, "raw": raw})
            continue

        # Catch Spotify URLs we recognize but don't support (artist, show) by
        # checking for the domain; fall through to text parsing otherwise.
        if "open.spotify.com/" in stripped or stripped.startswith("spotify:"):
            entries.append(
                {
                    "kind": "unsupported_url",
                    "raw": raw,
                    "error": "Unsupported Spotify URL — only track, album, and playlist URLs are accepted",
                }
            )
            continue

        entries.append({"kind": "text", "raw": raw})

    # Phase 2 — resolve Spotify URLs
    manifest = _load_manifest()

    # 2a. Batch-resolve all track URL IDs in one pass.
    resolved_tracks: list = []
    if track_url_ids:
        try:
            resolved_tracks = resolve_track_ids(track_url_ids)
        except SpotifyException as e:
            # A whole-batch failure is rare but possible (auth, network).
            # Mark every track URL entry as needs_review with the shared
            # error so the user sees what happened.
            for idx, raw in zip(track_url_entry_indexes, track_url_raw_lines):
                entries[idx] = {
                    "kind": "track_url_resolved",
                    "track": None,
                    "raw": raw,
                    "error": f"Spotify API error: {e}",
                }
            resolved_tracks = []

    # Attach each resolved track (or None) back to its pending entry.
    for i, idx in enumerate(track_url_entry_indexes):
        if entries[idx]["kind"] == "track_url_resolved":
            continue  # already errored in batch failure above
        resolved = resolved_tracks[i] if i < len(resolved_tracks) else None
        entries[idx] = {
            "kind": "track_url_resolved",
            "track": resolved,
            "raw": track_url_raw_lines[i],
            "error": None if resolved else "Spotify track not found",
        }

    # 2b. Resolve albums and playlists individually with per-URL error isolation.
    for entry in entries:
        if entry["kind"] == "album_url":
            try:
                entry["album"] = resolve_album(entry["id"])
            except SpotifyException as e:
                entry["album"] = None
                entry["error"] = f"Invalid album URL: {e}"
        elif entry["kind"] == "playlist_url":
            try:
                # The existing helper takes a URL, not an ID, but accepts
                # bare IDs too via extract_playlist_id.
                entry["playlist"] = get_playlist_tracks(entry["id"])
            except Exception as e:  # noqa: BLE001 — spotipy raises a variety
                entry["playlist"] = None
                entry["error"] = f"Invalid playlist URL: {e}"

    # Phase 3 — flatten into the final tracks list, preserving input order
    tracks: list = []
    for entry in entries:
        kind = entry["kind"]
        if kind == "text":
            tracks.append(_make_text_track(entry["raw"], manifest))

        elif kind == "track_url_resolved":
            if entry.get("error") or entry["track"] is None:
                tracks.append(
                    _needs_review_row("track", entry["raw"], entry.get("error") or "Spotify track not found")
                )
            else:
                tracks.append(
                    _apply_clean_and_state(entry["track"], "track", entry["raw"], manifest)
                )

        elif kind == "album_url":
            album = entry.get("album")
            if album is None:
                tracks.append(_needs_review_row("album", entry["raw"], entry.get("error") or "Invalid album URL"))
            else:
                for t in album.get("tracks", []):
                    tracks.append(_apply_clean_and_state(t, "album", entry["raw"], manifest))

        elif kind == "playlist_url":
            playlist = entry.get("playlist")
            if playlist is None:
                tracks.append(_needs_review_row("playlist", entry["raw"], entry.get("error") or "Invalid playlist URL"))
            else:
                for t in playlist.get("tracks", []):
                    tracks.append(_apply_clean_and_state(t, "playlist", entry["raw"], manifest))

        elif kind == "unsupported_url":
            tracks.append(_needs_review_row("text", entry["raw"], entry["error"]))

    # Phase 4 — auto-name and auto-image composition
    # "Single source" = exactly one entry, and it is a successful playlist or album.
    suggested_name: str
    suggested_image: Optional[str] = None

    non_skipped = entries
    if len(non_skipped) == 1:
        only = non_skipped[0]
        if only["kind"] == "playlist_url" and only.get("playlist") is not None:
            pl = only["playlist"]
            suggested_name = pl.get("name") or _mixed_list_name()
            suggested_image = pl.get("image")
        elif only["kind"] == "album_url" and only.get("album") is not None:
            album = only["album"]
            first_artist = album.get("first_artist", "")
            album_name = album.get("name", "")
            if first_artist and album_name:
                suggested_name = f"{first_artist} — {album_name}"
            else:
                suggested_name = album_name or _mixed_list_name()
            suggested_image = album.get("image")
        else:
            suggested_name = _mixed_list_name()
    else:
        suggested_name = _mixed_list_name()

    return {
        "tracks": tracks,
        "suggested_name": suggested_name,
        "suggested_image": suggested_image,
        "raw_text": raw_text,
    }
