import re
from typing import Optional
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from app.config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET


def get_spotify_client() -> spotipy.Spotify:
    auth_manager = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
    )
    return spotipy.Spotify(auth_manager=auth_manager)


# Spotify URL recognizer for the unified input. Only track / album / playlist
# are supported; artist and show URLs are intentionally out of scope (they are
# not single-resource downloads). The regex tolerates optional scheme, the
# intl-xx/ country prefix Spotify now redirects through, and any ?si=... or
# other query params.
_SPOTIFY_URL_RE = re.compile(
    r"""^
    (?:https?://)?                     # optional scheme
    open\.spotify\.com/                # host
    (?:intl-[a-z]{2,3}/)?              # optional country prefix (intl-ru, intl-de, ...)
    (track|album|playlist)/            # resource kind
    ([A-Za-z0-9]{22})                  # 22-char base62 ID
    (?:[/?#].*)?$                      # optional trailing path/query/fragment
    """,
    re.VERBOSE,
)

# Spotify URI form: spotify:{kind}:{id}
_SPOTIFY_URI_RE = re.compile(r"^spotify:(track|album|playlist):([A-Za-z0-9]{22})$")


def parse_spotify_url(url: str) -> Optional[tuple]:
    """Classify a Spotify URL into ``(kind, id)`` or return None.

    Accepts both ``https://open.spotify.com/...`` and ``spotify:...`` URI forms,
    plus optional ``intl-xx/`` country prefix and trailing query parameters.
    Returns None for unsupported shapes (artist, show, podcast, plain text,
    malformed IDs). The parser's caller treats None as "not a URL — fall
    through to text parsing or needs_review".
    """
    if not url:
        return None
    url = url.strip()
    m = _SPOTIFY_URL_RE.match(url)
    if m:
        return (m.group(1), m.group(2))
    m = _SPOTIFY_URI_RE.match(url)
    if m:
        return (m.group(1), m.group(2))
    return None


def extract_playlist_id(url: str) -> str:
    """Extract playlist ID from various Spotify URL formats."""
    # Handle direct ID
    if re.match(r"^[a-zA-Z0-9]{22}$", url):
        return url

    # Handle URLs like:
    # https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M
    # spotify:playlist:37i9dQZF1DXcBWIGoYBM5M
    patterns = [
        r"playlist[/:]([a-zA-Z0-9]{22})",
        r"playlist/([a-zA-Z0-9]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    raise ValueError(f"Could not extract playlist ID from: {url}")


def get_playlist_tracks(playlist_url: str) -> dict:
    """Fetch all tracks from a Spotify playlist.

    Returns playlist info and list of tracks with artist/title/album.
    """
    sp = get_spotify_client()
    playlist_id = extract_playlist_id(playlist_url)

    playlist = sp.playlist(playlist_id)
    playlist_name = playlist["name"]
    playlist_image = None
    if playlist.get("images"):
        playlist_image = playlist["images"][0]["url"]

    tracks = []
    results = playlist["tracks"]

    while True:
        for item in results["items"]:
            track = item.get("track")
            if not track:
                continue

            artists = ", ".join(a["name"] for a in track["artists"])
            tracks.append({
                "id": track.get("id", ""),
                "artist": artists,
                "title": track["name"],
                "album": track.get("album", {}).get("name", ""),
                "duration_ms": track.get("duration_ms", 0),
            })

        if results["next"]:
            results = sp.next(results)
        else:
            break

    return {
        "name": playlist_name,
        "image": playlist_image,
        "total": len(tracks),
        "tracks": tracks,
    }


# Spotify API batch size for sp.tracks(). The API caps at 50 ids per call.
_TRACKS_BATCH_SIZE = 50


def _normalize_track(t: dict) -> dict:
    """Convert a spotipy track object into the canonical track dict used
    throughout the app. Matches the shape produced by ``get_playlist_tracks``.
    """
    artists = ", ".join(a["name"] for a in t.get("artists", []))
    album_obj = t.get("album") or {}
    return {
        "id": t.get("id", ""),
        "artist": artists,
        "title": t.get("name", ""),
        "album": album_obj.get("name", ""),
        "duration_ms": t.get("duration_ms", 0),
    }


def resolve_track_ids(track_ids: list) -> list:
    """Batch-resolve Spotify track IDs via sp.tracks().

    Returns a list aligned with the input: each entry is either a
    normalized track dict or ``None`` (for IDs Spotify could not resolve —
    deleted tracks, region-locked, or malformed). Batches at 50 per call
    because that's the Spotify API limit.

    An empty input list yields an empty output list without touching the
    Spotify client, so callers can call this unconditionally.
    """
    if not track_ids:
        return []

    sp = get_spotify_client()
    out: list = []
    for start in range(0, len(track_ids), _TRACKS_BATCH_SIZE):
        batch = track_ids[start : start + _TRACKS_BATCH_SIZE]
        resp = sp.tracks(batch)
        for t in resp.get("tracks", []):
            if t is None:
                out.append(None)
            else:
                out.append(_normalize_track(t))
    return out


def resolve_album(album_id: str) -> dict:
    """Resolve a Spotify album ID into its metadata and full track list.

    Returns a dict with ``name``, ``first_artist``, ``image`` (URL or None),
    and ``tracks`` (list of normalized track dicts). Album tracks do not
    carry their own album object in the spotipy response, so we fill
    ``track["album"]`` from the parent album name to keep the shape
    consistent with playlist-sourced tracks.
    """
    sp = get_spotify_client()
    album = sp.album(album_id)
    album_name = album.get("name", "")
    artists = album.get("artists", [])
    first_artist = artists[0]["name"] if artists else ""
    images = album.get("images") or []
    image = images[0]["url"] if images else None

    tracks: list = []
    results = sp.album_tracks(album_id)
    while True:
        for item in results.get("items", []):
            item_artists = ", ".join(a["name"] for a in item.get("artists", []))
            tracks.append(
                {
                    "id": item.get("id", ""),
                    "artist": item_artists,
                    "title": item.get("name", ""),
                    "album": album_name,
                    "duration_ms": item.get("duration_ms", 0),
                }
            )
        if results.get("next"):
            results = sp.next(results)
        else:
            break

    return {
        "name": album_name,
        "first_artist": first_artist,
        "image": image,
        "tracks": tracks,
    }
