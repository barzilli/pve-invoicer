"""Parse the ```billing block from a Proxmox guest's notes (the `description` config key)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

# The block may be fenced with ``` or ~~~, tag "billing", closed by the same fence.
_BLOCK_RE = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*billing[ \t]*\r?\n(?P<body>.*?)^[ \t]*(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_CUSTOMER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

REQUIRED = ("customer_id", "email")
OPTIONAL = ("owner", "cc", "address", "vat_id", "po_number", "reference")
KNOWN = set(REQUIRED) | set(OPTIONAL)


@dataclass(frozen=True)
class BillingInfo:
    customer_id: str
    email: str
    owner: str = ""
    cc: tuple[str, ...] = ()
    address: str = ""
    vat_id: str = ""
    po_number: str = ""
    reference: str = ""

    def identity(self) -> tuple:
        """What must agree across guests and days of one customer before we send."""
        return (self.email.lower(), tuple(sorted(c.lower() for c in self.cc)))


@dataclass
class ParseResult:
    info: BillingInfo | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.info is not None and not self.errors


def parse_notes(description: str | None) -> ParseResult:
    if not description or not description.strip():
        return ParseResult(None, ["no notes"])

    blocks = list(_BLOCK_RE.finditer(description))
    if not blocks:
        return ParseResult(None, ["no ```billing block in notes"])
    if len(blocks) > 1:
        return ParseResult(None, ["more than one ```billing block in notes"])

    try:
        data = yaml.safe_load(blocks[0].group("body"))
    except yaml.YAMLError as e:
        return ParseResult(None, [f"billing block is not valid YAML: {str(e).splitlines()[0]}"])
    if not isinstance(data, dict):
        return ParseResult(None, ["billing block must be key: value pairs"])

    errors: list[str] = []
    warnings: list[str] = []

    unknown = sorted(str(k) for k in data if k not in KNOWN)
    if unknown:
        warnings.append("unknown keys ignored: " + ", ".join(unknown))

    for key in REQUIRED:
        if data.get(key) in (None, ""):
            errors.append(f"missing {key}")

    customer_id = str(data.get("customer_id", "") or "").strip()
    if customer_id and not _CUSTOMER_ID_RE.match(customer_id):
        errors.append("customer_id may only contain letters, digits, '.', '_' and '-' (max 64)")

    email = str(data.get("email", "") or "").strip()
    if email and not _EMAIL_RE.match(email):
        errors.append(f"email is not a valid address: {email!r}")

    cc_raw = data.get("cc") or []
    if isinstance(cc_raw, str):
        cc_raw = [c for c in re.split(r"[,\s]+", cc_raw) if c]
    if not isinstance(cc_raw, list):
        errors.append("cc must be an address or a list of addresses")
        cc_raw = []
    cc = []
    for c in cc_raw:
        c = str(c).strip()
        if not _EMAIL_RE.match(c):
            errors.append(f"cc is not a valid address: {c!r}")
        else:
            cc.append(c)

    def text(key: str) -> str:
        v = data.get(key)
        return "" if v is None else str(v).strip()

    if errors:
        return ParseResult(None, errors, warnings)

    return ParseResult(
        BillingInfo(
            customer_id=customer_id,
            email=email,
            owner=text("owner"),
            cc=tuple(cc),
            address=text("address"),
            vat_id=text("vat_id"),
            po_number=text("po_number"),
            reference=text("reference"),
        ),
        errors,
        warnings,
    )
