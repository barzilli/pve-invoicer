from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pve_invoicer.clients import Guest
from pve_invoicer.config import Config, Issuer, PrometheusConfig, ProxmoxConfig, Rates, SmtpConfig
from pve_invoicer.db import connect
from pve_invoicer.snapshot import build_queries


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        timezone=ZoneInfo("Europe/Amsterdam"),
        database=tmp_path / "t.db",
        output_dir=tmp_path / "out",
        currency="EUR",
        rates=Rates(Decimal("0.004"), Decimal("0.002"), Decimal("0.05"), Decimal("0.01")),
        prometheus=PrometheusConfig(url="http://prom"),
        proxmox=ProxmoxConfig(url="https://pve", token_id="billing@pve!inv", token_secret="x"),
        smtp=SmtpConfig("smtp", 587, "starttls", None, None, "billing@example.net", "admin@example.net",
                        ("accounts@example.net",)),
        issuer=Issuer("Example Hosting", "1 Main St\nTown", "billing@example.net", "IBAN XX00 0000"),
    )


@pytest.fixture
def conn(cfg):
    return connect(cfg.database)


class FakeProm:
    """Answers the snapshot queries from per-day tables: data[day][key] = {guest_id: value}."""

    def __init__(self, cfg: Config, data: dict):
        self.cfg, self.data, self.calls = cfg, data, []

    def query(self, promql: str, at: float):
        self.calls.append(promql)
        end = datetime.fromtimestamp(at, timezone.utc).astimezone(self.cfg.timezone)
        day = (end - timedelta(hours=12)).date()
        for window in (82800, 86400, 90000):
            for key, q in build_queries(self.cfg, window).items():
                if q == promql:
                    table = self.data.get(day, {}).get(key, {})
                    if key == "info":
                        return [({"id": g, "name": n, "node": "pve1"}, 1.0) for g, n in table.items()]
                    return [({"id": g}, v) for g, v in table.items()]
        raise AssertionError(f"unexpected query {promql}")


class FakePve:
    def __init__(self, notes: dict[str, str]):
        self.notes = notes

    def list_guests(self):
        return [Guest(g, "pve1", int(g.split("/")[1]), g.split("/")[0], f"vm{g.split('/')[1]}", False)
                for g in self.notes]

    def get_description(self, guest):
        return self.notes[guest.guest_id]


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


def day_usage(guests: dict[str, dict]) -> dict:
    """Build one day's query answers from {guest_id: {vcpus, ram_gib, disk_gib, up_hours, in_gb, out_gb}}."""
    out = {k: {} for k in ("running_hours", "vcpu_hours", "ram_gib_hours", "disk_gib_days", "net_in_gb",
                           "net_out_gb", "samples", "info")}
    for g, u in guests.items():
        h = u.get("up_hours", 24)
        out["running_hours"][g] = h
        out["vcpu_hours"][g] = u["vcpus"] * h
        out["ram_gib_hours"][g] = u["ram_gib"] * h
        out["disk_gib_days"][g] = u["disk_gib"]
        out["net_in_gb"][g] = u.get("in_gb", 0)
        out["net_out_gb"][g] = u.get("out_gb", 0)
        out["samples"][g] = 288
        out["info"][g] = f"vm{g.split('/')[1]}"
    return out


def month_of(year: int, month: int):
    d = date(year, month, 1)
    while d.month == month:
        yield d
        d += timedelta(days=1)
