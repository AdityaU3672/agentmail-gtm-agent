# GTM Agent — Python

A cold-outreach agent that personalizes the first touch, follows up after 4 days, classifies replies, and hands warm leads off to sales — without you babysitting it. Built on [AgentMail](https://agentmail.to) + Claude.

> **Polling vs webhooks.** This template polls AgentMail every 30s — zero infra, runs from your laptop. For production, switch to webhooks.

## Setup (5 minutes)

1. **Install**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure**
   ```bash
   cp .env.example .env
   ```
   Open `.env` and fill in:
   - `AGENTMAIL_API_KEY`, `ANTHROPIC_API_KEY`
   - `SENDER_NAME`, `SENDER_ROLE`, `SENDER_COMPANY` — used in the cold email's signoff and writer prompt
   - `SALES_EMAIL` — where interested-lead handoffs get forwarded

3. **Add prospects**
   ```bash
   cp prospects.example.csv prospects.csv
   ```
   Open `prospects.csv` and replace the example rows. Required columns: `email`, `name`, `role`, `company`, `hook`. Leave the rest blank — the agent fills them in.

   **The `hook` column is the most important field.** It's the specific signal Claude uses to personalize each email. Examples that work:
   - "Acme just announced a Series B and is hiring 5 sales reps"
   - "Beta Cloud launched last month, migrating from a legacy stack"
   - "Mentioned in latest Stratechery as the best example of usage-based pricing"

   Generic hooks ("Innovative growth-stage SaaS company") produce generic emails.

4. **Run**
   ```bash
   python agent.py
   ```
   On first run the agent creates a fresh inbox, then immediately works through every queued prospect in `prospects.csv`. Watch the terminal — each first-touch logs out as it sends.

## How it works

```
┌──────────────────┐
│  prospects.csv   │  ← you fill this
└─────────┬────────┘
          │ status='queued'
          ▼
┌──────────────────────────────────────────────────────────────┐
│  Polling loop (every 30s):                                   │
│    1. Send first-touch to queued prospects                   │
│    2. Send follow-up to prospects > 96h with no reply        │
│    3. Classify any new replies via Claude tool use:          │
│       - mark_interested  → forward to SALES_EMAIL            │
│       - mark_not_interested → stop                           │
│       - mark_ooo → pause                                     │
│       - mark_question → reply in-thread with suggested answer│
└──────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────┐  ┌──────────────────┐
│  prospects.csv   │  │   gtm_log.csv    │  audit log of every action
│  (status updates)│  │                  │
└──────────────────┘  └──────────────────┘
```

## Files

| File | What it does |
| --- | --- |
| `agent.py` | Polling loop. Sends touches, processes replies, schedules follow-ups. |
| `prompt.py` | Two prompts: WRITER (generates each cold email body) + CLASSIFIER (tool-driven reply handling). |
| `prospects.py` | CSV-backed prospect tracker + `gtm_log.csv` audit log helpers. |
| `prospects.example.csv` | Schema reference + sample rows. |
| `.env.example` | Copy to `.env`. |
| `leadgen.py` + `leadgen/` | **Generate** prospects from a product URL via GitHub + Hacker News. Writes draft rows for review. |

## Generating prospects automatically (dev-tool focus)

`leadgen.py` turns your product URL into draft `prospects.csv` rows by sourcing
developers from GitHub and enriching with Hacker News context.

Five-stage pipeline:

1. **Profile** your product (scrape landing page → structured profile via Claude).
2. **Synthesize ICP** (profile → GitHub topics + seed repos to mine).
3. **Source orgs** on GitHub via topic search, dependent-repo search, or stargazers of a seed repo.
4. **Enrich + hook** — pick the most active contributor with a public email, look up the org on HN, generate a one-line factual hook.
5. **Write** rows with `status=draft` so the sending loop ignores them until you promote to `queued`.

Setup:

```bash
# Add to .env (read-only is enough; lifts you from 60 to 5000 GitHub req/h)
GITHUB_TOKEN=ghp_...
```

Run:

```bash
# Auto: Claude picks GitHub topics from your URL
python leadgen.py --url https://yourtool.dev

# Manual override of discovery
python leadgen.py --url https://yourtool.dev \
    --topics ai-agents llm-agent \
    --stargazers-of langchain-ai/langgraph \
    --max 15

# Preview without touching the CSV
python leadgen.py --url https://yourtool.dev --dry-run --verbose

# Stage 3 prints discovery + per-org progress. Fully sequential GitHub calls:
python leadgen.py --url https://yourtool.dev --github-parallel 1
```

Stage 3 logs each topic/dependent/stargazer search, then each org resolution. By default it resolves orgs in **parallel** (bounded concurrency over a **single aiohttp `ClientSession`**, capped at **5** workers — override with `LEADGEN_GITHUB_PARALLEL` or `--github-parallel N`). That keeps TLS + connection pooling in one place; GitHub’s **hourly rate limit is unchanged**, so very high concurrency can increase **429** retries.

Hard rules baked into leadgen:

- **Public emails only.** GitHub `noreply` addresses are skipped; if no contributor on a repo has a real public email we drop the org.
- **Org-grain.** One champion per org by default (skip personal accounts; opt in with `--include-personal`).
- **Drafts.** New rows are written with `status=draft`. Open `prospects.csv`, sanity-check the hook, change `draft` → `queued`. The agent only sends when `status` is empty or `queued`.
- **HN is enrichment, not discovery.** We look up the prospect's company/domain on HN to feed the hook generator extra context; we do not scrape "Show HN" as a lead source in this version.

## Customize

- **Cold-email tone** — `prompt.py` WRITER_TEMPLATE. The current prompt avoids "I hope this email finds you well" / corporate-speak, asks for one specific next step. Tune to your voice.
- **Classification rules** — `prompt.py` CLASSIFIER_TEMPLATE. Default biases interested over question for warm-leads-with-questions; you can flip that.
- **Cadence** — `FOLLOWUP_AFTER_HOURS` in `.env`. Brief says 96h (4 days); 48-72h works for hot lists.
- **Subject lines** — `_subject_from_hook` in `agent.py`. Default: truncated hook. Replace with your own pattern.

## Hard rules baked in

- **Max two touches per prospect.** After the follow-up, status moves to `followed_up` and won't fire again.
- **Never follow up after a decline.** `mark_not_interested` sets status to `closed_lost`; ignored by the follow-up scanner.
- **Never reply to declines.** Per the brief — preserves goodwill.

## Beyond this template

### Switch to webhooks (recommended for production)

```python
client.webhooks.create(url=..., event_types=["message.received"])
```

### Other upgrades

- **Multi-touch sequences** — extend `FOLLOWUP_AFTER_HOURS` to a list (`[96, 168, 336]` for day-4, day-11, day-25 cadence) and track touch count.
- **A/B subject lines** — randomize between 2-3 patterns in `_subject_from_hook`, log which converts.
- **Enrichment** — for each prospect, hit Clearbit / Apollo / a public profile scraper at first-touch time to enrich the hook automatically.
- **Reply scoring** — extend the classifier with a `qualification_score` field and only hand off scores >= N to the sales team.
- **Send-window** — only send during business hours in the prospect's timezone.
