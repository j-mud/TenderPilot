"""Opportunity tracker and audit log (SQLite). Safe to call from several threads."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()  # one shared connection: serialize every statement + commit
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
        with self._lock:
            self.db.executescript("DELETE FROM opportunities; DELETE FROM actions; DELETE FROM audit;")
            self.db.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _write(self, sql: str, params: tuple) -> None:
        with self._lock:
            self.db.execute(sql, params)
            self.db.commit()

    def log(self, tender_id: str, step: str, detail: str) -> None:
        self._write(
            "INSERT INTO audit (ts, tender_id, step, detail) VALUES (?,?,?,?)",
            (self._now(), tender_id, step, detail),
        )

    def upsert_opportunity(self, tender_id, title, deadline, decision=None, fit_score=None, status="new"):
        self._write(
            """INSERT INTO opportunities (tender_id, title, deadline, decision, fit_score, status, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(tender_id) DO UPDATE SET
                 title=excluded.title, deadline=excluded.deadline,
                 decision=COALESCE(excluded.decision, decision),
                 fit_score=COALESCE(excluded.fit_score, fit_score),
                 status=excluded.status, updated_at=excluded.updated_at""",
            (tender_id, title, deadline, decision, fit_score, status, self._now()),
        )

    def add_action(self, tender_id: str, action: str, owner: str = "human") -> None:
        self._write(
            "INSERT INTO actions (tender_id, action, owner, created_at) VALUES (?,?,?,?)",
            (tender_id, action, owner, self._now()),
        )

    def rows(self, table: str) -> list[dict]:
        with self._lock:
            cur = self.db.execute(f"SELECT * FROM {table}")  # table names are internal constants
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
