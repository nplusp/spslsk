import asyncio
import uuid
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

    async def _post(self, path: str, json: dict | None = None) -> dict | list:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.api_url}{path}", headers=self.headers, json=json
            )
            resp.raise_for_status()
            return resp.json()

    async def _delete(self, path: str) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(
                f"{self.api_url}{path}", headers=self.headers
            )
            resp.raise_for_status()

    async def health_check(self) -> bool:
        """Check if slskd is reachable and connected to Soulseek."""
        try:
            data = await self._get("/application")
            return True
        except Exception:
            return False

    async def search(self, query: str, search_timeout: int = 15) -> str:
        """Start a search and return the search ID."""
        search_id = str(uuid.uuid4())
        await self._post("/searches", json={
            "id": search_id,
            "searchText": query,
            "searchTimeout": search_timeout,
        })
        return search_id

    async def get_search_results(self, search_id: str) -> dict:
        """Get results for a search by ID."""
        return await self._get(f"/searches/{search_id}")

    async def wait_for_search(
        self, search_id: str, timeout: int = 20, poll_interval: float = 2
    ) -> list:
        """Wait for search to complete and return responses."""
        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            data = await self.get_search_results(search_id)
            state = data.get("state", "")
            # Completed states: "Completed, ResponsesReceived" or similar
            if "Completed" in state:
                return data.get("responses", [])

        # Return whatever we have after timeout
        data = await self.get_search_results(search_id)
        return data.get("responses", [])

    async def download_file(self, username: str, file_info: dict) -> dict:
        """Enqueue a file for download.

        file_info should contain at minimum:
        - filename: full remote path
        - size: file size in bytes
        """
        return await self._post(
            f"/transfers/downloads/{username}",
            json=[file_info],
        )

    async def get_all_downloads(self) -> list:
        """Get all current downloads."""
        return await self._get("/transfers/downloads")

    async def delete_search(self, search_id: str) -> None:
        """Clean up a completed search."""
        try:
            await self._delete(f"/searches/{search_id}")
        except Exception:
            pass
