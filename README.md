# SpotaSlsk

Download entire Spotify playlists via Soulseek. Automatically finds the best quality available (FLAC preferred).

## How it works

Paste a Spotify playlist URL → app parses all tracks → searches Soulseek for each → downloads in best quality → organized in `./downloads/`.

## Quick Start

### 1. Clone

```bash
git clone git@github.com:nplusp/spslsk.git
cd spslsk
```

### 2. Get a Soulseek account

Register at [slsknet.org](https://www.slsknet.org/) (free).

### 3. Get Spotify API keys

Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard), create an app, copy **Client ID** and **Client Secret**.

Add `http://localhost:8000/callback` to **Redirect URIs** in your app settings.

### 4. Configure

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
SLSKD_SLSK_USERNAME=your_soulseek_username
SLSKD_SLSK_PASSWORD=your_soulseek_password
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
SLSKD_API_KEY=any_random_string_here
```

### 5. Run

```bash
./start.sh
```

Open **http://localhost:8000** and paste a Spotify playlist URL.

## Requirements

- [Docker](https://www.docker.com/products/docker-desktop/) or [OrbStack](https://orbstack.dev/) (recommended for Mac)
- Soulseek account (free)
- Spotify Developer account (free)

## Features

- Paste any Spotify playlist URL
- Automatic quality prioritization (FLAC > WAV > MP3 320 > ...)
- Skip already-downloaded tracks (tracked by Spotify ID)
- Playlist history with quick reload
- Open downloads folder from browser
- Live download progress

## Downloads

Files are saved to `./downloads/` in the project directory.

## Troubleshooting

**"slskd not connected"**
Make sure no other Soulseek client (Nicotine+, SoulseekQt) is running with the same account. Only one connection per account is allowed.

**Tracks not found**
Some niche artists may have limited availability on Soulseek. Try again later — it's a P2P network, availability changes.

**"0 results" for popular tracks**
If you just started, wait a minute for slskd to fully connect to the Soulseek network.

## Architecture

```
Browser → FastAPI (Python) → slskd (Docker) → Soulseek P2P Network
                ↓
         Spotify Web API
```

Everything runs locally. No data leaves your machine except Soulseek/Spotify API calls.

## License

MIT
