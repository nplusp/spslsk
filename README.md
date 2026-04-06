# spslsk

Download tracks from Spotify URLs or plain text lists via Soulseek. Automatically finds the best quality available (FLAC preferred).

## How it works

Paste any mix of Spotify URLs (playlist / album / track) and plain text `Artist - Title` lines → app resolves them into one track list → preview lets you edit / delete / add rows → searches Soulseek for each → downloads in best quality → organized in `./downloads/{list name}/`.

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
# Leave SLSKD_API_KEY at its placeholder value — start.sh will auto-generate
# a random value the first time you run it. It's a shared secret between
# the backend and slskd containers, not an external credential.
SLSKD_API_KEY=supersecretapikey123change_me
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

- Unified input: paste Spotify playlist, album, or track URLs, plain text lines, or any mix
- Editable preview: fix ambiguous rows, swap artist ↔ title in bulk, delete what you don't want
- Strict mode: unparseable rows block the download button until you fix or remove them
- Automatic quality prioritization (FLAC > WAV > MP3 320 > ...)
- Skip already-downloaded tracks (dedup works for both Spotify IDs and manual text entries)
- History sidebar with one-click reload of the original input
- Open downloads folder from browser
- Live download progress

## Downloads

Files are saved to `./downloads/` in the project directory.

## Troubleshooting

**"slskd not connected"**
Make sure no other Soulseek client (Nicotine+, SoulseekQt) is running with the same account. Only one connection per account is allowed.

**"Unknown API key beginning with: ..." in slskd logs**
This means `slskd-data/slskd.yml` is out of sync with `SLSKD_API_KEY` in `.env`. `start.sh` normally handles this automatically — it writes the API key into a managed block in `slskd.yml` on every run. If you ran `docker compose up` directly instead of `./start.sh`, or edited `.env` without re-running `./start.sh`, the two will drift. Fix: `docker compose down && ./start.sh`.

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
