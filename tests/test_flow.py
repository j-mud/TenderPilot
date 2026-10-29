"""Offline tests: python -m pytest tests  (or: python tests/test_flow.py)"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["TENDERPILOT_TODAY"] = "2026-10-30"

from agent.loop import run_agent, run_pipeline  # noqa: E402
from agent.store import Store  # noqa: E402
from agent.tools import TenderPilot  # noqa: E402
from tests.fake_llm import FakeLLM  # noqa: E402


SAMPLE = ROOT / "data" / "sample"


def make(tmp: Path, llm, data=SAMPLE) -> TenderPilot:
    return TenderPilot(llm, data, Store(tmp / "t.db"), on_event=lambda k, m: None)


def test_pipeline_end_to_end(tmp_path: Path):
    pilot = make(tmp_path, FakeLLM())
    run_pipeline(pilot)
    q = pilot.qualifications
    assert len(q) == 5
    # The naive model says BID for T104, but the guardrail catches the Zakat expiry.
    assert q["T104_archive_digitization_ar"].decision == "CONDITIONAL_BID"
    assert any("zakat" in b.lower() for b in q["T104_archive_digitization_ar"].blockers)
    assert q["T102_smart_city_platform"].decision == "NO_BID"  # CMMI in Appendix B
    assert q["T103_radiology_ai"].decision == "NO_BID"
    assert q["T105_school_construction"].decision == "NO_BID"
    assert q["T101_citizen_ai_assistant"].decision == "BID"
    # Draft forgot a requirement -> verify failed -> revised -> passed.
    assert pilot.drafts["T101_citizen_ai_assistant"].revision == 2
    assert pilot.verifications["T101_citizen_ai_assistant"].passed
    assert pilot.human_questions  # Zakat renewal question raised
    # Listing screen: AI listings recommended, others skipped.
    recs = {x.reference: x.recommend for x in pilot.shortlist}
    assert recs["L-2026-001"] == "download" and recs["L-2026-002"] == "skip"
    out = pilot.save(tmp_path / "out")
    assert out.exists() and (tmp_path / "out" / "proposal_T101_citizen_ai_assistant.docx").exists()


def test_guardrail_downgrades_met_without_evidence(tmp_path: Path):
    llm = FakeLLM()
    pilot = make(tmp_path, llm)
    original = llm._qualify

    def no_evidence(prompt):
        q = original(prompt)
        for c in q.checks:
            c.evidence = ""
        return q

    llm._qualify = no_evidence
    q = pilot.qualify_tender("T101_citizen_ai_assistant")
    assert all(c.status != "met" for c in q.checks)


def test_agent_loop_recovers_from_bad_tool_call(tmp_path: Path):
    t = "T101_citizen_ai_assistant"
    script = [
        [("list_tenders", {})],
        [("qualify_tender", {"tender_id": "does_not_exist"})],  # error is reported back, run continues
        [("qualify_tender", {"tender_id": t})],
        [("rank_opportunities", {})],
        [("draft_proposal", {"tender_id": t})],
        [("verify_proposal", {"tender_id": t})],
        [("draft_proposal", {"tender_id": t})],
        [("verify_proposal", {"tender_id": t})],
        [("finish", {"briefing": "Bid on T101."})],
    ]
    pilot = make(tmp_path, FakeLLM(script))
    assert run_agent(pilot, "test goal") == "Bid on T101."
    assert pilot.verifications[t].passed


def _make_pdf(path: Path, pages: list[str]) -> None:
    import pymupdf as fitz

    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 550, 800), text, fontsize=10)
    doc.save(path)


def test_real_style_pack_long_pdf_hijri(tmp_path: Path):
    """A multi-file PDF pack: long booklet, appendix with a hidden CMMI clause, Hijri deadline in meta.json."""
    import shutil

    import agent.documents as docs

    data = tmp_path / "data"
    shutil.copytree(SAMPLE / "company", data / "company")
    pack = data / "tenders" / "T201_pack"
    pack.mkdir(parents=True)
    filler = "General conditions. The contractor shall comply with applicable laws. " * 25
    booklet = ["Tender No. T201-2026\nTitle: Enterprise AI analytics platform\nSubmission deadline: see Etimad"]
    booklet += [f"Section {i}\n{filler}" for i in range(2, 30)]
    booklet.insert(3, "3.1 Valid Commercial Registration.\n3.2 ISO/IEC 27001 certification.")
    _make_pdf(pack / "booklet.pdf", booklet)
    _make_pdf(pack / "appendix_b.pdf", ["Appendix B\nB.1 Bidders must hold an active CMMI Level 3 appraisal. Mandatory."])
    (pack / "meta.json").write_text('{"deadline": "1448-06-24 هـ", "estimated_value_sar": 5000000}', encoding="utf-8")
    (data / "tenders" / "T202_closed.md").write_text(
        "Title: Old AI tender\nSubmission deadline: 2026-10-01\n3.1 Valid Commercial Registration.", encoding="utf-8")

    old = docs.CHUNK_CHARS
    docs.CHUNK_CHARS = 20_000  # force multi-part reading
    try:
        pilot = make(tmp_path, FakeLLM(), data)
        assert set(pilot.tenders) == {"T201_pack", "T202_closed"}
        ext = pilot.extract_requirements("T201_pack")
    finally:
        docs.CHUNK_CHARS = old
    assert len(docs.chunk_text(pilot.tenders["T201_pack"].text, 20_000)) > 1
    texts = [r.text for r in ext.requirements]
    assert any("CMMI" in t for t in texts), texts  # found in the second file
    assert [r.id for r in ext.requirements] == [f"R{i}" for i in range(1, len(texts) + 1)]
    assert ext.deadline.startswith("2026-12"), ext.deadline  # Hijri 1448-06-24 converted
    assert ext.estimated_value_sar == 5_000_000
    assert pilot.qualify_tender("T201_pack").decision == "NO_BID"
    closed = pilot.qualify_tender("T202_closed")
    assert closed.decision == "NO_BID" and "passed" in closed.blockers[0]


if __name__ == "__main__":
    import tempfile

    for fn in (test_pipeline_end_to_end, test_guardrail_downgrades_met_without_evidence,
               test_agent_loop_recovers_from_bad_tool_call, test_real_style_pack_long_pdf_hijri):
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
            print("PASS", fn.__name__)
