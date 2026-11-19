# pve-invoicer

Monthly usage invoices for Proxmox VE guests (QEMU VMs and LXC containers). It reads usage straight from
the Proxmox VE API with a read-only token, reads each guest's owner from a `billing` block in its notes,
and emails every owner one PDF invoice per month.

It does no provisioning, writes nothing to Proxmox, and needs nothing installed on the Proxmox hosts.

## How it works

| Job | When | What it does |
| --- | --- | --- |
| `pve-invoicer snapshot` | hourly, at :05 | Lists guests from `/cluster/resources`, stores each guest's last 70 minutes of 1-minute usage from `/nodes/{node}/{qemu,lxc}/{vmid}/rrddata`, updates the day's usage rows and records every guest's notes, all in SQLite. |
| `pve-invoicer invoice` | 1st of the month, 06:00 | Rebuilds any missing days from the Proxmox RRD history, prices last month, writes a PDF and JSON per customer, emails each owner, then emails the admin a summary. |

### Where the numbers come from

Proxmox keeps a round-robin history (RRD) for every guest. The API serves it per timeframe, about 70 points
each: `hour` at 1-minute resolution, `day` at 30 minutes, `week` at 3 hours, `month` at 12 hours.

The hourly snapshot reads the `hour` timeframe, so runs overlap by about 10 minutes and every minute of a
guest's life is stored at 1-minute resolution. If a run was missed or late, a node was unreachable, or a guest
is new, the snapshot reads coarser timeframes for just the minutes the finer data does not cover. Such days
are flagged in the admin summary as "partly built from 30-minute RRD averages", because a guest started or
stopped inside a coarse interval is counted as running or stopped for all of it.

Per-minute samples are kept for 7 days. After that, days that have a usage row are final and later runs
never change them.

A guest deleted between two runs is billed up to the last run before its deletion (at most about an hour is
lost), because a deleted guest no longer appears in `/cluster/resources`.

### What is billed

| Item | Measured as | Rate key |
| --- | --- | --- |
| CPU | allocated vCPUs (`maxcpu`) × minutes the guest runs (RRD `cpu` is reported) | `vcpu_hour` |
| RAM | allocated GiB (`maxmem`) × minutes the guest runs | `ram_gib_hour` |
| Disk | allocated GiB (`maxdisk`) for every minute the guest exists, running or stopped | `disk_gib_month` (prorated by day) |
| Traffic | GB in + out (`netin` + `netout`, bytes per second × interval) | `network_gb` |

Allocations are taken from each RRD point, so a guest resized mid-day pays the old size before and the new
size after. A guest created mid-day pays for part of the day, and a gap in the data under-bills rather than
over-bills. Each component of each guest line is rounded to cents, and the invoice total is their sum.
There is no tax.

For QEMU VMs, Proxmox reports `maxdisk` as the size of the boot disk, not the sum of all disks.

Day and month boundaries follow `timezone` in the config, including 23- and 25-hour days at DST changes.

## The billing block in VM notes

Add this anywhere in a guest's **Notes** (Summary → Notes in the GUI). The rest of the notes stay free text.

````
```billing
customer_id: ACME               # required; groups guests onto one invoice
email: billing@acme.example     # required; invoice recipient
cc: [ops@acme.example]          # optional, one address or a list
owner: Acme Ltd                 # optional, shown on the invoice
address: |                      # optional, multi-line
  1 Main Street
  Springfield 12345
vat_id: XX123456789             # optional
po_number: PO-2026-118          # optional
reference: Project Phoenix      # optional free text
```
````

Rules:

* Guests with the same `customer_id` share one invoice. Their `email`/`cc` must match.
* A guest with no block or an invalid block is **not billed**. It is listed in the admin summary as unbilled.
* Usage is attributed day by day to the block valid that day, so a VMID handed to a new owner mid-month is
  split correctly. Days whose notes were not read (backfill, deleted guest) use the last known block.
* **Safeguard:** if a customer's email or cc changed during the month, or differs between their guests, that
  invoice is **held**, not sent, and the admin summary says why. After checking, send it with
  `pve-invoicer release 2026-11 ACME`.
* Prices are never read from notes. Anyone with `VM.Config.Options` on a guest can edit its notes, so
  review who holds that permission.

Run `pve-invoicer check-notes` at any time to see which guests have a valid block.

## Install

On any small Linux VM or LXC that can reach the Proxmox API (port 8006) and your SMTP relay:

```sh
# 1. Proxmox: a read-only API token (run once on any node). PVEAuditor is enough for the
#    guest list, RRD usage history and notes.
pveum user add billing@pve
pveum acl modify / --users billing@pve --roles PVEAuditor
pveum user token add billing@pve invoicer --privsep 0     # note the secret it prints

# 2. The service
sudo useradd --system --home /var/lib/pve-invoicer --create-home pve-invoicer
sudo apt install python3-venv libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core   # WeasyPrint needs Pango
sudo python3 -m venv /opt/pve-invoicer/venv
sudo /opt/pve-invoicer/venv/bin/pip install /path/to/pve-invoicer

# 3. Config and secrets
sudo mkdir -p /etc/pve-invoicer
sudo cp config.example.yaml /etc/pve-invoicer/config.yaml        # then edit it
sudo cp deploy/env.example /etc/pve-invoicer/env                 # then add the secrets
sudo chown -R root:pve-invoicer /etc/pve-invoicer && sudo chmod 640 /etc/pve-invoicer/*

# 4. Try it before enabling timers
sudo -u pve-invoicer env $(sudo cat /etc/pve-invoicer/env | xargs) \
  /opt/pve-invoicer/venv/bin/pve-invoicer -c /etc/pve-invoicer/config.yaml check-notes

# 5. Timers
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pve-invoicer-snapshot.timer pve-invoicer-invoice.timer
```

Set the machine's clock timezone to the same `timezone` as the config, or adjust the `OnCalendar` times.

The first invoice run covers the first **full** month after you enable the hourly snapshot. Earlier days can
be backfilled with `pve-invoicer snapshot --since YYYY-MM-DD`, from the coarser RRD history (12-hour
averages a few weeks back), and are flagged as approximate. Backfilled days use the notes recorded nearest
to them.

## Commands

```text
pve-invoicer [-c CONFIG] snapshot [--since YYYY-MM-DD] [--no-notes]
pve-invoicer [-c CONFIG] invoice  [--month YYYY-MM] [--dry-run] [--no-backfill]
pve-invoicer [-c CONFIG] release  YYYY-MM CUSTOMER_ID
pve-invoicer [-c CONFIG] check-notes
```

* `invoice --dry-run` renders every PDF under `output_dir/dry-run/` and emails them only to the admin.
  Nothing is recorded, so use it to check rates and the template.
* Re-running `invoice` for a month is safe. Sent invoices are never sent again, failed sends are retried
  with the same number, and held invoices stay held until released.
* Exit codes: 0 ok, 1 a send failed (retried next run), 2 configuration or connection error.

## Files

* `output_dir/YYYY-MM/<number>_<customer>.pdf` and `.json`: each invoice and the exact figures behind it.
* `database` (SQLite): `usage_samples` (per-minute usage, last 7 days), `usage_daily` (one row per guest per
  day, with the notes seen that day, `coverage` and the coarsest RRD `resolution_s` used) and `invoices`
  (number, status `sent`/`held`/`failed`, timestamps, SMTP message ID).

To change the invoice layout, copy `src/pve_invoicer/templates/` to a folder, edit it, and set
`invoice.template_dir`.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

The tests use a fake Proxmox API, whose RRD history is computed from a per-minute model of each guest, and
a fake SMTP client. They cover pricing, held and failed invoices, re-runs, missed hourly runs, nodes that are
down, backfill from coarse history, DST days, missing days and real PDF rendering.
