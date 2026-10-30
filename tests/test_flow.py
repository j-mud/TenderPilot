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


def test_agent_runs_same_turn_tool_calls_in_parallel(tmp_path: Path):
    import threading

    llm = FakeLLM()
    pilot = make(tmp_path, llm)
    extracted, threads, original = [], set(), llm.structured

    def tracking(system, prompt, schema):
        threads.add(threading.get_ident())
        if schema.__name__ == "TenderExtraction":
            extracted.append(prompt)
        return original(system, prompt, schema)

    llm.structured = tracking
    tids = list(pilot.tenders)
    # extract + qualify of the same tender in one turn must extract it only once
    llm.script = [[("extract_requirements", {"tender_id": tids[0]})] + [("qualify_tender", {"tender_id": t}) for t in tids],
                  [("finish", {"briefing": "done"})]]
    run_agent(pilot, "test goal")
    assert set(pilot.qualifications) == set(tids)
    assert len(extracted) == len(tids)
    assert len(threads) > 1
    assert sum(r["step"] == "qualify" for r in pilot.store.rows("audit")) == len(tids)


def test_draft_streams_and_falls_back(tmp_path: Path):
    from agent.llm import LLM
    from agent.schemas import ProposalDraft

    t = "T101_citizen_ai_assistant"
    pilot = make(tmp_path, FakeLLM())
    chunks = []
    pilot.on_stream = lambda tid, chunk: chunks.append((tid, chunk))
    draft = pilot.draft_proposal(t)
    assert chunks and all(tid == t for tid, _ in chunks)
    assert ProposalDraft.model_validate_json("".join(c for _, c in chunks)).sections == draft.sections

    # The real wrapper: a broken stream (error or bad JSON) falls back to the normal call.
    good = '{"tender_id": "T", "sections": [{"title": "Executive summary", "content": "Hi"}]}'
    llm = LLM.__new__(LLM)
    llm.complete = lambda system, prompt, max_tokens=0: good
    seen = []

    def streams(text):
        def fake(system, prompt, on_text, max_tokens=0):
            on_text(text[:10])
            if text == "boom":
                raise ConnectionError("stream dropped")
            return text
        return fake

    for streamed in (good, "boom", '{"tender_id": "T", "sections": [{"title": '):
        llm.stream_complete = streams(streamed)
        assert llm.stream_structured("sys", "prompt", ProposalDraft, seen.append).sections[0].content == "Hi"
    assert seen


def test_run_budget_limits_live_runs(tmp_path: Path):
    from agent.run_budget import RunBudget

    budget = RunBudget(tmp_path / "budget.json", max_per_day=2, cooldown_s=120, max_run_s=600)
    t = 1_800_000_000  # a fixed moment, mid-day UTC
    assert budget.try_start(t) is None
    assert "in progress" in budget.try_start(t + 10)  # one live run at a time
    budget.finish(t + 90)
    assert "wait" in budget.try_start(t + 100)  # still inside the 2-minute cooldown
    assert budget.try_start(t + 130) is None
    assert budget.remaining(t + 131) == 0
    budget.finish(t + 300)
    assert "daily limit" in budget.try_start(t + 400)
    day2 = t + 86_400
    assert budget.try_start(day2) is None  # next UTC day: count resets
    assert "in progress" in budget.try_start(day2 + 300)  # that run never reports finishing...
    assert budget.try_start(day2 + 601) is None  # ...so it stops blocking after max_run_s


def test_app_password_gate_and_deployed_defaults():
    from streamlit.testing.v1 import AppTest

    local = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not local.exception and not local.text_input  # no secret: no gate
    assert "my_company" in local.selectbox[0].options and not local.toggle[0].value

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    app.secrets["APP_PASSWORD"] = "open sesame"
    app.run()
    assert app.text_input[0].label == "Password" and not app.selectbox  # nothing else rendered
    app.text_input[0].input("wrong").run()
    assert app.error and not app.selectbox
    app.text_input[0].input("open sesame").run()
    assert not app.exception and not app.text_input
    assert app.selectbox[0].options == ["sample", "Upload a tender"]  # my_company hidden when deployed
    assert app.toggle[0].value  # opens on the backup run
    assert any("Live runs left today" in c.value for c in app.sidebar.caption)
    app.selectbox[0].select("Upload a tender").run()
    assert app.radio[0].options == ["Falak AI (fictional sample)"]  # no my_company profile either
    assert any("sent to the AI provider" in c.value for c in app.sidebar.caption)
    app.selectbox[0].select("sample").run()

    # Daily limit reached: Run shows a message pointing to the backup and never calls the API.
    import json
    import time

    budget_file = ROOT / "output" / "run_budget.json"
    saved = budget_file.read_text() if budget_file.exists() else None
    today = time.strftime("%Y-%m-%d", time.gmtime())
    budget_file.write_text(json.dumps({"day": today, "runs": 10, "last_start": 0, "running_since": 0}))
    try:
        app.toggle[0].set_value(False).run()
        app.button[0].click().run()
        assert any("daily limit" in w.value and "Load backup run" in w.value for w in app.warning)
        assert not app.exception and not app.status  # the run never started
    finally:
        budget_file.write_text(saved) if saved is not None else budget_file.unlink()


def _make_pdf(path: Path, pages: list[str]) -> None:
    import pymupdf as fitz

    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 550, 800), text, fontsize=10)
    doc.save(path)


def test_upload_pack_single_tender_flow(tmp_path: Path):
    """An uploaded PDF pack + meta.json runs extract -> qualify -> draft -> verify in a temp session folder."""
    from agent import uploads
    from agent.loop import run_single

    data_before = sorted(p for p in (ROOT / "data").rglob("*") if p.is_file())
    src = tmp_path / "src"
    src.mkdir()
    filler = "General conditions. The contractor shall comply with applicable laws. " * 25
    booklet = ["Tender No. U-2026\nTitle: Permit OCR platform"] + [f"Section {i}\n{filler}" for i in range(2, 12)]
    booklet.insert(2, "3.1 Valid Commercial Registration.\n3.2 ISO/IEC 27001 certification.")
    _make_pdf(src / "booklet.pdf", booklet)
    _make_pdf(src / "appendix_b.pdf", ["Appendix B\nB.1 Valid Zakat certificate. Mandatory."])
    files = [(p.name, p.read_bytes()) for p in (src / "booklet.pdf", src / "appendix_b.pdf")]

    session = tmp_path / "uploads" / "session1"
    meta = uploads.build_meta("Permit OCR platform", "1448-06-24 هـ", "2,500,000")
    data_dir, tid = uploads.save_pack(session, files, meta)
    assert data_dir == session and (session / "tenders" / tid / "meta.json").exists()
    out = session / "results"
    pilot = TenderPilot(FakeLLM(), data_dir, Store(out / "tracker.db"), on_event=lambda k, m: None,
                        company_dir=SAMPLE / "company")
    assert list(pilot.tenders) == [tid] and not pilot.listings
    run_single(pilot, tid)
    ext, q = pilot.extractions[tid], pilot.qualifications[tid]
    assert ext.title == "Permit OCR platform" and ext.estimated_value_sar == 2_500_000  # meta.json wins
    assert ext.deadline.startswith("2026-12")  # Hijri converted
    assert any("Zakat" in r.text for r in ext.requirements)  # found in the second file
    assert q.decision in ("BID", "CONDITIONAL_BID") and pilot.verifications[tid].passed
    pilot.save(out)
    assert (out / "results.json").exists() and (out / f"proposal_{tid}.docx").exists()

    # A new upload replaces the old one; a NO_BID tender is not drafted.
    (src / "appendix_b.pdf").unlink()
    _make_pdf(src / "appendix_b.pdf", ["Appendix B\nB.1 Bidders must hold an active CMMI Level 3 appraisal. Mandatory."])
    files2 = [(p.name, p.read_bytes()) for p in (src / "booklet.pdf", src / "appendix_b.pdf")]
    data_dir, tid2 = uploads.save_pack(session, files2, uploads.build_meta("Smart city analytics"))
    assert not (session / "tenders" / tid).exists() and not out.exists()
    pilot2 = TenderPilot(FakeLLM(), data_dir, Store(tmp_path / "t2.db"), company_dir=SAMPLE / "company")
    run_single(pilot2, tid2)
    assert pilot2.qualifications[tid2].decision == "NO_BID" and not pilot2.drafts

    # Rejected uploads, with messages for the user
    for bad, kwargs in ([("tool.exe", b"x")], {}), (files, {"max_file_mb": 0.001}), (files, {"max_pages": 5}):
        try:
            uploads.save_pack(tmp_path / "uploads" / "session2", bad, {}, **kwargs)
            raise AssertionError(f"accepted {kwargs or bad[0][0]}")
        except uploads.UploadError as err:
            assert any(w in str(err) for w in ("unsupported", "limit")), err
    assert not (tmp_path / "uploads" / "session2").exists()  # nothing left behind
    try:
        uploads.build_meta(deadline="next Tuesday")
        raise AssertionError("accepted a bad deadline")
    except uploads.UploadError:
        pass
    assert sorted(p for p in (ROOT / "data").rglob("*") if p.is_file()) == data_before  # never writes into data/


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
               test_agent_loop_recovers_from_bad_tool_call, test_agent_runs_same_turn_tool_calls_in_parallel,
               test_draft_streams_and_falls_back, test_run_budget_limits_live_runs,
               test_upload_pack_single_tender_flow, test_real_style_pack_long_pdf_hijri):
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
            print("PASS", fn.__name__)
    test_app_password_gate_and_deployed_defaults()
    print("PASS test_app_password_gate_and_deployed_defaults")
