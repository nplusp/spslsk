import os
from dotenv import load_dotenv

load_dotenv()

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

SLSKD_URL = os.getenv("SLSKD_URL", "http://localhost:5030")
SLSKD_API_KEY = os.getenv("SLSKD_API_KEY", "")

# Absolute path of ./downloads on the HOST, injected by docker-compose.
# Inside the container everything lives under /app/downloads, which is a
# meaningless string for a user staring at Finder. start.sh exports
# HOST_DOWNLOADS_DIR=${PWD}/downloads so the UI can display and copy a path
# that actually exists on the user's machine. Empty when the backend runs
# outside compose — the UI falls back to the container path in that case.
HOST_DOWNLOADS_DIR = os.getenv("HOST_DOWNLOADS_DIR", "")

# Where the host-side "open in file manager" helper listens. The browser
# talks to it directly (the container cannot spawn Finder), so this value is
# handed to the frontend rather than used server-side.
HOST_HELPER_URL = os.getenv("HOST_HELPER_URL", "http://127.0.0.1:8001")
