"""Stage 1 — seller profiler.

Input:  product URL.
Output: structured profile (what they sell, who they sell to, value props, target devs).

We fetch the landing page (no JS execution), strip HTML, and ask Claude to
distill it. For dev-tool sites a single page is usually enough to seed
downstream stages.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import aiohttp
from anthropic import Anthropic

from .http import aget_text, make_aiohttp_connector
from .llm import json_completion


@dataclass
class SellerProfile:
    url: str
    company: str
    one_liner: str
    value_props: list[str]
    target_users: list[str]   # personas (e.g. "platform engineers", "ML infra teams")
    keywords: list[str]       # technical terms / categories useful for GitHub search
    competitors: list[str]    # optional, free-text


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(html: str, limit: int = 8000) -> str:
    text = _TAG.sub(" ", html)
    text = _WS.sub(" ", text).strip()
    return text[:limit]


_SYSTEM = """You are extracting a structured product profile from a startup's landing page.
Output STRICT JSON matching this schema:
{
  "company": string,                // brand name
  "one_liner": string,              // <=20 words, what the product does, no marketing fluff
  "value_props": [string, ...],     // 3-5 concrete benefits
  "target_users": [string, ...],    // personas (e.g. "platform engineers", "ML infra teams")
  "keywords": [string, ...],        // 5-12 technical terms useful as GitHub topic/repo search
  "competitors": [string, ...]      // 0-5 named alternatives if mentioned/inferable, else []
}
No prose, no markdown, JSON only."""


async def _fetch_html(url: str, user_agent: str, timeout_s: int) -> str:
    connector = make_aiohttp_connector(limit=10, limit_per_host=5)
    async with aiohttp.ClientSession(connector=connector) as session:
        return await aget_text(
            session, url, headers={"User-Agent": user_agent}, timeout_s=timeout_s,
        )


def fetch_profile(client: Anthropic, *, model: str, url: str, user_agent: str, timeout_s: int) -> SellerProfile:
    html = asyncio.run(_fetch_html(url, user_agent, timeout_s))
    body = _strip_html(html)
    user = f"URL: {url}\n\nVisible page text:\n{body}"
    data = json_completion(client, model=model, system=_SYSTEM, user=user)
    return SellerProfile(
        url=url,
        company=str(data.get("company", "")).strip(),
        one_liner=str(data.get("one_liner", "")).strip(),
        value_props=[str(x).strip() for x in data.get("value_props", []) if x],
        target_users=[str(x).strip() for x in data.get("target_users", []) if x],
        keywords=[str(x).strip() for x in data.get("keywords", []) if x],
        competitors=[str(x).strip() for x in data.get("competitors", []) if x],
    )
