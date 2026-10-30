"""TenderPilot demo UI.

    streamlit run app.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from eval import score
from run import DEFAULT_GOAL, output_dir

load_dotenv(Path(__file__).parent / ".env")

ROOT = Path(__file__).parent
DATA_SETS = sorted(p.name for p in (ROOT / "data").iterdir() if (p / "company").is_dir())

DECISION_LABEL = {"BID": "🟢 Bid", "CONDITIONAL_BID": "🟡 Conditional bid", "NO_BID": "🔴 No bid"}
STATUS_LABEL = {"met": "✅ met", "partial": "🟡 partial", "gap": "❌ gap", "unknown": "❔ unknown"}
REC_LABEL = {"download": "⬇ download", "maybe": "🤔 maybe", "skip": "⏭ skip"}
STEP_ICON = {"screen": "🗂️", "discover": "🔎", "extract": "📄", "qualify": "⚖️", "gap": "🚩", "rank": "🏆", "draft": "✍️",
             "verify": "🧪", "human": "🙋", "plan": "🧭", "think": "💭", "finish": "✅", "error": "⚠️"}

st.set_page_config(page_title="TenderPilot", page_icon="🧭", layout="wide")
st.title("🧭 TenderPilot")
st.caption("Autonomous agent: screens tenders, decides bid / no-bid, drafts and verifies the proposal.")

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Run the agent")
    data_set = st.selectbox("Data set", DATA_SETS, index=DATA_SETS.index("sample") if "sample" in DATA_SETS else 0,
                            help="sample = fictional demo data · my_company = your real company and tenders")
    DATA_DIR = ROOT / "data" / data_set
    OUT = output_dir(DATA_DIR)
    RESULTS = OUT / "results.json"
    goal = st.text_area("Goal", DEFAULT_GOAL, height=140)
    mode = st.radio("Mode", ["agent", "pipeline"], help="agent = the model plans its own steps; pipeline = fixed order (fallback)")
    run_clicked = st.button("▶ Run TenderPilot", type="primary", width="stretch")
    st.divider()
    st.caption(f"Provider: {os.getenv('LLM_PROVIDER', 'anthropic')} · Model: {os.getenv('LLM_MODEL', 'default')}")
    if RESULTS.exists():
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
        def on_event(kind: str, msg: str) -> None:
            st.write(f"{STEP_ICON.get(kind, '•')} **{kind}** — {msg}")

        try:
            pilot = build_pilot(DATA_DIR, on_event=on_event)
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
    rows = [{
        "Ref": x["reference"], "Title": by_ref.get(x["reference"], {}).get("title", ""),
        "Entity": by_ref.get(x["reference"], {}).get("entity", ""),
        "Deadline": by_ref.get(x["reference"], {}).get("deadline", ""),
        "Relevance": x["relevance"], "Recommendation": REC_LABEL[x["recommend"]], "Why": x["reason"],
    } for x in results["shortlist"]]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Relevance": st.column_config.ProgressColumn("Relevance", min_value=0, max_value=100, format="%d")})

# ------------------------------------------------------------------ ranking
if not results["ranking"]:
    st.info("No full tender documents analyzed yet. Download the recommended ones into the tenders folder and run again.")
    st.stop()
st.subheader("Ranked opportunities")
rank_rows = []
for r in results["ranking"]:
    e = exts.get(r["tender_id"], {})
    rank_rows.append({
        "Tender": r["tender_id"], "Title": e.get("title", ""), "Deadline": e.get("deadline", ""),
        "Value (SAR)": f"{e.get('estimated_value_sar') or 0:,.0f}", "Decision": DECISION_LABEL[r["decision"]],
        "Fit": r["fit_score"], "Blockers": len(r["blockers"]),
    })
st.dataframe(pd.DataFrame(rank_rows), hide_index=True, width="stretch",
             column_config={"Fit": st.column_config.ProgressColumn("Fit", min_value=0, max_value=100, format="%d")})

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
        st.markdown(f"### {e['title']}\n*{e['issuer']}* · deadline **{e['deadline']}** · {DECISION_LABEL[q['decision']]} (fit {q['fit_score']})")
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
