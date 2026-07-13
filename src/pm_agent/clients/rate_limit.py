"""Async token-bucket rate limiter + shared HTTP client."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)


@dataclass
class RateLimiter:
    """Token-bucket limiter: max `rps` requests per second, burst = rps."""
    rps: float
    _tokens: float = 0.0
    _last: float = 0.0
    _lock: asyncio.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._tokens = self.rps
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self.rps, self._tokens + elapsed * self.rps)
            self._last = now
            if self._tokens < 1:
                deficit = 1 - self._tokens
                await asyncio.sleep(deficit / self.rps)
                self._tokens = 0
                self._last = time.monotonic()
            else:
                self._tokens -= 1


class HttpClient:
    """httpx.AsyncClient wrapper with rate limiting, retry, 429 backoff."""

    def __init__(self, base_url: str, rps: float, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.limiter = RateLimiter(rps=rps)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": "pm-agent/0.1"},
            http2=True,
        )

    @retry(
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        reraise=True,
    )
    async def get(self, path: str, params: dict | None = None) -> dict | list:
        await self.limiter.acquire()
        resp = await self._client.get(path, params=params)
        if resp.status_code == 429:
            # Respect rate limit; retry via tenacity
            raise httpx.HTTPStatusError("429 Too Many Requests", request=resp.request, response=resp)
        resp.raise_for_status()
        return resp.json()

    async def get_raw(self, path: str, params: dict | None = None) -> httpx.Response:
        await self.limiter.acquire()
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp

    async def close(self) -> None:
        await self._client.aclose()
