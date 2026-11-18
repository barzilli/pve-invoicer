"""Thin read-only clients for the Prometheus HTTP API and the Proxmox VE API."""

from __future__ import annotations

from dataclasses import dataclass

import requests

from .config import PrometheusConfig, ProxmoxConfig


class ClientError(RuntimeError):
    pass


class PrometheusClient:
    def __init__(self, cfg: PrometheusConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        if cfg.bearer_token:
            self.session.headers["Authorization"] = f"Bearer {cfg.bearer_token}"

    def query(self, promql: str, at: float) -> list[tuple[dict, float]]:
        """Instant query evaluated at unix time `at`. Returns (labels, value) pairs."""
        try:
            r = self.session.get(
                f"{self.cfg.url}/api/v1/query",
                params={"query": promql, "time": f"{at:.3f}"},
                timeout=self.cfg.timeout,
                verify=self.cfg.verify_tls,
            )
        except requests.RequestException as e:
            raise ClientError(f"Prometheus request failed: {e}") from e
        try:
            body = r.json()
        except ValueError as e:
            raise ClientError(f"Prometheus returned HTTP {r.status_code}, not JSON") from e
        if r.status_code != 200 or body.get("status") != "success":
            raise ClientError(f"Prometheus query failed ({r.status_code}): {body.get('error', body)}")
        data = body["data"]
        if data.get("resultType") != "vector":
            raise ClientError(f"expected a vector result, got {data.get('resultType')}")
        out = []
        for item in data["result"]:
            out.append((item["metric"], float(item["value"][1])))
        return out


@dataclass(frozen=True)
class Guest:
    guest_id: str  # "qemu/100" or "lxc/101", same as the exporter's id label
    node: str
    vmid: int
    type: str
    name: str
    template: bool


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
        return r.json()["data"]

    def list_guests(self) -> list[Guest]:
        out = []
        for g in self._get("/cluster/resources", type="vm"):
            if g.get("type") not in ("qemu", "lxc"):
                continue
            out.append(
                Guest(
                    guest_id=g["id"],
                    node=g["node"],
                    vmid=int(g["vmid"]),
                    type=g["type"],
                    name=g.get("name", ""),
                    template=bool(g.get("template")),
                )
            )
        return out

    def get_description(self, guest: Guest) -> str:
        cfg = self._get(f"/nodes/{guest.node}/{guest.type}/{guest.vmid}/config")
        return cfg.get("description", "") or ""
