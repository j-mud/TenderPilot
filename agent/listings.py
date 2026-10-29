"""Public tender listings (e.g. rows copied from the Etimad tenders page).

listings.csv columns (header row required; Arabic text is fine):
    reference, title, entity, activity, deadline, value_sar, url
Only 'reference' and 'title' are required. Save the file as UTF-8 CSV.
"""
from __future__ import annotations

import csv
from pathlib import Path


def load_listings(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = [
            {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            for row in csv.DictReader(f)
        ]
    return [r for r in rows if r.get("reference") and r.get("title")]
