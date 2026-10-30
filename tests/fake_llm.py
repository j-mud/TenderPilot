"""A fake model for offline tests: no API key, deterministic answers.

It is deliberately naive (e.g. it claims every certificate is 'met') so the
tests can prove the guardrails catch what a careless model would miss.
"""
from __future__ import annotations

import json
import re

from agent.llm import ToolCall, TurnResult
from agent.schemas import (ChunkExtraction, ListingScore, ListingScreen, ProposalDraft, Qualification,
                           TenderExtraction, VerificationReport)

REQ_LINE = re.compile(r"^\s*([0-9]+\.[0-9]+|[A-Z]\.[0-9]+)\s+(.+)$")
BAD = ["cmmi", "sfda", "13485", "ten (10)", "classification", "iot"]


class FakeLLM:
    provider = "fake"
    model = "fake"

    def __init__(self, script: list[list[tuple[str, dict]]] | None = None):
        self.script = list(script or [])
        self.draft_calls = 0

    def structured(self, system, prompt, schema):
        if schema is TenderExtraction:
            return self._extract(prompt)
        if schema is ChunkExtraction:
            ext = self._extract(prompt)
            first_part = "PART 1 of" in prompt
            return ChunkExtraction(title=ext.title if first_part else "", deadline=ext.deadline if first_part else "",
                                   requirements=ext.requirements)
        if schema is ListingScreen:
            rows = json.loads(prompt.split("(JSON):\n", 1)[1].split("\n\nToday is", 1)[0])
            return ListingScreen(items=[ListingScore(
                reference=r["reference"], relevance=80 if "AI" in r["title"] or "ذكاء" in r["title"] else 10,
                recommend="download" if "AI" in r["title"] or "ذكاء" in r["title"] else "skip", reason="fake")
                for r in rows])
        if schema is Qualification:
            return self._qualify(prompt)
        if schema is ProposalDraft:
            return self._draft(prompt)
        if schema is VerificationReport:
            tid = re.search(r"tender_id '([^']+)'", prompt).group(1)
            return VerificationReport(tender_id=tid, passed=True, issues=[])
        raise NotImplementedError(schema)

    def stream_structured(self, system, prompt, schema, on_text):
        result = self.structured(system, prompt, schema)
        text = result.model_dump_json()
        for i in range(0, len(text), 40):
            on_text(text[i : i + 40])
        return result

    def _extract(self, prompt):
        tid = re.search(r"Tender ID: ([A-Za-z0-9_\-]+)", prompt).group(1)
        m = re.search(r"(\d{4}-\d{2}-\d{2})", prompt)
        deadline = m.group(1) if m else ""
        reqs, n = [], 0
        for line in prompt.splitlines():
            m = REQ_LINE.match(line)
            if m:
                n += 1
                reqs.append({"id": f"R{n}", "category": "eligibility", "text": m.group(2)[:200],
                             "mandatory": "desirable" not in line.lower(), "source": f"Section {m.group(1)}"})
        return TenderExtraction.model_validate({
            "tender_id": tid, "title": tid.replace("_", " "), "issuer": "Fictional authority",
            "deadline": deadline, "estimated_value_sar": 1_000_000, "summary": "Fake summary.", "requirements": reqs})

    def _qualify(self, prompt):
        data = json.loads(prompt.split("TENDER REQUIREMENTS (JSON):\n", 1)[1].split("\n\nCOMPANY FILES:", 1)[0])
        checks, blockers = [], []
        for r in data["requirements"]:
            bad = any(b in r["text"].lower() for b in BAD)
            checks.append({"requirement_id": r["id"], "status": "gap" if bad else "met",
                           "evidence": "" if bad else "certifications.json", "note": "fake"})
            if bad and r["mandatory"]:
                blockers.append(f"{r['id']}: {r['text'][:80]}")
        return Qualification.model_validate({
            "tender_id": data["tender_id"], "fit_score": 30 if blockers else 85,
            "decision": "NO_BID" if blockers else "BID", "reasons": ["fake reason"],
            "blockers": blockers, "checks": checks})

    def _draft(self, prompt):
        self.draft_calls += 1
        data = json.loads(prompt.split("TENDER REQUIREMENTS:\n", 1)[1].split("\n\nQUALIFICATION:", 1)[0])
        q = json.loads(prompt.split("QUALIFICATION:\n", 1)[1].split("\n\nCOMPANY FILES:", 1)[0])
        ids = [r["id"] for r in data["requirements"]]
        if self.draft_calls == 1:
            ids = ids[:-1]  # first draft "forgets" one requirement, to exercise the revise loop
        open_ids = [c["requirement_id"] for c in q["checks"] if c["status"] != "met"]
        return ProposalDraft.model_validate({"tender_id": data["tender_id"], "sections": [
            {"title": "Executive summary", "content": "Fake."},
            {"title": "Compliance statement", "content": "\n".join(f"{i}: complies" for i in ids)},
            {"title": "Open items for human review", "content": "\n".join(open_ids) or "None"}]})

    # scripted orchestrator
    def tool_turn(self, system, messages, tools):
        step = self.script.pop(0) if self.script else [("finish", {"briefing": "done"})]
        calls = [ToolCall(f"c{len(messages)}_{i}", name, args) for i, (name, args) in enumerate(step)]
        return TurnResult("", calls, {"role": "assistant", "content": "(tool calls)"})

    def tool_results_message(self, results):
        return [{"role": "user", "content": "\n".join(out for _, out in results)}]
