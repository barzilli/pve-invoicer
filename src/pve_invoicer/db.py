"""SQLite storage: recent per-minute samples, one usage row per guest per day, one invoice row per customer per month."""

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
    coverage        REAL NOT NULL DEFAULT 0,  -- share of the day's minutes with data, 0..1
    resolution_s    INTEGER NOT NULL DEFAULT 60,  -- coarsest RRD step used for the day (60 = 1-minute data)
    description     TEXT,               -- guest notes that day; NULL = not read (backfill/deleted)
    collected_at    TEXT NOT NULL,
    PRIMARY KEY (day, guest_id)
);

-- One row per guest per minute, from the Proxmox RRD API. Kept for a few days only, so
-- overlapping hourly runs can be merged and recent days rebuilt; usage_daily is the record.
CREATE TABLE IF NOT EXISTS usage_samples (
    guest_id   TEXT NOT NULL,
    ts         INTEGER NOT NULL,       -- unix time, start of the minute
    step       INTEGER NOT NULL,       -- RRD resolution the value came from, seconds
    running    INTEGER NOT NULL,       -- 1 if the guest was running
    vcpus      REAL NOT NULL,          -- allocated (maxcpu)
    mem_bytes  REAL NOT NULL,          -- allocated (maxmem)
    disk_bytes REAL NOT NULL,          -- allocated (maxdisk)
    net_in     REAL NOT NULL,          -- bytes received in this minute
    net_out    REAL NOT NULL,          -- bytes sent in this minute
    PRIMARY KEY (guest_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS usage_samples_ts ON usage_samples (ts);

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
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_daily)")}
    if "resolution_s" not in cols:  # databases created before the switch to the Proxmox API
        conn.execute("ALTER TABLE usage_daily ADD COLUMN resolution_s INTEGER NOT NULL DEFAULT 60")
    return conn
