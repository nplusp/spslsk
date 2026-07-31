#!/usr/bin/env python3
"""Host-side helper that opens folders in the native file manager.

The backend runs inside Docker and physically cannot spawn Finder or
Nautilus, so this tiny stdlib-only server runs on the host and the browser
talks to it directly on 127.0.0.1:8001.

Endpoints:
  GET  /health       -> {"ok": true, "downloads": "...", "open_cmd": "open"}
  POST /open-folder  -> {"subfolder": "..."} opens downloads/<subfolder>

``/health`` exists so the UI can tell "helper isn't running" apart from
"helper failed", and degrade to copy-path instead of failing silently.

Bound to 127.0.0.1 only: this process runs shell commands on behalf of any
caller, so it must never be reachable off-box.

Started by start.sh; also runnable standalone:

    python3 host_helper.py [--downloads ./downloads] [--port 8001]
"""
import argparse
import http.server
import json
import os
import platform
import subprocess
import sys


def detect_open_command() -> str:
    """Return the platform's "open this path in the file manager" command.

    Empty string means no known opener — the caller degrades to copy-path
    rather than shelling out to something that does not exist.
    """
    system = platform.system()
    if system == "Darwin":
        return "open"
    if system == "Windows":
        return "explorer"
    # Linux/BSD: xdg-open is the freedesktop standard, but it is not
    # guaranteed to be installed (headless boxes, minimal containers).
    from shutil import which
    return "xdg-open" if which("xdg-open") else ""


def resolve_target(downloads_root: str, subfolder: str) -> str:
    """Resolve downloads_root/subfolder, refusing anything outside the root.

    Both paths are realpath'd before comparison so ``..`` segments and
    symlinks cannot escape. Returns the downloads root itself when the
    subfolder is empty, missing, or out of bounds — opening the parent
    folder is a sane, non-destructive fallback that still shows the user
    their files.
    """
    root = os.path.realpath(downloads_root)
    if not subfolder:
        return root

    candidate = os.path.realpath(os.path.join(root, subfolder))
    # commonpath raises ValueError across drives on Windows; treat that as
    # "not contained" rather than crashing the request handler.
    try:
        contained = os.path.commonpath([root, candidate]) == root
    except ValueError:
        contained = False

    if not contained or not os.path.isdir(candidate):
        return root
    return candidate


class Handler(http.server.BaseHTTPRequestHandler):
    # Injected by main(); class attributes keep the stdlib handler signature
    # intact (BaseHTTPRequestHandler is instantiated per request by the
    # server, so there is no constructor hook to pass config through).
    downloads_root = os.path.join(os.getcwd(), "downloads")
    open_cmd = ""

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length)) or {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def do_GET(self):
        if self.path.startswith("/health"):
            self._respond(200, {
                "ok": True,
                "downloads": self.downloads_root,
                "open_cmd": self.open_cmd,
                "can_open": bool(self.open_cmd),
            })
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/open-folder"):
            self._respond(404, {"error": "not found"})
            return

        if not self.open_cmd:
            self._respond(501, {
                "ok": False,
                "error": "No file-manager opener available on this host",
                "path": self.downloads_root,
            })
            return

        body = self._read_json_body()
        target = resolve_target(self.downloads_root, str(body.get("subfolder", "")))

        # Create only the root — never conjure a subfolder that a stale
        # history entry points at, or the user gets an empty window and
        # thinks the download vanished.
        os.makedirs(self.downloads_root, exist_ok=True)

        try:
            subprocess.Popen([self.open_cmd, target])
        except OSError as e:
            self._respond(500, {"ok": False, "error": str(e), "path": target})
            return

        self._respond(200, {"ok": True, "path": target})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *args):
        pass  # Suppress per-request noise; start.sh tails compose logs.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--downloads",
        default=os.path.join(os.getcwd(), "downloads"),
        help="Path to the downloads directory on the host",
    )
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    Handler.downloads_root = os.path.realpath(args.downloads)
    Handler.open_cmd = detect_open_command()

    if not Handler.open_cmd:
        print(
            "host_helper: no file-manager opener found (xdg-open missing?); "
            "serving /health so the UI can fall back to copy-path.",
            file=sys.stderr,
        )

    server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
