"""Tests for the host-side folder-opening helper.

The helper shells out to `open`/`xdg-open` on behalf of any local caller, so
its path resolution is the security-relevant part and gets the most coverage.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from host_helper import detect_open_command, resolve_target


@pytest.fixture
def downloads(tmp_path):
    root = tmp_path / "downloads"
    (root / "My Playlist").mkdir(parents=True)
    (root / "Nested" / "Deeper").mkdir(parents=True)
    return root


class TestResolveTarget:
    def test_empty_subfolder_resolves_to_the_root(self, downloads):
        assert resolve_target(str(downloads), "") == os.path.realpath(downloads)

    def test_existing_subfolder_resolves_inside_the_root(self, downloads):
        result = resolve_target(str(downloads), "My Playlist")

        assert result == os.path.realpath(downloads / "My Playlist")

    def test_nested_subfolder_is_allowed(self, downloads):
        result = resolve_target(str(downloads), "Nested/Deeper")

        assert result == os.path.realpath(downloads / "Nested" / "Deeper")

    @pytest.mark.parametrize("attack", [
        "../..",
        "../../etc",
        "My Playlist/../../..",
        "/etc",
    ])
    def test_traversal_falls_back_to_the_root(self, downloads, attack):
        # Falling back (rather than erroring) is deliberate: the user still
        # gets a window showing their downloads instead of a dead click.
        assert resolve_target(str(downloads), attack) == os.path.realpath(downloads)

    def test_symlink_out_of_the_tree_falls_back_to_the_root(self, downloads, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (downloads / "escape").symlink_to(outside)

        assert resolve_target(str(downloads), "escape") == os.path.realpath(downloads)

    def test_missing_subfolder_falls_back_to_the_root(self, downloads):
        # Stale history entries point at folders that were renamed or deleted.
        assert resolve_target(str(downloads), "Gone") == os.path.realpath(downloads)


class TestDetectOpenCommand:
    def test_returns_open_on_macos(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")

        assert detect_open_command() == "open"

    def test_returns_explorer_on_windows(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")

        assert detect_open_command() == "explorer"

    def test_returns_xdg_open_on_linux_when_present(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/xdg-open")

        assert detect_open_command() == "xdg-open"

    def test_returns_empty_when_linux_lacks_xdg_open(self, monkeypatch):
        # Headless boxes have no opener; the UI must degrade to copy-path
        # rather than the helper shelling out to a nonexistent binary.
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("shutil.which", lambda name: None)

        assert detect_open_command() == ""
