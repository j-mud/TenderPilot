"""The agent's tools: each stage of the tender workflow.

The LLM does the reading and writing; plain Python guardrails check its work
(evidence must point to a real company file, expiring certificates are caught,
every mandatory requirement must appear in the proposal).
"""
from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Callable

from difflib import SequenceMatcher

from .documents import CompanyKnowledge, Tender, chunk_text, load_tenders, normalize_deadline
from .listings import load_listings
from .llm import LLM
from .schemas import (
    ChunkExtraction,
    ListingScore,
    ListingScreen,
    ProposalDraft,
    Requirement,
    Qualification,
    RequirementCheck,
    TenderExtraction,
    VerificationIssue,
    VerificationReport,
)
from .store import Store

EventFn = Callable[[str, str], None]
MAX_WORKERS = 5

ANALYST_SYSTEM = (
    "You are TenderPilot, a meticulous bid analyst for a Saudi technology company. "
    "You read government and enterprise tenders in Arabic and English. "
    "You never invent facts about the company: every claim must come from the company files provided. "
    "If evidence is missing, say so explicitly."
)


def _mentions(req_id: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(req_id)}(?![0-9])", text) is not None


def today() -> date:
    override = os.getenv("TENDERPILOT_TODAY")
    return date.fromisoformat(override) if override else date.today()


class TenderPilot:
    def __init__(self, llm: LLM, data_dir: Path, store: Store, on_event: EventFn | None = None,
                 company_dir: Path | None = None):
        self.llm = llm
        self.store = store
        self.data_dir = data_dir
        self.tenders: dict[str, Tender] = load_tenders(data_dir / "tenders")
        company_dir = company_dir or data_dir / "company"  # an uploaded tender is checked against a chosen company
        # Tools may run in worker threads: keep events in one piece, extract each tender once.
        event_fn, event_lock = on_event or (lambda kind, msg: None), threading.Lock()

        def locked_event(kind: str, msg: str) -> None:
            with event_lock:
                event_fn(kind, msg)

        self.on_event = locked_event
        self.thread_init: Callable[[], None] | None = None  # e.g. lets Streamlit write from workers
        self.on_stream: Callable[[str, str], None] | None = None  # (tender_id, text chunk) while a draft is written
        self._tender_locks = {tid: threading.Lock() for tid in self.tenders}
        self.company = CompanyKnowledge(company_dir)
        self.listings = load_listings(data_dir / "listings.csv")
        self.shortlist: list[ListingScore] = []
        self.extractions: dict[str, TenderExtraction] = {}
        self.qualifications: dict[str, Qualification] = {}
        self.drafts: dict[str, ProposalDraft] = {}
        self.verifications: dict[str, VerificationReport] = {}
        self.human_questions: list[dict] = []

    # ---------------------------------------------------------------- helpers
    def _emit(self, tender_id: str, step: str, msg: str) -> None:
        self.store.log(tender_id, step, msg)
        self.on_event(step, f"[{tender_id}] {msg}" if tender_id != "-" else msg)

    def _require(self, tender_id: str) -> None:
        if tender_id not in self.tenders:
            raise KeyError(f"Unknown tender_id '{tender_id}'. Known: {list(self.tenders)}")

    def parallel(self, fn: Callable, items: list) -> list:
        """fn(item) for every item, up to MAX_WORKERS at a time; results in input order."""
        if len(items) <= 1:
            return [fn(x) for x in items]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS, initializer=self.thread_init) as pool:
            return list(pool.map(fn, items))

    # ------------------------------------------------------------------ tools
    def list_tenders(self) -> list[dict]:
        out = []
        for tid, t in self.tenders.items():
            first = next((ln.strip("#* ").strip() for ln in t.text.splitlines() if "Title" in ln or "اسم المنافسة" in ln), "")
            out.append({"tender_id": tid, "title_line": (t.meta.get("title") or first)[:160],
                        "files": t.files, "chars": len(t.text)})
        self._emit("-", "discover", f"Found {len(out)} tender document sets")
        return out

    def screen_listings(self) -> list[dict]:
        """Score public tender listings (e.g. copied from Etimad) before downloading full documents."""
        if not self.listings:
            return [{"note": "No listings.csv in this data set."}]
        items: list[ListingScore] = []
        for i in range(0, len(self.listings), 40):
            batch = self.listings[i : i + 40]
            prompt = (
                f"COMPANY FILES:\n{self.company.context()}\n\n"
                f"PUBLIC TENDER LISTINGS (JSON):\n{json.dumps(batch, ensure_ascii=False, indent=1)}\n\n"
                f"Today is {today().isoformat()}. For EACH listing (use its 'reference'), score relevance to this "
                "company's capabilities 0-100 and recommend 'download' (worth getting the full documents), "
                "'maybe', or 'skip' (out of scope, deadline passed or too soon to prepare, far too large or small). "
                "Listings may be in Arabic; reasons in English."
            )
            items.extend(self.llm.structured(ANALYST_SYSTEM, prompt, ListingScreen).items)
        self.shortlist = sorted(items, key=lambda x: -x.relevance)
        n = sum(x.recommend == "download" for x in items)
        self._emit("-", "screen", f"Screened {len(items)} listings: {n} worth downloading")
        return [x.model_dump() for x in self.shortlist]

    def extract_requirements(self, tender_id: str) -> TenderExtraction:
        self._require(tender_id)
        with self._tender_locks[tender_id]:  # a parallel qualify of the same tender waits, then reuses it
            if tender_id in self.extractions:
                return self.extractions[tender_id]
            return self._extract(tender_id)

    def _extract(self, tender_id: str) -> TenderExtraction:
        tender = self.tenders[tender_id]
        chunks = chunk_text(tender.text)
        instructions = (
            "Extract every requirement a bidder must satisfy, including those hidden in appendices, "
            "annexes, general conditions and submission instructions (bid bonds, certificates, classification, "
            "experience, staffing, hosting, local content, deadlines). Translate Arabic into English. "
            "Split combined requirements into separate items (e.g. 'CR, Zakat, GOSI and Nitaqat' becomes four). "
            "Mark mandatory=true unless the document says desirable/optional. Skip generic legal boilerplate "
            "that applies to every contract unless it imposes a concrete bidder obligation. "
            "For the deadline, copy it exactly as written into deadline_original (it may be Hijri), "
            "and give the Gregorian YYYY-MM-DD in deadline if you can."
        )
        if len(chunks) == 1:
            prompt = f"Tender ID: {tender_id}\n\nTENDER DOCUMENT:\n{tender.text}\n\n{instructions} Use tender_id '{tender_id}'."
            result = self.llm.structured(ANALYST_SYSTEM, prompt, TenderExtraction)
        else:
            self._emit(tender_id, "extract", f"Long document ({len(tender.text):,} chars): reading in {len(chunks)} parts")
            prompts = [
                f"Tender ID: {tender_id}. This is PART {i} of {len(chunks)} of the tender pack.\n\n"
                f"{chunk}\n\n{instructions} Leave title/issuer/deadline/summary empty if not in this part."
                for i, chunk in enumerate(chunks, start=1)
            ]
            # parts are independent: read them in parallel, merge in document order
            parts = self.parallel(lambda p: self.llm.structured(ANALYST_SYSTEM, p, ChunkExtraction), prompts)
            result = self._merge_parts(tender_id, parts)
        result.tender_id = tender_id
        self._apply_meta(result, tender.meta)
        self.extractions[tender_id] = result
        self.store.upsert_opportunity(tender_id, result.title, result.deadline, status="analyzed")
        mandatory = sum(r.mandatory for r in result.requirements)
        self._emit(tender_id, "extract", f"{len(result.requirements)} requirements ({mandatory} mandatory), deadline {result.deadline or 'unknown'}")
        return result

    @staticmethod
    def _merge_parts(tender_id: str, parts: list[ChunkExtraction]) -> TenderExtraction:
        def first(attr):
            return next((getattr(p, attr) for p in parts if getattr(p, attr)), None)

        merged: list[Requirement] = []
        norm = lambda t: re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()  # noqa: E731
        for part in parts:
            for r in part.requirements:
                dup = next((m for m in merged if SequenceMatcher(None, norm(m.text), norm(r.text)).ratio() > 0.85), None)
                if dup:
                    dup.mandatory = dup.mandatory or r.mandatory
                    continue
                merged.append(r.model_copy())
        for i, r in enumerate(merged, start=1):
            r.id = f"R{i}"
        summaries = " ".join(p.summary for p in parts if p.summary)
        return TenderExtraction(
            tender_id=tender_id, title=first("title") or tender_id, issuer=first("issuer") or "",
            deadline=first("deadline") or "", deadline_original=first("deadline_original") or "",
            estimated_value_sar=first("estimated_value_sar"), summary=summaries[:600], requirements=merged,
        )

    def _apply_meta(self, ext: TenderExtraction, meta: dict) -> None:
        """Listing metadata (from Etimad) wins over model extraction; dates are normalized."""
        for key in ("title", "issuer", "estimated_value_sar"):
            if meta.get(key):
                setattr(ext, key, meta[key])
        raw = meta.get("deadline") or ext.deadline_original or ext.deadline
        greg, note = normalize_deadline(str(raw))
        if not greg and ext.deadline:  # fall back to the model's Gregorian answer
            greg, note = normalize_deadline(ext.deadline)
        ext.deadline = greg
        if note:
            self._emit(ext.tender_id, "extract", f"Deadline: {note}")

    def qualify_tender(self, tender_id: str) -> Qualification:
        self._require(tender_id)
        ext = self.extract_requirements(tender_id)
        expiring = self.company.expiring_before(ext.deadline)
        facts = {
            "today": today().isoformat(),
            "tender_deadline": ext.deadline,
            "certificates_expiring_before_deadline": [
                {"name": c["name"], "valid_until": c["valid_until"]} for c in expiring
            ],
            "certifications_not_held": self.company.not_held(),
            "valid_evidence_files": self.company.file_names(),
        }
        prompt = (
            f"TENDER REQUIREMENTS (JSON):\n{ext.model_dump_json(indent=1)}\n\n"
            f"COMPANY FILES:\n{self.company.context()}\n\n"
            f"VERIFIED FACTS:\n{json.dumps(facts, ensure_ascii=False, indent=1)}\n\n"
            "Check EVERY requirement against the company files and give one check per requirement id.\n"
            "- status 'met' ONLY if a company file proves it; put that file name in evidence.\n"
            "- A certificate that expires before the tender deadline is NOT met on the submission date: use 'gap'.\n"
            "- Anything in certifications_not_held is a 'gap'.\n"
            "- bid_manager_answers.md (if present) holds the bid manager's answers given after review: it is company "
            "evidence (cite it) and overrides older files where they conflict.\n"
            "- A certificate still valid on the deadline is 'met'; an expiry shortly after it is a note, not a gap.\n"
            "Requirements come in three kinds; only the first decides the bid:\n"
            "A. Eligibility and capability the company must already have (certificates, classification, past "
            "projects, years of experience, staff qualifications, being in scope).\n"
            "B. Delivery commitments the bidder promises in the proposal (features, SLAs, schedule, integrations, "
            "methodology). Without proof use 'partial' or 'unknown'; this is NOT a blocker unless the work is "
            "outside the company's scope or clearly beyond its capability.\n"
            "C. Submission formalities every bidder prepares (bid bond, envelopes, compliance matrix, forms, "
            "questions deadline, evaluation thresholds). Use 'unknown' with a note; this is NOT a blocker.\n"
            "Decision rules:\n"
            "- NO_BID if any mandatory type-A gap cannot realistically be fixed before the deadline "
            "(missing certifications/authorizations, years of experience), or a type-B item is out of scope.\n"
            "- CONDITIONAL_BID if every mandatory type-A gap can be fixed before the deadline (e.g. renewing a certificate).\n"
            "- BID if every mandatory type-A requirement is met.\n"
            "List every disqualifying or must-fix item in blockers, naming the requirement; never list type-B "
            "or type-C open items there. "
            "fit_score reflects strategic fit and win chance (0-100); give NO_BID tenders a score below 40."
        )
        q = self.llm.structured(ANALYST_SYSTEM, prompt, Qualification)
        q.tender_id = tender_id
        self._apply_guardrails(ext, q, expiring)
        self.qualifications[tender_id] = q
        self.store.upsert_opportunity(tender_id, ext.title, ext.deadline, q.decision, q.fit_score, status="qualified")
        self._emit(tender_id, "qualify", f"{q.decision} (fit {q.fit_score}/100). Blockers: {len(q.blockers)}")
        for b in q.blockers:
            self._emit(tender_id, "gap", b)
        return q

    def _apply_guardrails(self, ext: TenderExtraction, q: Qualification, expiring: list[dict]) -> None:
        """Deterministic checks that don't trust the model."""
        valid_files = set(self.company.file_names())
        # 0. Deadline: closed or unknown.
        if not ext.deadline:
            q.blockers.append("Submission deadline not found: confirm it on the Etimad tender page (add it to meta.json).")
            if q.decision == "BID":
                q.decision = "CONDITIONAL_BID"
        else:
            days_left = (date.fromisoformat(ext.deadline) - today()).days
            if days_left < 0:
                q.blockers.insert(0, f"Submission deadline {ext.deadline} has passed.")
                q.decision, q.fit_score = "NO_BID", min(q.fit_score, 10)
            elif days_left < 7 and q.decision != "NO_BID":
                q.blockers.append(f"Only {days_left} days until the deadline: confirm the team can deliver the bid in time.")
        # 1. No evidence, no 'met'.
        for c in q.checks:
            if c.status == "met" and not any(f in c.evidence for f in valid_files):
                c.status = "unknown"
                c.note = f"Downgraded by guardrail: no company file cited. {c.note}"
        # 2. Every requirement gets a check.
        checked = {c.requirement_id for c in q.checks}
        for r in ext.requirements:
            if r.id not in checked:
                q.checks.append(RequirementCheck(requirement_id=r.id, status="unknown", evidence="", note="Not assessed by model"))
        # 3. Certificates that expire before the deadline.
        for cert in expiring:
            kw = cert.get("keyword", "").lower()
            if not kw:
                continue
            hits = [r for r in ext.requirements if kw in r.text.lower() and r.mandatory]
            if not hits:
                continue
            for c in q.checks:
                if c.requirement_id in {r.id for r in hits} and c.status == "met":
                    c.status = "gap"
                    c.note = f"Guardrail: {cert['name']} expires {cert['valid_until']}, before deadline {ext.deadline}."
            if not any(kw in b.lower() for b in q.blockers):
                q.blockers.append(
                    f"{cert['name']} expires {cert['valid_until']}, before the {ext.deadline} deadline: renew before submitting."
                )
            if q.decision == "BID":
                q.decision = "CONDITIONAL_BID"

    def _ranking(self) -> list[dict]:
        order = {"BID": 0, "CONDITIONAL_BID": 1, "NO_BID": 2}
        ranked = sorted(self.qualifications.values(), key=lambda q: (order[q.decision], -q.fit_score, q.tender_id))
        return [
            {"tender_id": q.tender_id, "decision": q.decision, "fit_score": q.fit_score, "blockers": q.blockers}
            for q in ranked
        ]

    def rank_opportunities(self) -> list[dict]:
        out = self._ranking()
        if out:
            self._emit("-", "rank", "Ranking: " + ", ".join(f"{r['tender_id']} ({r['decision']}, {r['fit_score']})" for r in out))
        return out

    def draft_proposal(self, tender_id: str) -> ProposalDraft:
        self._require(tender_id)
        ext = self.extract_requirements(tender_id)
        q = self.qualifications.get(tender_id) or self.qualify_tender(tender_id)
        previous = self.drafts.get(tender_id)
        report = self.verifications.get(tender_id)
        revision_note = ""
        if previous and report and not report.passed:
            revision_note = (
                "\n\nTHIS IS A REVISION. The previous draft failed verification. Fix every issue:\n"
                + "\n".join(f"- {i.requirement_id}: {i.problem} -> {i.fix}" for i in report.issues)
                + f"\n\nPREVIOUS DRAFT:\n{previous.model_dump_json(indent=1)}"
            )
        prompt = (
            f"TENDER REQUIREMENTS:\n{ext.model_dump_json(indent=1)}\n\n"
            f"QUALIFICATION:\n{q.model_dump_json(indent=1)}\n\n"
            f"COMPANY FILES:\n{self.company.context()}\n\n"
            "Write a first-draft technical proposal with these sections, in this order:\n"
            "1. Executive summary\n2. Understanding of requirements\n3. Technical approach and methodology\n"
            "4. Relevant experience (cite past projects by name and value)\n5. Team\n"
            "6. Compliance statement (one line per requirement: '<id>: <how we comply or why not>')\n"
            "7. Open items for human review (every requirement with status gap/partial/unknown, by id)\n"
            "Never claim anything not supported by the company files. Where evidence is missing write "
            "'[TO CONFIRM: ...]'. Write in clear professional English."
            + revision_note
        )
        if self.on_stream and hasattr(self.llm, "stream_structured"):
            self._emit(tender_id, "draft", "Writing proposal" + (" revision" if previous else "") + "…")
            draft = self.llm.stream_structured(ANALYST_SYSTEM, prompt, ProposalDraft,
                                               lambda chunk: self.on_stream(tender_id, chunk))
        else:
            draft = self.llm.structured(ANALYST_SYSTEM, prompt, ProposalDraft)
        draft.tender_id = tender_id
        draft.revision = (previous.revision + 1) if previous else 1
        self.drafts[tender_id] = draft
        self.store.upsert_opportunity(tender_id, ext.title, ext.deadline, status=f"draft r{draft.revision}")
        self._emit(tender_id, "draft", f"Proposal draft r{draft.revision}: {len(draft.sections)} sections")
        return draft

    def verify_proposal(self, tender_id: str) -> VerificationReport:
        self._require(tender_id)
        ext = self.extractions[tender_id]
        q = self.qualifications[tender_id]
        draft = self.drafts.get(tender_id)
        if draft is None:
            raise ValueError("Draft the proposal before verifying it.")
        body = "\n\n".join(f"## {s.title}\n{s.content}" for s in draft.sections)
        issues: list[VerificationIssue] = []

        # Deterministic: every mandatory requirement is addressed by id.
        for r in ext.requirements:
            if r.mandatory and not _mentions(r.id, body):
                issues.append(VerificationIssue(requirement_id=r.id, problem="Mandatory requirement not addressed in the proposal", fix=f"Add a compliance line for {r.id}: {r.text}"))
        # Deterministic: every gap is surfaced for human review.
        open_section = next((s.content for s in draft.sections if "open item" in s.title.lower()), "")
        for c in q.checks:
            if c.status in {"gap", "partial", "unknown"} and not _mentions(c.requirement_id, open_section):
                issues.append(VerificationIssue(requirement_id=c.requirement_id, problem=f"Status '{c.status}' not listed in Open items", fix="List it under 'Open items for human review'"))

        # LLM critic: unsupported claims.
        critic_prompt = (
            f"REQUIREMENTS:\n{ext.model_dump_json(indent=1)}\n\nCOMPANY FILES:\n{self.company.context()}\n\n"
            f"PROPOSAL DRAFT:\n{body}\n\n"
            "Act as a strict bid reviewer. Find claims in the proposal that are NOT supported by the company files "
            "(invented certificates, numbers, projects or staff), and requirements answered incorrectly. "
            "Ignore text marked [TO CONFIRM]. Only report real problems; an empty issues list is fine. "
            f"Use tender_id '{tender_id}' and set passed=true if there are no issues."
        )
        critic = self.llm.structured(ANALYST_SYSTEM, critic_prompt, VerificationReport)
        issues.extend(critic.issues)

        report = VerificationReport(tender_id=tender_id, passed=not issues, issues=issues)
        self.verifications[tender_id] = report
        self._emit(tender_id, "verify", "PASSED" if report.passed else f"{len(issues)} issues found, revision needed")
        return report

    def request_human_input(self, tender_id: str, question: str) -> str:
        self.human_questions.append({"tender_id": tender_id, "question": question})
        self.store.add_action(tender_id, question, owner="human")
        self._emit(tender_id, "human", f"Question for the bid manager: {question}")
        return "Recorded for the bid manager. Do not assume the answer; continue with other work."

    def answer_question(self, index: int, answer: str) -> list[str]:
        """The bid manager answers question #index: audit it and add it to the company knowledge for this run."""
        h = self.human_questions[index]
        h["answer"] = answer.strip()
        self.store.answer_action(h["tender_id"], h["question"])
        self._emit(h["tender_id"], "answer", f"Bid manager: {h['answer']}")
        changes = self.company.add_answer(h["tender_id"], h["question"], h["answer"])
        for change in changes:
            self._emit(h["tender_id"], "knowledge", change)
        return changes

    def restore(self, results: dict) -> None:
        """Continue a saved run (results.json): its analysis, drafts, questions, and answers given so far."""
        self.extractions = {k: TenderExtraction.model_validate(v) for k, v in results.get("extractions", {}).items()}
        self.qualifications = {k: Qualification.model_validate(v) for k, v in results.get("qualifications", {}).items()}
        self.drafts = {k: ProposalDraft.model_validate(v) for k, v in results.get("drafts", {}).items()}
        self.verifications = {k: VerificationReport.model_validate(v) for k, v in results.get("verifications", {}).items()}
        self.shortlist = [ListingScore.model_validate(x) for x in results.get("shortlist", [])]
        self.human_questions = [dict(h) for h in results.get("human_questions", [])]
        for h in self.human_questions:
            if h.get("answer"):
                self.company.add_answer(h["tender_id"], h["question"], h["answer"])

    # ----------------------------------------------------------------- output
    def results(self) -> dict:
        return {
            "generated_on": today().isoformat(),
            "ranking": self._ranking(),
            "extractions": {k: v.model_dump() for k, v in self.extractions.items()},
            "qualifications": {k: v.model_dump() for k, v in self.qualifications.items()},
            "drafts": {k: v.model_dump() for k, v in self.drafts.items()},
            "verifications": {k: v.model_dump() for k, v in self.verifications.items()},
            "human_questions": self.human_questions,
            "shortlist": [x.model_dump() for x in self.shortlist],
            "listings": self.listings,
            "data_set": self.data_dir.name,
            "audit_log": self.store.rows("audit"),
            "actions": self.store.rows("actions"),
        }

    def save(self, out_dir: Path) -> Path:
        from .export import proposal_to_docx

        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "results.json"
        path.write_text(json.dumps(self.results(), ensure_ascii=False, indent=2), encoding="utf-8")
        for tid, draft in self.drafts.items():
            proposal_to_docx(draft, self.extractions[tid], self.qualifications[tid], out_dir / f"proposal_{tid}.docx")
        return path
