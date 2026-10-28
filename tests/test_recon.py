import asyncio
from types import SimpleNamespace
import pytest
from avrone import recon


def ctx():
    return SimpleNamespace(core=SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None)),
                           run_id="r", name="Recon-1", depth=0)


def run(c): return asyncio.run(c)


def test_denied(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "deny")
    for fn in (recon.t_tls_inspect, recon.t_http_fingerprint, recon.t_cert_transparency, recon.t_dns_enum):
        assert "disabled" in run(fn(ctx(), {"host": "example.com"}))


def test_portscan_requires_authorization(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    out = run(recon.t_port_scan(ctx(), {"host": "example.com"}))
    assert "authorized" in out and "refused" in out


def test_portscan_port_cap(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": list(range(40))}))
    assert "at most 32" in out


def test_allowlist(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "mysite.com")
    assert "not in AVRONE_RECON_ALLOWLIST" in run(recon.t_tls_inspect(ctx(), {"host": "evil.com"}))
    # subdomain of an allowed suffix passes the gate (checked offline, no real lookup)
    h, err = recon._host_arg({"host": "api.mysite.com"})
    assert h == "api.mysite.com" and err is None


def test_missing_host(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    assert "give" in run(recon.t_tls_inspect(ctx(), {}))


def test_host_strip(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    h, err = recon._host_arg({"host": "https://example.com:443/path?x=1"})
    assert h == "example.com" and err is None


def test_registration():
    from avrone.tools import ToolRegistry
    reg = ToolRegistry()
    recon.register_recon_tools(reg)
    for name in ("tls_inspect", "http_fingerprint", "port_scan", "cert_transparency", "dns_enum"):
        assert reg.get(name) is not None
