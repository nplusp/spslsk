"""Tests for the FastAPI endpoints affected by the unified-input feature.

Uses FastAPI's TestClient so tests exercise the real request/response
pipeline including pydantic validation. The parser and process_playlist
are patched at the module where ``app.main`` imported them, not at their
definition sites, to respect Python's import semantics.
"""
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


# ---------------------------------------------------------------------------
# POST /api/parse-input
# ---------------------------------------------------------------------------


class TestParseInputEndpoint:
    @patch("app.main.parse_input")
    def test_valid_text_returns_parsed_input(self, mock_parse):
        mock_parse.return_value = {
            "tracks": [
                {
                    "id": "manual:abc",
                    "artist": "Radiohead",
                    "title": "Creep",
                    "album": "",
                    "duration_ms": 0,
                    "source": "text",
                    "state": "ready",
                    "error": None,
                    "raw_line": "Radiohead - Creep",
                }
            ],
            "suggested_name": "Mixed list 2026-04-06 17:30",
            "suggested_image": None,
            "raw_text": "Radiohead - Creep",
        }

        resp = client.post("/api/parse-input", json={"text": "Radiohead - Creep"})

        assert resp.status_code == 200
        data = resp.json()
        assert "tracks" in data
        assert "suggested_name" in data
        assert "suggested_image" in data
        assert "raw_text" in data
        assert len(data["tracks"]) == 1
        assert data["tracks"][0]["artist"] == "Radiohead"
        mock_parse.assert_called_once_with("Radiohead - Creep")

    @patch("app.main.parse_input")
    def test_empty_text_is_not_an_error(self, mock_parse):
        mock_parse.return_value = {
            "tracks": [],
            "suggested_name": "Mixed list 2026-04-06 17:30",
            "suggested_image": None,
            "raw_text": "",
        }
        resp = client.post("/api/parse-input", json={"text": ""})
        assert resp.status_code == 200
        assert resp.json()["tracks"] == []

    def test_missing_text_field_is_rejected(self):
        resp = client.post("/api/parse-input", json={})
        assert resp.status_code == 422

    @patch("app.main.parse_input")
    def test_unexpected_parser_exception_returns_500(self, mock_parse):
        mock_parse.side_effect = RuntimeError("internal explosion")
        resp = client.post("/api/parse-input", json={"text": "whatever"})
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /api/download (new contract)
# ---------------------------------------------------------------------------


class TestDownloadEndpoint:
    @patch("app.main.asyncio.create_task")
    @patch("app.main.process_playlist")
    def test_starts_download_with_ready_tracks(self, mock_process, mock_create_task):
        # Mock process_playlist as a regular callable since we also mock
        # create_task — no real coroutine runs.
        tracks = [
            {
                "id": "manual:abc",
                "artist": "Radiohead",
                "title": "Creep",
                "album": "",
                "duration_ms": 0,
                "source": "text",
                "state": "ready",
                "error": None,
                "raw_line": "Radiohead - Creep",
            }
        ]
        resp = client.post(
            "/api/download",
            json={
                "tracks": tracks,
                "name": "My Mix",
                "raw_text": "Radiohead - Creep",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        # create_task was called, meaning the endpoint scheduled the work
        assert mock_create_task.called

    @patch("app.main.asyncio.create_task")
    @patch("app.main.process_playlist")
    def test_filters_out_non_ready_rows(self, mock_process, mock_create_task):
        tracks = [
            {
                "id": "manual:ok",
                "artist": "A",
                "title": "T1",
                "album": "",
                "duration_ms": 0,
                "source": "text",
                "state": "ready",
                "error": None,
                "raw_line": "A - T1",
            },
            {
                "id": "",
                "artist": "",
                "title": "",
                "album": "",
                "duration_ms": 0,
                "source": "text",
                "state": "needs_review",
                "error": "No separator",
                "raw_line": "bad",
            },
            {
                "id": "manual:done",
                "artist": "B",
                "title": "T2",
                "album": "",
                "duration_ms": 0,
                "source": "text",
                "state": "already_downloaded",
                "error": None,
                "raw_line": "B - T2",
            },
        ]
        resp = client.post(
            "/api/download",
            json={"tracks": tracks, "name": "Mix", "raw_text": "raw"},
        )
        assert resp.status_code == 200
        # Only the single 'ready' track is forwarded for download.
        data = resp.json()
        assert data["total"] == 1

    @patch("app.main.asyncio.create_task")
    @patch("app.main.process_playlist")
    def test_empty_ready_list_returns_zero_total(self, mock_process, mock_create_task):
        resp = client.post(
            "/api/download",
            json={"tracks": [], "name": "Empty", "raw_text": ""},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_missing_name_is_rejected(self):
        resp = client.post(
            "/api/download",
            json={"tracks": [], "raw_text": ""},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/playlist removal
# ---------------------------------------------------------------------------


class TestRemovedPlaylistEndpoint:
    def test_old_playlist_endpoint_is_gone(self):
        resp = client.post(
            "/api/playlist",
            json={"url": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"},
        )
        # 404 (route removed) or 405 (method not allowed because path is gone).
        assert resp.status_code in (404, 405)
