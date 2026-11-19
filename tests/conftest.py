from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pve_invoicer.clients import ClientError, Guest
from pve_invoicer.config import Config, Issuer, ProxmoxConfig, Rates, SmtpConfig
from pve_invoicer.db import connect
from pve_invoicer.snapshot import collect


def make_cfg(tmp_path: Path) -> Config:
    return Config(
        timezone=ZoneInfo("Europe/Amsterdam"),
        database=tmp_path / "t.db",
        output_dir=tmp_path / "out",
        currency="EUR",
        rates=Rates(Decimal("0.004"), Decimal("0.002"), Decimal("0.05"), Decimal("0.01")),
        proxmox=ProxmoxConfig(url="https://pve", token_id="billing@pve!inv", token_secret="x"),
        smtp=SmtpConfig("smtp", 587, "starttls", None, None, "billing@example.net", "admin@example.net",
                        ("accounts@example.net",)),
        issuer=Issuer("Example Hosting", "1 Main St\nTown", "billing@example.net", "IBAN XX00 0000"),
    )



@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return make_cfg(tmp_path)


@pytest.fixture
def conn(cfg):
    return connect(cfg.database)


class FakePve:
    """A cluster whose RRD history is computed from a per-minute model of each guest.

    usage[guest_id] = {vcpus, ram_gib, disk_gib, up_hours (from local midnight, default all day),
    exists (local hours (from, to), default all day), in_gb, out_gb (per day)}.
    Points are built like Proxmox RRD consolidation: 70 points per timeframe, AVERAGE, a value is
    known when at least half the interval is known, `time` is the start of the interval.
    """

    STEPS = {"hour": 60, "day": 1800, "week": 10800, "month": 43200, "year": 604800}

    def __init__(self, cfg: Config, usage: dict[str, dict], notes: dict[str, str], now: datetime | None = None):
        self.cfg, self.usage, self.notes, self.now = cfg, usage, notes, now
        self.calls: list[tuple[str, str]] = []
        self.down: set[str] = set()  # guest ids whose rrddata fails

    def list_guests(self):
        return [Guest(g, "pve1", int(g.split("/")[1]), g.split("/")[0], f"vm{g.split('/')[1]}", False, "running")
                for g in sorted(set(self.usage) | set(self.notes))]

    def get_description(self, guest):
        return self.notes.get(guest.guest_id, "")

    def minute(self, gid: str, ts: int):
        u = self.usage.get(gid)
        if u is None:
            return None
        local = datetime.fromtimestamp(ts, timezone.utc).astimezone(self.cfg.timezone)
        midnight = datetime.combine(local.date(), time(0), tzinfo=self.cfg.timezone)
        h = (ts - midnight.timestamp()) / 3600
        lo, hi = u.get("exists", (0, 99))
        if not lo <= h < hi:
            return None
        up = u.get("up_hours")
        running = up is None or h < up
        secs = (up if up is not None else 24) * 3600
        return {
            "running": running, "maxcpu": u["vcpus"], "maxmem": u["ram_gib"] * 1073741824,
            "maxdisk": u["disk_gib"] * 1073741824,
            "netin": u.get("in_gb", 0) * 1e9 / secs if running and secs else 0.0,
            "netout": u.get("out_gb", 0) * 1e9 / secs if running and secs else 0.0,
        }

    def rrddata(self, guest, timeframe, cf="AVERAGE"):
        self.calls.append((guest.guest_id, timeframe))
        if guest.guest_id in self.down:
            raise ClientError("HTTP 595 no route to host")
        step = self.STEPS[timeframe]
        last = int(self.now.timestamp()) // step * step - step  # last complete interval
        out = []
        for i in range(69, -1, -1):
            t = last - i * step
            mins = [self.minute(guest.guest_id, m) for m in range(t, t + step, 60)]
            known = [m for m in mins if m]
            if len(known) * 2 < len(mins):
                out.append({"time": t})
                continue
            n = len(known)
            p = {"time": t, "maxcpu": sum(m["maxcpu"] for m in known) / n,
                 "maxmem": sum(m["maxmem"] for m in known) / n, "maxdisk": sum(m["maxdisk"] for m in known) / n}
            running = [m for m in known if m["running"]]
            if len(running) * 2 >= len(mins):
                p["cpu"] = 0.05
                p["netin"] = sum(m["netin"] for m in running) / len(running)
                p["netout"] = sum(m["netout"] for m in running) / len(running)
            out.append(p)
        return out


def run_hourly(conn, cfg, pve, start: datetime, end: datetime, read_notes=lambda now: True, before=None,
               skip=lambda now: False):
    """Run the snapshot at :05 past every hour from `start` up to `end` (aware datetimes)."""
    now = start.astimezone(timezone.utc).replace(minute=5, second=0, microsecond=0)
    while now <= end:
        if not skip(now):
            if before:
                before(now)
            pve.now = now
            collect(conn, cfg, pve, read_notes=read_notes(now), now=now)
        now += timedelta(hours=1)


class FakeMailer:
    def __init__(self, fail_for: set[str] = frozenset()):
        self.sent, self.fail_for = [], set(fail_for)

    def send(self, to, subject, body, cc=(), bcc=(), attachments=()):
        if to[0] in self.fail_for:
            raise OSError("550 mailbox unavailable")
        self.sent.append({"to": list(to), "cc": list(cc), "bcc": list(bcc), "subject": subject, "body": body,
                          "attachments": [a.filename for a in attachments]})
        return f"<{len(self.sent)}@test>"


class FakeRenderer:
    def __init__(self, real):
        self.real = real

    def pdf(self, path, **ctx):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.real.html(**ctx).encode()
        path.write_bytes(data)
        return data

    def email_body(self, **ctx):
        return self.real.email_body(**ctx)


def month_of(year: int, month: int):
    d = date(year, month, 1)
    while d.month == month:
        yield d
        d += timedelta(days=1)
