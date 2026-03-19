import re
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from app.config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET


def get_spotify_client() -> spotipy.Spotify:
    auth_manager = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
    )
    return spotipy.Spotify(auth_manager=auth_manager)


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
