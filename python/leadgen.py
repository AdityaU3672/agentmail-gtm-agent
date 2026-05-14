"""leadgen — turn a product URL into draft prospects.csv rows.

Pipeline:
  1. Profile your product from a URL.
  2. Synthesize a GitHub-based ICP.
  3. Source candidate orgs (topic / dependents / stargazers — configurable).
  4. Pick a champion per org (public-email-only) and enrich with HN context.
  5. Append rows to prospects.csv with status='draft' for human review.

Examples:
    # Auto: let the LLM pick GitHub topics from your URL.
    python leadgen.py --url https://yourtool.dev

    # Manual: override discovery.
    python leadgen.py --url https://yourtool.dev \\
        --topics ai-agents llm-agent \\
        --stargazers-of langchain-ai/langgraph \\
        --max 15

    # Dry run: print what would be written, don't touch CSV.
    python leadgen.py --url https://yourtool.dev --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict

from anthropic import Anthropic
from dotenv import load_dotenv

from leadgen.config import LeadGenConfig
from leadgen.github import collect_org_leads
from leadgen.hn import lookup as hn_lookup
from leadgen.hook import generate as generate_hook
from leadgen.icp import synthesize as synthesize_icp
from leadgen.seller import fetch_profile
from leadgen.writer import Lead, append_drafts


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="Your product's landing page URL.")
    p.add_argument("--topics", nargs="*", default=None,
                   help="Override GitHub topics (otherwise inferred from your URL).")
    p.add_argument("--dependents-of", nargs="*", default=None,
                   help='Seed repos for "dependents" search, e.g. "langchain-ai/langchain".')
    p.add_argument("--stargazers-of", nargs="*", default=None,
                   help='Seed repos whose stargazers may be leads, e.g. "psf/black".')
    p.add_argument("--sources", nargs="*", default=None,
                   choices=["topic", "dependents", "stargazers"],
                   help="Which discovery modes to run (default: topic only).")
    p.add_argument("--max", type=int, default=25,
                   help="Max repos per discovery query (default 25).")
    p.add_argument("--min-stars", type=int, default=25, help="Skip repos under N stars.")
    p.add_argument("--recent-days", type=int, default=180,
                   help="Skip repos with no push in N days.")
    p.add_argument("--include-personal", action="store_true",
                   help="By default we skip user-owned (non-org) repos.")
    p.add_argument("--no-hn", action="store_true", help="Skip HN enrichment.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print leads to stdout instead of writing prospects.csv.")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument(
        "--github-parallel",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Concurrent GitHub org resolutions (default: env LEADGEN_GITHUB_PARALLEL or 5). "
            "Use 1 for fully sequential, easier logs. Higher = faster but more 429 risk."
        ),
    )
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    cfg = LeadGenConfig.from_env()
    cfg.max_orgs_per_query = args.max
    cfg.min_repo_stars = args.min_stars
    cfg.require_recent_push_days = args.recent_days
    cfg.skip_personal_accounts = not args.include_personal
    cfg.verbose = args.verbose
    if args.github_parallel is not None:
        cfg.github_parallel_workers = max(1, args.github_parallel)

    claude = Anthropic(api_key=cfg.anthropic_api_key)

    # --- Stage 1: seller profile -------------------------------------------
    print(f"[1/5] profiling {args.url} ...")
    seller = fetch_profile(
        claude, model=cfg.anthropic_model, url=args.url,
        user_agent=cfg.user_agent, timeout_s=cfg.request_timeout_s,
    )
    print(f"      company={seller.company!r} keywords={seller.keywords[:5]}")
    if args.verbose:
        print("      profile=", json.dumps(asdict(seller), indent=2))

    # --- Stage 2: ICP synthesis --------------------------------------------
    print("[2/5] synthesizing GitHub ICP ...")
    icp = synthesize_icp(claude, model=cfg.anthropic_model, profile=seller)
    if args.verbose:
        print("      icp=", json.dumps(asdict(icp), indent=2))

    # CLI overrides take precedence; otherwise use ICP suggestions.
    cfg.topics = args.topics if args.topics is not None else icp.github_topics
    cfg.dependents_of = args.dependents_of if args.dependents_of is not None else icp.dependents_of
    cfg.stargazers_of = args.stargazers_of if args.stargazers_of is not None else icp.stargazers_of
    cfg.sources = args.sources or _infer_sources(cfg)
    print(f"      sources={cfg.sources} topics={cfg.topics[:6]}")

    if not cfg.github_token:
        print("      ! GITHUB_TOKEN not set — running unauthenticated (60 req/h cap).")
    if cfg.github_parallel_workers > 1:
        print(f"      GitHub parallel workers: {cfg.github_parallel_workers}")

    # --- Stage 3: GitHub discovery + champion selection --------------------
    print("[3/5] discovering orgs on GitHub ...")
    org_leads = collect_org_leads(cfg)
    print(f"      {len(org_leads)} orgs with a contactable champion")

    if not org_leads:
        print("No leads found. Try widening: --min-stars 0 --recent-days 365 --include-personal,")
        print("or pass --topics / --stargazers-of explicitly.")
        return 0

    # --- Stage 4: HN enrichment + hook generation --------------------------
    print("[4/5] enriching + generating hooks ...")
    leads: list[Lead] = []
    for i, ol in enumerate(org_leads, 1):
        domain = ol.champion.company_domain or ""
        query = domain or ol.champion.company or ol.org_login
        hn_ctx = hn_lookup(query) if not args.no_hn else hn_lookup("")
        try:
            hook = generate_hook(
                claude, model=cfg.anthropic_model, seller=seller, lead=ol, hn=hn_ctx,
            )
        except Exception as e:
            print(f"      ! hook gen failed for {ol.org_login}: {e}")
            continue

        marker = "" if hook.quality_ok else " [LOW-QUALITY]"
        print(f"      {i:>2}. {ol.champion.email}  ({ol.org_login}){marker}")
        if args.verbose:
            print(f"          hook: {hook.hook}")

        leads.append(Lead(
            email=ol.champion.email,
            name=ol.champion.name or ol.champion.login,
            role=hook.role or ol.champion.role,
            company=ol.champion.company or _domain_to_company(domain) or ol.org_login,
            hook=hook.hook,
        ))
        time.sleep(cfg.sleep_between_calls_s)

    # --- Stage 5: write to prospects.csv -----------------------------------
    if args.dry_run:
        print("\n[5/5] dry run — would write:")
        for ld in leads:
            print(json.dumps(asdict(ld), ensure_ascii=False))
        return 0

    written, dupes = append_drafts(leads)
    print(f"[5/5] wrote {written} new draft rows to prospects.csv ({dupes} dupes skipped).")
    print("      Review them, then change status from 'draft' to 'queued' to enable sending.")
    return 0


def _infer_sources(cfg: LeadGenConfig) -> list[str]:
    """Pick reasonable sources based on what the ICP filled in."""
    out = []
    if cfg.topics:
        out.append("topic")
    if cfg.dependents_of:
        out.append("dependents")
    if cfg.stargazers_of:
        out.append("stargazers")
    return out or ["topic"]


def _domain_to_company(domain: str) -> str:
    if not domain:
        return ""
    host = domain.split(":")[0]
    parts = host.split(".")
    return parts[-2].capitalize() if len(parts) >= 2 else host.capitalize()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
