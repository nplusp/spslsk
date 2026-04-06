"""Tests for Spotify URL classification and batch resolution.

These tests never hit the real Spotify API — spotipy calls are mocked.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.spotify import (
    parse_spotify_url,
    resolve_track_ids,
    resolve_album,
)


# ---------------------------------------------------------------------------
# parse_spotify_url — pure regex, no network
# ---------------------------------------------------------------------------


class TestParseSpotifyUrl:
    def test_track_https(self):
        result = parse_spotify_url(
            "https://open.spotify.com/track/3WwnmMcnZueBYwnJ76QEli"
        )
        assert result == ("track", "3WwnmMcnZueBYwnJ76QEli")

    def test_album_https(self):
        result = parse_spotify_url(
            "https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3"
        )
        assert result == ("album", "1DFixLWuPkv3KT3TnV35m3")

    def test_playlist_https(self):
        result = parse_spotify_url(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
        )
        assert result == ("playlist", "37i9dQZF1DXcBWIGoYBM5M")

    def test_intl_country_prefix_is_stripped(self):
        result = parse_spotify_url(
            "https://open.spotify.com/intl-ru/track/3WwnmMcnZueBYwnJ76QEli"
        )
        assert result == ("track", "3WwnmMcnZueBYwnJ76QEli")

    def test_si_query_param_is_ignored(self):
        result = parse_spotify_url(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc123xyz"
        )
        assert result == ("playlist", "37i9dQZF1DXcBWIGoYBM5M")

    def test_intl_and_query_param_together(self):
        result = parse_spotify_url(
            "https://open.spotify.com/intl-de/album/1DFixLWuPkv3KT3TnV35m3?si=foo"
        )
        assert result == ("album", "1DFixLWuPkv3KT3TnV35m3")

    def test_uri_form_track(self):
        result = parse_spotify_url("spotify:track:3WwnmMcnZueBYwnJ76QEli")
        assert result == ("track", "3WwnmMcnZueBYwnJ76QEli")

    def test_uri_form_album(self):
        result = parse_spotify_url("spotify:album:1DFixLWuPkv3KT3TnV35m3")
        assert result == ("album", "1DFixLWuPkv3KT3TnV35m3")

    def test_plain_text_is_not_a_url(self):
        assert parse_spotify_url("Radiohead - Creep") is None

    def test_artist_urls_are_out_of_scope(self):
        # Artist URLs are a real Spotify URL shape but not supported by this
        # feature. Parser must reject them so the upstream caller can mark
        # the line as needs_review instead of silently downloading an artist.
        assert (
            parse_spotify_url(
                "https://open.spotify.com/artist/4Z8W4fKeB5YxbusRsdQVPb"
            )
            is None
        )

    def test_show_urls_are_out_of_scope(self):
        assert (
            parse_spotify_url(
                "https://open.spotify.com/show/4rOoJ6Egrf8K2IrywzwOMk"
            )
            is None
        )

    def test_malformed_id_length(self):
        # Spotify IDs are exactly 22 base62 chars. Anything else is invalid.
        assert parse_spotify_url("https://open.spotify.com/track/too-short") is None

    def test_empty_string(self):
        assert parse_spotify_url("") is None

    def test_http_not_https_is_accepted(self):
        # Some legacy copy-paste flows produce http://. Accept both.
        result = parse_spotify_url(
            "http://open.spotify.com/track/3WwnmMcnZueBYwnJ76QEli"
        )
        assert result == ("track", "3WwnmMcnZueBYwnJ76QEli")

    def test_no_scheme_is_accepted(self):
        # Bare open.spotify.com/... is common from chat apps that strip the scheme.
        result = parse_spotify_url(
            "open.spotify.com/track/3WwnmMcnZueBYwnJ76QEli"
        )
        assert result == ("track", "3WwnmMcnZueBYwnJ76QEli")


# ---------------------------------------------------------------------------
# resolve_track_ids — batched sp.tracks() calls
# ---------------------------------------------------------------------------


def _fake_track(track_id: str, artist: str, title: str, album: str = "Album X"):
    """Build a spotipy-shaped track dict for mocking."""
    return {
        "id": track_id,
        "name": title,
        "artists": [{"name": artist}],
        "album": {"name": album, "images": []},
        "duration_ms": 180000,
    }


class TestResolveTrackIds:
    @patch("app.spotify.get_spotify_client")
    def test_empty_input_returns_empty_list(self, mock_client):
        result = resolve_track_ids([])
        assert result == []
        mock_client.assert_not_called()

    @patch("app.spotify.get_spotify_client")
    def test_single_batch_under_50(self, mock_client):
        sp = MagicMock()
        sp.tracks.return_value = {
            "tracks": [
                _fake_track("id1", "Radiohead", "Creep"),
                _fake_track("id2", "Miles Davis", "So What"),
            ]
        }
        mock_client.return_value = sp

        result = resolve_track_ids(["id1", "id2"])

        assert len(result) == 2
        assert result[0]["id"] == "id1"
        assert result[0]["artist"] == "Radiohead"
        assert result[0]["title"] == "Creep"
        assert result[1]["artist"] == "Miles Davis"
        # Single call because 2 ids fit in one batch.
        assert sp.tracks.call_count == 1
        sp.tracks.assert_called_once_with(["id1", "id2"])

    @patch("app.spotify.get_spotify_client")
    def test_batches_over_50(self, mock_client):
        sp = MagicMock()
        # Simulate 75 tracks -> 50 + 25. Build two successive return values.
        batch1 = [_fake_track(f"id{i}", f"Artist{i}", f"Title{i}") for i in range(50)]
        batch2 = [_fake_track(f"id{i}", f"Artist{i}", f"Title{i}") for i in range(50, 75)]
        sp.tracks.side_effect = [{"tracks": batch1}, {"tracks": batch2}]
        mock_client.return_value = sp

        ids = [f"id{i}" for i in range(75)]
        result = resolve_track_ids(ids)

        assert len(result) == 75
        assert sp.tracks.call_count == 2

    @patch("app.spotify.get_spotify_client")
    def test_none_in_response_becomes_none_in_output(self, mock_client):
        # Spotipy returns None inside the tracks list for invalid IDs.
        sp = MagicMock()
        sp.tracks.return_value = {
            "tracks": [
                _fake_track("id1", "Radiohead", "Creep"),
                None,  # invalid id
                _fake_track("id3", "Daft Punk", "Around the World"),
            ]
        }
        mock_client.return_value = sp

        result = resolve_track_ids(["id1", "invalid", "id3"])

        assert len(result) == 3
        assert result[0] is not None
        assert result[1] is None
        assert result[2] is not None

    @patch("app.spotify.get_spotify_client")
    def test_multiple_artists_joined(self, mock_client):
        sp = MagicMock()
        sp.tracks.return_value = {
            "tracks": [
                {
                    "id": "id1",
                    "name": "Under Pressure",
                    "artists": [{"name": "Queen"}, {"name": "David Bowie"}],
                    "album": {"name": "Hot Space", "images": []},
                    "duration_ms": 245000,
                }
            ]
        }
        mock_client.return_value = sp

        result = resolve_track_ids(["id1"])
        assert result[0]["artist"] == "Queen, David Bowie"


# ---------------------------------------------------------------------------
# resolve_album — single album lookup + paginated track list
# ---------------------------------------------------------------------------


class TestResolveAlbum:
    @patch("app.spotify.get_spotify_client")
    def test_happy_path(self, mock_client):
        sp = MagicMock()
        sp.album.return_value = {
            "id": "album1",
            "name": "Kind of Blue",
            "artists": [{"name": "Miles Davis"}],
            "images": [{"url": "https://img.example/kob.jpg"}],
        }
        sp.album_tracks.return_value = {
            "items": [
                {
                    "id": "t1",
                    "name": "So What",
                    "artists": [{"name": "Miles Davis"}],
                    "duration_ms": 563000,
                },
                {
                    "id": "t2",
                    "name": "Freddie Freeloader",
                    "artists": [{"name": "Miles Davis"}],
                    "duration_ms": 587000,
                },
            ],
            "next": None,
        }
        mock_client.return_value = sp

        result = resolve_album("album1")

        assert result["name"] == "Kind of Blue"
        assert result["first_artist"] == "Miles Davis"
        assert result["image"] == "https://img.example/kob.jpg"
        assert len(result["tracks"]) == 2
        assert result["tracks"][0]["id"] == "t1"
        assert result["tracks"][0]["title"] == "So What"
        # Album tracks lack their own album object; resolver should fill it
        # from the parent album.
        assert result["tracks"][0]["album"] == "Kind of Blue"

    @patch("app.spotify.get_spotify_client")
    def test_album_without_image(self, mock_client):
        sp = MagicMock()
        sp.album.return_value = {
            "id": "album1",
            "name": "Obscure Album",
            "artists": [{"name": "Unknown Artist"}],
            "images": [],
        }
        sp.album_tracks.return_value = {"items": [], "next": None}
        mock_client.return_value = sp

        result = resolve_album("album1")
        assert result["image"] is None
        assert result["tracks"] == []

    @patch("app.spotify.get_spotify_client")
    def test_paginated_tracks(self, mock_client):
        sp = MagicMock()
        sp.album.return_value = {
            "id": "album1",
            "name": "Long Album",
            "artists": [{"name": "Some Artist"}],
            "images": [{"url": "img"}],
        }
        first_page = {
            "items": [
                {
                    "id": f"t{i}",
                    "name": f"Track {i}",
                    "artists": [{"name": "Some Artist"}],
                    "duration_ms": 200000,
                }
                for i in range(50)
            ],
            "next": "cursor",
        }
        second_page = {
            "items": [
                {
                    "id": f"t{i}",
                    "name": f"Track {i}",
                    "artists": [{"name": "Some Artist"}],
                    "duration_ms": 200000,
                }
                for i in range(50, 62)
            ],
            "next": None,
        }
        sp.album_tracks.return_value = first_page
        sp.next.return_value = second_page
        mock_client.return_value = sp

        result = resolve_album("album1")
        assert len(result["tracks"]) == 62
