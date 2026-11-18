"""SQLite storage: one usage row per guest per day, one invoice row per customer per month."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_daily (
    day             TEXT NOT NULL,      -- YYYY-MM-DD in the configured timezone
    guest_id        TEXT NOT NULL,      -- qemu/100, lxc/101
    name            TEXT NOT NULL DEFAULT '',
    node            TEXT NOT NULL DEFAULT '',
    running_hours   REAL NOT NULL DEFAULT 0,
    vcpu_hours      REAL NOT NULL DEFAULT 0,
    ram_gib_hours   REAL NOT NULL DEFAULT 0,
    disk_gib_days   REAL NOT NULL DEFAULT 0,
    net_in_gb       REAL NOT NULL DEFAULT 0,
    net_out_gb      REAL NOT NULL DEFAULT 0,
    coverage        REAL NOT NULL DEFAULT 0,  -- share of expected samples present, 0..1
    description     TEXT,               -- guest notes that day; NULL = not read (backfill/deleted)
    collected_at    TEXT NOT NULL,
    PRIMARY KEY (day, guest_id)
);

CREATE TABLE IF NOT EXISTS invoices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period       TEXT NOT NULL,          -- YYYY-MM
    seq          INTEGER NOT NULL,
    number       TEXT NOT NULL UNIQUE,
    customer_id  TEXT NOT NULL,
    email        TEXT NOT NULL,
    currency     TEXT NOT NULL,
    total        TEXT NOT NULL,          -- decimal as text
    status       TEXT NOT NULL,          -- held | failed | sent
    hold_reason  TEXT,
    pdf_path     TEXT,
    json_path    TEXT,
    created_at   TEXT NOT NULL,
    sent_at      TEXT,
    message_id   TEXT,
    last_error   TEXT,
    UNIQUE (period, customer_id),
    UNIQUE (period, seq)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
