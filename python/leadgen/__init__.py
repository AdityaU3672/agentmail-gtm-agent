"""GitHub + HN powered lead generation for the AgentMail GTM agent.

Pipeline (5 stages):
  1. seller   — URL -> structured product profile (Claude).
  2. icp      — product profile -> GitHub search criteria (Claude).
  3. sourcing — GitHub search (topics / dependents / stargazers) -> candidate orgs.
  4. enrich   — pick a champion per org, attach HN signals, generate a one-line hook.
  5. write    — append unique rows to prospects.csv with status=draft.
"""
