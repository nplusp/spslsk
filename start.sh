#!/bin/bash
set -e

echo "=== Spotify → Soulseek Downloader ==="
echo ""

# Check if Docker/OrbStack is running
if ! docker info > /dev/null 2>&1; then
    echo "ERROR: Docker is not running."
    echo "Please start Docker Desktop or OrbStack first."
    exit 1
fi

# Check .env file
if [ ! -f .env ]; then
    echo "No .env file found. Creating from template..."
    cp .env.example .env
    echo ""
    echo "Please edit .env with your credentials:"
    echo "  1. Soulseek username/password (https://www.slsknet.org/)"
    echo "  2. Spotify Client ID/Secret (https://developer.spotify.com/dashboard)"
    echo ""
    echo "Then run this script again."
    exit 1
fi

# Create directories
mkdir -p downloads slskd-data

echo "Starting services..."
docker compose up --build -d

echo ""
echo "Waiting for services to start..."
sleep 5

echo ""

# Start host-side helper for native Finder access (port 8001)
HELPER_PID=""
cleanup() {
    if [ -n "$HELPER_PID" ]; then
        kill "$HELPER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Tiny Python HTTP server on host to open Finder
python3 -c "
import http.server, subprocess, json, os

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/open-folder':
            folder = os.path.join(os.path.dirname(os.path.abspath('$0')), 'downloads')
            os.makedirs(folder, exist_ok=True)
            subprocess.Popen(['open', folder])
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True}).encode())
        else:
            self.send_response(404)
            self.end_headers()
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    def log_message(self, *args):
        pass  # Suppress logs

http.server.HTTPServer(('127.0.0.1', 8001), Handler).serve_forever()
" &
HELPER_PID=$!

echo "=== Ready! ==="
echo "  App:   http://localhost:8000"
echo "  slskd: http://localhost:5030 (user: slskd / pass: slskd)"
echo ""
echo "To stop: Ctrl+C or docker compose down"

# Wait for Docker containers (keeps script running so Ctrl+C stops everything)
docker compose logs -f
