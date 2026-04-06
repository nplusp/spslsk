"""Tests for the unified input parser.

These tests cover the core contract of parse_input: line classification,
text separator rules, Spotify URL resolution (mocked), _clean_query
integration, manifest-based already-downloaded marking, synthetic manual
IDs, and auto-name / auto-image composition logic.
"""
import re
from unittest.mock import MagicMock, patch

import pytest

from app.parser import parse_input, manual_track_id


# ---------------------------------------------------------------------------
# Text line parsing — separators and needs_review
# ---------------------------------------------------------------------------


class TestTextLineParsing:
    def test_empty_input(self):
        result = parse_input("")
        assert result["tracks"] == []
        assert result["suggested_image"] is None

    def test_blank_lines_and_comments_skipped(self):
        result = parse_input("\n# a comment\n   \n#another\n")
        assert result["tracks"] == []

    def test_hyphen_with_spaces(self):
        result = parse_input("Radiohead - Creep")
        assert len(result["tracks"]) == 1
        t = result["tracks"][0]
        assert t["artist"] == "Radiohead"
        assert t["title"] == "Creep"
        assert t["source"] == "text"
        assert t["state"] == "ready"

    def test_em_dash(self):
        result = parse_input("Miles Davis — So What")
        assert result["tracks"][0]["artist"] == "Miles Davis"
        assert result["tracks"][0]["title"] == "So What"

    def test_en_dash(self):
        result = parse_input("Daft Punk – Around the World")
        assert result["tracks"][0]["artist"] == "Daft Punk"
        assert result["tracks"][0]["title"] == "Around the World"

    def test_colon_separator(self):
        result = parse_input("Nine Inch Nails: Hurt")
        assert result["tracks"][0]["artist"] == "Nine Inch Nails"
        assert result["tracks"][0]["title"] == "Hurt"

    def test_slash_separator(self):
        result = parse_input("Boards of Canada / Roygbiv")
        assert result["tracks"][0]["artist"] == "Boards of Canada"
        assert result["tracks"][0]["title"] == "Roygbiv"

    def test_hyphen_without_spaces_is_not_a_separator(self):
        # Jay-Z should stay intact. This line has no separator → needs_review.
        result = parse_input("Jay-Z")
        assert len(result["tracks"]) == 1
        assert result["tracks"][0]["state"] == "needs_review"

    def test_jay_z_with_proper_separator_stays_intact(self):
        result = parse_input("Jay-Z - 99 Problems")
        t = result["tracks"][0]
        assert t["artist"] == "Jay-Z"
        assert t["title"] == "99 Problems"
        assert t["state"] == "ready"

    def test_first_separator_wins(self):
        # Extra dashes in the title are kept as part of the title and
        # later cleaned by _clean_query (which strips year/remaster noise).
        result = parse_input("Crosby, Stills, Nash - Helplessly Hoping - 2005 Remaster")
        t = result["tracks"][0]
        assert t["artist"] == "Crosby, Stills, Nash"
        # _clean_query strips "2005 Remaster" noise
        assert "Remaster" not in t["title"]
        assert "Helplessly Hoping" in t["title"]

    def test_leftmost_separator_wins_across_types(self):
        # When multiple different separator types appear, the one
        # appearing FIRST in the line should win (position-based), not
        # the one with the highest priority in the separator list.
        # Here ' - ' appears at position 9, '—' at position 21.
        # The hyphen should win: artist='Artist', title='Title — Version'
        result = parse_input("Artist - Title — Version")
        t = result["tracks"][0]
        assert t["artist"] == "Artist"
        assert "Title" in t["title"]
        # The em-dash content stays in the title (it's just noise words).
        assert "Version" in t["title"] or t["title"] == "Title"

    def test_no_separator_marks_needs_review(self):
        result = parse_input("radiohead paranoid android")
        assert result["tracks"][0]["state"] == "needs_review"
        assert result["tracks"][0]["error"]
        assert result["tracks"][0]["raw_line"] == "radiohead paranoid android"

    def test_clean_query_applied_to_title(self):
        # Existing _clean_query strips (feat. X), [Remastered], year markers,
        # etc. The preview should show exactly what will be searched.
        result = parse_input("Radiohead - Karma Police (2015 Remastered)")
        t = result["tracks"][0]
        assert "2015" not in t["title"]
        assert "Remastered" not in t["title"]
        assert "Karma Police" in t["title"]

    def test_clean_query_applied_to_feat(self):
        result = parse_input("Kanye West - Stronger (feat. Daft Punk)")
        t = result["tracks"][0]
        assert "feat" not in t["title"].lower()
        assert "Stronger" in t["title"]


# ---------------------------------------------------------------------------
# Manual track ID — synthetic hash-based IDs
# ---------------------------------------------------------------------------


class TestManualTrackId:
    def test_starts_with_manual_prefix(self):
        tid = manual_track_id("Radiohead", "Creep")
        assert tid.startswith("manual:")

    def test_stable_across_calls(self):
        assert manual_track_id("Radiohead", "Creep") == manual_track_id("Radiohead", "Creep")

    def test_case_insensitive(self):
        assert manual_track_id("radiohead", "creep") == manual_track_id("RADIOHEAD", "CREEP")

    def test_different_inputs_produce_different_ids(self):
        a = manual_track_id("Radiohead", "Creep")
        b = manual_track_id("Radiohead", "Karma Police")
        assert a != b

    def test_fixed_length(self):
        # manual: prefix + 16 hex chars
        tid = manual_track_id("Radiohead", "Creep")
        assert len(tid) == len("manual:") + 16


# ---------------------------------------------------------------------------
# Spotify URL resolution inside parser
# ---------------------------------------------------------------------------


class TestSpotifyResolution:
    @patch("app.parser.resolve_track_ids")
    def test_single_track_url(self, mock_resolve):
        mock_resolve.return_value = [
            {
                "id": "3WwnmMcnZueBYwnJ76QEli",
                "artist": "Radiohead",
                "title": "Creep",
                "album": "Pablo Honey",
                "duration_ms": 238000,
            }
        ]
        result = parse_input("https://open.spotify.com/track/3WwnmMcnZueBYwnJ76QEli")
        assert len(result["tracks"]) == 1
        t = result["tracks"][0]
        assert t["id"] == "3WwnmMcnZueBYwnJ76QEli"
        assert t["artist"] == "Radiohead"
        assert t["source"] == "track"
        assert t["state"] == "ready"

    @patch("app.parser.resolve_track_ids")
    def test_track_not_found_becomes_needs_review(self, mock_resolve):
        mock_resolve.return_value = [None]
        result = parse_input("https://open.spotify.com/track/3WwnmMcnZueBYwnJ76QEli")
        t = result["tracks"][0]
        assert t["state"] == "needs_review"
        assert "not found" in t["error"].lower() or "not found" in t["error"]

    @patch("app.parser.resolve_track_ids")
    def test_multiple_track_urls_batched(self, mock_resolve):
        mock_resolve.return_value = [
            {"id": "id1", "artist": "A", "title": "T1", "album": "", "duration_ms": 0},
            {"id": "id2", "artist": "B", "title": "T2", "album": "", "duration_ms": 0},
        ]
        text = (
            "https://open.spotify.com/track/3WwnmMcnZueBYwnJ76QEli\n"
            "https://open.spotify.com/track/2OMTsHqCK5Gj2oiguW0iWM"
        )
        result = parse_input(text)
        assert len(result["tracks"]) == 2
        # Single batched call despite two URL lines
        assert mock_resolve.call_count == 1

    @patch("app.parser.resolve_album")
    def test_album_url_expanded(self, mock_album):
        mock_album.return_value = {
            "name": "Kind of Blue",
            "first_artist": "Miles Davis",
            "image": "https://img/kob.jpg",
            "tracks": [
                {"id": "t1", "artist": "Miles Davis", "title": "So What", "album": "Kind of Blue", "duration_ms": 0},
                {"id": "t2", "artist": "Miles Davis", "title": "Freddie Freeloader", "album": "Kind of Blue", "duration_ms": 0},
            ],
        }
        result = parse_input("https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3")
        assert len(result["tracks"]) == 2
        for t in result["tracks"]:
            assert t["source"] == "album"
            assert t["state"] == "ready"

    @patch("app.parser.get_playlist_tracks")
    def test_playlist_url_expanded(self, mock_playlist):
        mock_playlist.return_value = {
            "name": "My Playlist",
            "image": "https://img/p.jpg",
            "total": 2,
            "tracks": [
                {"id": "t1", "artist": "A", "title": "T1", "album": "", "duration_ms": 0},
                {"id": "t2", "artist": "B", "title": "T2", "album": "", "duration_ms": 0},
            ],
        }
        result = parse_input("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
        assert len(result["tracks"]) == 2
        for t in result["tracks"]:
            assert t["source"] == "playlist"

    @patch("app.parser.resolve_album")
    def test_album_error_becomes_needs_review(self, mock_album):
        from spotipy.exceptions import SpotifyException

        mock_album.side_effect = SpotifyException(404, -1, "not found")
        result = parse_input("https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3")
        assert len(result["tracks"]) == 1
        assert result["tracks"][0]["state"] == "needs_review"
        assert result["tracks"][0]["source"] == "album"

    @patch("app.parser.get_playlist_tracks")
    @patch("app.parser.resolve_track_ids")
    def test_mixed_input_preserves_order(self, mock_tracks, mock_playlist):
        mock_playlist.return_value = {
            "name": "PL",
            "image": None,
            "total": 1,
            "tracks": [{"id": "pl1", "artist": "PA", "title": "PT", "album": "", "duration_ms": 0}],
        }
        mock_tracks.return_value = [
            {"id": "tr1", "artist": "TA", "title": "TT", "album": "", "duration_ms": 0}
        ]
        text = (
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M\n"
            "Some Artist - Some Title\n"
            "https://open.spotify.com/track/3WwnmMcnZueBYwnJ76QEli\n"
            "bad line no separator\n"
        )
        result = parse_input(text)
        tracks = result["tracks"]
        assert len(tracks) == 4
        assert tracks[0]["source"] == "playlist"
        assert tracks[1]["source"] == "text"
        assert tracks[1]["artist"] == "Some Artist"
        assert tracks[2]["source"] == "track"
        assert tracks[3]["state"] == "needs_review"

    def test_artist_url_rejected_as_needs_review(self):
        # Artist URLs are not supported — should fall through as needs_review,
        # not silently become a text line.
        result = parse_input("https://open.spotify.com/artist/4Z8W4fKeB5YxbusRsdQVPb")
        assert len(result["tracks"]) == 1
        assert result["tracks"][0]["state"] == "needs_review"


# ---------------------------------------------------------------------------
# Manifest integration — already_downloaded state
# ---------------------------------------------------------------------------


class TestManifestIntegration:
    @patch("app.parser._load_manifest")
    def test_text_track_marked_already_downloaded(self, mock_manifest):
        # Parser computes the manual: id for "Radiohead - Creep" and looks it up.
        expected_id = manual_track_id("Radiohead", "Creep")
        mock_manifest.return_value = {
            expected_id: {"filename": "creep.flac", "quality": "FLAC (lossless)"}
        }
        result = parse_input("Radiohead - Creep")
        assert result["tracks"][0]["state"] == "already_downloaded"

    @patch("app.parser._load_manifest")
    @patch("app.parser.resolve_track_ids")
    def test_spotify_track_marked_already_downloaded(self, mock_resolve, mock_manifest):
        mock_resolve.return_value = [
            {
                "id": "3WwnmMcnZueBYwnJ76QEli",
                "artist": "Radiohead",
                "title": "Creep",
                "album": "Pablo Honey",
                "duration_ms": 0,
            }
        ]
        mock_manifest.return_value = {
            "3WwnmMcnZueBYwnJ76QEli": {"filename": "creep.flac", "quality": "FLAC (lossless)"}
        }
        result = parse_input("https://open.spotify.com/track/3WwnmMcnZueBYwnJ76QEli")
        assert result["tracks"][0]["state"] == "already_downloaded"

    @patch("app.parser._load_manifest")
    def test_not_in_manifest_stays_ready(self, mock_manifest):
        mock_manifest.return_value = {}
        result = parse_input("Radiohead - Creep")
        assert result["tracks"][0]["state"] == "ready"


# ---------------------------------------------------------------------------
# Auto-name and auto-image logic
# ---------------------------------------------------------------------------


class TestAutoName:
    @patch("app.parser.get_playlist_tracks")
    def test_single_playlist_uses_playlist_name(self, mock_playlist):
        mock_playlist.return_value = {
            "name": "My Favorite Playlist",
            "image": "https://img/pl.jpg",
            "total": 1,
            "tracks": [{"id": "t1", "artist": "A", "title": "T", "album": "", "duration_ms": 0}],
        }
        result = parse_input("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
        assert result["suggested_name"] == "My Favorite Playlist"
        assert result["suggested_image"] == "https://img/pl.jpg"

    @patch("app.parser.resolve_album")
    def test_single_album_uses_artist_dash_album(self, mock_album):
        mock_album.return_value = {
            "name": "Kind of Blue",
            "first_artist": "Miles Davis",
            "image": "https://img/kob.jpg",
            "tracks": [
                {"id": "t1", "artist": "Miles Davis", "title": "So What", "album": "Kind of Blue", "duration_ms": 0},
            ],
        }
        result = parse_input("https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3")
        assert result["suggested_name"] == "Miles Davis — Kind of Blue"
        assert result["suggested_image"] == "https://img/kob.jpg"

    def test_text_only_uses_mixed_list(self):
        result = parse_input("Radiohead - Creep")
        # Text-only input should NOT be treated as a single-source case.
        assert result["suggested_name"].startswith("Mixed list ")
        assert result["suggested_image"] is None
        # Format: "Mixed list YYYY-MM-DD HH:MM"
        assert re.match(
            r"^Mixed list \d{4}-\d{2}-\d{2} \d{2}:\d{2}$",
            result["suggested_name"],
        )

    @patch("app.parser.get_playlist_tracks")
    def test_playlist_plus_extra_line_is_mixed(self, mock_playlist):
        mock_playlist.return_value = {
            "name": "My Playlist",
            "image": "https://img/p.jpg",
            "total": 1,
            "tracks": [{"id": "t1", "artist": "A", "title": "T", "album": "", "duration_ms": 0}],
        }
        result = parse_input(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M\n"
            "Radiohead - Creep"
        )
        assert result["suggested_name"].startswith("Mixed list ")
        assert result["suggested_image"] is None

    @patch("app.parser.get_playlist_tracks")
    def test_single_playlist_with_comments_still_single(self, mock_playlist):
        # Comments and blank lines are not meaningful — a playlist URL
        # surrounded by comments is still "single source".
        mock_playlist.return_value = {
            "name": "Cozy",
            "image": "https://img/cozy.jpg",
            "total": 0,
            "tracks": [],
        }
        result = parse_input(
            "# my playlist\n"
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M\n"
            "\n"
        )
        assert result["suggested_name"] == "Cozy"

    def test_empty_input_produces_mixed_list_name(self):
        result = parse_input("")
        assert result["suggested_name"].startswith("Mixed list ")


# ---------------------------------------------------------------------------
# raw_text preservation
# ---------------------------------------------------------------------------


class TestRawText:
    def test_raw_text_preserved_exactly(self):
        text = "Radiohead - Creep\n# comment\n\nMiles Davis — So What"
        result = parse_input(text)
        assert result["raw_text"] == text
