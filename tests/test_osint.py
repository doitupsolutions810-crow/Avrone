import asyncio, json
from types import SimpleNamespace
import pytest
from avrone import osint, recon


def ctx(tmp=None):
    settings = SimpleNamespace(data_dir=str(tmp) if tmp else "/tmp/none")
    core = SimpleNamespace(settings=settings, codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r", name="OSINT-1", depth=0)


def run(c): return asyncio.run(c)


def test_domain_denied(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "deny")
    assert "disabled" in run(osint.t_osint_domain(ctx(), {"host": "example.com"}))


def test_domain_allowlist(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "mysite.com")
    assert "not in AVRONE_RECON_ALLOWLIST" in run(osint.t_osint_domain(ctx(), {"host": "evil.com"}))


def test_domain_aggregates_sections(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.delenv("AVRONE_RECON_ALLOWLIST", raising=False)
    async def fake_dns(c, a): return "DNS ok"
    async def fake_tls(c, a): return "TLS ok"
    async def fake_http(c, a): return "HTTP ok"
    async def fake_ct(c, a): return "certificate-transparency subdomains of x: 3 unique (public crt.sh data)\na.x"
    monkeypatch.setattr(recon, "t_dns_enum", fake_dns)
    monkeypatch.setattr(recon, "t_tls_inspect", fake_tls)
    monkeypatch.setattr(recon, "t_http_fingerprint", fake_http)
    monkeypatch.setattr(recon, "t_cert_transparency", fake_ct)
    out = run(osint.t_osint_domain(ctx(), {"host": "example.com"}))
    for marker in ("[DNS]", "DNS ok", "[TLS]", "TLS ok", "[HTTP]", "HTTP ok", "[Subdomains]", "3 unique"):
        assert marker in out


def test_metrics_empty(tmp_path):
    assert "no recon/OSINT activity" in run(osint.t_osint_metrics(ctx(tmp_path), {}))


def test_metrics_rollup(tmp_path):
    runs = tmp_path / "runs"; runs.mkdir()
    evs = [{"kind": "recon.dns", "data": {"host": "a.com"}},
           {"kind": "recon.tls", "data": {"host": "a.com"}},
           {"kind": "osint.domain", "data": {"host": "b.com"}},
           {"kind": "agent.done", "data": {}}]
    (runs / "r1.jsonl").write_text("\n".join(json.dumps(e) for e in evs))
    out = run(osint.t_osint_metrics(ctx(tmp_path), {}))
    assert "recon.dns: 1" in out and "osint.domain: 1" in out
    assert "a.com: 2" in out and "total probes: 3" in out


def test_registration():
    from avrone.tools import ToolRegistry
    reg = ToolRegistry()
    osint.register_osint_tools(reg)
    assert reg.get("osint_domain") and reg.get("osint_metrics")


def test_metrics_skips_malformed_and_non_object_events(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    lines = [
        "{broken-json",
        json.dumps(None),
        json.dumps([]),
        json.dumps({"kind": None, "data": {"host": "ignored.example"}}),
        json.dumps({"kind": "recon.dns", "data": []}),
        json.dumps({"kind": "recon.dns", "data": {"host": "safe.example"}}),
        json.dumps({"kind": "agent.done", "data": {"host": "ignored.example"}}),
    ]
    (runs / "mixed.jsonl").write_text("\n".join(lines))
    out = run(osint.t_osint_metrics(ctx(tmp_path), {}))
    assert "recon.dns: 2" in out
    assert "safe.example: 1" in out
    assert "ignored.example" not in out
    assert "total probes: 2" in out


def test_metrics_ignores_non_string_host_values(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    events = [
        {"kind": "osint.domain", "data": {"host": ["not", "a", "hostname"]}},
        {"kind": "osint.domain", "data": {"host": "  example.com  "}},
    ]
    (runs / "typed.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    out = run(osint.t_osint_metrics(ctx(tmp_path), {}))
    assert "example.com: 1" in out
    assert "not" not in out
