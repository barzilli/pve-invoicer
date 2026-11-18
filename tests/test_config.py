from pathlib import Path

import pytest

from pve_invoicer.config import ConfigError, load_config

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"


def test_example_config_loads():
    cfg = load_config(EXAMPLE, env={"PVE_TOKEN_SECRET": "s", "SMTP_PASSWORD": "p"})
    assert cfg.currency == "EUR"
    assert str(cfg.rates.disk_gib_month) == "0.05"
    assert cfg.proxmox.token_secret == "s" and cfg.smtp.password == "p"
    assert cfg.smtp.bcc == ("accounts@example.net",)
    assert cfg.timezone.key == "Europe/Amsterdam"


def test_missing_secret():
    with pytest.raises(ConfigError, match="PVE_TOKEN_SECRET"):
        load_config(EXAMPLE, env={})


def test_negative_rate(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(EXAMPLE.read_text().replace("network_gb: 0.01", "network_gb: -1"))
    with pytest.raises(ConfigError, match="network_gb"):
        load_config(p, env={"PVE_TOKEN_SECRET": "s"})
