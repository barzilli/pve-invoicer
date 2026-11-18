"""Monthly run: number, render, send and record invoices, then email the admin a summary."""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .billing import Draft, MonthResult, build_month
from .config import Config
from .mailer import Attachment

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    customer_id: str
    number: str
    email: str
    total: str
    status: str  # sent | held | failed | already-sent | dry-run
    detail: str = ""
    pdf_path: Path | None = None


@dataclass
class RunReport:
    period: str
    dry_run: bool
    outcomes: list[Outcome] = field(default_factory=list)
    month: MonthResult | None = None
    missing_days: list[date] = field(default_factory=list)
    summary_error: str | None = None

    @property
    def failed(self) -> bool:
        return any(o.status == "failed" for o in self.outcomes) or self.summary_error is not None


def _context(cfg: Config, draft: Draft, number: str, period: str, issue_date: date) -> dict:
    return {
        "number": number,
        "period": period,
        "issue_date": issue_date,
        "due_date": issue_date + timedelta(days=cfg.due_days),
        "issuer": cfg.issuer,
        "customer": draft.customer,
        "lines": draft.lines,
        "total": draft.total,
        "currency": cfg.currency,
        "rates": cfg.rates,
    }


def _figures(ctx: dict) -> dict:
    """JSON copy of what was invoiced, for audit."""
    c = ctx["customer"]
    return {
        "number": ctx["number"],
        "period": ctx["period"],
        "issue_date": ctx["issue_date"].isoformat(),
        "due_date": ctx["due_date"].isoformat(),
        "currency": ctx["currency"],
        "customer": {"customer_id": c.customer_id, "email": c.email, "cc": list(c.cc), "owner": c.owner,
                     "address": c.address, "vat_id": c.vat_id, "po_number": c.po_number},
        "rates": {k: str(v) for k, v in vars(ctx["rates"]).items()},
        "lines": [
            {
                "guest_id": l.guest_id, "name": l.name, "days": l.usage.days,
                "running_hours": l.usage.running_hours, "vcpu_hours": l.usage.vcpu_hours,
                "ram_gib_hours": l.usage.ram_gib_hours, "disk_gib_days": l.usage.disk_gib_days,
                "net_in_gb": l.usage.net_in_gb, "net_out_gb": l.usage.net_out_gb,
                "cpu": str(l.cpu), "ram": str(l.ram), "disk": str(l.disk), "net": str(l.net), "total": str(l.total),
            }
            for l in ctx["lines"]
        ],
        "total": str(ctx["total"]),
    }


def _number(cfg: Config, year: int, month: int, seq: int) -> str:
    return cfg.number_format.format(year=year, month=month, seq=seq)


def run_month(conn, cfg: Config, year: int, month: int, renderer, mailer, *, dry_run: bool = False,
              release: set[str] | None = None, only: set[str] | None = None,
              now: datetime | None = None) -> RunReport:
    now = now or datetime.now(timezone.utc)
    issue_date = now.astimezone(cfg.timezone).date()
    period = f"{year:04d}-{month:02d}"
    release = release or set()
    report = RunReport(period, dry_run)
    result = build_month(conn, cfg, year, month)
    report.month = result

    have = {r[0] for r in conn.execute("SELECT DISTINCT day FROM usage_daily WHERE day LIKE ?", (period + "-%",))}
    report.missing_days = [d for d in result.days if d.isoformat() not in have]

    out_dir = cfg.output_dir / ("dry-run" if dry_run else "") / period
    dry_pdfs: list[Attachment] = []

    for i, draft in enumerate(result.drafts, start=1):
        cust = draft.customer.customer_id
        if only and cust not in only:
            continue
        existing = conn.execute("SELECT * FROM invoices WHERE period=? AND customer_id=?", (period, cust)).fetchone()

        if existing and existing["status"] == "sent":
            report.outcomes.append(Outcome(cust, existing["number"], existing["email"], existing["total"],
                                           "already-sent", f"sent {existing['sent_at']}"))
            continue

        if dry_run:
            number = existing["number"] if existing else f"DRAFT-{period}-{i:04d}"
        elif existing:
            number = existing["number"]
        else:
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM invoices WHERE period=?", (period,)).fetchone()[0]
            number = _number(cfg, year, month, seq)
            with conn:
                conn.execute(
                    """INSERT INTO invoices (period, seq, number, customer_id, email, currency, total, status, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (period, seq, number, cust, draft.customer.email, cfg.currency, str(draft.total), "failed",
                     now.isoformat(timespec="seconds")),
                )

        ctx = _context(cfg, draft, number, period, issue_date)
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in f"{number}_{cust}")
        pdf_path = out_dir / f"{safe}.pdf"
        json_path = out_dir / f"{safe}.json"
        pdf = renderer.pdf(pdf_path, **ctx)
        json_path.write_text(json.dumps(_figures(ctx), indent=2), encoding="utf-8")
        total = str(draft.total)

        if dry_run:
            dry_pdfs.append(Attachment(pdf_path.name, pdf, "application/pdf"))
            report.outcomes.append(Outcome(cust, number, draft.customer.email, total, "dry-run",
                                           draft.hold_reason or "", pdf_path))
            continue

        def record(status: str, **cols) -> None:
            fields = {"status": status, "email": draft.customer.email, "total": total,
                      "pdf_path": str(pdf_path), "json_path": str(json_path), **cols}
            sets = ", ".join(f"{k}=?" for k in fields)
            with conn:
                conn.execute(f"UPDATE invoices SET {sets} WHERE period=? AND customer_id=?",
                             (*fields.values(), period, cust))

        if draft.hold_reason and cust not in release:
            record("held", hold_reason=draft.hold_reason)
            report.outcomes.append(Outcome(cust, number, draft.customer.email, total, "held", draft.hold_reason, pdf_path))
            continue

        try:
            msg_id = mailer.send(
                to=[draft.customer.email],
                cc=list(draft.customer.cc),
                bcc=list(cfg.smtp.bcc),
                subject=f"Invoice {number} for {period} from {cfg.issuer.name}",
                body=renderer.email_body(**ctx),
                attachments=[Attachment(f"invoice-{number}.pdf", pdf, "application/pdf")],
            )
        except Exception as e:  # noqa: BLE001  any SMTP failure is recorded and retried next run
            log.exception("sending %s to %s failed", number, draft.customer.email)
            record("failed", last_error=str(e)[:500])
            report.outcomes.append(Outcome(cust, number, draft.customer.email, total, "failed", str(e), pdf_path))
            continue
        record("sent", sent_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), message_id=msg_id,
               hold_reason=None, last_error=None)
        report.outcomes.append(Outcome(cust, number, draft.customer.email, total, "sent", "", pdf_path))

    try:
        send_summary(cfg, report, renderer, mailer, dry_pdfs)
    except Exception as e:  # noqa: BLE001
        log.exception("admin summary failed")
        report.summary_error = str(e)
    return report


def summary_csv(report: RunReport, currency: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["number", "customer_id", "email", f"total_{currency}", "status", "detail"])
    for o in report.outcomes:
        w.writerow([o.number, o.customer_id, o.email, o.total, o.status, o.detail])
    return buf.getvalue()


def summary_text(cfg: Config, report: RunReport) -> str:
    m = report.month
    by = lambda s: [o for o in report.outcomes if o.status == s]  # noqa: E731
    lines = [f"pve-invoicer {'DRY RUN ' if report.dry_run else ''}summary for {report.period}", ""]
    for status, label in (("dry-run", "Would send"), ("sent", "Sent"), ("held", "HELD, not sent"),
                          ("failed", "FAILED, will retry next run"), ("already-sent", "Already sent earlier")):
        items = by(status)
        if items:
            lines.append(f"{label}: {len(items)}")
            for o in items:
                lines.append(f"  {o.number}  {o.customer_id:<20} {o.email:<35} {o.total:>12} {cfg.currency}"
                             + (f"\n      {o.detail}" if o.detail else ""))
            lines.append("")
    if report.dry_run:
        held = [o for o in by("dry-run") if o.detail]
        if held:
            lines.append("These would be HELD in a real run:")
            lines += [f"  {o.customer_id}: {o.detail}" for o in held]
            lines.append("")
    if any(o.status == "held" for o in report.outcomes):
        lines.append(f"Release a held invoice after checking it: pve-invoicer release {report.period} <customer_id>")
        lines.append("")
    if m and m.unbilled:
        lines.append(f"Unbilled guests (no valid billing block in notes): {len(m.unbilled)}")
        for u in m.unbilled:
            lines.append(f"  {u.guest_id:<12} {u.name:<25} {u.usage.days:>2} days  {u.reason}")
        lines.append("")
    if report.missing_days:
        lines.append("Days with no snapshot (usage on these days is not billed): "
                     + ", ".join(d.isoformat() for d in report.missing_days))
        lines.append("")
    if m and m.warnings:
        lines.append(f"Warnings: {len(m.warnings)}")
        lines += [f"  {w}" for w in m.warnings[:200]]
        if len(m.warnings) > 200:
            lines.append(f"  ... and {len(m.warnings) - 200} more")
    return "\n".join(lines).rstrip() + "\n"


def send_summary(cfg: Config, report: RunReport, renderer, mailer, pdfs: list[Attachment]) -> None:
    attachments = [Attachment(f"invoices-{report.period}.csv", summary_csv(report, cfg.currency).encode(), "text/csv"),
                   *pdfs]
    flag = ""
    if report.dry_run:
        flag = "[DRY RUN] "
    elif any(o.status in ("held", "failed") for o in report.outcomes) or (report.month and report.month.unbilled):
        flag = "[ACTION NEEDED] "
    mailer.send(to=[cfg.smtp.admin_email], subject=f"{flag}Proxmox invoices {report.period}",
                body=summary_text(cfg, report), attachments=attachments)
