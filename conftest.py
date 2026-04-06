"""Root conftest — ensures the project root is on sys.path so tests can
import ``app.*`` without installing the package. Also stubs out the
Spotify credentials env vars so ``app.config`` can be imported during
test collection without a real .env file."""
import os
import sys
from pathlib import Path

# Make ``from app.X import ...`` work from any test file.
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Provide harmless defaults for env vars consumed at import time by
# ``app.config``. Tests should never hit the real Spotify API.
os.environ.setdefault("SPOTIFY_CLIENT_ID", "test-client-id")
os.environ.setdefault("SPOTIFY_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("SLSKD_URL", "http://localhost:5030")
os.environ.setdefault("SLSKD_API_KEY", "test-api-key")
