"""TenderPilot demo UI.

    streamlit run app.py
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from agent.documents import normalize_deadline
from eval import score
from run import DEFAULT_GOAL, output_dir

load_dotenv(Path(__file__).parent / ".env")

ROOT = Path(__file__).parent
DATA_SETS = sorted(p.name for p in (ROOT / "data").iterdir() if (p / "company").is_dir())
BACKUP = ROOT / "demo_backup"  # a saved good run of the sample data, for when the API or Wi-Fi fails

DECISION_LABEL = {"BID": "🟢 Bid", "CONDITIONAL_BID": "🟡 Conditional bid", "NO_BID": "🔴 No bid"}
STATUS_LABEL = {"met": "✅ met", "partial": "🟡 partial", "gap": "❌ gap", "unknown": "❔ unknown"}
REC_LABEL = {"download": "⬇ download", "maybe": "🤔 maybe", "skip": "⏭ skip"}
REC_ORDER = {"download": 0, "maybe": 1, "skip": 2}
STEP_ICON = {"screen": "🗂️", "discover": "🔎", "extract": "📄", "qualify": "⚖️", "gap": "🚩", "rank": "🏆", "draft": "✍️",
             "verify": "🧪", "human": "🙋", "plan": "🧭", "think": "💭", "finish": "✅", "error": "⚠️"}

SECTION_FIELD = re.compile(r'"(title|content)"\s*:\s*"((?:[^"\\]|\\.)*)', re.S)


def draft_preview(partial_json: str) -> str:
    """Readable markdown from a proposal draft whose JSON is still being written."""
    out = []
    for key, raw in SECTION_FIELD.findall(partial_json):
        try:
            text = json.loads(f'"{raw.rstrip(chr(92))}"')  # drop an escape cut off mid-stream
        except json.JSONDecodeError:
            text = raw
        out.append(f"#### {text}" if key == "title" else text)
    return "\n\n".join(out) + " ▌"


ARABIC_RUN = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿][؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿\s\d،؛:/\-–]*")


def english_part(text: str) -> str:
    """Mixed Arabic/English renders badly in tables: keep the English part (pure Arabic stays as is)."""
    if not ARABIC_RUN.search(text):
        return text
    latin = re.sub(r"\(\s*\)|\[\s*\]", "", ARABIC_RUN.sub(" ", text))
    latin = re.sub(r"\s{2,}", " ", latin).strip(" -–—:/|،")
    return latin if re.search("[A-Za-z]", latin) else text


def arabic_part(text: str) -> str:
    return " ".join(m.strip(" -–—:/") for m in ARABIC_RUN.findall(text)).strip()


def short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


st.set_page_config(page_title="TenderPilot", page_icon="🧭", layout="wide")
st.title("🧭 TenderPilot")
st.caption("Autonomous agent: screens tenders, decides bid / no-bid, drafts and verifies the proposal.")

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Run the agent")
    data_set = st.selectbox("Data set", DATA_SETS, index=DATA_SETS.index("sample") if "sample" in DATA_SETS else 0,
                            help="sample = fictional demo data · my_company = your real company and tenders")
    DATA_DIR = ROOT / "data" / data_set
    use_backup = data_set == "sample" and (BACKUP / "results.json").exists() and st.toggle(
        "Load backup run", help="Show a saved run instantly, with no API calls (if the API or Wi-Fi fails).")
    OUT = BACKUP if use_backup else output_dir(DATA_DIR)
    RESULTS = OUT / "results.json"
    goal = st.text_area("Goal", DEFAULT_GOAL, height=140)
    mode = st.radio("Mode", ["agent", "pipeline"], help="agent = the model plans its own steps; pipeline = fixed order (fallback)")
    run_clicked = st.button("▶ Run TenderPilot", type="primary", width="stretch", disabled=use_backup)
    st.divider()
    st.caption(f"Provider: {os.getenv('LLM_PROVIDER', 'anthropic')} · Model: {os.getenv('LLM_MODEL', 'default')}")
    if use_backup:
        st.caption("Showing the saved backup run. Turn the toggle off to run live.")
    elif RESULTS.exists():
        st.caption("Showing the last saved run until you start a new one.")

if data_set == "sample":
    st.caption("Sample data set: all companies and tenders are fictional.")
else:
    from agent.documents import CompanyKnowledge

    todo = CompanyKnowledge(DATA_DIR / "company").unfinished()
    if todo:
        st.warning(f"Fill in the TODO placeholders in data/{data_set}/company/ first: {', '.join(todo)}. "
                   f"See data/{data_set}/HOW_TO_USE.md.")

# ---------------------------------------------------------------------- run
if run_clicked:
    from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

    from agent.loop import run_agent, run_pipeline
    from run import build_pilot

    with st.status("TenderPilot is working…", expanded=True) as status:
        live = {"box": None, "text": "", "shown": 0.0}  # the proposal being written right now

        def on_stream(tender_id: str, chunk: str) -> None:
            if live["box"] is None:
                live.update(box=status.container(border=True).empty(), text="")
            live["text"] += chunk
            if time.monotonic() - live["shown"] > 0.25:
                live["box"].markdown(draft_preview(live["text"]))
                live["shown"] = time.monotonic()

        def on_event(kind: str, msg: str) -> None:
            if kind == "draft" and live["box"] is not None:  # draft finished: show it in full
                live["box"].markdown(draft_preview(live["text"]).removesuffix(" ▌"))
                live["box"] = None
            # write into the status box explicitly: worker threads don't see the `with` block above
            status.write(f"{STEP_ICON.get(kind, '•')} **{kind}** — {msg}")

        try:
            pilot = build_pilot(DATA_DIR, on_event=on_event)
            pilot.on_stream = on_stream
            ctx = get_script_run_ctx()
            pilot.thread_init = lambda: add_script_run_ctx(ctx=ctx)  # parallel tools can write to the live log
            if pilot.company.unfinished():
                st.warning(f"Company files still contain TODO placeholders: {', '.join(pilot.company.unfinished())}")
            summary = (run_agent if mode == "agent" else run_pipeline)(pilot, goal)
            pilot.save(OUT)
            status.update(label=f"Done. {summary[:120]}", state="complete", expanded=False)
        except Exception as err:
            status.update(label="Run failed", state="error")
            st.exception(err)
            st.stop()

if not RESULTS.exists():
    st.info(f"No results yet for **{data_set}**. Add your API key to .env and press **Run TenderPilot**. "
            "To use real tenders, see data/my_company/HOW_TO_USE.md.")
    st.stop()

results = json.loads(RESULTS.read_text(encoding="utf-8"))
quals, exts = results["qualifications"], results["extractions"]

# ------------------------------------------------------------------ metrics
key_path = ROOT / "answer_key.json"
s = score(results, json.loads(key_path.read_text(encoding="utf-8"))) if data_set == "sample" and key_path.exists() else None
if s and not s["rows"]:
    s = None
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Tenders analyzed", len(quals))
c2.metric("Worth bidding", sum(q["decision"] != "NO_BID" for q in quals.values()))
c3.metric("Gaps caught", sum(len(q["blockers"]) for q in quals.values()))
c4.metric("Questions for you", len(results["human_questions"]))
if s:
    c5.metric("Decisions correct", f"{s['percent']['decision']}%",
              f"{s['percent']['requirements']}% reqs · {s['percent']['traps']}% traps", delta_color="off")

# ---------------------------------------------------------------- listings
if results.get("shortlist"):
    st.subheader("Listing screen (public Etimad listings)")
    by_ref = {r["reference"]: r for r in results.get("listings", [])}

    def rec_label(x: dict) -> str:
        deadline, _ = normalize_deadline(str(by_ref.get(x["reference"], {}).get("deadline", "")))
        if x["recommend"] == "skip" and deadline and deadline < results.get("generated_on", ""):
            return "⏭ skip: deadline passed"
        return REC_LABEL[x["recommend"]]

    shortlist = sorted(results["shortlist"], key=lambda x: (REC_ORDER[x["recommend"]], -x["relevance"]))
    rows = [{
        "Ref": x["reference"], "Recommendation": rec_label(x), "Relevance": x["relevance"],
        "Title": short(english_part(by_ref.get(x["reference"], {}).get("title", "")), 44),
        "Entity": short(english_part(by_ref.get(x["reference"], {}).get("entity", "")), 30),
        "Deadline": by_ref.get(x["reference"], {}).get("deadline", ""),
    } for x in shortlist]
    # widths in px: the six columns fit in the ~940px table of a 1400px-wide window
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "Ref": st.column_config.TextColumn(width=85), "Recommendation": st.column_config.TextColumn(width=170),
        "Relevance": st.column_config.ProgressColumn("Relevance", min_value=0, max_value=100, format="%d", width=105),
        "Title": st.column_config.TextColumn(width=280), "Entity": st.column_config.TextColumn(width=195),
        "Deadline": st.column_config.TextColumn(width=90)})
    with st.expander("Why: the agent's reason for each listing"):
        st.dataframe(pd.DataFrame([{"Ref": x["reference"], "Why": x["reason"]} for x in shortlist]),
                     hide_index=True, width="stretch", column_config={"Ref": st.column_config.TextColumn(width=85)})

# ------------------------------------------------------------------ ranking
if not results["ranking"]:
    st.info("No full tender documents analyzed yet. Download the recommended ones into the tenders folder and run again.")
    st.stop()
st.subheader("Ranked opportunities")
rank_rows = []
for r in results["ranking"]:
    e = exts.get(r["tender_id"], {})
    rank_rows.append({
        "Tender": r["tender_id"], "Title": short(english_part(e.get("title", "")), 54), "Deadline": e.get("deadline", ""),
        "Decision": DECISION_LABEL[r["decision"]], "Fit": r["fit_score"], "Blockers": len(r["blockers"]),
    })
st.dataframe(pd.DataFrame(rank_rows), hide_index=True, width="stretch", column_config={
    "Tender": st.column_config.TextColumn(width=205), "Title": st.column_config.TextColumn(width=345),
    "Deadline": st.column_config.TextColumn(width=90), "Decision": st.column_config.TextColumn(width=140),
    "Fit": st.column_config.ProgressColumn("Fit", min_value=0, max_value=100, format="%d", width=85),
    "Blockers": st.column_config.NumberColumn(width=65)})

if results["human_questions"]:
    st.subheader("🙋 Waiting on you")
    for hq in results["human_questions"]:
        st.warning(f"**{hq['tender_id']}** — {hq['question']}")

# ---------------------------------------------------------------- per tender
st.subheader("Tender details")
tabs = st.tabs([r["tender_id"] for r in results["ranking"]] + ["Audit log"])
for tab, r in zip(tabs, results["ranking"]):
    tid = r["tender_id"]
    e, q = exts[tid], quals[tid]
    with tab:
        st.markdown(f"### {english_part(e['title'])}")
        if english_part(e["title"]) != e["title"] and arabic_part(e["title"]):  # bilingual: Arabic on its own RTL line
            st.markdown(f'<div dir="rtl" style="font-size:1.25rem;margin:-0.5rem 0 0.5rem">{html.escape(arabic_part(e["title"]))}</div>',
                        unsafe_allow_html=True)
        value = f" · value **SAR {e['estimated_value_sar']:,.0f}**" if e.get("estimated_value_sar") else ""
        st.markdown(f"*{e['issuer']}* · deadline **{e['deadline']}**{value} · {DECISION_LABEL[q['decision']]} (fit {q['fit_score']})")
        st.write(e["summary"])
        left, right = st.columns(2)
        left.markdown("**Why**\n" + "\n".join(f"- {x}" for x in q["reasons"]))
        if q["blockers"]:
            right.markdown("**Blockers**\n" + "\n".join(f"- 🚩 {x}" for x in q["blockers"]))

        checks = {c["requirement_id"]: c for c in q["checks"]}
        matrix = [{
            "ID": req["id"], "Requirement": req["text"], "Source": req["source"],
            "Mandatory": "Yes" if req["mandatory"] else "No",
            "Status": STATUS_LABEL[checks.get(req["id"], {}).get("status", "unknown")],
            "Evidence": checks.get(req["id"], {}).get("evidence", ""),
            "Note": checks.get(req["id"], {}).get("note", ""),
        } for req in e["requirements"]]
        st.markdown("**Compliance matrix**")
        st.dataframe(pd.DataFrame(matrix), hide_index=True, width="stretch")

        draft = results["drafts"].get(tid)
        if draft:
            ver = results["verifications"].get(tid)
            st.markdown(f"**Proposal draft r{draft['revision']}** — verification: "
                        + ("✅ passed" if ver and ver["passed"] else f"⚠️ {len(ver['issues']) if ver else '?'} open issues"))
            for sec in draft["sections"]:
                with st.expander(sec["title"]):
                    st.write(sec["content"])
            if ver and ver["issues"]:
                with st.expander("Verification issues"):
                    st.dataframe(pd.DataFrame(ver["issues"]), hide_index=True, width="stretch")
            docx = OUT / f"proposal_{tid}.docx"
            if docx.exists():
                st.download_button("⬇ Download proposal (.docx)", docx.read_bytes(), file_name=docx.name)

with tabs[-1]:
    st.dataframe(pd.DataFrame(results["audit_log"]), hide_index=True, width="stretch")
