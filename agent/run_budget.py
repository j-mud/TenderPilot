"""Spending guard for the public demo: a daily cap on live runs, a cooldown, and one run at a time.

State lives in a small JSON file. On Streamlit Community Cloud that file resets when the app
restarts, which is fine for a demo: it is a brake on the API bill, not accounting.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()  # every session of a Streamlit app runs in the same process


class RunBudget:
    def __init__(self, path: Path, max_per_day: int = 10, cooldown_s: int = 120, max_run_s: int = 600):
        self.path = path
        self.max_per_day = max_per_day
        self.cooldown_s = cooldown_s
        self.max_run_s = max_run_s  # a run that never reported finishing stops blocking after this

    def _load(self, now: float) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        today = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if state.get("day") != today:  # new day (UTC): reset the count, keep the timers
            state = {"day": today, "runs": 0, "last_start": state.get("last_start", 0),
                     "running_since": state.get("running_since", 0)}
        return state

    def _save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state), encoding="utf-8")

    def try_start(self, now: float | None = None) -> str | None:
        """Reserve a live run. Returns None if it may start, otherwise the reason it can't."""
        now = time.time() if now is None else now
        with _LOCK:
            s = self._load(now)
            if s["running_since"] and now - s["running_since"] < self.max_run_s:
                return "Another live run is in progress."
            if s["runs"] >= self.max_per_day:
                return f"The daily limit of {self.max_per_day} live runs has been reached."
            wait = self.cooldown_s - (now - s["last_start"])
            if wait > 0:
                return f"Please wait {int(wait) + 1} s before the next live run."
            s.update(runs=s["runs"] + 1, last_start=now, running_since=now)
            self._save(s)
        return None

    def finish(self, now: float | None = None) -> None:
        with _LOCK:
            s = self._load(time.time() if now is None else now)
            s["running_since"] = 0
            self._save(s)

    def remaining(self, now: float | None = None) -> int:
        with _LOCK:
            return max(0, self.max_per_day - self._load(time.time() if now is None else now)["runs"])
