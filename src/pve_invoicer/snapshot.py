"""Hourly snapshot: guest usage from the Proxmox VE RRD API, rolled up into one row per guest per day.

Each run reads the guest list from /cluster/resources and, per guest, the last ~70 minutes of
1-minute RRD data (timeframe=hour). Samples are stored per minute, so overlapping runs merge
cleanly. When a guest's data has a gap since the previous run (a missed or late run, a node
that was down, a new guest), coarser RRD data (30-minute, 3-hour, ...) fills only the minutes
the finer data does not cover. Every day a run touched is then rebuilt from the samples.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from .clients import TIMEFRAMES, ClientError, Guest
from .config import Config

log = logging.getLogger(__name__)

GIB = 1073741824
MINUTE = 60
# Days older than this whose daily rows exist are final: their samples are pruned and no
# later run changes them.
SAMPLE_RETENTION_DAYS = 7
# Fallback step per timeframe when a response has too few points to infer it.
DEFAULT_STEPS = {"hour": 60, "day": 1800, "week": 10800, "month": 43200, "year": 604800}


def day_bounds(day: date, cfg: Config) -> tuple[datetime, datetime]:
    """Local midnight to next local midnight (23 or 25 hours on DST change days)."""
    start = datetime.combine(day, time(0), tzinfo=cfg.timezone)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=cfg.timezone)
    return start, end


def _local_day(ts: int, cfg: Config) -> date:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(cfg.timezone).date()


@dataclass
class SnapshotReport:
    guests: int = 0
    samples: int = 0
    notes_read: int = 0
    days: list[date] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _step(points: list[dict], timeframe: str) -> int:
    times = sorted({int(p["time"]) for p in points if "time" in p})
    diffs = [b - a for a, b in zip(times, times[1:]) if b > a]
    return min(diffs) if diffs else DEFAULT_STEPS[timeframe]


def _known(p: dict) -> bool:
    return any(p.get(k) is not None for k in ("maxcpu", "maxmem", "maxdisk"))


def _upsert_samples(conn, gid: str, points: list[dict], step: int, lo: int, hi: int, skip=None) -> list[int]:
    """Store each point as one sample per minute in [lo, hi). Finer data replaces coarser.

    `skip(minute)` returning True leaves that minute alone. Returns the minute timestamps written.
    """
    rows = []
    for p in points:
        if "time" not in p or not _known(p):
            continue
        t = int(p["time"]) // MINUTE * MINUTE
        running = 1 if p.get("cpu") is not None else 0
        vcpus = float(p.get("maxcpu") or 0)
        mem = float(p.get("maxmem") or 0)
        disk = float(p.get("maxdisk") or 0)
        # netin/netout are bytes per second averaged over the interval.
        nin = max(float(p.get("netin") or 0), 0.0) * MINUTE
        nout = max(float(p.get("netout") or 0), 0.0) * MINUTE
        for m in range(max(t, lo), min(t + step, hi), MINUTE):
            if skip and skip(m):
                continue
            rows.append((gid, m, step, running, vcpus, mem, disk, nin, nout))
    conn.executemany(
        """
        INSERT INTO usage_samples (guest_id, ts, step, running, vcpus, mem_bytes, disk_bytes, net_in, net_out)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT (guest_id, ts) DO UPDATE SET
            step = excluded.step, running = excluded.running, vcpus = excluded.vcpus,
            mem_bytes = excluded.mem_bytes, disk_bytes = excluded.disk_bytes,
            net_in = excluded.net_in, net_out = excluded.net_out
        WHERE excluded.step <= usage_samples.step
        """,
        rows,
    )
    return [r[1] for r in rows]


def _collect_guest(conn, pve, g: Guest, want_from: int, now_ts: int, skip, report: SnapshotReport) -> list[int]:
    """Fetch RRD data from fine to coarse until it reaches back to `want_from`."""
    written: list[int] = []
    covered_from: int | None = None  # earliest minute covered by a finer timeframe already fetched
    any_known = False
    for tf in TIMEFRAMES:
        try:
            points = pve.rrddata(g, tf)
        except ClientError as e:
            report.warnings.append(f"{g.guest_id} ({g.name}): could not read {tf} RRD data: {e}")
            break
        times = [int(p["time"]) for p in points if "time" in p]
        if not times:
            break
        step = _step(points, tf)
        # Finer data is authoritative where it exists, even where it says "no data".
        hi = now_ts if covered_from is None else covered_from
        lo = min(times) if tf == "hour" else want_from
        written += _upsert_samples(conn, g.guest_id, points, step, lo, hi, skip)
        any_known = any_known or any(_known(p) for p in points)
        first = min(times) // MINUTE * MINUTE
        covered_from = first if covered_from is None else min(covered_from, first)
        if tf != "hour" and first < hi:
            log.info("%s: gap before %s filled from %s RRD data (%ds steps)", g.guest_id,
                     datetime.fromtimestamp(hi, timezone.utc).isoformat(), tf, step)
        if covered_from <= want_from:
            break
    if not any_known and g.status == "running":
        report.warnings.append(f"{g.guest_id} ({g.name}): running but the RRD API returned no data")
    return written


def _rebuild_day(conn, cfg: Config, day: date, guests: dict[str, Guest], collected_at: str) -> int:
    start, end = day_bounds(day, cfg)
    lo, hi = int(start.timestamp()), int(end.timestamp())
    expected = (hi - lo) / MINUTE
    rows = conn.execute(
        """
        SELECT guest_id, COUNT(*) AS n, SUM(running) AS run_min, SUM(running * vcpus) AS vcpu_min,
               SUM(running * mem_bytes) AS mem_min, SUM(disk_bytes) AS disk_min,
               SUM(net_in) AS net_in, SUM(net_out) AS net_out, MAX(step) AS step
        FROM usage_samples WHERE ts >= ? AND ts < ? GROUP BY guest_id
        """,
        (lo, hi),
    ).fetchall()
    for r in rows:
        g = guests.get(r["guest_id"])
        conn.execute(
            """
            INSERT INTO usage_daily (day, guest_id, name, node, running_hours, vcpu_hours, ram_gib_hours,
                                     disk_gib_days, net_in_gb, net_out_gb, coverage, resolution_s,
                                     description, collected_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
            ON CONFLICT (day, guest_id) DO UPDATE SET
                name = CASE WHEN excluded.name != '' THEN excluded.name ELSE usage_daily.name END,
                node = CASE WHEN excluded.node != '' THEN excluded.node ELSE usage_daily.node END,
                running_hours = excluded.running_hours, vcpu_hours = excluded.vcpu_hours,
                ram_gib_hours = excluded.ram_gib_hours, disk_gib_days = excluded.disk_gib_days,
                net_in_gb = excluded.net_in_gb, net_out_gb = excluded.net_out_gb,
                coverage = excluded.coverage, resolution_s = excluded.resolution_s,
                collected_at = excluded.collected_at
            """,
            (
                day.isoformat(), r["guest_id"], g.name if g else "", g.node if g else "",
                r["run_min"] / 60, r["vcpu_min"] / 60, r["mem_min"] / GIB / 60, r["disk_min"] / GIB / 1440,
                r["net_in"] / 1e9, r["net_out"] / 1e9, min(r["n"] / expected, 1.0), r["step"], collected_at,
            ),
        )
    return len(rows)


def _frozen_days(conn, cutoff: date) -> set[date]:
    return {date.fromisoformat(r[0]) for r in
            conn.execute("SELECT DISTINCT day FROM usage_daily WHERE day < ?", (cutoff.isoformat(),))}


def collect(conn, cfg: Config, pve, read_notes: bool = True, since: datetime | None = None,
            now: datetime | None = None) -> SnapshotReport:
    """One snapshot run. Normally hourly; `since` forces a look back as far as the RRD data reaches."""
    now = now or datetime.now(timezone.utc)
    now_ts = int(now.timestamp()) // MINUTE * MINUTE
    today = _local_day(now_ts, cfg)
    cutoff = today - timedelta(days=SAMPLE_RETENTION_DAYS)
    cutoff_ts = int(day_bounds(cutoff, cfg)[0].timestamp())
    frozen = _frozen_days(conn, cutoff)
    # A guest seen for the first time is read back one day (1-minute data, then 30-minute).
    bootstrap_ts = now_ts - 86400
    since_ts = int(since.timestamp()) // MINUTE * MINUTE if since else None
    last = {r[0]: r[1] for r in conn.execute("SELECT guest_id, MAX(ts) FROM usage_samples GROUP BY guest_id")}

    report = SnapshotReport()
    guests = pve.list_guests()
    by_id = {g.guest_id: g for g in guests}
    touched: set[date] = set()

    def skip(m: int) -> bool:  # never change a final day
        return m < cutoff_ts and _local_day(m, cfg) in frozen

    with conn:
        for g in guests:
            if since_ts is not None:
                want_from = since_ts
            elif g.guest_id in last:
                want_from = last[g.guest_id] + MINUTE
            else:
                want_from = bootstrap_ts
            minutes = _collect_guest(conn, pve, g, want_from, now_ts, skip, report)
            report.samples += len(minutes)
            if minutes:
                d, last_day = _local_day(min(minutes), cfg), _local_day(max(minutes), cfg)
                while d <= last_day:
                    touched.add(d)
                    d += timedelta(days=1)

        collected_at = now.isoformat(timespec="seconds")
        report.days = sorted(touched - frozen)
        for d in report.days:
            _rebuild_day(conn, cfg, d, by_id, collected_at)

        if read_notes:
            # Notes are the guest's state now, so they belong to today's row.
            for g in guests:
                try:
                    desc = pve.get_description(g)
                except ClientError as e:
                    report.warnings.append(f"{g.guest_id} ({g.name}): could not read notes: {e}")
                    continue
                report.notes_read += 1
                conn.execute(
                    """
                    INSERT INTO usage_daily (day, guest_id, name, node, description, collected_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT (day, guest_id) DO UPDATE SET description = excluded.description
                    """,
                    (today.isoformat(), g.guest_id, g.name, g.node, desc, collected_at),
                )

        conn.execute("DELETE FROM usage_samples WHERE ts < ?", (cutoff_ts,))

    report.guests = len(guests)
    log.info("snapshot: %d guests, %d samples, %d days updated, %d notes read",
             report.guests, report.samples, len(report.days), report.notes_read)
    return report


def missing_days(conn, days: list[date]) -> list[date]:
    have = {r[0] for r in conn.execute("SELECT DISTINCT day FROM usage_daily")}
    return [d for d in days if d.isoformat() not in have]
