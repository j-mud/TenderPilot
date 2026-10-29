"""Opportunity tracker and audit log (SQLite)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS opportunities (
                tender_id TEXT PRIMARY KEY, title TEXT, deadline TEXT,
                decision TEXT, fit_score INTEGER, status TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tender_id TEXT,
                action TEXT, owner TEXT, status TEXT DEFAULT 'open', created_at TEXT);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
                tender_id TEXT, step TEXT, detail TEXT);
            """
        )

    def reset(self) -> None:
        self.db.executescript("DELETE FROM opportunities; DELETE FROM actions; DELETE FROM audit;")
        self.db.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def log(self, tender_id: str, step: str, detail: str) -> None:
        self.db.execute(
            "INSERT INTO audit (ts, tender_id, step, detail) VALUES (?,?,?,?)",
            (self._now(), tender_id, step, detail),
        )
        self.db.commit()

    def upsert_opportunity(self, tender_id, title, deadline, decision=None, fit_score=None, status="new"):
        self.db.execute(
            """INSERT INTO opportunities (tender_id, title, deadline, decision, fit_score, status, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(tender_id) DO UPDATE SET
                 title=excluded.title, deadline=excluded.deadline,
                 decision=COALESCE(excluded.decision, decision),
                 fit_score=COALESCE(excluded.fit_score, fit_score),
                 status=excluded.status, updated_at=excluded.updated_at""",
            (tender_id, title, deadline, decision, fit_score, status, self._now()),
        )
        self.db.commit()

    def add_action(self, tender_id: str, action: str, owner: str = "human") -> None:
        self.db.execute(
            "INSERT INTO actions (tender_id, action, owner, created_at) VALUES (?,?,?,?)",
            (tender_id, action, owner, self._now()),
        )
        self.db.commit()

    def rows(self, table: str) -> list[dict]:
        cur = self.db.execute(f"SELECT * FROM {table}")  # table names are internal constants
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
