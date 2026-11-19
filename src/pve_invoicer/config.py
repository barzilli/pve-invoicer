"""Configuration loading. Secrets come from environment variables, never the file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Rates:
    vcpu_hour: Decimal
    ram_gib_hour: Decimal
    disk_gib_month: Decimal
    network_gb: Decimal


@dataclass(frozen=True)
class ProxmoxConfig:
    url: str
    token_id: str  # user@realm!tokenname
    token_secret: str  # from env PVE_TOKEN_SECRET
    verify_tls: bool | str = True  # True, False or a CA bundle path
    timeout: int = 30


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    security: str  # starttls | ssl | none
    username: str | None
    password: str | None  # from env SMTP_PASSWORD
    sender: str
    admin_email: str
    bcc: tuple[str, ...] = ()


@dataclass(frozen=True)
class Issuer:
    name: str
    address: str = ""
    email: str = ""
    payment_details: str = ""


@dataclass(frozen=True)
class Config:
    timezone: ZoneInfo
    database: Path
    output_dir: Path
    currency: str
    rates: Rates
    proxmox: ProxmoxConfig
    smtp: SmtpConfig
    issuer: Issuer
    number_format: str = "{year}-{month:02d}-{seq:04d}"
    due_days: int = 14
    template_dir: Path | None = None
    min_coverage: float = 0.95  # flag guest-days with fewer minutes of data than this share
    extra: dict = field(default_factory=dict)


def _req(d: dict, key: str, where: str):
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"missing required setting {where}.{key}")
    return d[key]


def _dec(d: dict, key: str) -> Decimal:
    v = _req(d, key, "rates")
    try:
        r = Decimal(str(v))
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"rates.{key} is not a number: {v!r}") from e
    if r < 0:
        raise ConfigError(f"rates.{key} must not be negative")
    return r


def load_config(path: str | Path, env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    rates_raw = _req(raw, "rates", "")
    rates = Rates(
        vcpu_hour=_dec(rates_raw, "vcpu_hour"),
        ram_gib_hour=_dec(rates_raw, "ram_gib_hour"),
        disk_gib_month=_dec(rates_raw, "disk_gib_month"),
        network_gb=_dec(rates_raw, "network_gb"),
    )

    x = _req(raw, "proxmox", "")
    secret = env.get("PVE_TOKEN_SECRET")
    if not secret:
        raise ConfigError("environment variable PVE_TOKEN_SECRET is not set")
    proxmox = ProxmoxConfig(
        url=_req(x, "url", "proxmox").rstrip("/"),
        token_id=_req(x, "token_id", "proxmox"),
        token_secret=secret,
        verify_tls=x.get("verify_tls", True),
        timeout=int(x.get("timeout", 30)),
    )

    s = _req(raw, "smtp", "")
    security = str(s.get("security", "starttls")).lower()
    if security not in ("starttls", "ssl", "none"):
        raise ConfigError("smtp.security must be starttls, ssl or none")
    smtp = SmtpConfig(
        host=_req(s, "host", "smtp"),
        port=int(s.get("port", 465 if security == "ssl" else 587)),
        security=security,
        username=s.get("username") or None,
        password=env.get("SMTP_PASSWORD") or None,
        sender=_req(s, "from", "smtp"),
        admin_email=_req(s, "admin_email", "smtp"),
        bcc=tuple(s.get("bcc") or ()),
    )

    i = _req(raw, "issuer", "")
    issuer = Issuer(
        name=_req(i, "name", "issuer"),
        address=str(i.get("address", "") or ""),
        email=str(i.get("email", "") or ""),
        payment_details=str(i.get("payment_details", "") or ""),
    )

    inv = raw.get("invoice") or {}
    tz_name = raw.get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"unknown timezone {tz_name!r}") from e

    return Config(
        timezone=tz,
        database=Path(raw.get("database", "/var/lib/pve-invoicer/invoicer.db")),
        output_dir=Path(raw.get("output_dir", "/var/lib/pve-invoicer/invoices")),
        currency=str(_req(raw, "currency", "")),
        rates=rates,
        proxmox=proxmox,
        smtp=smtp,
        issuer=issuer,
        number_format=str(inv.get("number_format", "{year}-{month:02d}-{seq:04d}")),
        due_days=int(inv.get("due_days", 14)),
        template_dir=Path(inv["template_dir"]) if inv.get("template_dir") else None,
        min_coverage=float(raw.get("min_coverage", 0.95)),
    )
