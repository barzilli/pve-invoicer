from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from zoneinfo import ZoneInfo

from conftest import FakeMailer, FakePve, FakeRenderer, make_cfg, run_hourly
from pve_invoicer.billing import build_month
from pve_invoicer.db import connect
from pve_invoicer.invoicing import run_month
from pve_invoicer.render import Renderer
from pve_invoicer.snapshot import collect

ACME = "```billing\ncustomer_id: ACME\nowner: Acme Ltd\nemail: billing@acme.example\ncc: [ops@acme.example]\naddress: 1 Main St\n```"
BETA_OLD = "```billing\ncustomer_id: BETA\nemail: old@beta.example\n```"
BETA_NEW = "```billing\ncustomer_id: BETA\nemail: new@beta.example\n```"
NOW = datetime(2026, 12, 1, 5, 0, tzinfo=timezone.utc)
NOV_START = datetime(2026, 11, 1, tzinfo=ZoneInfo("Europe/Amsterdam"))
DEC_START = datetime(2026, 12, 1, tzinfo=ZoneInfo("Europe/Amsterdam"))

USAGE = {
    "qemu/100": {"vcpus": 2, "ram_gib": 4, "disk_gib": 50, "in_gb": 1, "out_gb": 2},
    "lxc/101": {"vcpus": 1, "ram_gib": 1, "disk_gib": 10, "up_hours": 0},  # stopped
    "qemu/102": {"vcpus": 1, "ram_gib": 2, "disk_gib": 20},
    "qemu/103": {"vcpus": 1, "ram_gib": 1, "disk_gib": 10},
}


@pytest.fixture(scope="module")
def november_db(tmp_path_factory):
    """Hourly snapshots for all of November, BETA's email changes on the 15th. Built once per module."""
    cfg = make_cfg(tmp_path_factory.mktemp("november"))
    conn = connect(cfg.database)
    notes = {"qemu/100": ACME, "lxc/101": "Database\n\n" + ACME, "qemu/102": "no billing here", "qemu/103": BETA_OLD}
    pve = FakePve(cfg, USAGE, notes)

    def before(now):
        if now.astimezone(cfg.timezone).date() >= date(2026, 11, 15):
            notes["qemu/103"] = BETA_NEW

    run_hourly(conn, cfg, pve, NOV_START, DEC_START + timedelta(minutes=5), before=before)
    return conn


@pytest.fixture
def november(november_db, conn):
    november_db.backup(conn)
    return conn


def test_pricing_and_grouping(cfg, november):
    m = build_month(november, cfg, 2026, 11)
    acme = next(d for d in m.drafts if d.customer.customer_id == "ACME")
    lines = {l.guest_id: l for l in acme.lines}
    l100, l101 = lines["qemu/100"], lines["lxc/101"]
    assert (l100.cpu, l100.ram, l100.disk, l100.net) == (Decimal("5.76"), Decimal("5.76"), Decimal("2.50"), Decimal("0.90"))
    assert l101.total == Decimal("0.50")  # stopped all month: disk only
    assert acme.total == Decimal("15.42")
    assert acme.hold_reason is None
    beta = next(d for d in m.drafts if d.customer.customer_id == "BETA")
    assert "old@beta.example" in beta.hold_reason and "new@beta.example" in beta.hold_reason
    assert [u.guest_id for u in m.unbilled] == ["qemu/102"]
    assert m.unbilled[0].usage.days == 30


def test_monthly_run_send_hold_rerun_release(cfg, november):
    mailer = FakeMailer()
    renderer = FakeRenderer(Renderer(cfg))
    rep = run_month(november, cfg, 2026, 11, renderer, mailer, now=NOW)

    status = {o.customer_id: (o.status, o.number) for o in rep.outcomes}
    assert status == {"ACME": ("sent", "2026-11-0001"), "BETA": ("held", "2026-11-0002")}
    invoice_mail, summary = mailer.sent
    assert invoice_mail["to"] == ["billing@acme.example"]
    assert invoice_mail["cc"] == ["ops@acme.example"]
    assert invoice_mail["bcc"] == ["accounts@example.net"]
    assert invoice_mail["attachments"] == ["invoice-2026-11-0001.pdf"]
    assert "15.42 EUR" in invoice_mail["body"]
    assert summary["to"] == ["admin@example.net"] and summary["subject"].startswith("[ACTION NEEDED]")
    assert "qemu/102" in summary["body"] and "HELD" in summary["body"]
    pdf = (cfg.output_dir / "2026-11" / "2026-11-0001_ACME.pdf").read_text()
    assert "15.42" in pdf and "Acme Ltd" in pdf

    # Re-running the month sends nothing new to owners.
    mailer2 = FakeMailer()
    rep2 = run_month(november, cfg, 2026, 11, renderer, mailer2, now=NOW)
    assert {o.customer_id: o.status for o in rep2.outcomes} == {"ACME": "already-sent", "BETA": "held"}
    assert [m["to"] for m in mailer2.sent] == [["admin@example.net"]]

    # Releasing the held invoice sends it with the same number, to the latest address.
    mailer3 = FakeMailer()
    rep3 = run_month(november, cfg, 2026, 11, renderer, mailer3, release={"BETA"}, only={"BETA"}, now=NOW)
    assert [(o.customer_id, o.status, o.number) for o in rep3.outcomes] == [("BETA", "sent", "2026-11-0002")]
    assert mailer3.sent[0]["to"] == ["new@beta.example"]


def test_smtp_failure_is_retried_with_same_number(cfg, november):
    renderer = FakeRenderer(Renderer(cfg))
    rep = run_month(november, cfg, 2026, 11, renderer, FakeMailer(fail_for={"billing@acme.example"}), now=NOW)
    assert rep.failed
    assert {o.customer_id: o.status for o in rep.outcomes}["ACME"] == "failed"
    row = november.execute("SELECT status, last_error, number FROM invoices WHERE customer_id='ACME'").fetchone()
    assert row["status"] == "failed" and "550" in row["last_error"]

    mailer = FakeMailer()
    rep2 = run_month(november, cfg, 2026, 11, renderer, mailer, now=NOW)
    acme = next(o for o in rep2.outcomes if o.customer_id == "ACME")
    assert (acme.status, acme.number) == ("sent", row["number"])


def test_dry_run_mails_only_admin_and_records_nothing(cfg, november):
    mailer = FakeMailer()
    rep = run_month(november, cfg, 2026, 11, FakeRenderer(Renderer(cfg)), mailer, dry_run=True, now=NOW)
    assert {o.status for o in rep.outcomes} == {"dry-run"}
    assert len(mailer.sent) == 1 and mailer.sent[0]["subject"].startswith("[DRY RUN]")
    assert len(mailer.sent[0]["attachments"]) == 3  # csv + 2 pdfs
    assert november.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0


def test_missing_day_reported_and_not_billed(cfg, november):
    november.execute("DELETE FROM usage_daily WHERE day='2026-11-10'")
    mailer = FakeMailer()
    rep = run_month(november, cfg, 2026, 11, FakeRenderer(Renderer(cfg)), mailer, dry_run=True, now=NOW)
    assert rep.missing_days == [date(2026, 11, 10)]
    assert "2026-11-10" in mailer.sent[0]["body"]
    acme = next(d for d in rep.month.drafts if d.customer.customer_id == "ACME")
    assert {l.usage.days for l in acme.lines} == {29}


def test_month_of_hourly_runs_is_exact(cfg, november):
    rows = november.execute(
        "SELECT * FROM usage_daily WHERE guest_id='qemu/100' AND day LIKE '2026-11-%' ORDER BY day").fetchall()
    assert len(rows) == 30
    assert {(r["running_hours"], r["vcpu_hours"], r["coverage"], r["resolution_s"]) for r in rows} == {(24, 48, 1, 60)}
    assert {round(r["net_in_gb"], 9) for r in rows} == {1.0}
    # Only 1-minute data was needed after the first run.
    m = build_month(november, cfg, 2026, 11)
    assert m.warnings == []


def test_backfilled_days_carry_notes_forward(cfg, conn):
    pve = FakePve(cfg, {"qemu/100": USAGE["qemu/100"]}, {"qemu/100": ACME})
    run_hourly(conn, cfg, pve, NOV_START, DEC_START + timedelta(minutes=5),
               read_notes=lambda now: now.astimezone(cfg.timezone).date() == date(2026, 11, 20))  # notes seen once
    m = build_month(conn, cfg, 2026, 11)
    assert not m.unbilled
    assert m.drafts[0].lines[0].usage.days == 30


def test_backfilled_month_uses_notes_recorded_after_it(cfg, conn):
    """Installed in December: November is backfilled, notes are only known from December on."""
    now = datetime(2026, 12, 10, 12, 5, tzinfo=timezone.utc)
    pve = FakePve(cfg, {"qemu/100": USAGE["qemu/100"]}, {"qemu/100": ACME}, now=now)
    collect(conn, cfg, pve, read_notes=True, now=now)  # first hourly snapshot records notes on Dec 10
    collect(conn, cfg, pve, read_notes=False, since=NOV_START, now=now)  # the invoice run's backfill
    m = build_month(conn, cfg, 2026, 11)
    assert not m.unbilled
    assert m.drafts[0].customer.customer_id == "ACME"


def test_guest_stopped_part_of_day_pays_cpu_for_running_hours_only(cfg, conn):
    usage = {"qemu/100": {"vcpus": 4, "ram_gib": 8, "disk_gib": 40, "up_hours": 9.5}}
    pve = FakePve(cfg, usage, {"qemu/100": ACME})
    run_hourly(conn, cfg, pve, datetime(2026, 11, 3, tzinfo=cfg.timezone), datetime(2026, 11, 4, 0, 5, tzinfo=cfg.timezone))
    r = conn.execute("SELECT * FROM usage_daily WHERE day='2026-11-03'").fetchone()
    assert (r["running_hours"], r["vcpu_hours"], r["ram_gib_hours"], r["disk_gib_days"]) == (9.5, 38, 76, 40)
    assert (r["coverage"], r["resolution_s"]) == (1, 60)


def test_missed_runs_fall_back_to_30_minute_data(cfg, conn):
    pve = FakePve(cfg, {"qemu/100": USAGE["qemu/100"]}, {"qemu/100": ACME})
    day = datetime(2026, 11, 3, tzinfo=cfg.timezone)
    missed = lambda now: 3 <= now.astimezone(cfg.timezone).hour <= 9 and now.astimezone(cfg.timezone).day == 3
    run_hourly(conn, cfg, pve, day, day + timedelta(days=1, minutes=5), skip=missed)
    assert ("qemu/100", "day") in pve.calls
    r = conn.execute("SELECT * FROM usage_daily WHERE day='2026-11-03'").fetchone()
    assert (r["running_hours"], r["coverage"], r["resolution_s"]) == (24, 1, 1800)
    m = build_month(conn, cfg, 2026, 11)
    assert any(w.startswith("2026-11-03: partly built from 30-minute RRD averages") for w in m.warnings)


def test_node_down_for_a_few_hours_is_caught_up(cfg, conn):
    pve = FakePve(cfg, {"qemu/100": USAGE["qemu/100"]}, {"qemu/100": ACME})
    day = datetime(2026, 11, 3, tzinfo=cfg.timezone)

    def before(now):
        pve.down = {"qemu/100"} if 10 <= now.astimezone(cfg.timezone).hour <= 12 else set()

    run_hourly(conn, cfg, pve, day, day + timedelta(days=1, minutes=5), before=before)
    r = conn.execute("SELECT * FROM usage_daily WHERE day='2026-11-03'").fetchone()
    assert (r["running_hours"], r["coverage"]) == (24, 1)


def test_low_coverage_warns(cfg, conn):
    usage = {"qemu/100": dict(USAGE["qemu/100"], exists=(0, 8.4))}  # deleted at 08:24
    pve = FakePve(cfg, usage, {"qemu/100": ACME})
    run_hourly(conn, cfg, pve, datetime(2026, 11, 3, tzinfo=cfg.timezone), datetime(2026, 11, 3, 8, 5, tzinfo=cfg.timezone))
    m = build_month(conn, cfg, 2026, 11)
    assert any("35% of expected samples" in w for w in m.warnings)


def test_dst_day_has_25_hours(cfg, conn):
    pve = FakePve(cfg, {"lxc/101": {"vcpus": 1, "ram_gib": 1, "disk_gib": 10}}, {"lxc/101": ACME})
    day = datetime(2026, 10, 25, tzinfo=cfg.timezone)
    run_hourly(conn, cfg, pve, day, datetime(2026, 10, 26, 0, 5, tzinfo=cfg.timezone))
    r = conn.execute("SELECT * FROM usage_daily WHERE day='2026-10-25'").fetchone()
    assert (r["running_hours"], r["coverage"]) == (25, 1)
    assert r["disk_gib_days"] == pytest.approx(10 * 25 / 24)


def test_since_rebuilds_missing_days_from_coarse_history(cfg, conn):
    pve = FakePve(cfg, {"qemu/100": USAGE["qemu/100"]}, {"qemu/100": ACME}, now=NOW)
    rep = collect(conn, cfg, pve, since=datetime(2026, 11, 20, tzinfo=cfg.timezone), now=NOW)
    assert [tf for _, tf in pve.calls] == ["hour", "day", "week", "month"]
    assert rep.days[0] == date(2026, 11, 20)
    rows = {r["day"]: r for r in conn.execute("SELECT * FROM usage_daily WHERE day < '2026-12-01'")}
    assert sorted(rows) == [f"2026-11-{d}" for d in range(20, 31)]
    assert {(r["running_hours"], r["coverage"]) for r in rows.values()} == {(24, 1)}
    assert rows["2026-11-20"]["resolution_s"] == 43200 and rows["2026-11-30"]["resolution_s"] == 1800


def test_final_days_are_not_changed_and_samples_pruned(cfg, november):
    before = dict(november.execute("SELECT * FROM usage_daily WHERE day='2026-11-10' AND guest_id='qemu/100'").fetchone())
    pve = FakePve(cfg, {"qemu/100": dict(USAGE["qemu/100"], vcpus=64)}, {"qemu/100": ACME}, now=NOW)
    collect(november, cfg, pve, since=datetime(2026, 11, 1, tzinfo=cfg.timezone), now=NOW)
    after = dict(november.execute("SELECT * FROM usage_daily WHERE day='2026-11-10' AND guest_id='qemu/100'").fetchone())
    assert after == before
    oldest = november.execute("SELECT MIN(ts) FROM usage_samples").fetchone()[0]
    assert oldest >= datetime(2026, 11, 24, tzinfo=cfg.timezone).timestamp()


def test_notes_go_on_todays_row(cfg, conn):
    pve = FakePve(cfg, {"qemu/100": USAGE["qemu/100"]}, {"qemu/100": ACME}, now=datetime(2026, 11, 3, 14, 5, tzinfo=cfg.timezone))
    collect(conn, cfg, pve, now=pve.now)
    days = {r["day"]: r["description"] for r in conn.execute("SELECT day, description FROM usage_daily")}
    assert days == {"2026-11-02": None, "2026-11-03": ACME}


def test_real_pdf(cfg, november):
    pytest.importorskip("weasyprint")
    rep = run_month(november, cfg, 2026, 11, Renderer(cfg), FakeMailer(), dry_run=True, now=NOW)
    pdf = rep.outcomes[0].pdf_path.read_bytes()
    assert pdf.startswith(b"%PDF")
