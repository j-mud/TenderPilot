"""Two ways to run TenderPilot.

run_agent():    the LLM is the orchestrator. It sees the goal and the tools and
                decides what to call next (this is the autonomous mode to demo).
run_pipeline(): the same tools in a fixed order. Use it as a reliable fallback.
"""
from __future__ import annotations

import json

from .tools import TenderPilot, today

ORCHESTRATOR_SYSTEM = """You are TenderPilot, an autonomous bid-management agent for a Saudi technology company.
You own the process of finding the right tenders and preparing a submission package, end to end.

Work like an experienced bid manager:
0. If public listings are available, screen_listings first and mention which tenders are worth
   downloading in your final briefing.
1. list_tenders to see which full tender documents are available.
2. extract_requirements and qualify_tender for EVERY tender (skip nothing; problems hide in appendices).
3. rank_opportunities.
4. For the best BID or CONDITIONAL_BID tender: draft_proposal, then verify_proposal.
   If verification fails, call draft_proposal again (it revises using the issues), then verify again.
   Stop after 3 drafts at most.
5. For each blocker that only a human can resolve (renewing a certificate, confirming a partner,
   approving a price), call request_human_input with a specific, actionable question.
6. Call finish with a short briefing for the bid manager.

Rules: never invent company facts; never recommend submitting while a mandatory blocker is open;
keep your own messages short."""

TOOLS = [
    {"name": "screen_listings", "description": "Score public tender listings (from Etimad) to decide which full documents are worth downloading.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "list_tenders", "description": "List available tender document sets.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "extract_requirements", "description": "Read a tender and extract all requirements, deadline and value.",
     "parameters": {"type": "object", "properties": {"tender_id": {"type": "string"}}, "required": ["tender_id"]}},
    {"name": "qualify_tender", "description": "Check a tender's requirements against company evidence and decide BID / CONDITIONAL_BID / NO_BID.",
     "parameters": {"type": "object", "properties": {"tender_id": {"type": "string"}}, "required": ["tender_id"]}},
    {"name": "rank_opportunities", "description": "Rank all qualified tenders.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "draft_proposal", "description": "Write (or revise after failed verification) the proposal for a tender.",
     "parameters": {"type": "object", "properties": {"tender_id": {"type": "string"}}, "required": ["tender_id"]}},
    {"name": "verify_proposal", "description": "Check the latest draft against every requirement and the company files.",
     "parameters": {"type": "object", "properties": {"tender_id": {"type": "string"}}, "required": ["tender_id"]}},
    {"name": "request_human_input", "description": "Ask the bid manager a question only a human can answer. Recorded as an open action.",
     "parameters": {"type": "object", "properties": {"tender_id": {"type": "string"}, "question": {"type": "string"}},
                    "required": ["tender_id", "question"]}},
    {"name": "finish", "description": "End the run with a short briefing for the bid manager.",
     "parameters": {"type": "object", "properties": {"briefing": {"type": "string"}}, "required": ["briefing"]}},
]


def _summarize(name: str, result) -> str:
    """Compact tool output for the orchestrator (full objects stay in TenderPilot)."""
    if name == "screen_listings" and isinstance(result, list):
        return json.dumps(result[:15], ensure_ascii=False)
    if hasattr(result, "model_dump"):
        d = result.model_dump()
        if name == "extract_requirements":
            d = {k: d[k] for k in ("tender_id", "title", "deadline", "estimated_value_sar")} | {
                "requirements": len(d["requirements"]),
                "mandatory": sum(r["mandatory"] for r in d["requirements"]),
            }
        elif name == "qualify_tender":
            d = {k: d[k] for k in ("tender_id", "decision", "fit_score", "reasons", "blockers")}
        elif name == "draft_proposal":
            d = {"tender_id": d["tender_id"], "revision": d["revision"], "sections": [s["title"] for s in d["sections"]]}
        return json.dumps(d, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result


def run_agent(pilot: TenderPilot, goal: str, max_steps: int = 40) -> str:
    pilot.on_event("plan", f"Goal: {goal}")
    messages: list = [{"role": "user", "content": f"Today is {today().isoformat()}.\n\nGOAL: {goal}"}]
    for _ in range(max_steps):
        turn = pilot.llm.tool_turn(ORCHESTRATOR_SYSTEM, messages, TOOLS)
        messages.append(turn.raw_assistant)
        if turn.text.strip():
            pilot.on_event("think", turn.text.strip())
        if not turn.tool_calls:
            messages.append({"role": "user", "content": "Continue. Use the tools, and call finish when done."})
            continue
        results = []
        for call in turn.tool_calls:
            if call.name == "finish":
                briefing = call.args.get("briefing", "")
                pilot.on_event("finish", briefing)
                return briefing
            try:
                fn = getattr(pilot, call.name)
                output = _summarize(call.name, fn(**call.args))
            except Exception as err:  # report errors back so the agent can recover
                output = f"ERROR: {err}"
                pilot.on_event("error", f"{call.name}: {err}")
            results.append((call, output))
        messages.extend(pilot.llm.tool_results_message(results))
    pilot.on_event("finish", "Stopped: step limit reached.")
    return "Step limit reached."


def run_pipeline(pilot: TenderPilot, goal: str = "", max_revisions: int = 2) -> str:
    pilot.on_event("plan", "Running fixed pipeline: screen -> extract -> qualify -> rank -> draft -> verify")
    if pilot.listings:
        pilot.screen_listings()
    for t in pilot.list_tenders():
        pilot.qualify_tender(t["tender_id"])  # extracts first
    ranking = pilot.rank_opportunities()
    best = next((r for r in ranking if r["decision"] != "NO_BID"), None)
    if best is None:
        downloads = [x.reference for x in pilot.shortlist if x.recommend == "download"]
        msg = "No analyzed tender is worth bidding on right now."
        if downloads:
            msg += " From the listings, download the full documents for: " + ", ".join(downloads) + "."
        pilot.on_event("finish", msg)
        return msg
    tid = best["tender_id"]
    pilot.draft_proposal(tid)
    for _ in range(max_revisions + 1):
        if pilot.verify_proposal(tid).passed:
            break
        if pilot.drafts[tid].revision > max_revisions:
            break
        pilot.draft_proposal(tid)
    for r in ranking:
        if r["decision"] == "CONDITIONAL_BID":
            for b in r["blockers"]:
                pilot.request_human_input(r["tender_id"], f"Please resolve: {b}")
    report = pilot.verifications.get(tid)
    msg = (
        f"Recommended: {tid} ({best['decision']}, fit {best['fit_score']}). "
        f"Draft r{pilot.drafts[tid].revision} {'passed' if report and report.passed else 'has open issues'}; "
        f"{len(pilot.human_questions)} questions for the bid manager."
    )
    pilot.on_event("finish", msg)
    return msg
