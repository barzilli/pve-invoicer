"""Thin read-only client for the Proxmox VE API."""

from __future__ import annotations

from dataclasses import dataclass

import requests

from .config import ProxmoxConfig

# RRD timeframes the API offers, finest first.
TIMEFRAMES = ("hour", "day", "week", "month", "year")


class ClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class Guest:
    guest_id: str  # "qemu/100" or "lxc/101", the id from /cluster/resources
    node: str
    vmid: int
    type: str  # qemu | lxc
    name: str
    template: bool
    status: str = ""  # running | stopped | unknown ...


class ProxmoxClient:
    def __init__(self, cfg: ProxmoxConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"PVEAPIToken={cfg.token_id}={cfg.token_secret}"

    def _get(self, path: str, **params):
        try:
            r = self.session.get(
                f"{self.cfg.url}/api2/json{path}",
                params=params or None,
                timeout=self.cfg.timeout,
                verify=self.cfg.verify_tls,
            )
        except requests.RequestException as e:
            raise ClientError(f"Proxmox request failed: {e}") from e
        if r.status_code != 200:
            raise ClientError(f"Proxmox GET {path} failed: HTTP {r.status_code} {r.text[:200]}")
        try:
            return r.json()["data"]
        except (ValueError, KeyError, TypeError) as e:
            raise ClientError(f"Proxmox GET {path} returned no data") from e

    def list_guests(self) -> list[Guest]:
        """Every VM and container in the cluster, from /cluster/resources."""
        out = []
        for g in self._get("/cluster/resources", type="vm") or []:
            if g.get("type") not in ("qemu", "lxc"):
                continue
            out.append(
                Guest(
                    guest_id=g["id"],
                    node=g["node"],
                    vmid=int(g["vmid"]),
                    type=g["type"],
                    name=g.get("name", "") or "",
                    template=bool(g.get("template")),
                    status=g.get("status", "") or "",
                )
            )
        return out

    def get_description(self, guest: Guest) -> str:
        cfg = self._get(f"/nodes/{guest.node}/{guest.type}/{guest.vmid}/config")
        return cfg.get("description", "") or ""

    def rrddata(self, guest: Guest, timeframe: str, cf: str = "AVERAGE") -> list[dict]:
        """The guest's RRD history: about 70 points at the timeframe's resolution.

        Each point has `time` (unix seconds, start of the interval) and, when known, maxcpu,
        cpu, maxmem, mem, maxdisk, disk and netin/netout (bytes per second). A point with only
        `time` means no data, for example before the guest existed. `cpu` is missing while
        the guest is stopped.
        """
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unknown RRD timeframe {timeframe!r}")
        data = self._get(f"/nodes/{guest.node}/{guest.type}/{guest.vmid}/rrddata", timeframe=timeframe, cf=cf)
        return list(data or [])
