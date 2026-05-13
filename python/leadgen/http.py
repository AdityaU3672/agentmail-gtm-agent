"""Tiny HTTP helpers using stdlib only (no extra deps).

Wraps urllib with: JSON decoding, basic retry on 403/429/5xx, GitHub auth header,
and respect for `Retry-After` headers when present.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} on {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


def _build_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> urllib.request.Request:
    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(params)}"
    return urllib.request.Request(url, headers=headers or {})


def get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: int = 20,
    max_retries: int = 3,
) -> Any:
    """GET a URL and decode JSON. Retries transient failures with backoff."""
    req = _build_request(url, headers=headers, params=params)
    delay = 1.0
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            # Retryable cases: rate-limited or transient server error.
            if e.code in (403, 429, 500, 502, 503, 504) and attempt < max_retries - 1:
                wait = float(e.headers.get("Retry-After") or delay)
                time.sleep(min(wait, 30))
                delay *= 2
                last_err = HttpError(e.code, url, body)
                continue
            raise HttpError(e.code, url, body) from e
        except urllib.error.URLError as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("unreachable")


def get_text(url: str, *, headers: dict[str, str] | None = None, timeout_s: int = 20) -> str:
    """GET a URL and return the decoded body."""
    req = _build_request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8", errors="replace")
