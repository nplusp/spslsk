---
title: slskd API Search Returns Empty Results Despite Working Web UI
category: integration-issues
component: slskd_client
severity: critical
tags: [slskd, soulseek, api, search, rest-api]
date_solved: 2026-03-19
time_to_solve: 2h
root_cause: Missing query parameter and concurrent client conflict
---

# slskd API Search Returns Empty Results Despite Working Web UI

## Symptom

Searches via slskd REST API returned 0 files and 0 responses for every query,
even for popular artists like "Radiohead" and "Nirvana." Meanwhile, the same
searches through the slskd web UI (localhost:5030) returned thousands of results.

## Investigation Steps

### 1. Verified API connectivity
- `GET /api/v0/server` returned connected state — API was reachable
- Health check passed, JWT auth was working

### 2. Checked search query format
- Tried various query formats: `"artist title"`, `"artist"`, broad/narrow
- All returned `responseCount: 0`

### 3. Discovered `includeResponses` parameter
- `GET /api/v0/searches/{id}` returned `responseCount: 250` but `responses: []`
- **Root cause #1**: The `responses` array is only populated when
  `?includeResponses=true` is explicitly passed as a query parameter
- This is NOT documented in the slskd API docs — discovered by trial and error

### 4. Still getting 0 results after fix
- After fixing `includeResponses`, some searches still returned 0 on the API
  while working in the web UI
- **Root cause #2**: Nicotine+ (desktop Soulseek client) was running in the
  background on the same Soulseek account
- Soulseek only allows ONE connection per account — the server was routing
  search results to Nicotine+ instead of slskd

### 5. Multi-word search queries returning 0
- Even with both fixes, queries like `"Barker Filter Bubbles"` returned 0
- **Root cause #3**: Soulseek matches ALL words against full file paths
- "Bubbles" doesn't appear in filenames like `Barker - Filter.flac`
- **Solution**: Search by artist only (broad), then filter results on our side

## Root Cause (Summary)

Three independent issues combined:

1. **`?includeResponses=true`** — required but undocumented query parameter
2. **Concurrent Soulseek client** — Nicotine+ stealing the connection
3. **Overly specific search queries** — Soulseek word matching is strict

## Working Solution

### Fix 1: Always pass includeResponses

```python
# In slskd_client.py
async def get_search_results(self, search_id: str) -> list:
    resp = await self.client.get(
        f"/api/v0/searches/{search_id}",
        params={"includeResponses": "true"},  # CRITICAL
    )
    data = resp.json()
    return data.get("responses", [])
```

### Fix 2: Kill other Soulseek clients

```bash
# Before starting slskd
pkill -f nicotine
# Then restart slskd
docker compose restart slskd
```

### Fix 3: Broad search, filter locally

```python
def _build_search_queries(artist: str, title: str) -> list[str]:
    # Search by artist only (gets many results)
    queries = [first_artist]
    # Optionally: artist + first significant title word
    if title_words:
        queries.append(f"{first_artist} {title_words[0]}")
    return queries

def _matches_track(filename: str, artist: str, title: str) -> bool:
    # Filter results on our side — lenient matching
    return any(word in filename.lower() for word in significant_words)
```

## Prevention

1. **Always test API calls with the web UI side-by-side** — if the UI finds
   results but the API doesn't, the issue is in the API call, not Soulseek
2. **Document undocumented API parameters** — maintain a local reference of
   API quirks (see `feedback_slskd_api.md`)
3. **Add a startup check** for competing Soulseek clients (ps aux | grep)
4. **Add flood protection** — 5+ second delays between searches to avoid
   getting kicked by the Soulseek server

## Related

- `feedback_slskd_api.md` — Complete list of slskd API quirks
- `feedback_slsk_flood.md` — Soulseek flood protection details
- slskd GitHub: https://github.com/slskd/slskd
- slskd Python API docs: https://slskd-api.readthedocs.io/
