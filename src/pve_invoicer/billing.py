"""Turn a month of daily snapshots into one priced draft invoice per customer."""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .config import Config, Rates
from .notes import BillingInfo, parse_notes

CENT = Decimal("0.01")


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def month_days(year: int, month: int) -> list[date]:
    n = calendar.monthrange(year, month)[1]
    return [date(year, month, d) for d in range(1, n + 1)]


@dataclass
class Usage:
    days: int = 0
    running_hours: float = 0.0
    vcpu_hours: float = 0.0
    ram_gib_hours: float = 0.0
    disk_gib_days: float = 0.0
    net_in_gb: float = 0.0
    net_out_gb: float = 0.0

    def add(self, r) -> None:
        self.days += 1
        for f in ("running_hours", "vcpu_hours", "ram_gib_hours", "disk_gib_days", "net_in_gb", "net_out_gb"):
            setattr(self, f, getattr(self, f) + float(r[f]))

    @property
    def net_gb(self) -> float:
        return self.net_in_gb + self.net_out_gb

    def is_zero(self) -> bool:
        return not any((self.vcpu_hours, self.ram_gib_hours, self.disk_gib_days, self.net_gb))


@dataclass
class Line:
    guest_id: str
    name: str
    usage: Usage
    cpu: Decimal
    ram: Decimal
    disk: Decimal
    net: Decimal
    disk_gib_avg: float  # average allocated GiB over the month

    @property
    def total(self) -> Decimal:
        return self.cpu + self.ram + self.disk + self.net


def price_line(guest_id: str, name: str, usage: Usage, rates: Rates, days_in_month: int) -> Line:
    D = lambda f: Decimal(repr(f))  # noqa: E731  exact decimal of the float as stored
    disk_gib_months = D(usage.disk_gib_days) / Decimal(days_in_month)
    return Line(
        guest_id=guest_id,
        name=name,
        usage=usage,
        cpu=money(D(usage.vcpu_hours) * rates.vcpu_hour),
        ram=money(D(usage.ram_gib_hours) * rates.ram_gib_hour),
        disk=money(disk_gib_months * rates.disk_gib_month),
        net=money(D(usage.net_gb) * rates.network_gb),
        disk_gib_avg=usage.disk_gib_days / days_in_month,
    )


@dataclass
class Draft:
    customer: BillingInfo
    lines: list[Line]
    hold_reason: str | None = None

    @property
    def total(self) -> Decimal:
        return sum((l.total for l in self.lines), Decimal("0.00"))


@dataclass
class Unbilled:
    guest_id: str
    name: str
    reason: str
    usage: Usage = field(default_factory=Usage)


@dataclass
class MonthResult:
    period: str
    days: list[date]
    drafts: list[Draft]
    unbilled: list[Unbilled]
    warnings: list[str]


def build_month(conn, cfg: Config, year: int, month: int) -> MonthResult:
    days = month_days(year, month)
    first, last = days[0].isoformat(), days[-1].isoformat()
    period = f"{year:04d}-{month:02d}"
    warnings: list[str] = []

    rows = conn.execute(
        "SELECT * FROM usage_daily WHERE day BETWEEN ? AND ? ORDER BY guest_id, day", (first, last)
    ).fetchall()

    # Notes known before the month starts, to carry into days where notes were not read.
    before = {
        r["guest_id"]: r["description"]
        for r in conn.execute(
            """SELECT u.guest_id, u.description FROM usage_daily u
               JOIN (SELECT guest_id, MAX(day) AS day FROM usage_daily
                     WHERE day < ? AND description IS NOT NULL GROUP BY guest_id) l
               ON u.guest_id = l.guest_id AND u.day = l.day""",
            (first,),
        )
    }

    by_guest: dict[str, list] = defaultdict(list)
    for r in rows:
        by_guest[r["guest_id"]].append(r)

    parse_cache: dict[str, object] = {}

    def parsed(desc: str | None):
        key = desc or ""
        if key not in parse_cache:
            parse_cache[key] = parse_notes(desc)
        return parse_cache[key]

    usage: dict[tuple[str, str], Usage] = defaultdict(Usage)  # (customer_id, guest_id)
    names: dict[str, str] = {}
    latest_info: dict[str, tuple[str, BillingInfo]] = {}  # customer -> (day, info)
    identities: dict[str, dict[tuple, set[str]]] = defaultdict(lambda: defaultdict(set))
    unbilled: dict[str, Unbilled] = {}

    for gid, grows in sorted(by_guest.items()):
        carry = before.get(gid)
        if carry is None:  # no notes before the month: use the first notes seen in it
            carry = next((r["description"] for r in grows if r["description"] is not None), None)
        for r in grows:
            names[gid] = r["name"] or names.get(gid, "")
            if r["description"] is not None:
                carry = r["description"]
            if r["coverage"] < cfg.min_coverage and r["running_hours"] > 0:
                warnings.append(
                    f"{gid} ({r['name']}) on {r['day']}: only {r['coverage']:.0%} of expected samples, may be under-billed"
                )
            res = parsed(carry)
            if not res.ok:
                reason = "no notes recorded" if carry is None else "; ".join(res.errors)
                u = unbilled.setdefault(gid, Unbilled(gid, r["name"], reason))
                u.reason = reason
                u.usage.add(r)
                continue
            info = res.info
            usage[(info.customer_id, gid)].add(r)
            identities[info.customer_id][info.identity()].add(f"{gid} on {r['day']}")
            prev = latest_info.get(info.customer_id)
            if prev is None or r["day"] >= prev[0]:
                latest_info[info.customer_id] = (r["day"], info)

    drafts: list[Draft] = []
    for cust in sorted(latest_info):
        lines = [
            price_line(gid, names.get(gid, ""), u, cfg.rates, len(days))
            for (c, gid), u in sorted(usage.items())
            if c == cust and not u.is_zero()
        ]
        if not lines:
            continue
        hold = None
        ids = identities[cust]
        if len(ids) > 1:
            variants = "; ".join(
                f"{email}{' cc ' + ','.join(cc) if cc else ''} ({len(seen)} guest-days, e.g. {sorted(seen)[0]})"
                for (email, cc), seen in sorted(ids.items())
            )
            hold = f"email/cc differs between guests or changed during the month: {variants}"
        drafts.append(Draft(latest_info[cust][1], lines, hold))

    unbilled_list = [u for u in sorted(unbilled.values(), key=lambda u: u.guest_id) if not u.usage.is_zero()]
    return MonthResult(period, days, drafts, unbilled_list, warnings)


def previous_month(today: date) -> tuple[int, int]:
    d = today.replace(day=1) - timedelta(days=1)
    return d.year, d.month
