"""Tests for rename-on-move feature.

Unit 1 covers pure filename-building helpers:
- _parse_title_and_suffix: extracts remix/edit/version suffix from a Spotify title
- _build_target_filename: assembles canonical `{Artist} - {Title (Suffix)}.ext`

Unit 2 covers the rewritten _move_to_playlist_folder and the _download_one_track
integration (manifest sync, cleanup of empty source parent).
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.downloader as downloader_module
from app.downloader import (
    TrackStatus,
    _build_target_filename,
    _parse_title_and_suffix,
    _move_to_playlist_folder,
)


# ---------------------------------------------------------------------------
# Unit 1: _parse_title_and_suffix
# ---------------------------------------------------------------------------


class TestParseTitleAndSuffix:
    def test_remix_keyword_splits_suffix(self):
        base, suffix = _parse_title_and_suffix("Rub - Paula Temple Remix")
        assert base == "Rub"
        assert suffix == "Paula Temple Remix"

    def test_no_dash_returns_title_as_is(self):
        base, suffix = _parse_title_and_suffix("Gym Tonic")
        assert base == "Gym Tonic"
        assert suffix is None

    def test_no_keyword_match_returns_title_as_is(self):
        # "Black - White" has no keyword — must not be split
        base, suffix = _parse_title_and_suffix("Black - White")
        assert base == "Black - White"
        assert suffix is None

    def test_year_suffix_detected_as_live_version(self):
        # "Alive 2007" has no keyword in isolation, but the 4-digit year
        # reliably signals a version/live marker
        base, suffix = _parse_title_and_suffix("Harder Better Faster - Alive 2007")
        assert base == "Harder Better Faster"
        assert suffix == "Alive 2007"

    def test_last_dash_wins_when_title_contains_dash(self):
        # "A - B - Club Mix" → split on last " - ", keyword "mix" matches
        base, suffix = _parse_title_and_suffix("A - B - Club Mix")
        assert base == "A - B"
        assert suffix == "Club Mix"

    def test_empty_title_returns_empty(self):
        base, suffix = _parse_title_and_suffix("")
        assert base == ""
        assert suffix is None

    @pytest.mark.parametrize(
        "title,expected_suffix",
        [
            ("Song - Radio Edit", "Radio Edit"),
            ("Song - Extended Mix", "Extended Mix"),
            ("Song - Club Version", "Club Version"),
            ("Song - 2013 Remaster", "2013 Remaster"),
            ("Song - Remastered", "Remastered"),
            ("Song - Live", "Live"),
            ("Song - Acoustic", "Acoustic"),
            ("Song - Instrumental", "Instrumental"),
            ("Song - Dub Mix", "Dub Mix"),
            ("Song - VIP Mix", "VIP Mix"),
            ("Song - Bootleg", "Bootleg"),
            ("Song - Demo", "Demo"),
            ("Song - Unplugged", "Unplugged"),
            ("Song - Mashup", "Mashup"),
            ("Song - Rework", "Rework"),
            # Hyphenated keywords — previously lived in a separate
            # _REMIX_MULTIWORD_MARKERS tuple, now folded into the main set
            # since token regex `[^\w-]` preserves hyphens.
            ("Song - Re-edit", "Re-edit"),
            ("Song - Re-rub", "Re-rub"),
            ("Song - Re-work", "Re-work"),
            # Leftover suffix variants that need to survive tokenisation
            ("Song - Club", "Club"),
            ("Song - Cut", "Cut"),
            ("Song - Flip", "Flip"),
            ("Song - Interlude", "Interlude"),
        ],
    )
    def test_keyword_coverage(self, title, expected_suffix):
        base, suffix = _parse_title_and_suffix(title)
        assert base == "Song"
        assert suffix == expected_suffix

    def test_leading_dash_stripped(self):
        # " - Song" should NOT produce orphan "- - Song" style filenames
        base, suffix = _parse_title_and_suffix(" - Song")
        assert base == "Song"
        assert suffix is None

    def test_trailing_dash_stripped(self):
        base, suffix = _parse_title_and_suffix("Song - ")
        assert base == "Song"
        assert suffix is None

    def test_bare_dash_returns_empty_or_original(self):
        base, suffix = _parse_title_and_suffix(" - ")
        # After stripping, cleaned is empty → fall back to original
        assert suffix is None
        # Result must not contain a surviving dash that would leak into FS


# ---------------------------------------------------------------------------
# Unit 1: _build_target_filename
# ---------------------------------------------------------------------------


class TestBuildTargetFilename:
    def test_happy_path_with_remix(self):
        result = _build_target_filename(
            "Peaches, Paula Temple",
            "Rub - Paula Temple Remix",
            "02 - rub.mp3",
        )
        assert result == "Peaches, Paula Temple - Rub (Paula Temple Remix).mp3"

    def test_happy_path_no_remix(self):
        result = _build_target_filename("Bob Sinclar", "Gym Tonic", "Gym_Tonic.FLAC")
        # Extension lowercased
        assert result == "Bob Sinclar - Gym Tonic.flac"

    def test_year_based_live_suffix(self):
        result = _build_target_filename(
            "Daft Punk", "Harder Better Faster - Alive 2007", "x.flac"
        )
        assert result == "Daft Punk - Harder Better Faster (Alive 2007).flac"

    def test_false_positive_guard(self):
        # "Black - White" has no keyword → no wrapping, title preserved
        result = _build_target_filename("Artist", "Black - White", "x.mp3")
        assert result == "Artist - Black - White.mp3"

    def test_title_with_embedded_dash_before_suffix(self):
        result = _build_target_filename("X", "A - B - Club Mix", "y.mp3")
        assert result == "X - A - B (Club Mix).mp3"

    def test_multi_artist_verbatim(self):
        result = _build_target_filename("A, B, C", "Song", "z.mp3")
        assert result == "A, B, C - Song.mp3"

    def test_filesystem_unsafe_characters_stripped(self):
        # / and ? are stripped by _sanitize_dirname
        result = _build_target_filename("Artist/Name", "Title?", "x.mp3")
        assert result == "ArtistName - Title.mp3"

    def test_missing_extension(self):
        result = _build_target_filename("A", "B", "file_with_no_ext")
        assert result == "A - B"

    def test_extension_case_normalized(self):
        result = _build_target_filename("A", "B", "x.MP3")
        assert result == "A - B.mp3"

    def test_empty_artist_falls_back_to_original(self):
        result = _build_target_filename("", "Title", "orig.mp3")
        assert result == "orig.mp3"

    def test_empty_title_falls_back_to_original(self):
        result = _build_target_filename("Artist", "", "orig.mp3")
        assert result == "orig.mp3"

    def test_whitespace_only_artist_falls_back(self):
        result = _build_target_filename("   ", "Title", "orig.mp3")
        assert result == "orig.mp3"

    def test_square_brackets_stripped(self):
        # Square brackets are legal on disk but break pathlib.rglob
        # (fnmatch character class). _sanitize_dirname strips them so the
        # manifest round-trip (canonical name → rglob → match) stays sound.
        result = _build_target_filename(
            "Artist", "Song [Skit]", "orig.mp3"
        )
        assert "[" not in result
        assert "]" not in result
        assert result == "Artist - Song Skit.mp3"

    def test_leading_dash_in_title(self):
        result = _build_target_filename("Artist", " - Song", "x.mp3")
        # Must not contain an orphan " - - " artifact
        assert " - - " not in result
        assert result == "Artist - Song.mp3"

    def test_trailing_dash_in_title(self):
        result = _build_target_filename("Artist", "Song - ", "x.mp3")
        assert " - - " not in result
        # Sanitizer does not strip trailing "-" but the parser's rpartition
        # falls through cleanly — the final result is "Artist - Song.mp3"
        assert result == "Artist - Song.mp3"


# ---------------------------------------------------------------------------
# Unit 2: _move_to_playlist_folder
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_downloads(tmp_path, monkeypatch):
    """Redirect DOWNLOADS_DIR to a tmp path for filesystem tests."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(downloader_module, "DOWNLOADS_DIR", downloads)
    monkeypatch.setattr(
        downloader_module, "MANIFEST_FILE", downloads / ".manifest.json"
    )
    return downloads


class TestMoveToPlaylistFolder:
    def test_rename_move_and_cleanup_happy_path(self, tmp_downloads):
        # File dropped by slskd under a peer-named folder
        peer_dir = tmp_downloads / "peer_folder"
        peer_dir.mkdir()
        src = peer_dir / "02 - rub.mp3"
        src.write_bytes(b"audio")

        new_name = _move_to_playlist_folder(
            "02 - rub.mp3",
            "My Playlist",
            "Peaches, Paula Temple",
            "Rub - Paula Temple Remix",
        )

        assert new_name == "Peaches, Paula Temple - Rub (Paula Temple Remix).mp3"
        target = tmp_downloads / "My Playlist" / new_name
        assert target.exists()
        assert target.read_bytes() == b"audio"
        # Source parent was empty after move → cleaned up
        assert not peer_dir.exists()

    def test_playlist_folder_created_if_missing(self, tmp_downloads):
        peer_dir = tmp_downloads / "peer"
        peer_dir.mkdir()
        (peer_dir / "a.mp3").write_bytes(b"x")

        _move_to_playlist_folder("a.mp3", "New Playlist", "Artist", "Song")

        assert (tmp_downloads / "New Playlist").is_dir()
        assert (tmp_downloads / "New Playlist" / "Artist - Song.mp3").exists()

    def test_source_parent_non_empty_not_removed(self, tmp_downloads):
        peer_dir = tmp_downloads / "peer"
        peer_dir.mkdir()
        (peer_dir / "a.mp3").write_bytes(b"x")
        (peer_dir / "cover.jpg").write_bytes(b"img")  # unrelated sibling

        _move_to_playlist_folder("a.mp3", "Playlist", "Artist", "Song")

        assert peer_dir.exists()
        assert (peer_dir / "cover.jpg").exists()

    def test_source_parent_is_downloads_root_not_removed(self, tmp_downloads):
        # File dropped directly into downloads root
        (tmp_downloads / "a.mp3").write_bytes(b"x")

        _move_to_playlist_folder("a.mp3", "Playlist", "Artist", "Song")

        # DOWNLOADS_DIR must still exist
        assert tmp_downloads.exists()
        assert (tmp_downloads / "Playlist" / "Artist - Song.mp3").exists()

    def test_target_file_already_exists_skips(self, tmp_downloads):
        peer_dir = tmp_downloads / "peer"
        peer_dir.mkdir()
        src = peer_dir / "a.mp3"
        src.write_bytes(b"new")

        playlist_dir = tmp_downloads / "Playlist"
        playlist_dir.mkdir()
        existing = playlist_dir / "Artist - Song.mp3"
        existing.write_bytes(b"old")

        result = _move_to_playlist_folder("a.mp3", "Playlist", "Artist", "Song")

        # Returned the ORIGINAL filename on conflict
        assert result == "a.mp3"
        # Source still there
        assert src.exists()
        # Existing target untouched
        assert existing.read_bytes() == b"old"
        # Source parent NOT cleaned up on conflict
        assert peer_dir.exists()

    def test_shutil_move_oserror_preserves_state(self, tmp_downloads, monkeypatch):
        peer_dir = tmp_downloads / "peer"
        peer_dir.mkdir()
        (peer_dir / "a.mp3").write_bytes(b"x")

        import shutil

        def boom(*args, **kwargs):
            raise OSError("mock failure")

        monkeypatch.setattr(shutil, "move", boom)

        result = _move_to_playlist_folder("a.mp3", "Playlist", "Artist", "Song")

        # Original name returned on OSError
        assert result == "a.mp3"
        # Source file still there
        assert (peer_dir / "a.mp3").exists()

    def test_file_not_found_returns_original_name(self, tmp_downloads):
        # No file exists under DOWNLOADS_DIR
        result = _move_to_playlist_folder(
            "missing.mp3", "Playlist", "Artist", "Song"
        )
        assert result == "missing.mp3"


# ---------------------------------------------------------------------------
# Unit 2: Integration — _download_one_track updates filename + manifest
# ---------------------------------------------------------------------------


def _make_track_status(filename: str = "02 - rub.mp3") -> TrackStatus:
    t = TrackStatus(
        artist="Peaches, Paula Temple",
        title="Rub - Paula Temple Remix",
        track_id="spotify:track:xyz",
    )
    t.status = "queued"
    t.candidates = [
        {
            "username": "peer1",
            "file": {
                "filename": f"peer_folder\\{filename}",
                "size": 1000,
                "bitRate": 320,
            },
        }
    ]
    return t


@pytest.mark.asyncio
class TestDownloadOneTrackIntegration:
    async def test_success_updates_filename_and_manifest(self, tmp_downloads):
        """Full success path: filename is rewritten to canonical form AND
        the manifest stores the new name, so a subsequent
        _is_already_downloaded call finds it."""
        from app.downloader import _download_one_track, _is_already_downloaded

        # Pre-create the "downloaded" file as if slskd dropped it
        peer_dir = tmp_downloads / "peer_folder"
        peer_dir.mkdir()
        src = peer_dir / "02 - rub.mp3"
        src.write_bytes(b"audio")

        track = _make_track_status()
        client = MagicMock()
        client.download_file = AsyncMock(return_value=None)

        await _download_one_track(client, track, playlist_name="My Playlist")

        assert track.status == "completed"
        assert track.filename == (
            "Peaches, Paula Temple - Rub (Paula Temple Remix).mp3"
        )
        # Manifest was written with the NEW filename
        manifest_path = tmp_downloads / ".manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["spotify:track:xyz"]["filename"] == track.filename
        # _is_already_downloaded finds the renamed file
        assert _is_already_downloaded("spotify:track:xyz") == track.filename

    async def test_bracket_title_manifest_round_trip(self, tmp_downloads):
        """A title containing square brackets must survive the full
        rename → record → _is_already_downloaded cycle. Brackets in the
        canonical filename would otherwise make rglob interpret them as
        a character class and miss the file on the next run."""
        from app.downloader import _download_one_track, _is_already_downloaded

        peer_dir = tmp_downloads / "peer"
        peer_dir.mkdir()
        (peer_dir / "weird.mp3").write_bytes(b"audio")

        track = TrackStatus(
            artist="Artist",
            title="Song [Skit]",
            track_id="spotify:track:bracket",
        )
        track.status = "queued"
        track.candidates = [
            {
                "username": "peer1",
                "file": {
                    "filename": "peer\\weird.mp3",
                    "size": 1000,
                    "bitRate": 320,
                },
            }
        ]
        client = MagicMock()
        client.download_file = AsyncMock(return_value=None)

        await _download_one_track(client, track, playlist_name="Playlist")

        assert track.status == "completed"
        # Brackets are stripped from the canonical name
        assert "[" not in track.filename
        assert "]" not in track.filename
        # Critical: next-run detection finds the file via rglob
        assert _is_already_downloaded("spotify:track:bracket") == track.filename

    async def test_rename_conflict_still_records_manifest(self, tmp_downloads):
        """On rename conflict the original filename stays on disk and must
        still be recorded in the manifest so re-runs work."""
        from app.downloader import _download_one_track, _is_already_downloaded

        peer_dir = tmp_downloads / "peer_folder"
        peer_dir.mkdir()
        (peer_dir / "02 - rub.mp3").write_bytes(b"new")

        # Pre-create the target canonical name to force a conflict
        playlist_dir = tmp_downloads / "My Playlist"
        playlist_dir.mkdir()
        (
            playlist_dir / "Peaches, Paula Temple - Rub (Paula Temple Remix).mp3"
        ).write_bytes(b"old")

        track = _make_track_status()
        client = MagicMock()
        client.download_file = AsyncMock(return_value=None)

        await _download_one_track(client, track, playlist_name="My Playlist")

        assert track.status == "completed"
        # filename fell back to original
        assert track.filename == "02 - rub.mp3"
        # manifest records whatever is on disk → original
        manifest = json.loads((tmp_downloads / ".manifest.json").read_text())
        assert manifest["spotify:track:xyz"]["filename"] == "02 - rub.mp3"
        # rglob finds it under peer_folder
        assert _is_already_downloaded("spotify:track:xyz") == "02 - rub.mp3"
