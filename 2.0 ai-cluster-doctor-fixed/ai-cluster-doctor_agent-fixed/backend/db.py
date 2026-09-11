"""
db.py
SQLite-backed device registry.

This owns device *identity* (device_id, original hostname, user-assigned
nickname, which provider it came from, first/last seen, soft-delete flag)
so that rename/delete/search/last-seen/offline-detection all survive a
process restart - unlike the in-memory telemetry history.

Scope note: this module intentionally does NOT yet store telemetry
readings, alerts, or predictions - those still come from the live
TelemetryProvider + ml.py on each request, unchanged. Persisting telemetry
history itself is a separate follow-up task.
"""

from __future__ import annotations
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

DB_PATH = Path(os.environ.get("CLUSTER_DOCTOR_DB_PATH", str(Path(__file__).parent / "cluster_doctor.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    hostname    TEXT NOT NULL,        -- original/immutable name reported by the provider
    nickname    TEXT,                 -- user-assigned display name; NULL = use hostname
    source      TEXT NOT NULL,        -- which TelemetryProvider this device came from
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer',  -- 'admin' or 'viewer'
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def upsert_seen(device_id: str, hostname: str, source: str) -> None:
    """Register a device on first sighting, or bump last_seen if already known.

    A device that reappears after being soft-deleted is treated as
    reconnected and un-deleted, matching how real device-management tools
    behave when a previously-removed device comes back online.
    """
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO devices (device_id, hostname, nickname, source, first_seen, last_seen, deleted)
            VALUES (?, ?, NULL, ?, ?, ?, 0)
            ON CONFLICT(device_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                deleted = 0
            """,
            (device_id, hostname, source, now, now),
        )


def set_nickname(device_id: str, nickname: Optional[str]) -> bool:
    """Returns True if a device row was updated (i.e. it exists)."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE devices SET nickname = ? WHERE device_id = ?",
            (nickname, device_id),
        )
        return cur.rowcount > 0


def soft_delete(device_id: str) -> bool:
    """Hide a device from the visible inventory without touching the
    underlying provider - it may keep reporting telemetry in the
    background; the next sighting will un-delete it (see upsert_seen)."""
    with _connect() as conn:
        cur = conn.execute("UPDATE devices SET deleted = 1 WHERE device_id = ?", (device_id,))
        return cur.rowcount > 0


def get_device(device_id: str) -> Optional[Dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        return dict(row) if row else None


def list_devices(include_deleted: bool = False) -> List[Dict]:
    with _connect() as conn:
        q = "SELECT * FROM devices" if include_deleted else "SELECT * FROM devices WHERE deleted = 0"
        rows = conn.execute(q).fetchall()
        return [dict(r) for r in rows]


def is_deleted(device_id: str) -> bool:
    device = get_device(device_id)
    return bool(device and device["deleted"])


# ---- settings (used by auth.py to persist JWT secret / agent API key) ----

def get_setting(key: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


# ---- users (JWT authentication + RBAC) ----

def create_user(username: str, password_hash: str, role: str = "viewer") -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, role, time.time()),
        )


def get_user(username: str) -> Optional[Dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def user_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
