"""Stage 4a — Hacker News enrichment via the Algolia HN Search API.

Docs: https://hn.algolia.com/api
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import aiohttp

from .http import aget_json, make_aiohttp_connector


HN_API = "https://hn.algolia.com/api/v1/search"


@dataclass
class HNContext:
    top_story_title: str = ""
    top_story_url: str = ""
    top_story_points: int = 0
    discussion_snippets: list[str] = field(default_factory=list)


def lookup(query: str, *, timeout_s: int = 15) -> HNContext:
    """Best-effort HN lookup. Returns an empty HNContext on any failure."""
    return asyncio.run(_lookup_async(query, timeout_s=timeout_s))


async def _lookup_async(query: str, *, timeout_s: int) -> HNContext:
    if not query.strip():
        return HNContext()
    ctx = HNContext()
    connector = make_aiohttp_connector(limit=20, limit_per_host=10)
    async with aiohttp.ClientSession(connector=connector) as session:

        async def stories() -> dict:
            try:
                return await aget_json(
                    session,
                    HN_API,
                    params={"query": query, "tags": "story", "hitsPerPage": 5},
                    timeout_s=timeout_s,
                )
            except Exception:
                return {}

        async def comments() -> dict:
            try:
                return await aget_json(
                    session,
                    HN_API,
                    params={"query": query, "tags": "comment", "hitsPerPage": 5},
                    timeout_s=timeout_s,
                )
            except Exception:
                return {}

        stories_data, comments_data = await asyncio.gather(stories(), comments())

        hits = sorted(
            (stories_data if isinstance(stories_data, dict) else {}).get("hits") or [],
            key=lambda h: int(h.get("points") or 0),
            reverse=True,
        )
        if hits:
            top = hits[0]
            ctx.top_story_title = (top.get("title") or "").strip()
            ctx.top_story_url = (top.get("url") or top.get("story_url") or "").strip()
            ctx.top_story_points = int(top.get("points") or 0)

        for c in ((comments_data if isinstance(comments_data, dict) else {}).get("hits") or [])[:3]:
            text = (c.get("comment_text") or "").strip()
            if text:
                ctx.discussion_snippets.append(text[:280])

    return ctx
