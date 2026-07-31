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
- Always know where files landed: destination path shown before, during, and after a download
- One-click "Open folder" in your native file manager, with copy-path fallback
- Files sidebar grouped by list, with per-file download straight from the browser
- Live download progress

## Downloads

Files are saved to `./downloads/{list name}/` in the project directory.

The folder name is not always byte-identical to the list name you typed:
characters that are illegal in filenames (`<>:"/\|?*[]`) are stripped, and
whitespace is collapsed. The UI always shows the **real** path — click any
path chip to copy it.

### "Open folder" and the host helper

The backend runs inside Docker and cannot spawn Finder or Nautilus. `start.sh`
therefore launches `host_helper.py` on the host at `127.0.0.1:8001`, and the
browser calls it directly. Consequences:

- Started with `./start.sh` → "Open folder" buttons open your file manager.
- Started with a bare `docker compose up` → no helper. The buttons relabel
  themselves to **Copy path** and copy the folder path to your clipboard
  instead. Nothing fails silently.

You can also run the helper on its own:

```bash
python3 host_helper.py --downloads ./downloads --port 8001
```

It binds to `127.0.0.1` only, and refuses any path that resolves outside
`./downloads` (checked after resolving symlinks and `..`).

### Getting files without a file manager

Every file in the **Files** sidebar has a ↓ link that streams it through the
browser (`GET /api/file?path=...`), so downloads are retrievable even on a
headless or remote host.

## Troubleshooting

**"slskd not connected"**
Make sure no other Soulseek client (Nicotine+, SoulseekQt) is running with the same account. Only one connection per account is allowed.

**"Unknown API key beginning with: ..." in slskd logs**
This means `slskd-data/slskd.yml` is out of sync with `SLSKD_API_KEY` in `.env`. `start.sh` normally handles this automatically — it writes the API key into a managed block in `slskd.yml` on every run. If you ran `docker compose up` directly instead of `./start.sh`, or edited `.env` without re-running `./start.sh`, the two will drift. Fix: `docker compose down && ./start.sh`.

**Tracks not found**
Some niche artists may have limited availability on Soulseek. Try again later — it's a P2P network, availability changes.

**"Open folder" does nothing / just copies the path**
The host helper isn't running. It's started by `./start.sh` and lives only as
long as that script does — if you started the stack with `docker compose up`,
or closed the terminal running `start.sh`, the helper is gone. Either re-run
`./start.sh`, or start the helper alone:
`python3 host_helper.py --downloads ./downloads`.

**The path shown in the UI starts with `/app/downloads`**
The backend didn't receive `HOST_DOWNLOADS_DIR`, so it's showing its own
container path. `./start.sh` exports it automatically; a bare
`docker compose up` does not. Run `HOST_DOWNLOADS_DIR="$(pwd)/downloads" docker compose up`
or just use `./start.sh`.

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
