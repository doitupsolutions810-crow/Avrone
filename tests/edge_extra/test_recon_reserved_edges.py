"""Round 3 recon edges: IPv6 loopback, link-local, multicast, reserved ranges.

Never open a real remote socket. Never weaken production guardrails: even
authorized=True cannot scan non-public addresses, and any successful (mocked)
scan path uses at most 5 ports.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from avrone import recon


def ctx():
    return SimpleNamespace(core=SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None)),
                           run_id="r", name="Recon-1", depth=0)


def run(c):
    return asyncio.run(c)


def _allow(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "*")


# Literal hosts that production already classifies as non-public, exercised through
# t_port_scan + authorized=True. These ranges were not covered as port_scan literals
# in the Round 1/2 edge suite.
RESERVED_LITERALS = [
    "::1",
    "[::1]",
    "fe80::1",
    "[fe80::1]",
    "ff02::1",          # IPv6 multicast
    "224.0.0.1",        # IPv4 multicast
    "239.255.255.250",  # IPv4 multicast (SSDP)
    "240.0.0.1",        # IPv4 reserved (Class E)
    "255.255.255.255",
    "0.0.0.0",
    "198.18.0.1",       # benchmarking / documentation range often used by local resolvers
    "169.254.1.1",      # IPv4 link-local
]


@pytest.mark.parametrize("host", RESERVED_LITERALS)
def test_port_scan_refuses_reserved_literal_even_when_authorized(monkeypatch, host):
    _allow(monkeypatch)
    # Do not stub _resolve_ips here: the literal itself must be rejected before connect.
    out = run(recon.t_port_scan(ctx(), {"host": host, "authorized": True, "ports": [80]}))
    low = out.lower()
    # Production wording varies ("refused", "IP literal", "private", ...); any denial is success.
    assert "error" in low or "refused" in low, out
    assert any(w in low for w in (
        "refused", "literal", "private", "loopback", "multicast", "reserved",
        "link-local", "link local", "not public",
    )), out
    assert "open:" not in low


# Names that look public but resolve to a reserved/non-public address.
RESOLVED_NONPUBLIC = [
    ["::1"],
    ["fe80::1"],
    ["ff02::1"],
    ["224.0.0.1"],
    ["240.0.0.1"],
    ["198.18.0.1"],
    ["255.255.255.255"],
    ["0.0.0.0"],
    ["169.254.1.1"],
    ["93.184.216.34", "224.0.0.1"],  # mixed public + multicast: still refuse
]


@pytest.mark.parametrize("ips", RESOLVED_NONPUBLIC)
def test_port_scan_refuses_name_resolving_to_reserved(monkeypatch, ips):
    _allow(monkeypatch)
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: ips)
    opened = []

    async def fake_open(host, port):
        opened.append((host, port))
        raise AssertionError("must not connect to a reserved/non-public resolution")

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    out = run(recon.t_port_scan(ctx(), {"host": "rebind.example.com", "authorized": True, "ports": [80]}))
    assert "refused" in out.lower() or "resolve" in out.lower(), out
    assert opened == []


def test_authorized_mocked_scan_stays_at_most_five_ports(monkeypatch):
    """Happy path still respects the test-side 5-port budget; production cap is unchanged."""
    _allow(monkeypatch)
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: ["93.184.216.34"])
    hosts = []

    async def fake_open(host, port):
        hosts.append((host, port))
        raise ConnectionRefusedError

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    ports = [80, 443, 8080, 8443, 22]
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": ports}))
    assert "open: none" in out
    assert set(h for h, _ in hosts) == {"93.184.216.34"}
    assert set(p for _, p in hosts) <= set(ports)
    assert len(hosts) <= 5


def test_default_deny_still_holds_without_allowlist(monkeypatch):
    monkeypatch.delenv("AVRONE_RECON", raising=False)
    monkeypatch.delenv("AVRONE_RECON_ALLOWLIST", raising=False)
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: ["93.184.216.34"])
    opened = []

    async def fake_open(host, port):
        opened.append((host, port))
        raise AssertionError("default-deny must not connect")

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": [80]}))
    low = out.lower()
    assert "error" in low or "refused" in low, out
    assert "allowlist" in low or "default-deny" in low or "default deny" in low, out
    assert opened == []
