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
echo "=== Ready! ==="
echo "  App:   http://localhost:8000"
echo "  slskd: http://localhost:5030 (user: slskd / pass: slskd)"
echo ""
echo "To stop: docker compose down"
