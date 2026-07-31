"""Tests for the folder/path discovery endpoints behind the "where did my
files go?" UX.

``app.main`` imports DOWNLOADS_DIR and HOST_DOWNLOADS_DIR by value at import
time, so tests patch them on ``app.main`` (the binding the endpoints actually
read) rather than at their definition sites.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    """A realistic downloads tree: two list folders plus a loose root file."""
    root = tmp_path / "downloads"
    (root / "My Playlist").mkdir(parents=True)
    (root / "Another List").mkdir(parents=True)

    (root / "My Playlist" / "Artist - Song.flac").write_bytes(b"x" * 2048)
    (root / "My Playlist" / "Artist - Other.mp3").write_bytes(b"y" * 1024)
    (root / "Another List" / "Someone - Track.mp3").write_bytes(b"z" * 512)
    (root / "loose.mp3").write_bytes(b"w" * 256)
    # Hidden files (the manifest lives here) must never surface in the UI.
    (root / ".manifest.json").write_text("{}")

    monkeypatch.setattr("app.main.DOWNLOADS_DIR", root)
    monkeypatch.setattr("app.main.HOST_DOWNLOADS_DIR", "/host/spslsk/downloads")
    return root


# ---------------------------------------------------------------------------
# GET /api/paths
# ---------------------------------------------------------------------------


class TestPathsEndpoint:
    def test_reports_host_path_when_known(self, downloads):
        data = client.get("/api/paths").json()

        assert data["host_downloads"] == "/host/spslsk/downloads"
        assert data["is_host_path_known"] is True
        assert data["helper_url"].startswith("http://")

    def test_falls_back_to_container_path_when_host_unknown(self, downloads, monkeypatch):
        monkeypatch.setattr("app.main.HOST_DOWNLOADS_DIR", "")

        data = client.get("/api/paths").json()

        # A wrong-but-real path beats an empty string: the UI renders this
        # verbatim, and "" would read as a bug.
        assert data["host_downloads"] == str(downloads)
        assert data["is_host_path_known"] is False


# ---------------------------------------------------------------------------
# GET /api/files
# ---------------------------------------------------------------------------


class TestFilesEndpoint:
    def test_tags_each_file_with_its_folder(self, downloads):
        files = client.get("/api/files").json()["files"]
        by_name = {f["name"]: f for f in files}

        assert by_name["Artist - Song.flac"]["folder"] == "My Playlist"
        assert by_name["Someone - Track.mp3"]["folder"] == "Another List"
        # Loose files in the root get "" so the UI can still group them.
        assert by_name["loose.mp3"]["folder"] == ""

    def test_host_path_is_absolute_on_the_host(self, downloads):
        files = client.get("/api/files").json()["files"]
        song = next(f for f in files if f["name"] == "Artist - Song.flac")

        assert song["host_path"] == "/host/spslsk/downloads/My Playlist/Artist - Song.flac"

    def test_hidden_files_are_excluded(self, downloads):
        files = client.get("/api/files").json()["files"]

        assert all(not f["name"].startswith(".") for f in files)
        assert len(files) == 4


# ---------------------------------------------------------------------------
# GET /api/folders
# ---------------------------------------------------------------------------


class TestFoldersEndpoint:
    def test_groups_files_and_counts_sizes(self, downloads):
        folders = {f["name"]: f for f in client.get("/api/folders").json()["folders"]}

        assert folders["My Playlist"]["files"] == 2
        assert folders["My Playlist"]["audio_files"] == 2
        assert folders["Another List"]["files"] == 1
        assert folders["My Playlist"]["path"] == "/host/spslsk/downloads/My Playlist"

    def test_loose_root_files_are_reported_under_the_empty_folder(self, downloads):
        names = [f["name"] for f in client.get("/api/folders").json()["folders"]]

        assert "" in names

    def test_sorted_newest_first(self, downloads):
        # Touch a known folder into the future; it must come first because the
        # folder a user just filled is the one they want to open.
        target = downloads / "Another List" / "Someone - Track.mp3"
        import os
        os.utime(target, (2_000_000_000, 2_000_000_000))

        folders = client.get("/api/folders").json()["folders"]

        assert folders[0]["name"] == "Another List"

    def test_empty_downloads_dir_returns_no_folders(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.main.DOWNLOADS_DIR", tmp_path / "nope")

        data = client.get("/api/folders").json()

        assert data["folders"] == []
        assert data["total"] == 0


# ---------------------------------------------------------------------------
# GET /api/resolve-folder
# ---------------------------------------------------------------------------


class TestResolveFolderEndpoint:
    def test_maps_display_name_to_existing_folder(self, downloads):
        data = client.get("/api/resolve-folder", params={"name": "My Playlist"}).json()

        assert data["folder"] == "My Playlist"
        assert data["exists"] is True
        assert data["path"] == "/host/spslsk/downloads/My Playlist"

    def test_applies_the_same_sanitization_as_the_downloader(self, downloads):
        # A list literally named 'My: Playlist?' lands in 'My Playlist' on
        # disk — this endpoint exists precisely so the UI never re-derives it.
        (downloads / "My Playlist").exists()
        data = client.get("/api/resolve-folder", params={"name": 'My: Playlist?'}).json()

        assert data["folder"] == "My Playlist"
        assert data["exists"] is True

    def test_reports_missing_folder_without_creating_it(self, downloads):
        data = client.get("/api/resolve-folder", params={"name": "Never Downloaded"}).json()

        assert data["exists"] is False
        assert not (downloads / "Never Downloaded").exists()


# ---------------------------------------------------------------------------
# GET /api/file
# ---------------------------------------------------------------------------


class TestFileDownloadEndpoint:
    def test_streams_a_downloaded_file(self, downloads):
        resp = client.get("/api/file", params={"path": "My Playlist/Artist - Song.flac"})

        assert resp.status_code == 200
        assert resp.content == b"x" * 2048

    def test_missing_file_is_404(self, downloads):
        resp = client.get("/api/file", params={"path": "My Playlist/ghost.flac"})

        assert resp.status_code == 404

    @pytest.mark.parametrize("attack", [
        "../../etc/passwd",
        "My Playlist/../../../etc/passwd",
        "/etc/passwd",
    ])
    def test_rejects_traversal_outside_downloads(self, downloads, attack):
        resp = client.get("/api/file", params={"path": attack})

        assert resp.status_code in (400, 404)
        assert b"root:" not in resp.content

    def test_symlink_escape_is_rejected(self, downloads, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("classified")
        (downloads / "escape.txt").symlink_to(secret)

        resp = client.get("/api/file", params={"path": "escape.txt"})

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/status — the on-disk folder name
# ---------------------------------------------------------------------------


class TestStatusFolderField:
    def test_status_exposes_the_sanitized_folder_name(self, monkeypatch):
        from app import downloader

        monkeypatch.setattr(downloader.session, "playlist_name", 'Best of 2026: "Vol 1"')

        data = client.get("/api/status").json()

        # The UI builds the completion banner's path from this, so it must be
        # the directory name, not the display name.
        assert data["playlist_name"] == 'Best of 2026: "Vol 1"'
        assert data["folder"] == "Best of 2026 Vol 1"

    def test_folder_is_empty_when_no_session_ran(self, monkeypatch):
        from app import downloader

        monkeypatch.setattr(downloader.session, "playlist_name", "")

        assert client.get("/api/status").json()["folder"] == ""
