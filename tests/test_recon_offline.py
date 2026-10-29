"""Offline coverage for recon.py: every tool body runs against fakes, never the network."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from avrone import recon


def ctx(events=None):
    def emit(run_id, kind, agent, **data):
        if events is not None:
            events.append((kind, data))
    return SimpleNamespace(core=SimpleNamespace(codex=SimpleNamespace(emit=emit)),
                           run_id="r", name="Recon-1", depth=0)


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def _allow(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "*")  # v0.21: recon is default-deny; "*" = any public host
    # connect-time resolution guard: answer with a public documentation-free address, no real DNS
    monkeypatch.setattr(recon, "_resolve_ips", lambda host: ["93.184.216.34"])


# ------------------------------------------------------------ allowlist edge cases
@pytest.mark.parametrize("host,ok", [
    ("mysite.com", True),
    ("api.mysite.com", True),
    ("a.b.mysite.com", True),
    ("MYSITE.COM", True),
    ("evilmysite.com", False),     # suffix-without-dot bypass (fixed in v0.12)
    ("mysite.com.evil.net", False),
    ("othersite.com", False),
])
def test_allowlist_matching(monkeypatch, host, ok):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "mysite.com, .partner.org")
    assert recon._allowed_host(host) is ok


def test_allowlist_leading_dot_entry(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", ".partner.org")
    assert recon._allowed_host("partner.org")
    assert recon._allowed_host("x.partner.org")
    assert not recon._allowed_host("evilpartner.org")


def test_allowlist_blocks_every_tool(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "mysite.com")
    for fn in (recon.t_tls_inspect, recon.t_http_fingerprint, recon.t_cert_transparency, recon.t_dns_enum):
        assert "not in AVRONE_RECON_ALLOWLIST" in run(fn(ctx(), {"host": "evilmysite.com"}))
    out = run(recon.t_port_scan(ctx(), {"host": "evilmysite.com", "authorized": True}))
    assert "not in AVRONE_RECON_ALLOWLIST" in out


@pytest.mark.parametrize("args", [{"domain": "example.com"}, {"target": "http://example.com/x"}])
def test_host_aliases(args):
    assert recon._host_arg(args) == ("example.com", None)


# ------------------------------------------------------------ fake httpx
class FakeResp:
    def __init__(self, status=200, headers=None, json_data=None, url="https://example.com/", bad_json=False):
        self.status_code = status
        self.headers = httpx.Headers(headers or {})
        self._json = json_data
        self._bad = bad_json
        self.url = url

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


def fake_client(handler):
    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            return handler(url, params or {})
    return C


# ------------------------------------------------------------ http_fingerprint
def test_http_fingerprint_headers_and_missing(monkeypatch):
    resp = FakeResp(200, {"server": "nginx", "x-powered-by": "PHP/8", "strict-transport-security": "max-age=1",
                          "x-frame-options": "DENY"}, url="https://example.com/home")
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(lambda u, p: resp))
    ev = []
    out = run(recon.t_http_fingerprint(ctx(ev), {"host": "example.com"}))
    assert "-> 200" in out and "server: nginx" in out and "x-powered-by: PHP/8" in out
    assert "content-security-policy" in out.split("missing security headers:")[1]
    assert "strict-transport-security" not in out.split("missing security headers:")[1]
    assert ev and ev[0][0] == "recon.http"


def test_http_fingerprint_all_secure(monkeypatch):
    hdrs = {h: "x" for h in ("strict-transport-security", "content-security-policy", "x-frame-options",
                             "x-content-type-options", "referrer-policy")}
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(lambda u, p: FakeResp(200, hdrs)))
    assert "missing security headers: none" in run(recon.t_http_fingerprint(ctx(), {"host": "example.com"}))


def test_http_fingerprint_error(monkeypatch):
    def boom(u, p):
        raise httpx.ConnectError("nope")
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(boom))
    out = run(recon.t_http_fingerprint(ctx(), {"host": "example.com", "scheme": "http"}))
    assert out.startswith("ERROR: HTTP to http://example.com/")


# ------------------------------------------------------------ cert_transparency
def test_ct_dedupes_and_filters(monkeypatch):
    rows = [{"name_value": "*.example.com\nwww.example.com"}, {"name_value": "WWW.example.com"},
            {"name_value": "api.example.com"}, {"name_value": "unrelated.org"}, {}]
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(lambda u, p: FakeResp(json_data=rows)))
    ev = []
    out = run(recon.t_cert_transparency(ctx(ev), {"host": "example.com"}))
    assert "3 unique" in out
    assert "unrelated.org" not in out and "api.example.com" in out
    assert ev[0] == ("recon.ct", {"host": "example.com", "found": 3})


def test_ct_truncates_long_lists(monkeypatch):
    rows = [{"name_value": f"h{i}.example.com"} for i in range(95)]
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(lambda u, p: FakeResp(json_data=rows)))
    assert "(+15 more)" in run(recon.t_cert_transparency(ctx(), {"host": "example.com"}))


def test_ct_empty_and_errors(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(lambda u, p: FakeResp(json_data=[])))
    assert "no certificate-transparency records" in run(recon.t_cert_transparency(ctx(), {"host": "example.com"}))
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(lambda u, p: FakeResp(503)))
    assert "ERROR: crt.sh" in run(recon.t_cert_transparency(ctx(), {"host": "example.com"}))
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(lambda u, p: FakeResp(bad_json=True)))
    assert "ERROR: crt.sh" in run(recon.t_cert_transparency(ctx(), {"host": "example.com"}))


# ------------------------------------------------------------ dns_enum
def test_dns_enum_types_and_cap(monkeypatch):
    seen = []

    def h(url, params):
        seen.append(params["type"])
        if params["type"] == "A":
            return FakeResp(json_data={"Answer": [{"data": "1.2.3.4"}, {"data": ""}]})
        return FakeResp(json_data={})
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(h))
    out = run(recon.t_dns_enum(ctx(), {"host": "example.com", "types": ["A", "MX"] + ["TXT"] * 20}))
    assert "A: 1.2.3.4" in out and "MX: -" in out
    assert len(seen) == 10  # capped


def test_dns_enum_error(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(lambda u, p: FakeResp(bad_json=True)))
    assert "ERROR: DNS lookup" in run(recon.t_dns_enum(ctx(), {"host": "example.com"}))


# ------------------------------------------------------------ port_scan
def test_port_scan_open_closed_and_dedupe(monkeypatch):
    attempted = []

    class W:
        def close(self):
            pass

        async def wait_closed(self):
            raise RuntimeError("ignored")

    async def fake_open(host, port):
        attempted.append(port)
        if port in (22, 443):
            return object(), W()
        raise ConnectionRefusedError
    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    ev = []
    out = run(recon.t_port_scan(ctx(ev), {"host": "example.com", "authorized": True, "ports": [443, 22, 80, 22]}))
    assert "open: 22/ssh, 443/https" in out
    assert "(3 ports)" in out and "closed/filtered: 1" in out
    assert sorted(attempted) == [22, 80, 443]
    assert ev[0][0] == "recon.portscan"


def test_port_scan_none_open_default_ports(monkeypatch):
    async def fake_open(host, port):
        raise asyncio.TimeoutError
    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True}))
    assert "open: none" in out and f"({len(recon.COMMON_PORTS)} ports)" in out


@pytest.mark.parametrize("ports,msg", [
    ([0], "between 1 and 65535"), ([70000], "between 1 and 65535"),
    (["x"], "integers"), ("80,443", "at most 32"),
])
def test_port_scan_bad_ports(ports, msg):
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": ports}))
    assert msg in out


def test_port_scan_authorized_must_be_truthy():
    assert "refused" in run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": False}))


# ------------------------------------------------------------ tls_inspect
def test_tls_inspect_parses_cert(monkeypatch):
    cert = {"subject": ((("commonName", "example.com"),),),
            "issuer": ((("organizationName", "Test CA"),),),
            "subjectAltName": (("DNS", "example.com"), ("DNS", "www.example.com"), ("IP Address", "1.1.1.1")),
            "notBefore": "Jan  1 00:00:00 2026 GMT", "notAfter": "Jan  1 00:00:00 2099 GMT"}

    async def fake_to_thread(fn, *a, **k):
        return cert, "TLSv1.3", ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    ev = []
    out = run(recon.t_tls_inspect(ctx(ev), {"host": "example.com"}))
    assert "TLSv1.3" in out and "subject CN: example.com" in out and "issuer: Test CA" in out
    assert "SANs (2): example.com, www.example.com" in out and "days left" in out
    assert ev[0] == ("recon.tls", {"host": "example.com", "port": 443})


def test_tls_inspect_bad_date_and_error(monkeypatch):
    async def ok(fn, *a, **k):
        return {"notAfter": "garbage"}, "TLSv1.2", None
    monkeypatch.setattr(asyncio, "to_thread", ok)
    out = run(recon.t_tls_inspect(ctx(), {"host": "example.com", "port": 8443}))
    assert "example.com:8443" in out and "days left" not in out and "subject CN: ?" in out

    async def bad(fn, *a, **k):
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(asyncio, "to_thread", bad)
    assert "ERROR: TLS to example.com:443" in run(recon.t_tls_inspect(ctx(), {"host": "example.com"}))


def test_emit_failure_is_swallowed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("codex down")
    c = SimpleNamespace(core=SimpleNamespace(codex=SimpleNamespace(emit=boom)), run_id="r", name="n")
    monkeypatch.setattr(httpx, "AsyncClient", fake_client(lambda u, p: FakeResp(json_data={})))
    assert run(recon.t_dns_enum(c, {"host": "example.com", "types": ["A"]})).startswith("DNS for example.com")
