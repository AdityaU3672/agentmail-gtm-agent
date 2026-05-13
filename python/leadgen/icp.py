"""Stage 2 — ICP synthesizer.

Translates a seller profile into concrete GitHub search inputs:
  - github_topics : terms to feed `topic:<x>` searches (intent signal: building in this space)
  - dependents_of : packages whose dependents are good leads (intent signal: already using related infra)
  - stargazers_of : seed repos whose stargazers/forkers are likely to care
  - role_titles  : buyer titles to populate `role` column when we infer it from a contributor's bio

These are *suggestions* — the operator can override via CLI flags or a config file.
"""

from __future__ import annotations

from dataclasses import dataclass

from anthropic import Anthropic

from .llm import json_completion
from .seller import SellerProfile


@dataclass
class ICP:
    github_topics: list[str]
    dependents_of: list[str]   # "owner/repo"
    stargazers_of: list[str]   # "owner/repo"
    role_titles: list[str]


_SYSTEM = """You are designing a GitHub-based prospecting strategy for a developer-tool company.
Given the product profile, propose concrete search inputs. Output STRICT JSON:
{
  "github_topics": [string, ...],     // 3-8 GitHub topic slugs (lowercase, hyphenated)
  "dependents_of": [string, ...],     // 0-5 "owner/repo" whose dependent repos are likely buyers
  "stargazers_of": [string, ...],     // 0-5 "owner/repo" whose stargazers are likely buyers
  "role_titles": [string, ...]        // 3-6 plausible buyer titles (e.g. "Platform Engineer")
}
Rules:
- Only include "owner/repo" slugs you are highly confident exist publicly on GitHub.
- Prefer specific, recent ecosystem repos over generic / abandoned ones.
- Topics should be terms developers actually tag repos with.
JSON only, no prose."""


def synthesize(client: Anthropic, *, model: str, profile: SellerProfile) -> ICP:
    user = (
        f"Company: {profile.company}\n"
        f"One-liner: {profile.one_liner}\n"
        f"Value props: {profile.value_props}\n"
        f"Target users: {profile.target_users}\n"
        f"Keywords: {profile.keywords}\n"
        f"Competitors: {profile.competitors}\n"
    )
    data = json_completion(client, model=model, system=_SYSTEM, user=user)
    return ICP(
        github_topics=[str(x).strip().lower() for x in data.get("github_topics", []) if x],
        dependents_of=[str(x).strip() for x in data.get("dependents_of", []) if "/" in str(x)],
        stargazers_of=[str(x).strip() for x in data.get("stargazers_of", []) if "/" in str(x)],
        role_titles=[str(x).strip() for x in data.get("role_titles", []) if x],
    )
