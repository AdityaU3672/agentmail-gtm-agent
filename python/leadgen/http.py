"""HTTP helpers: aiohttp (async) with certifi TLS, plus small sync wrappers.

Uses certifi's CA bundle so HTTPS works on macOS/python.org installs that lack
a proper default trust store. Retries 403/429/5xx with backoff + Retry-After.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any

import aiohttp


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def make_aiohttp_connector(*, limit: int = 100, limit_per_host: int = 30) -> aiohttp.TCPConnector:
    """Shared connector for leadgen (connection pooling + TLS)."""
    return aiohttp.TCPConnector(ssl=_ssl_context(), limit=limit, limit_per_host=limit_per_host)


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} on {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


async def aget_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: int = 20,
    max_retries: int = 3,
) -> Any:
    """GET JSON with retries (rate limits + transient errors)."""
    delay = 1.0
    last_err: Exception | None = None
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    for attempt in range(max_retries):
        try:
            async with session.get(url, headers=headers, params=params, timeout=timeout) as resp:
                text = await resp.text(encoding="utf-8", errors="replace")
                status = resp.status
                final_url = str(resp.url)

                if status in (403, 429, 500, 502, 503, 504) and attempt < max_retries - 1:
                    wait = float(resp.headers.get("Retry-After") or delay)
                    await asyncio.sleep(min(wait, 30))
                    delay *= 2
                    last_err = HttpError(status, final_url, text)
                    continue
                if status >= 400:
                    raise HttpError(status, final_url, text)
                return json.loads(text) if text else None
        except aiohttp.ClientError as e:
            last_err = e
            if attempt < max_retries - 1:
                await asyncio.sleep(min(delay, 30))
                delay *= 2
                continue
            raise
        except json.JSONDecodeError as e:
            raise HttpError(200, final_url, text[:200]) from e

    if last_err:
        raise last_err
    raise RuntimeError("unreachable")


async def aget_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_s: int = 20,
    max_retries: int = 3,
) -> str:
    """GET body as text with the same retry policy as aget_json."""
    delay = 1.0
    last_err: Exception | None = None
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    for attempt in range(max_retries):
        try:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                text = await resp.text(encoding="utf-8", errors="replace")
                status = resp.status
                final_url = str(resp.url)

                if status in (403, 429, 500, 502, 503, 504) and attempt < max_retries - 1:
                    wait = float(resp.headers.get("Retry-After") or delay)
                    await asyncio.sleep(min(wait, 30))
                    delay *= 2
                    last_err = HttpError(status, final_url, text)
                    continue
                if status >= 400:
                    raise HttpError(status, final_url, text)
                return text
        except aiohttp.ClientError as e:
            last_err = e
            if attempt < max_retries - 1:
                await asyncio.sleep(min(delay, 30))
                delay *= 2
                continue
            raise

    if last_err:
        raise last_err
    raise RuntimeError("unreachable")


# --- sync wrappers (one-shot ClientSession per call; fine for CLI) ----------


def get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: int = 20,
    max_retries: int = 3,
) -> Any:
    async def _run() -> Any:
        async with aiohttp.ClientSession(connector=make_aiohttp_connector()) as session:
            return await aget_json(
                session, url, headers=headers, params=params,
                timeout_s=timeout_s, max_retries=max_retries,
            )

    return asyncio.run(_run())


def get_text(url: str, *, headers: dict[str, str] | None = None, timeout_s: int = 20) -> str:
    async def _run() -> str:
        async with aiohttp.ClientSession(connector=make_aiohttp_connector()) as session:
            return await aget_text(session, url, headers=headers, timeout_s=timeout_s)

    return asyncio.run(_run())
