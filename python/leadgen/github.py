"""Stage 3 — GitHub discovery + champion picking (aiohttp, one shared session).

Discovery modes: topic, stargazers, dependents (code search heuristic).
Champion: top contributors with a public email (no noreply.github.com).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Iterable

import aiohttp

from .config import LeadGenConfig
from .http import aget_json, make_aiohttp_connector


GITHUB_API = "https://api.github.com"
NOREPLY_RE = re.compile(r"@users\.noreply\.github\.com$", re.IGNORECASE)


@dataclass
class RepoSignal:
    full_name: str
    description: str
    stars: int
    language: str
    pushed_at: str
    homepage: str
    topics: list[str] = field(default_factory=list)


@dataclass
class Champion:
    login: str
    name: str
    email: str
    role: str
    company: str
    company_domain: str
    blog_url: str
    bio: str


@dataclass
class OrgLead:
    org_login: str
    repo: RepoSignal
    champion: Champion


def _headers(cfg: LeadGenConfig) -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": cfg.user_agent,
    }
    if cfg.github_token:
        h["Authorization"] = f"Bearer {cfg.github_token}"
    return h


async def _gh(
    session: aiohttp.ClientSession,
    cfg: LeadGenConfig,
    path: str,
    **params: object,
) -> object:
    return await aget_json(
        session,
        f"{GITHUB_API}{path}",
        params={k: v for k, v in params.items() if v is not None},
        timeout_s=cfg.request_timeout_s,
    )


# --- discovery --------------------------------------------------------------


async def search_by_topic(
    session: aiohttp.ClientSession, cfg: LeadGenConfig, topic: str,
) -> list[RepoSignal]:
    q = f"topic:{topic} stars:>={cfg.min_repo_stars} pushed:>={_recent_iso(cfg.require_recent_push_days)}"
    data = await _gh(
        session, cfg, "/search/repositories",
        q=q, sort="stars", order="desc",
        per_page=min(cfg.max_orgs_per_query * 2, 100),
    )
    return [_repo_from(item) for item in (data.get("items") or [])]


async def search_stargazers_seed(
    session: aiohttp.ClientSession, cfg: LeadGenConfig, seed_repo: str,
) -> list[str]:
    data = await _gh(
        session, cfg, f"/repos/{seed_repo}/stargazers",
        per_page=min(cfg.max_orgs_per_query, 100),
    )
    if not isinstance(data, list):
        return []
    return [u["login"] for u in data if isinstance(u, dict) and u.get("type") == "User"]


async def search_dependents_via_code(
    session: aiohttp.ClientSession, cfg: LeadGenConfig, seed_repo: str,
) -> list[RepoSignal]:
    package = seed_repo.split("/")[-1]
    queries = [
        f'"from {package}" language:python',
        f'"require(\\"{package}\\")" language:javascript',
        f'"\\"{package}\\":" filename:package.json',
    ]
    found: dict[str, RepoSignal] = {}
    for q in queries:
        try:
            data = await _gh(session, cfg, "/search/code", q=q, per_page=30)
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


# --- champion ---------------------------------------------------------------


async def _email_from_recent_commits(
    session: aiohttp.ClientSession, cfg: LeadGenConfig, login: str,
) -> str:
    try:
        events = await _gh(session, cfg, f"/users/{login}/events/public", per_page=30)
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


async def pick_champion(
    session: aiohttp.ClientSession, cfg: LeadGenConfig, repo: RepoSignal,
) -> Champion | None:
    try:
        contributors = await _gh(session, cfg, f"/repos/{repo.full_name}/contributors", per_page=10)
    except Exception:
        return None
    if not isinstance(contributors, list):
        return None

    for c in contributors:
        login = c.get("login")
        if not login or c.get("type") != "User":
            continue
        try:
            user = await _gh(session, cfg, f"/users/{login}")
        except Exception:
            continue

        email = (user.get("email") or "").strip()
        if not email:
            email = await _email_from_recent_commits(session, cfg, login)
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


async def _is_org(session: aiohttp.ClientSession, cfg: LeadGenConfig, login: str) -> bool:
    try:
        u = await _gh(session, cfg, f"/users/{login}")
    except Exception:
        return False
    return u.get("type") == "Organization"


async def _user_top_repo(
    session: aiohttp.ClientSession, cfg: LeadGenConfig, login: str,
) -> RepoSignal | None:
    try:
        repos = await _gh(session, cfg, f"/users/{login}/repos", sort="updated", per_page=10)
    except Exception:
        return None
    if not isinstance(repos, list) or not repos:
        return None
    repos.sort(key=lambda r: int(r.get("stargazers_count") or 0), reverse=True)
    return _repo_from(repos[0])


# --- orchestration ----------------------------------------------------------


async def _try_org_lead(
    session: aiohttp.ClientSession, cfg: LeadGenConfig, repo: RepoSignal,
) -> OrgLead | None:
    owner = repo.full_name.split("/")[0]
    if cfg.skip_personal_accounts and not await _is_org(session, cfg, owner):
        return None
    champ = await pick_champion(session, cfg, repo)
    if not champ:
        return None
    return OrgLead(org_login=owner, repo=repo, champion=champ)


async def _collect_sequential_async(
    session: aiohttp.ClientSession, cfg: LeadGenConfig, deduped: list[RepoSignal],
) -> list[OrgLead]:
    out: list[OrgLead] = []
    total = len(deduped)
    for i, repo in enumerate(deduped, 1):
        print(f"      [{i}/{total}] resolving {repo.full_name} ...", flush=True)
        try:
            ol = await _try_org_lead(session, cfg, repo)
        except Exception as e:
            print(f"      ... error: {e}", flush=True)
            continue
        if ol:
            print(f"      ... champion {ol.champion.email} ({ol.champion.login})", flush=True)
            out.append(ol)
        else:
            print("      ... skipped (not an org / no public email on top contributors)", flush=True)
    return out


async def _collect_parallel_async(
    session: aiohttp.ClientSession, cfg: LeadGenConfig, deduped: list[RepoSignal],
) -> list[OrgLead]:
    total = len(deduped)
    sem = asyncio.Semaphore(cfg.github_parallel_workers)
    lock = asyncio.Lock()
    done = 0

    async def run_one(repo: RepoSignal) -> OrgLead | None:
        nonlocal done
        async with sem:
            try:
                ol = await _try_org_lead(session, cfg, repo)
            except Exception as e:
                async with lock:
                    done += 1
                    print(f"      [{done}/{total}] {repo.full_name} → error: {e}", flush=True)
                return None
            async with lock:
                done += 1
                if ol:
                    print(
                        f"      [{done}/{total}] {repo.full_name} → champion {ol.champion.email} "
                        f"({ol.champion.login})",
                        flush=True,
                    )
                else:
                    print(
                        f"      [{done}/{total}] {repo.full_name} → skipped "
                        "(not an org / no public email on top contributors)",
                        flush=True,
                    )
            return ol

    results = await asyncio.gather(*(run_one(r) for r in deduped))
    return [x for x in results if x is not None]


async def collect_org_leads_async(cfg: LeadGenConfig) -> list[OrgLead]:
    """Full stage 3: discovery + champion resolution with one aiohttp session."""
    connector = make_aiohttp_connector(
        limit=max(100, cfg.github_parallel_workers * 20),
        limit_per_host=max(30, cfg.github_parallel_workers * 10),
    )
    async with aiohttp.ClientSession(connector=connector, headers=_headers(cfg)) as session:
        repos: dict[str, RepoSignal] = {}

        print("      discovery …", flush=True)
        if "topic" in cfg.sources:
            for topic in cfg.topics:
                print(f"      topic search: {topic!r} …", flush=True)
                n_before = len(repos)
                for r in await search_by_topic(session, cfg, topic):
                    repos.setdefault(r.full_name, r)
                print(f"      … +{len(repos) - n_before} repos (cumulative {len(repos)} keys)", flush=True)

        if "dependents" in cfg.sources:
            for seed in cfg.dependents_of:
                print(f"      dependents (code search): {seed!r} …", flush=True)
                n_before = len(repos)
                for r in await search_dependents_via_code(session, cfg, seed):
                    repos.setdefault(r.full_name, r)
                print(f"      … +{len(repos) - n_before} repos (cumulative {len(repos)} keys)", flush=True)

        if "stargazers" in cfg.sources:
            for seed in cfg.stargazers_of:
                logins = await search_stargazers_seed(session, cfg, seed)
                print(
                    f"      stargazers: {seed!r} → {len(logins)} users to map to repos …",
                    flush=True,
                )
                n_before = len(repos)
                for j, login in enumerate(logins, 1):
                    if cfg.verbose and j % 5 == 0:
                        print(f"      … user {j}/{len(logins)}", flush=True)
                    top = await _user_top_repo(session, cfg, login)
                    if top:
                        repos.setdefault(top.full_name, top)
                print(f"      … +{len(repos) - n_before} repos (cumulative {len(repos)} keys)", flush=True)

        deduped = list(_dedupe_by_org(repos.values()))
        print(
            f"      dedupe by owner: {len(repos)} repo keys → {len(deduped)} unique owners",
            flush=True,
        )
        if not deduped:
            return []

        workers = cfg.github_parallel_workers
        print(
            f"      resolving champions ({len(deduped)} owners, "
            f"{'sequential' if workers <= 1 else f'parallel aiohttp workers={workers}'}) …",
            flush=True,
        )

        if workers <= 1:
            return await _collect_sequential_async(session, cfg, deduped)
        return await _collect_parallel_async(session, cfg, deduped)


def collect_org_leads(cfg: LeadGenConfig) -> list[OrgLead]:
    """Sync entrypoint for leadgen CLI (runs asyncio event loop)."""
    return asyncio.run(collect_org_leads_async(cfg))


# --- internals --------------------------------------------------------------


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
    best: dict[str, RepoSignal] = {}
    for r in repos:
        owner = r.full_name.split("/")[0]
        if owner not in best or r.stars > best[owner].stars:
            best[owner] = r
    return list(best.values())


def _recent_iso(days: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


def _guess_role(bio: str) -> str:
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
