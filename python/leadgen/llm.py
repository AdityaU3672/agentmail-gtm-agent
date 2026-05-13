"""Thin Claude wrapper that returns parsed JSON.

Stages call `json_completion(...)` with a prompt that instructs strict JSON output.
We strip code fences if Claude wraps the response, then parse.
"""

from __future__ import annotations

import json
import re

from anthropic import Anthropic


_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def json_completion(
    client: Anthropic,
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 1500,
) -> dict:
    """Call Claude and parse the response as JSON. Raises if invalid."""
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = next(
        (b.text for b in resp.content if getattr(b, "type", None) == "text"),
        "",
    ).strip()
    cleaned = _CODE_FENCE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude did not return valid JSON: {e}\n---\n{text[:500]}") from e
