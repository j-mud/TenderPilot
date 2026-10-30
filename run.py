"""Run TenderPilot from the command line.

    python run.py                    # autonomous agent mode
    python run.py --mode pipeline    # fixed-order fallback
    python run.py --data data/my_company   # run on your own company + real tenders
"""
from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from agent.llm import LLM
from agent.loop import run_agent, run_pipeline
from agent.store import Store
from agent.tools import TenderPilot

ROOT = Path(__file__).parent
DEFAULT_GOAL = (
    "Review the public tender listings and all tender documents we have, decide which are worth "
    "bidding on, and prepare a verified proposal for the strongest one."
)


def output_dir(data_dir: Path) -> Path:
    return ROOT / "output" / Path(data_dir).name


def build_pilot(data_dir: Path, on_event=None, company_dir: Path | None = None, out_dir: Path | None = None,
                reset: bool = True) -> TenderPilot:
    load_dotenv(ROOT / ".env")
    store = Store((out_dir or output_dir(data_dir)) / "tracker.db")
    if reset:  # a new run; reset=False continues a saved run (e.g. after the bid manager answers)
        store.reset()
    return TenderPilot(LLM(), Path(data_dir), store, on_event=on_event, company_dir=company_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["agent", "pipeline"], default="agent")
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument("--data", default=str(ROOT / "data" / "sample"), help="data set folder")
    args = parser.parse_args()

    pilot = build_pilot(Path(args.data), on_event=lambda kind, msg: print(f"{kind.upper():>8} | {msg}"))
    if pilot.company.unfinished():
        print(f"WARNING: company files still contain TODO: {pilot.company.unfinished()}")
    if args.mode == "agent":
        summary = run_agent(pilot, args.goal)
    else:
        summary = run_pipeline(pilot, args.goal)
    path = pilot.save(output_dir(Path(args.data)))
    print(f"\n{summary}\n\nSaved results to {path}. Score the sample data with: python eval.py")


if __name__ == "__main__":
    main()
