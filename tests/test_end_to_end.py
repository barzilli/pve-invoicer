from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from conftest import FakeMailer, FakePve, FakeProm, FakeRenderer, day_usage, month_of
from pve_invoicer.billing import build_month
from pve_invoicer.invoicing import run_month
from pve_invoicer.render import Renderer
from pve_invoicer.snapshot import build_queries, snapshot_day

ACME = "```billing\ncustomer_id: ACME\nowner: Acme Ltd\nemail: billing@acme.example\ncc: [ops@acme.example]\naddress: 1 Main St\n```"
BETA_OLD = "```billing\ncustomer_id: BETA\nemail: old@beta.example\n```"
BETA_NEW = "```billing\ncustomer_id: BETA\nemail: new@beta.example\n```"
NOW = datetime(2026, 12, 1, 5, 0, tzinfo=timezone.utc)

USAGE = {
    "qemu/100": {"vcpus": 2, "ram_gib": 4, "disk_gib": 50, "in_gb": 1, "out_gb": 2},
    "lxc/101": {"vcpus": 1, "ram_gib": 1, "disk_gib": 10, "up_hours": 0},
    "qemu/102": {"vcpus": 1, "ram_gib": 2, "disk_gib": 20},
    "qemu/103": {"vcpus": 1, "ram_gib": 1, "disk_gib": 10},
}


@pytest.fixture
def november(cfg, conn):
    data = {d: day_usage(USAGE) for d in month_of(2026, 11)}
    prom = FakeProm(cfg, data)
    notes = {"qemu/100": ACME, "lxc/101": "Database\n\n" + ACME, "qemu/102": "no billing here", "qemu/103": BETA_OLD}
    pve = FakePve(notes)
    for d in month_of(2026, 11):
        if d.day == 15:
            notes["qemu/103"] = BETA_NEW
        snapshot_day(conn, cfg, prom, pve, d, now=NOW)
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


def test_backfilled_days_carry_notes_forward(cfg, conn):
    data = {d: day_usage({"qemu/100": USAGE["qemu/100"]}) for d in month_of(2026, 11)}
    prom, pve = FakeProm(cfg, data), FakePve({"qemu/100": ACME})
    for d in month_of(2026, 11):
        snapshot_day(conn, cfg, prom, pve, d, read_notes=(d.day == 20), now=NOW)  # notes only seen once
    m = build_month(conn, cfg, 2026, 11)
    assert not m.unbilled
    assert m.drafts[0].lines[0].usage.days == 30


def test_low_coverage_warns(cfg, conn):
    data = {date(2026, 11, 3): day_usage({"qemu/100": USAGE["qemu/100"]})}
    data[date(2026, 11, 3)]["samples"]["qemu/100"] = 100
    snapshot_day(conn, cfg, FakeProm(cfg, data), FakePve({"qemu/100": ACME}), date(2026, 11, 3), now=NOW)
    m = build_month(conn, cfg, 2026, 11)
    assert any("35% of expected samples" in w for w in m.warnings)


def test_dst_day_window_and_dedup(cfg):
    q = build_queries(cfg, 90000)
    assert "[90000s:5m]" in q["vcpu_hours"]
    assert q["vcpu_hours"].count("max by (id)") == 2
    assert 'pve_network_receive_bytes_total{id=~"(qemu|lxc)/.*"}' in q["net_in_gb"]


def test_snapshot_queries_the_dst_day_with_25_hours(cfg, conn):
    prom = FakeProm(cfg, {})
    snapshot_day(conn, cfg, prom, FakePve({}), date(2026, 10, 25), read_notes=False, now=NOW)
    assert all("90000s" in c for c in prom.calls)


def test_snapshot_refuses_unfinished_day(cfg, conn):
    with pytest.raises(ValueError, match="has not finished"):
        snapshot_day(conn, cfg, FakeProm(cfg, {}), FakePve({}), date(2026, 12, 1), now=NOW)


def test_real_pdf(cfg, november):
    pytest.importorskip("weasyprint")
    rep = run_month(november, cfg, 2026, 11, Renderer(cfg), FakeMailer(), dry_run=True, now=NOW)
    pdf = rep.outcomes[0].pdf_path.read_bytes()
    assert pdf.startswith(b"%PDF")
