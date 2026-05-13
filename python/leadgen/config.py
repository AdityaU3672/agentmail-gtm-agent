"""Runtime configuration for the lead-gen pipeline.

Loaded from environment variables (.env) and per-run CLI flags.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class LeadGenConfig:
    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-4-6"

    # GitHub PAT recommended (5k req/h authed vs 60 unauth). Read-only scope is enough.
    github_token: str | None = None

    # Optional knobs — see CLI flags for the per-run versions.
    user_agent: str = "agentmail-gtm-leadgen/0.1 (+https://agentmail.to)"
    request_timeout_s: int = 20
    max_orgs_per_query: int = 25
    min_repo_stars: int = 25
    require_recent_push_days: int = 180
    skip_personal_accounts: bool = True
    sleep_between_calls_s: float = 0.25

    # Discovery sources to try (any subset of: topic, dependents, stargazers).
    sources: list[str] = field(default_factory=lambda: ["topic"])

    # Free-form inputs the user passes via CLI for each source.
    topics: list[str] = field(default_factory=list)
    dependents_of: list[str] = field(default_factory=list)  # "owner/repo"
    stargazers_of: list[str] = field(default_factory=list)  # "owner/repo"

    @classmethod
    def from_env(cls) -> "LeadGenConfig":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise SystemExit("ANTHROPIC_API_KEY required (see python/.env.example).")
        return cls(
            anthropic_api_key=key,
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            github_token=os.getenv("GITHUB_TOKEN"),
        )
