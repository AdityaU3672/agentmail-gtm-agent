"""Stage 4a — Hacker News enrichment via the Algolia HN Search API.

Docs: https://hn.algolia.com/api

We pull the top recent story and a high-signal recent comment thread that mention
the prospect's company domain or name. Used purely as additional context for the
hook generator; absence of HN data is fine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .http import get_json


HN_API = "https://hn.algolia.com/api/v1/search"


@dataclass
class HNContext:
    top_story_title: str = ""
    top_story_url: str = ""
    top_story_points: int = 0
    discussion_snippets: list[str] = field(default_factory=list)


def lookup(query: str, *, timeout_s: int = 15) -> HNContext:
    """Best-effort HN lookup. Returns an empty HNContext on any failure."""
    if not query.strip():
        return HNContext()
    ctx = HNContext()
    try:
        stories = get_json(
            HN_API,
            params={"query": query, "tags": "story", "hitsPerPage": 5},
            timeout_s=timeout_s,
        )
        hits = sorted(stories.get("hits") or [], key=lambda h: int(h.get("points") or 0), reverse=True)
        if hits:
            top = hits[0]
            ctx.top_story_title = (top.get("title") or "").strip()
            ctx.top_story_url = (top.get("url") or top.get("story_url") or "").strip()
            ctx.top_story_points = int(top.get("points") or 0)
    except Exception:
        pass

    try:
        comments = get_json(
            HN_API,
            params={"query": query, "tags": "comment", "hitsPerPage": 5},
            timeout_s=timeout_s,
        )
        for c in (comments.get("hits") or [])[:3]:
            text = (c.get("comment_text") or "").strip()
            if text:
                ctx.discussion_snippets.append(text[:280])
    except Exception:
        pass

    return ctx
