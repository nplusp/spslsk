import asyncio
import httpx
from app.config import SLSKD_URL, SLSKD_API_KEY


class SlskdClient:
    """REST client for slskd API."""

    def __init__(self):
        self.base_url = SLSKD_URL.rstrip("/")
        self.api_url = f"{self.base_url}/api/v0"
        self.headers = {"X-API-Key": SLSKD_API_KEY}

    async def _get(self, path: str) -> dict | list:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.api_url}{path}", headers=self.headers
            )
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, json: dict | None = None):
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.api_url}{path}", headers=self.headers, json=json
            )
            resp.raise_for_status()
            # Some endpoints return strings (errors) not JSON
            try:
                return resp.json()
            except Exception:
                return resp.text

    async def _delete(self, path: str) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(
                f"{self.api_url}{path}", headers=self.headers
            )
            resp.raise_for_status()

    async def health_check(self) -> bool:
        """Check if slskd is reachable AND connected+logged in to Soulseek."""
        try:
            data = await self._get("/server")
            return data.get("isConnected", False) and data.get("isLoggedIn", False)
        except Exception:
            return False

    # Backoff delay (seconds) before retrying a search after slskd returns
    # HTTP 429 Too Many Requests. One retry, then propagate.
    _SEARCH_RATELIMIT_BACKOFF_SEC = 5

    async def search(self, query: str) -> str:
        """Start a search and return the search ID.

        Retries once on HTTP 429 (slskd's REST rate limit) after a short
        backoff. Other status errors propagate immediately because they are
        either deterministic (400 — bad query) or slskd-internal bugs (500)
        that retry would not fix.
        """
        try:
            result = await self._post(
                "/searches", json={"searchText": query}
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429:
                raise
            # 429 Too Many Requests — wait and retry once.
            await asyncio.sleep(self._SEARCH_RATELIMIT_BACKOFF_SEC)
            result = await self._post(
                "/searches", json={"searchText": query}
            )

        # If POST returns the search object directly
        if isinstance(result, dict) and "id" in result:
            return result["id"]

        # If POST returned an error string, the server might be disconnected
        if isinstance(result, str):
            raise RuntimeError(f"Search failed: {result}")

        # Fallback: find the search in the list
        searches = await self._get("/searches")
        if isinstance(searches, list):
            for s in searches:
                if s.get("searchText") == query:
                    return s["id"]
            if searches:
                return searches[-1]["id"]

        raise RuntimeError("Search was not created")

    async def get_search_results(self, search_id: str) -> dict:
        """Get results for a search by ID, including response data."""
        return await self._get(f"/searches/{search_id}?includeResponses=true")

    async def wait_for_search(
        self, search_id: str, timeout: int = 30, poll_interval: float = 5
    ) -> list:
        """Wait for search to complete and return responses.

        Uses longer poll intervals to avoid hammering slskd.
        """
        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                data = await self.get_search_results(search_id)
            except Exception:
                continue

            state = data.get("state", "")
            responses = data.get("responses", [])

            # If we got results, return them
            if responses:
                return responses

            # If completed with no results, no point waiting
            if "Completed" in state:
                return []

        # Final check
        try:
            data = await self.get_search_results(search_id)
            return data.get("responses", [])
        except Exception:
            return []

    async def download_file(self, username: str, file_info: dict) -> dict:
        """Enqueue a file for download and wait for completion."""
        result = await self._post(
            f"/transfers/downloads/{username}",
            json=[file_info],
        )

        # Wait for the download to actually complete (poll transfers)
        filename = file_info.get("filename", "").split("\\")[-1]
        for _ in range(120):  # Max 10 minutes (120 * 5s)
            await asyncio.sleep(5)
            try:
                downloads = await self._get(f"/transfers/downloads/{username}")
                if isinstance(downloads, dict):
                    dirs = downloads.get("directories", [])
                    for d in dirs:
                        for f in d.get("files", []):
                            fname = f.get("filename", "").split("\\")[-1]
                            if fname == filename:
                                state = f.get("state", "")
                                # Completed states in slskd
                                if "Completed" in state and "Succeeded" in state:
                                    return result
                                if "Errored" in state or "Cancelled" in state or "Rejected" in state:
                                    raise RuntimeError(f"Download failed: {state}")
            except RuntimeError:
                raise
            except Exception:
                continue

        return result

    async def get_all_downloads(self) -> list:
        """Get all current downloads."""
        return await self._get("/transfers/downloads")

    async def delete_search(self, search_id: str) -> None:
        """Clean up a completed search."""
        try:
            await self._delete(f"/searches/{search_id}")
        except Exception:
            pass
