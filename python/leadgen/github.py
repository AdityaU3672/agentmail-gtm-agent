"""Stage 3 — GitHub discovery + champion picking.

Three discovery modes (combine freely):
  - topic       : repos tagged with a topic (proxies "building in this space")
  - stargazers  : users who starred / forked a seed repo (proxies "interested in this stack")
  - dependents  : repos depending on a seed package (intent: already using it)
                  GitHub's dependents view has no public REST API, so we approximate
                  by code-searching for import statements of the seed repo's package
                  name. It's a heuristic, not a complete list.

Per-org champion selection:
  We look at top contributors of the matched repo, then for each one fetch their
  public profile + recent commits to extract a real email (skipping noreply.github.com).
  If we can't find one for any contributor, the org is skipped (per public-only policy).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .config import LeadGenConfig
from .http import get_json


GITHUB_API = "https://api.github.com"
NOREPLY_RE = re.compile(r"@users\.noreply\.github\.com$", re.IGNORECASE)


@dataclass
class RepoSignal:
    full_name: str           # "owner/repo"
    description: str
    stars: int
    language: str
    pushed_at: str
    homepage: str            # often the org's marketing URL — useful for HN lookup
    topics: list[str] = field(default_factory=list)


@dataclass
class Champion:
    login: str
    name: str
    email: str
    role: str                # heuristic from bio / GitHub bio + repo role
    company: str
    company_domain: str
    blog_url: str
    bio: str


@dataclass
class OrgLead:
    org_login: str           # GitHub org/user that owns the matched repo
    repo: RepoSignal
    champion: Champion


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _headers(cfg: LeadGenConfig) -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": cfg.user_agent,
    }
    if cfg.github_token:
        h["Authorization"] = f"Bearer {cfg.github_token}"
    return h


def _gh(cfg: LeadGenConfig, path: str, **params) -> object:
    return get_json(
        f"{GITHUB_API}{path}",
        headers=_headers(cfg),
        params=params or None,
        timeout_s=cfg.request_timeout_s,
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def search_by_topic(cfg: LeadGenConfig, topic: str) -> list[RepoSignal]:
    """Search repos tagged with `topic`, sorted by stars desc."""
    q = f"topic:{topic} stars:>={cfg.min_repo_stars} pushed:>={_recent_iso(cfg.require_recent_push_days)}"
    data = _gh(cfg, "/search/repositories", q=q, sort="stars", order="desc",
               per_page=min(cfg.max_orgs_per_query * 2, 100))
    return [_repo_from(item) for item in (data.get("items") or [])]


def search_stargazers_seed(cfg: LeadGenConfig, seed_repo: str) -> list[str]:
    """Return logins of users who recently starred `owner/repo`. Capped by config.

    Returned logins are *individual users*, not orgs. We then look at their
    public repos to find an org affiliation.
    """
    data = _gh(cfg, f"/repos/{seed_repo}/stargazers",
               per_page=min(cfg.max_orgs_per_query, 100))
    if not isinstance(data, list):
        return []
    return [u["login"] for u in data if isinstance(u, dict) and u.get("type") == "User"]


def search_dependents_via_code(cfg: LeadGenConfig, seed_repo: str) -> list[RepoSignal]:
    """Approximate dependents via code search for the package name.

    GitHub's official dependents graph isn't in the REST API. We code-search
    for import lines, which catches the common case for npm/PyPI/Go modules.
    """
    package = seed_repo.split("/")[-1]
    queries = [
        f'"from {package}" language:python',
        f'"require(\\"{package}\\")" language:javascript',
        f'"\\"{package}\\":" filename:package.json',
    ]
    found: dict[str, RepoSignal] = {}
    for q in queries:
        try:
            data = _gh(cfg, "/search/code", q=q, per_page=30)
        except Exception:
            continue
        for item in (data.get("items") or []):
            repo = item.get("repository") or {}
            full = repo.get("full_name")
            if not full or full == seed_repo or full in found:
                continue
            found[full] = RepoSignal(
                full_name=full,
                description=(repo.get("description") or "").strip(),
                stars=int(repo.get("stargazers_count") or 0),
                language=(repo.get("language") or "").strip(),
                pushed_at=(repo.get("pushed_at") or "").strip(),
                homepage=(repo.get("homepage") or "").strip(),
                topics=list(repo.get("topics") or []),
            )
            if len(found) >= cfg.max_orgs_per_query:
                break
    return list(found.values())


# ---------------------------------------------------------------------------
# Champion selection
# ---------------------------------------------------------------------------


def pick_champion(cfg: LeadGenConfig, repo: RepoSignal) -> Champion | None:
    """Find a public-email-bearing contributor for the org owning this repo."""
    try:
        contributors = _gh(cfg, f"/repos/{repo.full_name}/contributors", per_page=10)
    except Exception:
        return None
    if not isinstance(contributors, list):
        return None

    for c in contributors:
        login = c.get("login")
        if not login or c.get("type") != "User":
            continue
        try:
            user = _gh(cfg, f"/users/{login}")
        except Exception:
            continue

        email = (user.get("email") or "").strip()
        if not email:
            email = _email_from_recent_commits(cfg, login)
        if not email or NOREPLY_RE.search(email):
            continue

        company_raw = (user.get("company") or "").strip().lstrip("@")
        return Champion(
            login=login,
            name=(user.get("name") or login).strip(),
            email=email,
            role=_guess_role(user.get("bio") or ""),
            company=company_raw or repo.full_name.split("/")[0],
            company_domain=_domain_from_blog(user.get("blog") or "") or _domain_from_email(email),
            blog_url=(user.get("blog") or "").strip(),
            bio=(user.get("bio") or "").strip(),
        )
    return None


def _email_from_recent_commits(cfg: LeadGenConfig, login: str) -> str:
    """Scan a user's recent push events for a non-noreply commit email."""
    try:
        events = _gh(cfg, f"/users/{login}/events/public", per_page=30)
    except Exception:
        return ""
    if not isinstance(events, list):
        return ""
    for ev in events:
        if ev.get("type") != "PushEvent":
            continue
        for commit in (ev.get("payload", {}).get("commits") or []):
            author = commit.get("author") or {}
            email = (author.get("email") or "").strip()
            if email and not NOREPLY_RE.search(email):
                return email
    return ""


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def collect_org_leads(cfg: LeadGenConfig) -> list[OrgLead]:
    """Run the configured discovery sources and return deduped OrgLead rows."""
    repos: dict[str, RepoSignal] = {}

    if "topic" in cfg.sources:
        for topic in cfg.topics:
            for r in search_by_topic(cfg, topic):
                repos.setdefault(r.full_name, r)

    if "dependents" in cfg.sources:
        for seed in cfg.dependents_of:
            for r in search_dependents_via_code(cfg, seed):
                repos.setdefault(r.full_name, r)

    if "stargazers" in cfg.sources:
        # Stargazers give us users; resolve them to their most-starred org repo.
        for seed in cfg.stargazers_of:
            for login in search_stargazers_seed(cfg, seed):
                top = _user_top_repo(cfg, login)
                if top:
                    repos.setdefault(top.full_name, top)

    out: list[OrgLead] = []
    for repo in _dedupe_by_org(repos.values()):
        if cfg.skip_personal_accounts and not _is_org(cfg, repo.full_name.split("/")[0]):
            continue
        champ = pick_champion(cfg, repo)
        if champ:
            out.append(OrgLead(
                org_login=repo.full_name.split("/")[0],
                repo=repo,
                champion=champ,
            ))
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _repo_from(item: dict) -> RepoSignal:
    return RepoSignal(
        full_name=item["full_name"],
        description=(item.get("description") or "").strip(),
        stars=int(item.get("stargazers_count") or 0),
        language=(item.get("language") or "").strip(),
        pushed_at=(item.get("pushed_at") or "").strip(),
        homepage=(item.get("homepage") or "").strip(),
        topics=list(item.get("topics") or []),
    )


def _dedupe_by_org(repos: Iterable[RepoSignal]) -> list[RepoSignal]:
    """Keep at most one repo per owning org — the one with the most stars."""
    best: dict[str, RepoSignal] = {}
    for r in repos:
        owner = r.full_name.split("/")[0]
        if owner not in best or r.stars > best[owner].stars:
            best[owner] = r
    return list(best.values())


def _user_top_repo(cfg: LeadGenConfig, login: str) -> RepoSignal | None:
    try:
        repos = _gh(cfg, f"/users/{login}/repos", sort="updated", per_page=10)
    except Exception:
        return None
    if not isinstance(repos, list) or not repos:
        return None
    repos.sort(key=lambda r: int(r.get("stargazers_count") or 0), reverse=True)
    return _repo_from(repos[0])


def _is_org(cfg: LeadGenConfig, login: str) -> bool:
    try:
        u = _gh(cfg, f"/users/{login}")
    except Exception:
        return False
    return u.get("type") == "Organization"


def _recent_iso(days: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


def _guess_role(bio: str) -> str:
    """Cheap heuristic; the LLM hook stage will refine using the same context."""
    bio_l = bio.lower()
    candidates = [
        ("cto", "CTO"), ("ceo", "CEO"), ("founder", "Founder"),
        ("vp eng", "VP Engineering"), ("head of eng", "Head of Engineering"),
        ("staff eng", "Staff Engineer"), ("principal eng", "Principal Engineer"),
        ("platform eng", "Platform Engineer"), ("devops", "DevOps Engineer"),
        ("ml eng", "ML Engineer"), ("data eng", "Data Engineer"),
        ("backend", "Backend Engineer"), ("frontend", "Frontend Engineer"),
        ("engineer", "Engineer"),
    ]
    for needle, label in candidates:
        if needle in bio_l:
            return label
    return "Maintainer"


def _domain_from_blog(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    try:
        from urllib.parse import urlparse
        return urlparse(u).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def _domain_from_email(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""
