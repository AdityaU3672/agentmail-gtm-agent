"""Stage 4b — hook generator.

Combines:
  - seller profile (what we sell)
  - GitHub repo signal (what *they* are doing)
  - Champion bio + role
  - Optional HN context (what people are saying about them)

into a single short, factual `hook` sentence + a clean `role` string. We ask Claude
to be specific and refuse to invent details, then validate against a quality bar.
"""

from __future__ import annotations

from dataclasses import dataclass

from anthropic import Anthropic

from .github import OrgLead
from .hn import HNContext
from .llm import json_completion
from .seller import SellerProfile


@dataclass
class HookResult:
    hook: str
    role: str
    quality_ok: bool
    reason: str


_SYSTEM = """You write a single hook sentence for cold outreach. The hook will be
the FIRST line of an email to a developer. Output STRICT JSON:
{
  "hook": string,        // ONE sentence, <=25 words, factual + specific to THIS prospect
  "role": string,        // refined buyer title (e.g. "Maintainer of <repo>", "Platform Engineer at <co>")
  "quality_ok": boolean, // false if the supplied signals were too thin to be specific
  "reason": string       // 1 sentence explaining the hook (or why quality_ok=false)
}

Hook rules:
- Reference a SPECIFIC artifact: a repo name, a release, a topic, a comment, a stack choice.
- No generic praise. No "I came across your work". No "your impressive contributions".
- Never invent: only use facts present in the inputs.
- If the signals are weak (no description, no recent activity, no HN, generic bio),
  set quality_ok=false and write a still-best-effort hook so the operator can review.
- Do not mention our product by name; the rest of the email handles that.

JSON only, no prose."""


def generate(
    client: Anthropic,
    *,
    model: str,
    seller: SellerProfile,
    lead: OrgLead,
    hn: HNContext,
) -> HookResult:
    user = (
        f"## Our product\n"
        f"Company: {seller.company}\n"
        f"What we do: {seller.one_liner}\n"
        f"Target users: {seller.target_users}\n\n"
        f"## Their GitHub\n"
        f"Repo: {lead.repo.full_name}\n"
        f"Description: {lead.repo.description or '(none)'}\n"
        f"Stars: {lead.repo.stars}\n"
        f"Language: {lead.repo.language or '(unknown)'}\n"
        f"Topics: {lead.repo.topics}\n"
        f"Last pushed: {lead.repo.pushed_at}\n\n"
        f"## Champion\n"
        f"Name: {lead.champion.name}\n"
        f"Login: {lead.champion.login}\n"
        f"Bio: {lead.champion.bio or '(none)'}\n"
        f"Company field: {lead.champion.company or '(none)'}\n\n"
        f"## HN context (optional)\n"
        f"Top story: {hn.top_story_title or '(none)'} ({hn.top_story_points} pts)\n"
        f"Comment snippets: {hn.discussion_snippets or '(none)'}\n"
    )
    data = json_completion(client, model=model, system=_SYSTEM, user=user, max_tokens=400)
    return HookResult(
        hook=str(data.get("hook", "")).strip(),
        role=str(data.get("role", "")).strip() or lead.champion.role,
        quality_ok=bool(data.get("quality_ok", False)),
        reason=str(data.get("reason", "")).strip(),
    )
