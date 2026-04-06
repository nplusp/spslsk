"""Tests for the candidate-fallback loop in _search_and_download_track.

Mocks SlskdClient methods to simulate flaky-peer scenarios. Verifies that
when a download attempt fails, the next-best candidate is tried, up to 3
candidates total per track. This is the Unit 4 fix from the search-quality
plan: production logs showed real downloads getting `error` status because
the chosen peer dropped connection mid-transfer, with no retry on the next
ranked candidate.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.downloader import _search_and_download_track, TrackStatus


def _make_response(username: str, filename: str) -> dict:
    """Build a slskd-shaped response with one peer and one audio file.

    The filename intentionally contains 'karma' so the current
    `_matches_track` (which requires >=1 significant title word in the
    filename) accepts it for the canonical Radiohead - Karma Police track.
    """
    return {
        "username": username,
        "freeUploadSlots": 1,
        "files": [
            {
                "filename": filename,
                "size": 1000,
                "bitRate": 0,
            }
        ],
    }


def _make_client(responses: list, download_outcomes: list) -> MagicMock:
    """Build a mocked SlskdClient.

    responses: list of slskd response dicts (one per peer)
    download_outcomes: list of side effects for download_file calls.
        Exception instances are raised; anything else is returned as the result.
    """
    client = MagicMock()
    client.health_check = AsyncMock(return_value=True)
    client.search = AsyncMock(return_value="search-id")
    client.delete_search = AsyncMock(return_value=None)
    client.wait_for_search = AsyncMock(return_value=responses)

    side_effects = [
        e if isinstance(e, BaseException) else (e or {"ok": True})
        for e in download_outcomes
    ]
    client.download_file = AsyncMock(side_effect=side_effects)

    return client


def _make_track(artist: str = "Radiohead", title: str = "Karma Police") -> TrackStatus:
    return TrackStatus(artist=artist, title=title, track_id="")


# Three peers, each offering one FLAC of the requested track. Stable
# ordering: peer1 first, then peer2, then peer3 (all share format / size /
# free_slots so the sort is stable on insertion order).
THREE_PEER_RESPONSES = [
    _make_response("peer1", "Radiohead - Karma A.flac"),
    _make_response("peer2", "Radiohead - Karma B.flac"),
    _make_response("peer3", "Radiohead - Karma C.flac"),
]


class TestCandidateFallback:
    @pytest.mark.asyncio
    async def test_first_candidate_succeeds(self):
        client = _make_client(THREE_PEER_RESPONSES, [{"ok": True}])
        track = _make_track()
        await _search_and_download_track(client, track)
        assert track.status == "completed"
        assert client.download_file.call_count == 1

    @pytest.mark.asyncio
    async def test_second_candidate_succeeds_after_first_fails(self):
        client = _make_client(
            THREE_PEER_RESPONSES,
            [RuntimeError("Download failed: Errored, Completed"), {"ok": True}],
        )
        track = _make_track()
        await _search_and_download_track(client, track)
        assert track.status == "completed"
        assert client.download_file.call_count == 2
        # The successful filename should reflect the SECOND candidate, not the
        # first (status was overwritten as we moved through attempts).
        assert "B" in track.filename and "A" not in track.filename

    @pytest.mark.asyncio
    async def test_third_candidate_succeeds_after_two_failures(self):
        client = _make_client(
            THREE_PEER_RESPONSES,
            [
                RuntimeError("peer1 dropped"),
                RuntimeError("peer2 dropped"),
                {"ok": True},
            ],
        )
        track = _make_track()
        await _search_and_download_track(client, track)
        assert track.status == "completed"
        assert client.download_file.call_count == 3
        assert "C" in track.filename

    @pytest.mark.asyncio
    async def test_all_three_candidates_fail(self):
        client = _make_client(
            THREE_PEER_RESPONSES,
            [
                RuntimeError("peer1 dropped"),
                RuntimeError("peer2 dropped"),
                RuntimeError("peer3 dropped"),
            ],
        )
        track = _make_track()
        await _search_and_download_track(client, track)
        assert track.status == "error"
        assert client.download_file.call_count == 3
        # Error message should reference all 3 attempts and the last error.
        assert "3" in track.error
        assert "peer3" in track.error

    @pytest.mark.asyncio
    async def test_caps_at_three_when_more_candidates_exist(self):
        # 5 peers, all fail. Loop must attempt only 3 — never reach peer4/peer5.
        five_peers = [
            _make_response(f"peer{i}", f"Radiohead - Karma {i}.flac")
            for i in range(5)
        ]
        client = _make_client(
            five_peers,
            [RuntimeError(f"peer{i} dropped") for i in range(5)],
        )
        track = _make_track()
        await _search_and_download_track(client, track)
        assert track.status == "error"
        assert client.download_file.call_count == 3  # not 5

    @pytest.mark.asyncio
    async def test_only_two_candidates_both_fail(self):
        two_peers = [
            _make_response("peer1", "Radiohead - Karma A.flac"),
            _make_response("peer2", "Radiohead - Karma B.flac"),
        ]
        client = _make_client(
            two_peers,
            [RuntimeError("p1"), RuntimeError("p2")],
        )
        track = _make_track()
        await _search_and_download_track(client, track)
        assert track.status == "error"
        assert client.download_file.call_count == 2  # not 3 — only 2 candidates exist
        assert "2" in track.error

    @pytest.mark.asyncio
    async def test_only_one_candidate_fails(self):
        one_peer = [_make_response("peer1", "Radiohead - Karma A.flac")]
        client = _make_client(one_peer, [RuntimeError("only one")])
        track = _make_track()
        await _search_and_download_track(client, track)
        assert track.status == "error"
        assert client.download_file.call_count == 1

    @pytest.mark.asyncio
    async def test_generic_exception_also_triggers_fallback(self):
        # Not a RuntimeError — a network/httpx-style exception. The catch
        # must be broad enough to handle any exception from download_file.
        client = _make_client(
            THREE_PEER_RESPONSES,
            [ConnectionError("network died"), {"ok": True}],
        )
        track = _make_track()
        await _search_and_download_track(client, track)
        assert track.status == "completed"
        assert client.download_file.call_count == 2

    @pytest.mark.asyncio
    async def test_no_candidates_does_not_enter_loop(self):
        # Empty results — should set not_found, not error. Pre-existing
        # behavior must be preserved (the not_found guard fires before the
        # candidate loop is reached).
        client = _make_client([], [])
        track = _make_track()
        await _search_and_download_track(client, track)
        assert track.status == "not_found"
        assert client.download_file.call_count == 0

    @pytest.mark.asyncio
    async def test_quality_field_reflects_winning_candidate(self):
        # When fallback succeeds on attempt N, the quality field should
        # describe the file from attempt N, not from attempt 0. This matters
        # for the UI — users see what actually downloaded.
        client = _make_client(
            THREE_PEER_RESPONSES,
            [RuntimeError("p1"), {"ok": True}],
        )
        track = _make_track()
        await _search_and_download_track(client, track)
        assert track.status == "completed"
        # All three peer files are FLAC so quality is "FLAC (lossless)" either way,
        # but the filename uniquely identifies which candidate won.
        assert track.quality  # non-empty
        assert "B" in track.filename
