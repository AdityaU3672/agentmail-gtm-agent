"""Append generated leads to prospects.csv.

Reuses the schema in `prospects.py` so the existing GTM agent picks them up.
Writes rows with status='draft' so the operator must promote to 'queued' before
they actually get sent. Dedupes by email.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# Reuse schema from sibling module without restructuring the existing layout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import prospects as _prospects  # noqa: E402


@dataclass
class Lead:
    email: str
    name: str
    role: str
    company: str
    hook: str


def append_drafts(leads: list[Lead]) -> tuple[int, int]:
    """Append leads to prospects.csv with status=draft. Returns (written, skipped_dupes)."""
    if not leads:
        return 0, 0

    existing = _prospects.load_all()
    seen = {r["email"].lower() for r in existing if r.get("email")}

    new_rows: list[dict] = []
    skipped = 0
    for lead in leads:
        key = (lead.email or "").lower().strip()
        if not key or key in seen:
            skipped += 1
            continue
        seen.add(key)
        new_rows.append({
            "email": lead.email.strip(),
            "name": lead.name.strip(),
            "role": lead.role.strip(),
            "company": lead.company.strip(),
            "hook": lead.hook.strip(),
            "status": "draft",
            "first_touch_at": "",
            "followup_at": "",
            "replied_at": "",
            "classification": "",
            "thread_id": "",
        })

    if new_rows:
        _prospects.save_all(existing + new_rows)
    return len(new_rows), skipped
