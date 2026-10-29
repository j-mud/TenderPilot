"""Score a TenderPilot run against answer_key.json.

    python eval.py                 # scores output/sample/results.json
    python eval.py path/to/results.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def _hit(keywords: list[str], texts: list[str]) -> bool:
    blob = "\n".join(texts).lower()
    for kw in keywords:
        k = kw.lower()
        if len(k) <= 3:  # short keywords like "CR" must match a whole word
            if re.search(rf"\b{re.escape(k)}\b", blob):
                return True
        elif k in blob:
            return True
    return False


def score(results: dict, key: dict) -> dict:
    rows, totals = [], {"decision": [0, 0], "requirements": [0, 0], "traps": [0, 0]}
    for tid, exp in key["tenders"].items():
        if tid not in results.get("extractions", {}):
            continue
        ext = results["extractions"].get(tid)
        q = results["qualifications"].get(tid)
        req_texts = [f"{r['text']} {r['source']}" for r in ext["requirements"]] if ext else []
        gap_texts = (q["blockers"] + [c["note"] for c in q["checks"] if c["status"] != "met"]) if q else []

        decision = q["decision"] if q else "—"
        dec_ok = decision == exp["expected_decision"]
        req_found = [r["name"] for r in exp["key_requirements"] if _hit(r["keywords"], req_texts)]
        req_missed = [r["name"] for r in exp["key_requirements"] if r["name"] not in req_found]
        traps_found = [t["name"] for t in exp["traps"] if _hit(t["keywords"], gap_texts)]

        totals["decision"][0] += dec_ok
        totals["decision"][1] += 1
        totals["requirements"][0] += len(req_found)
        totals["requirements"][1] += len(exp["key_requirements"])
        totals["traps"][0] += len(traps_found)
        totals["traps"][1] += len(exp["traps"])
        rows.append({
            "tender": tid, "expected": exp["expected_decision"], "got": decision, "decision_ok": dec_ok,
            "requirements": f"{len(req_found)}/{len(exp['key_requirements'])}", "missed": req_missed,
            "traps": f"{len(traps_found)}/{len(exp['traps'])}",
        })
    pct = {k: round(100 * a / b) if b else 100 for k, (a, b) in totals.items()}
    return {"rows": rows, "totals": totals, "percent": pct}


def main() -> None:
    results_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output" / "sample" / "results.json"
    results = json.loads(results_path.read_text(encoding="utf-8"))
    key = json.loads((ROOT / "answer_key.json").read_text(encoding="utf-8"))
    s = score(results, key)
    print(f"{'Tender':<32}{'Expected':<17}{'Got':<20}{'Reqs':<7}{'Traps':<6}")
    for r in s["rows"]:
        mark = "OK " if r["decision_ok"] else "XX "
        print(f"{r['tender']:<32}{r['expected']:<17}{mark + r['got']:<20}{r['requirements']:<7}{r['traps']:<6}")
        if r["missed"]:
            print(f"{'':<32}missed: {', '.join(r['missed'])}")
    p = s["percent"]
    print(f"\nDecision accuracy: {p['decision']}%   Requirement recall: {p['requirements']}%   Traps caught: {p['traps']}%")


if __name__ == "__main__":
    main()
