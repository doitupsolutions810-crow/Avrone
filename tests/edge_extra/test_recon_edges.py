"""Recon default-deny edges. Never open real remote sockets; never weaken the gates."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from avrone import recon


def ctx():
    return SimpleNamespace(core=SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None)),
                           run_id="r", name="Recon-1", depth=0)


def run(c):
    return asyncio.run(c)


def _allow_public(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "*")
    # Isolate from this box's DNS (example.com has resolved to a reserved range here).
    monkeypatch.setattr(recon, "_resolve_ips", lambda host: ["93.184.216.34"])


def test_loopback_literal_refused_even_when_authorized(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "*")
    out = run(recon.t_port_scan(ctx(), {"host": "127.0.0.1", "authorized": True, "ports": [80]}))
    assert "refused" in out.lower()
    assert "private" in out.lower() or "loopback" in out.lower() or "literal" in out.lower()


def test_localhost_name_refused_even_when_authorized(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "*")
    out = run(recon.t_port_scan(ctx(), {"host": "localhost", "authorized": True, "ports": [80]}))
    assert "refused" in out.lower()


def test_missing_authorized_is_refused(monkeypatch):
    _allow_public(monkeypatch)
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "ports": [80, 443]}))
    assert "authorized" in out and "refused" in out


def test_authorized_false_is_refused(monkeypatch):
    _allow_public(monkeypatch)
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": False, "ports": [80]}))
    assert "authorized" in out and "refused" in out


def test_default_deny_without_allowlist(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.delenv("AVRONE_RECON_ALLOWLIST", raising=False)
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": [80]}))
    assert "AVRONE_RECON_ALLOWLIST" in out or "default-deny" in out or "not in" in out


def test_ports_out_of_range_refused(monkeypatch):
    _allow_public(monkeypatch)
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": [80, 0]}))
    assert "1 and 65535" in out
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": [80, 65536]}))
    assert "1 and 65535" in out


def test_ports_must_be_a_list(monkeypatch):
    _allow_public(monkeypatch)
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": "80,443"}))
    assert "at most 32" in out or "list" in out.lower()


def test_non_integer_ports_refused(monkeypatch):
    _allow_public(monkeypatch)
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": [80, "x"]}))
    assert "must be integers" in out


def test_authorized_five_or_fewer_ports_mocked_no_network(monkeypatch):
    """Happy path uses at most 5 ports and never opens a real socket."""
    _allow_public(monkeypatch)

    async def mock_open(*args, **kwargs):
        raise OSError("mock offline - no real network")

    monkeypatch.setattr(asyncio, "open_connection", mock_open)
    out = run(recon.t_port_scan(ctx(), {
        "host": "example.com", "authorized": True, "ports": [22, 80, 443, 8080, 8443],
    }))
    assert "port_scan" in out and "open: none" in out
    assert "5 ports" in out
