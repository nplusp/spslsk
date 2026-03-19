import os
from dotenv import load_dotenv

load_dotenv()

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

SLSKD_URL = os.getenv("SLSKD_URL", "http://localhost:5030")
SLSKD_API_KEY = os.getenv("SLSKD_API_KEY", "")
