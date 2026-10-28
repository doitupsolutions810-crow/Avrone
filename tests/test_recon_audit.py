"""Allowlist enforcement + audit trail, uniformly across EVERY recon/OSINT tool. Fully offline.

Each tool reached through the real ToolRegistry must:
  * honour AVRONE_RECON=deny (and fail closed on unknown values),
  * honour AVRONE_RECON_ALLOWLIST (exact host / true subdomain only),
  * refuse private/loopback/metadata targets,
  * emit its own Codex audit event on success and on failure,
  * emit a recon.refused audit event (tool, target, reason) on every refusal,
and must never touch the network while refusing.
"""
import asyncio
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

from avrone import osint, recon
from avrone.tools import ToolRegistry

PUBLIC_IP = "93.184.216.34"
TOOL_NAMES = ["tls_inspect", "http_fingerprint", "port_scan", "cert_transparency", "dns_enum", "osint_domain"]
EXTRA = {"port_scan": {"authorized": True, "ports": [443]}}
OWN_KIND = {"tls_inspect": "recon.tls", "http_fingerprint": "recon.http", "port_scan": "recon.portscan",
            "cert_transparency": "recon.ct", "dns_enum": "recon.dns", "osint_domain": "osint.domain"}


@pytest.fixture(scope="module")
def registry():
    reg = ToolRegistry()
    recon.register_recon_tools(reg)
    osint.register_osint_tools(reg)
    return reg


def make_ctx(tmp="/tmp/none"):
    events = []

    def emit(run_id, kind, agent, **data):
        events.append((kind, data))
    core = SimpleNamespace(codex=SimpleNamespace(emit=emit), settings=SimpleNamespace(data_dir=str(tmp)))
    return SimpleNamespace(core=core, run_id="r", name="Recon-1", depth=0), events


def call(registry, name, args):
    c, events = make_ctx()
    out = asyncio.run(registry.get(name).fn(c, {**args, **EXTRA.get(name, {})}))
    return out, events


class NetworkTouched(AssertionError):
    pass


def _no_net(monkeypatch):
    def boom(*a, **k):
        raise NetworkTouched(repr(a))

    async def aboom(*a, **k):
        raise NetworkTouched(repr(a))

    class NoClient:
        def __init__(self, *a, **k):
            raise NetworkTouched("httpx")
    monkeypatch.setattr(recon, "_resolve_ips", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(asyncio, "open_connection", aboom)
    monkeypatch.setattr(httpx, "AsyncClient", NoClient)


def _fake_net(monkeypatch, fail=False):
    """Offline stand-ins for every network path, healthy or failing."""
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: [PUBLIC_IP])

    async def to_thread(fn, *a, **k):
        if fail:
            raise ConnectionRefusedError("refused")
        return {"notAfter": "x", "subjectAltName": (("DNS", "example.com"),)}, "TLSv1.3", ("C", "TLSv1.3", 1)
    monkeypatch.setattr(asyncio, "to_thread", to_thread)

    class W:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def open_conn(host, port):
        assert host == PUBLIC_IP
        if fail:
            raise ConnectionRefusedError
        return object(), W()
    monkeypatch.setattr(asyncio, "open_connection", open_conn)

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            if fail:
                raise httpx.ConnectError("down")
            if "crt.sh" in url:
                data = [{"name_value": "www.example.com"}]
            elif "dns.google" in url:
                data = {"Answer": [{"data": PUBLIC_IP}]}
            else:
                data = None
            return SimpleNamespace(status_code=200, headers=httpx.Headers({"server": "x"}), url=url,
                                   json=lambda: data, raise_for_status=lambda: None)
    monkeypatch.setattr(httpx, "AsyncClient", C)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.delenv("AVRONE_RECON_ALLOWLIST", raising=False)


def refusals(events):
    return [d for k, d in events if k == recon.REFUSED_KIND]


# ------------------------------------------------------------------ registry coverage
def test_every_recon_tool_is_covered_here(registry):
    names = {n for n, *_ in recon.RECON_TOOLS} | {n for n, *_ in osint.OSINT_TOOLS if n != "osint_metrics"}
    assert names == set(TOOL_NAMES)
    for n in TOOL_NAMES:
        assert registry.get(n) is not None


# ------------------------------------------------------------------ kill switch
@pytest.mark.parametrize("mode", ["deny", "DENY", " deny", "off", "bogus"])
@pytest.mark.parametrize("name", TOOL_NAMES)
def test_deny_is_honoured_and_audited(monkeypatch, registry, name, mode):
    monkeypatch.setenv("AVRONE_RECON", mode)
    _no_net(monkeypatch)
    out, events = call(registry, name, {"host": "example.com"})
    assert out.startswith("ERROR") and "disabled" in out
    r = refusals(events)
    assert len(r) == 1 and r[0]["tool"] == name and "disabled" in r[0]["reason"]
    assert all(k == recon.REFUSED_KIND for k, _ in events)  # no probe event when denied


# ------------------------------------------------------------------ allowlist
@pytest.mark.parametrize("target", ["evil.com", "evilmysite.com", "mysite.com.evil.net"])
@pytest.mark.parametrize("name", TOOL_NAMES)
def test_allowlist_is_honoured_and_audited(monkeypatch, registry, name, target):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "mysite.com")
    _no_net(monkeypatch)
    out, events = call(registry, name, {"host": target})
    assert "not in AVRONE_RECON_ALLOWLIST" in out
    r = refusals(events)
    assert len(r) == 1 and r[0]["tool"] == name and r[0]["target"] == target


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_allowlisted_subdomain_runs_and_emits_own_event(monkeypatch, registry, name):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "example.com")
    _fake_net(monkeypatch)
    out, events = call(registry, name, {"host": "api.example.com"})
    assert not out.startswith("ERROR"), out
    kinds = [k for k, _ in events]
    assert OWN_KIND[name] in kinds and recon.REFUSED_KIND not in kinds
    own = [d for k, d in events if k == OWN_KIND[name]]
    assert own[-1]["host"] == "api.example.com" and "error" not in own[-1]


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_set_but_empty_allowlist_refuses_everything(monkeypatch, registry, name):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", " , ")
    _no_net(monkeypatch)
    out, events = call(registry, name, {"host": "example.com"})
    assert "not in AVRONE_RECON_ALLOWLIST" in out and refusals(events)


# ------------------------------------------------------------------ private targets
@pytest.mark.parametrize("target", ["127.0.0.1", "localhost", "169.254.169.254", "metadata.google.internal",
                                    "10.0.0.1", "[::1]", "fd00::1", "nas.local", "192.168.1.1.nip.io"])
@pytest.mark.parametrize("name", TOOL_NAMES)
def test_private_targets_refused_and_audited(monkeypatch, registry, name, target):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "")  # no allowlist: private refusal must still hold
    _no_net(monkeypatch)
    out, events = call(registry, name, {"host": target})
    assert out.startswith("ERROR"), out
    r = refusals(events)
    assert len(r) == 1 and r[0]["tool"] == name


@pytest.mark.parametrize("name", ["tls_inspect", "http_fingerprint", "port_scan", "osint_domain"])
def test_name_resolving_private_is_refused_and_audited(monkeypatch, registry, name):
    _fake_net(monkeypatch)
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: ["169.254.169.254"])

    async def real_thread(fn, *a, **k):  # let tls_inspect's in-thread vetting run
        return fn(*a, **k)
    monkeypatch.setattr(asyncio, "to_thread", real_thread)
    out, events = call(registry, name, {"host": "rebind.example.com"})
    r = refusals(events)
    if name == "osint_domain":  # sub-probes refuse; the profile reports their ERRORs
        assert "resolves to a private" in out and {d["tool"] for d in r} >= {"tls_inspect", "http_fingerprint"}
    else:
        assert out.startswith("ERROR") and "resolves to a private" in out
        assert len(r) == 1 and r[0]["tool"] == name


# ------------------------------------------------------------------ audit on failure
@pytest.mark.parametrize("name", ["tls_inspect", "http_fingerprint", "cert_transparency", "dns_enum"])
def test_failed_probe_still_emits_own_event_with_error(monkeypatch, registry, name):
    _fake_net(monkeypatch, fail=True)
    out, events = call(registry, name, {"host": "example.com"})
    assert out.startswith("ERROR")
    own = [d for k, d in events if k == OWN_KIND[name]]
    assert own and own[0]["host"] == "example.com" and own[0].get("error")


def test_port_scan_failure_still_audited(monkeypatch, registry):
    _fake_net(monkeypatch, fail=True)
    out, events = call(registry, "port_scan", {"host": "example.com"})
    assert "open: none" in out and ("recon.portscan", {"host": "example.com", "ports": 1}) in events


# ------------------------------------------------------------------ other refusals are audited too
@pytest.mark.parametrize("args,needle", [
    ({"host": "example.com"}, "authorized"),
    ({"host": "example.com", "authorized": False}, "authorized"),
    ({"host": "example.com", "authorized": True, "ports": list(range(1, 40))}, "at most 32"),
    ({"host": "example.com", "authorized": True, "ports": [0]}, "between 1 and 65535"),
])
def test_port_scan_refusals_audited(monkeypatch, registry, args, needle):
    _no_net(monkeypatch)
    c, events = make_ctx()
    out = asyncio.run(registry.get("port_scan").fn(c, args))
    assert needle in out
    r = refusals(events)
    assert len(r) == 1 and r[0]["tool"] == "port_scan" and needle in r[0]["reason"]


@pytest.mark.parametrize("name,args", [
    ("tls_inspect", {"host": "example.com", "port": "x"}),
    ("http_fingerprint", {"host": "example.com", "scheme": "file"}),
    ("dns_enum", {"host": "example.com", "types": "A"}),
    ("tls_inspect", {"host": "a.com,b.com"}),
    ("dns_enum", {"host": "10.0.0.0/8"}),
    ("cert_transparency", {"host": "*.example.com"}),
])
def test_input_refusals_audited(monkeypatch, registry, name, args):
    _no_net(monkeypatch)
    c, events = make_ctx()
    out = asyncio.run(registry.get(name).fn(c, args))
    assert out.startswith("ERROR")
    assert [d["tool"] for d in refusals(events)] == [name]


def test_refusal_audit_target_is_bounded(monkeypatch, registry):
    _no_net(monkeypatch)
    out, events = call(registry, "dns_enum", {"host": "x" * 1500 + ".local"})
    r = refusals(events)
    assert r and len(r[0]["target"]) <= 200 and len(r[0]["reason"]) <= 300


def test_audit_survives_broken_codex(monkeypatch, registry):
    _no_net(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("codex down")
    c = SimpleNamespace(core=SimpleNamespace(codex=SimpleNamespace(emit=boom)), run_id="r", name="n")
    assert asyncio.run(registry.get("tls_inspect").fn(c, {"host": "127.0.0.1"})).startswith("ERROR")


# ------------------------------------------------------------------ osint_domain sub-probe audit
def test_osint_domain_emits_every_sub_probe_event(monkeypatch, registry):
    _fake_net(monkeypatch)
    out, events = call(registry, "osint_domain", {"host": "example.com"})
    kinds = [k for k, _ in events]
    for k in ("recon.dns", "recon.tls", "recon.http", "recon.ct", "osint.domain"):
        assert k in kinds, kinds
    assert kinds[-1] == "osint.domain" and recon.REFUSED_KIND not in kinds


# ------------------------------------------------------------------ metrics keep refusals separate
def test_metrics_separate_refusals_from_probes(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    evs = [{"kind": "recon.dns", "data": {"host": "a.com"}},
           {"kind": recon.REFUSED_KIND, "data": {"tool": "tls_inspect", "target": "127.0.0.1"}},
           {"kind": recon.REFUSED_KIND, "data": {"tool": "port_scan", "target": "10.0.0.1"}}]
    (runs / "r1.jsonl").write_text("\n".join(json.dumps(e) for e in evs))
    c, _ = make_ctx(tmp_path)
    out = asyncio.run(osint.t_osint_metrics(c, {}))
    assert "total probes: 1" in out and "refused by guardrails: 2" in out
    assert "127.0.0.1" not in out.split("refused by guardrails")[0]  # refused targets not ranked


def test_metrics_only_refusals(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r.jsonl").write_text(json.dumps({"kind": recon.REFUSED_KIND, "data": {"tool": "dns_enum"}}))
    c, _ = make_ctx(tmp_path)
    out = asyncio.run(osint.t_osint_metrics(c, {}))
    assert "total probes: 0" in out and "dns_enum: 1" in out
