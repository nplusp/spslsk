"""Tests for SlskdClient defensive behavior — 429 retry on search.

slskd's REST API rate-limits search calls. In the user's diagnostic logs,
two tracks failed with HTTP 429 Too Many Requests. The client should retry
once after a short backoff before propagating the error.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.slskd_client import SlskdClient


def _make_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an httpx HTTPStatusError carrying the given status code."""
    request = httpx.Request("POST", "http://test/api/v0/searches")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response
    )


class TestSearchRetryOn429:
    @pytest.mark.asyncio
    async def test_first_call_succeeds_no_retry(self):
        client = SlskdClient()
        client._post = AsyncMock(return_value={"id": "ok"})
        result = await client.search("test")
        assert result == "ok"
        assert client._post.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_once_on_429_then_succeeds(self):
        client = SlskdClient()
        client._post = AsyncMock(
            side_effect=[
                _make_status_error(429),
                {"id": "search-id-123"},
            ]
        )
        # Patch asyncio.sleep so the test doesn't actually wait the backoff
        with patch("app.slskd_client.asyncio.sleep", new_callable=AsyncMock):
            search_id = await client.search("test query")
        assert search_id == "search-id-123"
        assert client._post.call_count == 2

    @pytest.mark.asyncio
    async def test_429_twice_propagates(self):
        client = SlskdClient()
        client._post = AsyncMock(
            side_effect=[_make_status_error(429), _make_status_error(429)]
        )
        with patch("app.slskd_client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await client.search("test query")
        assert exc_info.value.response.status_code == 429
        assert client._post.call_count == 2  # one initial + one retry, no more

    @pytest.mark.asyncio
    async def test_400_not_retried(self):
        # HTTP 400 Bad Request (e.g., the S L F single-char-tokens case
        # before Unit 1 collapses them). Should not retry — the error is
        # deterministic, retry would be wasteful.
        client = SlskdClient()
        client._post = AsyncMock(side_effect=_make_status_error(400))
        with patch("app.slskd_client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await client.search("S L F")
        assert exc_info.value.response.status_code == 400
        assert client._post.call_count == 1

    @pytest.mark.asyncio
    async def test_500_not_retried(self):
        # HTTP 500 (e.g., the DHÆÜR EntityFramework concurrency bug).
        # Not retried because it's a slskd-internal bug, not transient
        # rate-limiting. ASCII-fold in Unit 1 prevents this from happening.
        client = SlskdClient()
        client._post = AsyncMock(side_effect=_make_status_error(500))
        with patch("app.slskd_client.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx.HTTPStatusError):
                await client.search("test")
        assert client._post.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_after_429_uses_backoff_delay(self):
        client = SlskdClient()
        client._post = AsyncMock(
            side_effect=[_make_status_error(429), {"id": "ok"}]
        )
        with patch(
            "app.slskd_client.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:
            await client.search("test")
        # Backoff delay was applied between attempts
        mock_sleep.assert_called_once()
        # The delay should be a non-trivial wait — at least 1 second
        assert mock_sleep.call_args[0][0] >= 1
