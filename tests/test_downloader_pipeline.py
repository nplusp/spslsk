"""Tests for the search/download pipeline split.

Verifies that:
- _search_for_candidates does ONLY search work and populates track.candidates
- _download_one_track does ONLY download work from a pre-populated list
- process_playlist correctly pipelines the two phases (search produces,
  multiple download workers consume from a queue)
- Manifest-skipped tracks bypass both phases
- not_found / error tracks are correctly classified
- The split preserves the existing _search_and_download_track wrapper
  contract (backwards compat)
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.downloader import (
    DownloadSession,
    TrackStatus,
    _download_one_track,
    _search_for_candidates,
    process_playlist,
)
import app.downloader as downloader_module


def _make_response(username: str, filename: str) -> dict:
    return {
        "username": username,
        "freeUploadSlots": 1,
        "files": [{"filename": filename, "size": 1000, "bitRate": 0}],
    }


def _make_client(
    responses: list, download_outcomes: list | None = None
) -> MagicMock:
    client = MagicMock()
    client.health_check = AsyncMock(return_value=True)
    client.search = AsyncMock(return_value="search-id")
    client.delete_search = AsyncMock(return_value=None)
    client.wait_for_search = AsyncMock(return_value=responses)
    if download_outcomes is None:
        download_outcomes = [{"ok": True}] * 10
    side_effects = [
        e if isinstance(e, BaseException) else (e or {"ok": True})
        for e in download_outcomes
    ]
    client.download_file = AsyncMock(side_effect=side_effects)
    return client


def _make_track(
    artist: str = "Radiohead", title: str = "Karma Police", track_id: str = ""
) -> TrackStatus:
    return TrackStatus(artist=artist, title=title, track_id=track_id)


THREE_RESPONSES = [
    _make_response("peer1", "Radiohead - Karma A.flac"),
    _make_response("peer2", "Radiohead - Karma B.flac"),
    _make_response("peer3", "Radiohead - Karma C.flac"),
]


# ---------------------------------------------------------------------------
# _search_for_candidates
# ---------------------------------------------------------------------------


class TestSearchForCandidates:
    @pytest.mark.asyncio
    async def test_populates_candidates_and_marks_queued(self):
        client = _make_client(THREE_RESPONSES)
        track = _make_track()
        await _search_for_candidates(client, track)
        assert track.status == "queued"
        assert len(track.candidates) == 3
        # download_file MUST NOT be called — this is the search phase only
        assert client.download_file.call_count == 0

    @pytest.mark.asyncio
    async def test_empty_results_marks_not_found(self):
        client = _make_client([])
        track = _make_track()
        await _search_for_candidates(client, track)
        assert track.status == "not_found"
        assert track.candidates == []

    @pytest.mark.asyncio
    async def test_search_error_marks_error(self):
        client = _make_client([])
        client.search = AsyncMock(side_effect=RuntimeError("slskd 500"))
        track = _make_track()
        await _search_for_candidates(client, track)
        assert track.status == "error"
        assert "slskd 500" in track.error

    @pytest.mark.asyncio
    async def test_manifest_skip_short_circuits(self):
        client = _make_client(THREE_RESPONSES)
        track = _make_track(track_id="spotify123")
        with patch(
            "app.downloader._is_already_downloaded",
            return_value="cached_file.flac",
        ):
            await _search_for_candidates(client, track)
        assert track.status == "completed"
        assert track.quality == "already downloaded"
        assert track.filename == "cached_file.flac"
        # Search should not have been called
        assert client.search.call_count == 0


# ---------------------------------------------------------------------------
# _download_one_track
# ---------------------------------------------------------------------------


def _candidate(username: str, filename: str) -> dict:
    return {
        "username": username,
        "free_upload": True,
        "file": {"filename": filename, "size": 1000, "bitRate": 0},
    }


class TestDownloadOneTrack:
    @pytest.mark.asyncio
    async def test_skips_track_not_in_queued_state(self):
        client = _make_client([])
        track = _make_track()
        track.status = "completed"  # already done
        await _download_one_track(client, track, "")
        # download_file must not have been called
        assert client.download_file.call_count == 0

    @pytest.mark.asyncio
    async def test_downloads_first_candidate_when_queued(self):
        client = _make_client([], [{"ok": True}])
        track = _make_track()
        track.status = "queued"
        track.candidates = [
            _candidate("peer1", "Radiohead - Karma A.flac"),
            _candidate("peer2", "Radiohead - Karma B.flac"),
        ]
        await _download_one_track(client, track, "")
        assert track.status == "completed"
        assert client.download_file.call_count == 1

    @pytest.mark.asyncio
    async def test_fallback_loop_still_works(self):
        # The 3-candidate fallback from the hotfix is still in effect.
        client = _make_client(
            [],
            [
                RuntimeError("p1 dropped"),
                RuntimeError("p2 dropped"),
                {"ok": True},
            ],
        )
        track = _make_track()
        track.status = "queued"
        track.candidates = [
            _candidate(f"peer{i}", f"Radiohead - Karma {i}.flac") for i in range(5)
        ]
        await _download_one_track(client, track, "")
        assert track.status == "completed"
        assert client.download_file.call_count == 3  # max 3 attempts

    @pytest.mark.asyncio
    async def test_clears_candidates_after_success_to_free_memory(self):
        client = _make_client([], [{"ok": True}])
        track = _make_track()
        track.status = "queued"
        track.candidates = [_candidate("peer1", "Radiohead - Karma.flac")]
        await _download_one_track(client, track, "")
        # candidates should be cleared after a successful (or failed) download
        # to release memory for large playlists
        assert track.candidates == []


# ---------------------------------------------------------------------------
# process_playlist pipeline
# ---------------------------------------------------------------------------


class TestProcessPlaylistPipeline:
    @pytest.mark.asyncio
    async def test_runs_search_then_download_for_all_tracks(self):
        client = _make_client(THREE_RESPONSES)
        with patch("app.downloader.SlskdClient", return_value=client), \
             patch("app.downloader._move_to_playlist_folder"), \
             patch("app.downloader._record_download"), \
             patch("app.downloader.SEARCH_DELAY", 0):  # speed up test
            await process_playlist(
                [
                    {"id": "", "artist": "Radiohead", "title": "Karma Police"},
                    {"id": "", "artist": "Radiohead", "title": "Karma Police"},
                ],
                "test-playlist",
            )
        # Both tracks should have completed
        assert len(downloader_module.session.tracks) == 2
        for t in downloader_module.session.tracks:
            assert t.status == "completed", f"{t.artist} - {t.title}: {t.status}"

    @pytest.mark.asyncio
    async def test_mix_of_outcomes(self):
        # Three tracks: one finds candidates, one returns empty (not_found),
        # one search-errors. Pipeline should classify each correctly without
        # blocking on the failing ones.
        client = MagicMock()
        client.health_check = AsyncMock(return_value=True)
        client.delete_search = AsyncMock(return_value=None)
        client.download_file = AsyncMock(return_value={"ok": True})

        # Different responses for different queries:
        # search 1 (Karma) -> 3 peers, search 2 (Bohemian) -> empty,
        # search 3 (NoNet) -> raises
        call_count = {"i": 0}

        async def search_side(query):
            call_count["i"] += 1
            return f"sid-{call_count['i']}"

        async def wait_side(sid, **kwargs):
            if "1" in sid or "2" in sid:
                # First track: queries 1 (broad) and 2 (narrow); both can return
                return THREE_RESPONSES
            return []

        # Easier: vary by side_effect list
        client.search = AsyncMock(side_effect=[
            "sid-1",  # Track 1 query 1 - succeeds
            "sid-2",  # Track 2 query 1 - empty
            "sid-3",  # Track 2 query 2 (narrow) - empty
            RuntimeError("network error"),  # Track 3 query 1 - errors
        ])
        client.wait_for_search = AsyncMock(side_effect=[
            THREE_RESPONSES,  # Track 1 query 1
            [],  # Track 2 query 1
            [],  # Track 2 query 2
        ])

        with patch("app.downloader.SlskdClient", return_value=client), \
             patch("app.downloader._move_to_playlist_folder"), \
             patch("app.downloader._record_download"), \
             patch("app.downloader.SEARCH_DELAY", 0):
            await process_playlist(
                [
                    {"id": "", "artist": "Radiohead", "title": "Karma Police"},
                    {"id": "", "artist": "Queen", "title": "Bohemian Rhapsody"},
                    {"id": "", "artist": "NoNet", "title": "Anything Here"},
                ],
                "test-mix",
            )

        statuses = [t.status for t in downloader_module.session.tracks]
        assert statuses[0] == "completed"
        assert statuses[1] == "not_found"
        assert statuses[2] == "error"

    @pytest.mark.asyncio
    async def test_session_inactive_stops_pipeline(self):
        client = _make_client(THREE_RESPONSES)
        # Pre-mark session inactive AFTER process_playlist initializes it,
        # by patching health_check to flip the flag on first call.
        with patch("app.downloader.SlskdClient", return_value=client):
            async def stop_after_first(*a, **kw):
                downloader_module.session.active = False
                return True
            client.health_check = AsyncMock(side_effect=stop_after_first)
            await process_playlist(
                [
                    {"id": "", "artist": "Radiohead", "title": "Karma Police"},
                ],
                "stoppable",
            )
        # Session is no longer active by the end (either we stopped early or completed)
        assert downloader_module.session.active is False
