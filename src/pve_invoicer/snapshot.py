"""Daily snapshot: one day's usage per guest from Prometheus, plus each guest's notes."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from .clients import ClientError
from .config import Config

log = logging.getLogger(__name__)

GIB = 1073741824
FIELDS = ("running_hours", "vcpu_hours", "ram_gib_hours", "disk_gib_days", "net_in_gb", "net_out_gb")


def day_bounds(day: date, cfg: Config) -> tuple[datetime, datetime]:
    """Local midnight to next local midnight (23 or 25 hours on DST change days)."""
    start = datetime.combine(day, time(0), tzinfo=cfg.timezone)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=cfg.timezone)
    return start, end


def build_queries(cfg: Config, window_seconds: int) -> dict[str, str]:
    """PromQL for one day, evaluated at the end of the day.

    Every metric is reduced with `max by (id)` so it does not matter how many
    exporters scrape the same cluster. Time-based measures use sum_over_time over a
    subquery, so a guest that existed for half a day is billed for half a day and
    gaps in Prometheus data under-bill rather than over-bill.
    """
    sel = 'id=~"(qemu|lxc)/.*"'
    if cfg.prometheus.extra_selector:
        sel += "," + cfg.prometheus.extra_selector
    s = cfg.prometheus.step_minutes
    step, win = f"{s}m", f"{window_seconds}s"
    hours_per_sample = s / 60
    days_per_sample = s / 1440

    def m(metric: str) -> str:
        return f"max by (id) ({metric}{{{sel}}})"

    up = m("pve_up")
    return {
        "running_hours": f"sum_over_time({up}[{win}:{step}]) * {hours_per_sample}",
        "vcpu_hours": f"sum_over_time(({up} * on(id) {m('pve_cpu_usage_limit')})[{win}:{step}]) * {hours_per_sample}",
        "ram_gib_hours": (
            f"sum_over_time(({up} * on(id) {m('pve_memory_size_bytes')})[{win}:{step}]) * {hours_per_sample} / {GIB}"
        ),
        "disk_gib_days": f"sum_over_time({m('pve_disk_size_bytes')}[{win}:{step}]) * {days_per_sample} / {GIB}",
        "net_in_gb": f"max by (id) (increase(pve_network_receive_bytes_total{{{sel}}}[{win}])) / 1e9",
        "net_out_gb": f"max by (id) (increase(pve_network_transmit_bytes_total{{{sel}}}[{win}])) / 1e9",
        "samples": f"count_over_time({up}[{win}:{step}])",
        "info": f"max by (id, name, node) (last_over_time(pve_guest_info{{{sel}}}[{win}]))",
    }


@dataclass
class SnapshotReport:
    day: date
    guests: int = 0
    notes_read: int = 0
    warnings: list[str] = field(default_factory=list)


def snapshot_day(conn, cfg: Config, prom, pve, day: date, read_notes: bool = True, now: datetime | None = None) -> SnapshotReport:
    start, end = day_bounds(day, cfg)
    now = now or datetime.now(timezone.utc)
    if end > now:
        raise ValueError(f"{day} has not finished yet in {cfg.timezone.key}; nothing to snapshot")

    # Subtract as UTC: aware datetimes in the same zone ignore DST when subtracted.
    window = int(end.timestamp() - start.timestamp())
    at = end.timestamp()
    queries = build_queries(cfg, window)
    report = SnapshotReport(day)

    rows: dict[str, dict] = {}

    def row(gid: str) -> dict:
        return rows.setdefault(gid, {f: 0.0 for f in FIELDS} | {"name": "", "node": "", "samples": 0.0, "description": None})

    for key in (*FIELDS, "samples"):
        for labels, value in prom.query(queries[key], at):
            gid = labels.get("id")
            if gid:
                row(gid)[key] = max(value, 0.0)
    for labels, _ in prom.query(queries["info"], at):
        gid = labels.get("id")
        if gid in rows:
            rows[gid]["name"] = labels.get("name", "")
            rows[gid]["node"] = labels.get("node", "")

    if read_notes:
        for g in pve.list_guests():
            r = row(g.guest_id)
            r["name"] = r["name"] or g.name
            r["node"] = g.node
            try:
                r["description"] = pve.get_description(g)
                report.notes_read += 1
            except ClientError as e:
                report.warnings.append(f"{g.guest_id} ({g.name}): could not read notes: {e}")

    expected = window / (cfg.prometheus.step_minutes * 60)
    collected_at = now.isoformat(timespec="seconds")
    with conn:
        for gid, r in rows.items():
            coverage = min(r["samples"] / expected, 1.0) if expected else 0.0
            conn.execute(
                """
                INSERT INTO usage_daily (day, guest_id, name, node, running_hours, vcpu_hours, ram_gib_hours,
                                         disk_gib_days, net_in_gb, net_out_gb, coverage, description, collected_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (day, guest_id) DO UPDATE SET
                    name = CASE WHEN excluded.name != '' THEN excluded.name ELSE usage_daily.name END,
                    node = CASE WHEN excluded.node != '' THEN excluded.node ELSE usage_daily.node END,
                    running_hours = excluded.running_hours, vcpu_hours = excluded.vcpu_hours,
                    ram_gib_hours = excluded.ram_gib_hours, disk_gib_days = excluded.disk_gib_days,
                    net_in_gb = excluded.net_in_gb, net_out_gb = excluded.net_out_gb,
                    coverage = excluded.coverage,
                    description = COALESCE(excluded.description, usage_daily.description),
                    collected_at = excluded.collected_at
                """,
                (
                    day.isoformat(), gid, r["name"], r["node"],
                    r["running_hours"], r["vcpu_hours"], r["ram_gib_hours"], r["disk_gib_days"],
                    r["net_in_gb"], r["net_out_gb"], coverage, r["description"], collected_at,
                ),
            )
    report.guests = len(rows)
    log.info("snapshot %s: %d guests, %d notes read", day, report.guests, report.notes_read)
    return report


def missing_days(conn, days: list[date]) -> list[date]:
    have = {r[0] for r in conn.execute("SELECT DISTINCT day FROM usage_daily")}
    return [d for d in days if d.isoformat() not in have]
