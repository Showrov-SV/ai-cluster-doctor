"""
action_logger.py
Persists each planned action (node, action text, priority, reason,
before/after health score, result) to a dedicated `actions` table in the
same SQLite file db.py already uses - own connection, db.py itself is
untouched.
"""

from __future__ import annotations
import sqlite3
import time
from contextlib import contextmanager
from typing import Dict, List, Optional

import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      TEXT NOT NULL,
    action       TEXT NOT NULL,
    priority     TEXT NOT NULL,
    reason       TEXT,
    before_score REAL,
    after_score  REAL,
    result       TEXT,
    created_at   REAL NOT NULL
);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_action_log() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def save_action(node_id: str, action: str, priority: str, reason: Optional[str], before_score: float) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO actions (node_id, action, priority, reason, before_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (node_id, action, priority, reason, before_score, time.time()),
        )


def get_actions(node_id: str) -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM actions WHERE node_id = ? ORDER BY created_at DESC",
            (node_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def demo_resolve_latest(node_id: str) -> None:
    """Prototype-only: fills in a fixed demo outcome so the History panel has
    something to show live in a demo without wiring real before/after
    health-check polling yet."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM actions WHERE node_id = ? ORDER BY created_at DESC LIMIT 1",
            (node_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE actions SET after_score = ?, result = ? WHERE id = ?",
                (75, "Improved", row["id"]),
            )
