"""Command line: pve-invoicer snapshot | invoice | release | check-notes."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, time, timezone

from . import __version__
from .billing import month_days, previous_month
from .clients import ClientError, ProxmoxClient
from .config import ConfigError, load_config
from .db import connect
from .invoicing import run_month
from .mailer import SmtpMailer
from .notes import parse_notes
from .render import Renderer
from .snapshot import collect, missing_days

log = logging.getLogger("pve_invoicer")


def _month(s: str) -> tuple[int, int]:
    try:
        d = datetime.strptime(s, "%Y-%m")
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM") from None
    return d.year, d.month


def _day(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD") from None


def cmd_snapshot(cfg, args) -> int:
    conn = connect(cfg.database)
    since = datetime.combine(args.since, time(0), tzinfo=cfg.timezone) if args.since else None
    rep = collect(conn, cfg, ProxmoxClient(cfg.proxmox), read_notes=not args.no_notes, since=since)
    days = f"{rep.days[0]}..{rep.days[-1]}" if len(rep.days) > 1 else (str(rep.days[0]) if rep.days else "none")
    print(f"{rep.guests} guests, {rep.samples} minute samples, days updated: {days}, notes read for {rep.notes_read}")
    for w in rep.warnings:
        print("warning:", w, file=sys.stderr)
    return 0


def cmd_invoice(cfg, args, release=None, only=None) -> int:
    conn = connect(cfg.database)
    today = datetime.now(timezone.utc).astimezone(cfg.timezone).date()
    year, month = args.month or previous_month(today)
    if (year, month) >= (today.year, today.month):
        print(f"{year}-{month:02d} has not ended yet", file=sys.stderr)
        return 2

    if not args.no_backfill:
        gaps = missing_days(conn, month_days(year, month))
        if gaps:
            since = datetime.combine(gaps[0], time(0), tzinfo=cfg.timezone)
            log.info("backfilling %d missing day(s) from %s out of the Proxmox RRD history", len(gaps), gaps[0])
            try:
                collect(conn, cfg, ProxmoxClient(cfg.proxmox), read_notes=False, since=since)
            except ClientError as e:
                log.error("could not backfill from %s: %s", gaps[0], e)
            still = set(missing_days(conn, gaps))
            for d in gaps:
                if d in still:
                    log.error("no usage data for %s, not even in the Proxmox RRD history", d)
                else:
                    log.warning("backfilled missing day %s from Proxmox RRD history (notes carried forward)", d)

    log.info("building invoices for %d-%02d%s", year, month, " (dry run)" if args.dry_run else "")
    rep = run_month(conn, cfg, year, month, Renderer(cfg), SmtpMailer(cfg.smtp),
                    dry_run=args.dry_run, release=release, only=only)
    for o in rep.outcomes:
        print(f"{o.status:<12} {o.number:<18} {o.customer_id:<20} {o.email:<35} {o.total:>12} {cfg.currency}"
              + (f"  ({o.detail})" if o.detail else ""))
    if rep.month and rep.month.unbilled:
        print(f"{len(rep.month.unbilled)} guest(s) unbilled: no valid billing block (see admin summary)")
    if rep.summary_error:
        print("admin summary email failed:", rep.summary_error, file=sys.stderr)
    return 1 if rep.failed else 0


def cmd_release(cfg, args) -> int:
    args.dry_run, args.no_backfill = False, True
    return cmd_invoice(cfg, args, release={args.customer_id}, only={args.customer_id})


def cmd_check_notes(cfg, args) -> int:
    pve = ProxmoxClient(cfg.proxmox)
    bad = 0
    for g in sorted(pve.list_guests(), key=lambda g: g.vmid):
        res = parse_notes(pve.get_description(g))
        kind = "template" if g.template else g.type
        if res.ok:
            print(f"OK    {g.guest_id:<12} {g.name:<25} {kind:<8} {res.info.customer_id:<20} {res.info.email}")
        else:
            bad += 0 if g.template else 1
            print(f"MISS  {g.guest_id:<12} {g.name:<25} {kind:<8} {'; '.join(res.errors)}")
        for w in res.warnings:
            print(f"      warning: {w}")
    print(f"\n{bad} non-template guest(s) without a valid billing block")
    return 1 if bad else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pve-invoicer", description=__doc__)
    p.add_argument("-c", "--config", default="/etc/pve-invoicer/config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="store recent guest usage and notes from the Proxmox API (run hourly)")
    s.add_argument("--since", type=_day, metavar="YYYY-MM-DD",
                   help="also rebuild from this day, as far back as the Proxmox RRD history reaches")
    s.add_argument("--no-notes", action="store_true", help="do not read guest notes")

    i = sub.add_parser("invoice", help="invoice a month (default: last month) and email owners")
    i.add_argument("--month", type=_month, help="YYYY-MM")
    i.add_argument("--dry-run", action="store_true", help="render PDFs and email only the admin")
    i.add_argument("--no-backfill", action="store_true", help="do not rebuild missing days from the Proxmox RRD history")

    r = sub.add_parser("release", help="send a held invoice after checking it")
    r.add_argument("month", type=_month, help="YYYY-MM")
    r.add_argument("customer_id")

    sub.add_parser("check-notes", help="validate the billing block of every guest now")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
        return {"snapshot": cmd_snapshot, "invoice": cmd_invoice, "release": cmd_release,
                "check-notes": cmd_check_notes}[args.cmd](cfg, args)
    except (ConfigError, ClientError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
