"""Round 36: recon guardrails, next layer. DNS-rebinding pinning for http_fingerprint (connect to
the vetted address only), strict port/authorized arguments, allowlist entry normalisation, IPs
embedded in names (dash/hex forms, loopback wildcard domains), IPv6 site-local, TLS address
fallback. Offline: a local HTTP server on 127.0.0.1 proves the pin; no public network."""
import asyncio
import http.server
import socket
import threading
from types import SimpleNamespace

import httpx
import pytest

from avrone import recon

PUBLIC_IP = "93.184.216.34"
PUBLIC_IP2 = "93.184.216.35"


def ctx(events=None):
    ev = events if events is not None else []
    codex = SimpleNamespace(emit=lambda run_id, kind, agent, **d: ev.append((kind, d)))
    return SimpleNamespace(core=SimpleNamespace(codex=codex), run_id="r", name="a")


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "*")


# ================================================================ pinned transport (real socket)
@pytest.fixture()
def local_server():
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Host"))
            body = b"ok"
            self.send_response(200)
            self.send_header("Server", "pinned-test")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv.server_address[1], seen
    srv.shutdown()
    srv.server_close()


def test_pinned_transport_connects_to_pin_not_dns(local_server):
    port, seen = local_server

    async def go():
        t = recon._PinnedTransport({"rebind.invalid": "127.0.0.1"})
        async with httpx.AsyncClient(transport=t) as c:
            return await c.get(f"http://rebind.invalid:{port}/x")
    r = run(go())
    # rebind.invalid has no DNS at all: the request can only have reached the pinned address
    assert r.status_code == 200 and "pinned-test" in r.headers["server"]
    assert seen == [f"rebind.invalid:{port}"]
    assert str(r.url) == f"http://rebind.invalid:{port}/x"  # the response keeps the hostname


def test_pinned_transport_refuses_unvetted_host(local_server):
    port, seen = local_server

    async def go():
        t = recon._PinnedTransport({})
        async with httpx.AsyncClient(transport=t) as c:
            await c.get(f"http://127.0.0.1:{port}/")
    with pytest.raises(recon._Refused, match="not vetted"):
        run(go())
    assert seen == []


def test_pinned_transport_ipv6_pin_bracketed():
    sent = []

    class Inner:
        async def handle_async_request(self, request):
            sent.append((str(request.url), request.headers["host"], request.extensions.get("sni_hostname")))
            return httpx.Response(204)

        async def aclose(self):
            pass
    t = recon._PinnedTransport({"v6.example.com": "2606:2800:220:1::1"})
    t.inner = Inner()

    async def go():
        async with httpx.AsyncClient(transport=t) as c:
            return await c.get("https://v6.example.com/")
    assert run(go()).status_code == 204
    url, host, sni = sent[0]
    assert url.startswith("https://[2606:2800:220:1::1]") and host == "v6.example.com" and sni == "v6.example.com"


def test_pinned_transport_http_has_no_sni():
    sent = []

    class Inner:
        async def handle_async_request(self, request):
            sent.append(request.extensions.get("sni_hostname"))
            return httpx.Response(200)

        async def aclose(self):
            pass
    t = recon._PinnedTransport({"a.example.com": PUBLIC_IP})
    t.inner = Inner()

    async def go():
        async with httpx.AsyncClient(transport=t) as c:
            await c.get("http://a.example.com/")
    run(go())
    assert sent == [None]


# ================================================================ rebinding through the tool
class RecordingInner:
    def __init__(self, redirect=None):
        self.urls, self.redirect = [], redirect

    async def handle_async_request(self, request):
        self.urls.append((str(request.url), request.headers.get("host")))
        if self.redirect and len(self.urls) == 1:
            return httpx.Response(302, headers={"location": self.redirect})
        return httpx.Response(200, headers={"server": "edge"})

    async def aclose(self):
        pass


def patch_inner(monkeypatch, inner):
    real = recon._PinnedTransport.__init__

    def init(self, pins):
        real(self, pins)
        self.inner = inner
    monkeypatch.setattr(recon._PinnedTransport, "__init__", init)


def test_rebinding_name_never_reaches_second_answer(monkeypatch):
    answers = iter([[PUBLIC_IP], [PUBLIC_IP], ["127.0.0.1"], ["127.0.0.1"]])
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: next(answers))
    inner = RecordingInner()
    patch_inner(monkeypatch, inner)
    out = run(recon.t_http_fingerprint(ctx(), {"host": "rebind.example.com"}))
    assert "-> 200" in out
    assert inner.urls == [(f"https://{PUBLIC_IP}/", "rebind.example.com")]


def test_rebinding_second_vet_private_refused(monkeypatch):
    answers = iter([[PUBLIC_IP], ["169.254.169.254"]])
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: next(answers))
    inner = RecordingInner()
    patch_inner(monkeypatch, inner)
    out = run(recon.t_http_fingerprint(ctx(), {"host": "rebind.example.com"}))
    assert "refused" in out and inner.urls == []


def test_redirect_hop_pinned_to_its_own_vetted_address(monkeypatch):
    monkeypatch.setattr(recon, "_resolve_ips",
                        lambda h: [PUBLIC_IP2] if h == "www.example.com" else [PUBLIC_IP])
    inner = RecordingInner(redirect="https://www.example.com/home")
    patch_inner(monkeypatch, inner)
    out = run(recon.t_http_fingerprint(ctx(), {"host": "example.com"}))
    assert "-> 200" in out and "final: https://www.example.com/home" in out
    assert inner.urls == [(f"https://{PUBLIC_IP}/", "example.com"),
                          (f"https://{PUBLIC_IP2}/home", "www.example.com")]


def test_redirect_to_private_never_connects(monkeypatch):
    monkeypatch.setattr(recon, "_resolve_ips",
                        lambda h: ["10.1.2.3"] if h == "evil.example.net" else [PUBLIC_IP])
    inner = RecordingInner(redirect="https://evil.example.net/")
    patch_inner(monkeypatch, inner)
    out = run(recon.t_http_fingerprint(ctx(), {"host": "example.com"}))
    assert out.startswith("ERROR: redirect to evil.example.net refused")
    assert len(inner.urls) == 1


@pytest.mark.parametrize("loc", ["http://127.0.0.1/", "http://[::1]/", "http://2130706433/", "http://0x7f.1/"])
def test_redirect_to_ip_literal_never_connects(monkeypatch, loc):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: [PUBLIC_IP])
    inner = RecordingInner(redirect=loc)
    patch_inner(monkeypatch, inner)
    out = run(recon.t_http_fingerprint(ctx(), {"host": "example.com"}))
    assert "refused" in out and len(inner.urls) == 1


# ================================================================ strict ints / authorized
@pytest.mark.parametrize("v,want", [(443, 443), ("443", 443), (443.0, 443), (" 22 ", 22), ("+80", 80)])
def test_strict_int_ok(v, want):
    assert recon._strict_int(v) == want


@pytest.mark.parametrize("v", [True, False, None, 443.9, "443.5", "abc", float("nan"), float("inf"),
                               [443], {"p": 1}, "1e3", "9999999"])
def test_strict_int_refuses(v):
    with pytest.raises((ValueError, OverflowError)):
        recon._strict_int(v)


@pytest.mark.parametrize("v", [True, "true", "TRUE", " yes ", "1", 1])
def test_authorized_yes(v):
    assert recon._authorized(v) is True


@pytest.mark.parametrize("v", [False, "false", "no", "0", "", None, 0, 2, "maybe", [], ["true"], {"a": 1}, 1.0])
def test_authorized_no(v):
    assert recon._authorized(v) is False


@pytest.mark.parametrize("auth", ["false", "no", "0", "off"])
def test_port_scan_string_false_refused(monkeypatch, auth):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: (_ for _ in ()).throw(AssertionError("network")))
    ev = []
    out = run(recon.t_port_scan(ctx(ev), {"host": "example.com", "authorized": auth, "ports": [80]}))
    assert "authorized" in out and "refused" in out
    assert ev and ev[-1][0] == recon.REFUSED_KIND


@pytest.mark.parametrize("ports", [[True], [80, False], [22.5], ["80.5"], [None]])
def test_port_scan_junk_ports_refused(monkeypatch, ports):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: (_ for _ in ()).throw(AssertionError("network")))
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": ports}))
    assert out.startswith("ERROR: ports must be")


def test_port_scan_float_and_string_ports_ok(monkeypatch):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: [PUBLIC_IP])

    async def fake_open(host, port):
        raise OSError("closed")
    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": "true", "ports": [80.0, "443"]}))
    assert "(2 ports)" in out


@pytest.mark.parametrize("port", [True, 443.7, "443.5", [443]])
def test_tls_port_junk_refused(monkeypatch, port):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: (_ for _ in ()).throw(AssertionError("network")))
    out = run(recon.t_tls_inspect(ctx(), {"host": "example.com", "port": port}))
    assert out == "ERROR: port must be an integer"


# ================================================================ TLS address fallback
def test_tls_tries_next_vetted_address(monkeypatch):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: ["2606:2800:220:1::1", PUBLIC_IP])
    tried = []

    class Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def conn(addr, timeout=None):
        tried.append(addr[0])
        if ":" in addr[0]:
            raise OSError("no route to host")
        return Sock()

    class SSLSock(Sock):
        def getpeercert(self):
            return {"subject": ((("commonName", "example.com"),),), "issuer": (), "notAfter": "",
                    "subjectAltName": (("DNS", "example.com"),)}

        def version(self):
            return "TLSv1.3"

        def cipher(self):
            return ("TLS_AES_128_GCM_SHA256", "TLSv1.3", 128)

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            assert server_hostname == "example.com"
            return SSLSock()
    monkeypatch.setattr(socket, "create_connection", conn)
    monkeypatch.setattr(recon.ssl, "create_default_context", lambda: Ctx())
    out = run(recon.t_tls_inspect(ctx(), {"host": "example.com"}))
    assert tried == ["2606:2800:220:1::1", PUBLIC_IP] and "TLSv1.3" in out


def test_tls_all_addresses_fail_reports_last(monkeypatch):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: [PUBLIC_IP, PUBLIC_IP2])
    tried = []

    def conn(addr, timeout=None):
        tried.append(addr[0])
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(socket, "create_connection", conn)
    ev = []
    out = run(recon.t_tls_inspect(ctx(ev), {"host": "example.com"}))
    assert tried == [PUBLIC_IP, PUBLIC_IP2] and "ConnectionRefusedError" in out
    assert ev[-1][0] == "recon.tls" and ev[-1][1]["error"] == "ConnectionRefusedError"


def test_tls_mixed_private_answer_refused_before_connect(monkeypatch):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: [PUBLIC_IP, "10.0.0.5"])
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("connected")))
    out = run(recon.t_tls_inspect(ctx(), {"host": "example.com"}))
    assert "resolves to a private" in out


# ================================================================ allowlist normalisation
@pytest.mark.parametrize("entry,host,ok", [
    ("https://MySite.com/", "mysite.com", True), ("mysite.com:443", "www.mysite.com", True),
    ("user@mysite.com", "mysite.com", True), ("http://mysite.com/path?q=1#f", "mysite.com", True),
    ("bücher.de", "xn--bcher-kva.de", True), ("BÜCHER.DE", "shop.xn--bcher-kva.de", True),
    (" .mysite.com. ", "mysite.com", True), ("mysite.com", "evilmysite.com", False),
    ("https://mysite.com", "mysite.com.evil.net", False), ("[::1]", "mysite.com", False),
    ("93.184.216.34", "mysite.com", False),
])
def test_allowlist_entry_normalised(monkeypatch, entry, host, ok):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", entry)
    assert recon._allowed_host(host) is ok


def test_allowlist_url_entry_allows_tool_target(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "https://mysite.com/, other.org")
    host, err = recon._host_arg({"host": "api.mysite.com"})
    assert err is None and host == "api.mysite.com"


@pytest.mark.parametrize("raw", [", ,", "*.mysite.com", "https://", "[::1]:80", "@"])
def test_allowlist_junk_entries_deny(monkeypatch, raw):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", raw)
    assert recon._allowlist() == [] and not recon._allowed_host("mysite.com")


def test_allowlist_star_still_any(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", " * ")
    assert recon._allowlist() is None


def test_allowlist_unset_still_denies(monkeypatch):
    monkeypatch.delenv("AVRONE_RECON_ALLOWLIST")
    assert recon._allowlist() == [] and not recon._allowed_host("example.com")


# ================================================================ IPs embedded in names
@pytest.mark.parametrize("host", [
    "10-0-0-1.sslip.io", "127-0-0-1.nip.io", "app-192-168-1-5.nip.io", "169-254-169-254.nip.io",
    "x.172-16-0-9.example.com", "100-64-0-1.sslip.io", "7f000001.nip.io", "0a000001.sslip.io",
    "a9fea9fe.nip.io", "app.c0a80105.nip.io", "foo.localtest.me", "localtest.me", "anything.lvh.me",
    "a.vcap.me", "x.lacolhost.com", "010-000-000-001.nip.io",
])
def test_embedded_private_refused(host):
    err = recon._reject_private(host)
    assert err and "refused" in err


@pytest.mark.parametrize("host", [
    "8-8-8-8.nip.io", "93-184-216-34.sslip.io", "08080808.nip.io", "build-1-2-3.example.com",
    "0a000001.example.com", "v1-2-3-4567.example.com", "deadbeef.nip.io", "example.com",
    "release-2026-11-03.example.com",
])
def test_embedded_public_or_not_ip_allowed(host):
    assert recon._reject_private(host) is None


@pytest.mark.parametrize("host", ["10-0-0-1.sslip.io", "7f000001.nip.io", "foo.localtest.me"])
def test_embedded_refused_through_gate_without_dns(monkeypatch, host):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: (_ for _ in ()).throw(AssertionError("network")))
    ev = []
    out = run(recon.t_http_fingerprint(ctx(ev), {"host": host}))
    assert "refused" in out and ev[-1][0] == recon.REFUSED_KIND


# ================================================================ IPv6 classes
@pytest.mark.parametrize("ip", ["fec0::1", "feff::1", "fec0:0:0:ffff::1"])
def test_ipv6_site_local_non_public(ip):
    import ipaddress
    assert recon._non_public_ip(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["2606:2800:220:1::1", "2001:4860:4860::8888"])
def test_ipv6_global_public(ip):
    import ipaddress
    assert not recon._non_public_ip(ipaddress.ip_address(ip))


def test_resolved_site_local_refused():
    assert "private" in recon._reject_resolved("example.com", ["fec0::5"])


@pytest.mark.parametrize("ip", ["64:ff9b::7f00:1", "2002:7f00:1::", "::ffff:7f00:1", "::ffff:169.254.169.254"])
def test_resolved_transition_forms_refused(ip):
    assert recon._reject_resolved("example.com", [ip])


# ================================================================ real TLS: SNI + cert check use the name
@pytest.fixture()
def tls_server(tmp_path):
    import shutil
    import ssl
    import subprocess
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(crt), "-days", "1", "-subj", "/CN=pin.example.com",
                    "-addext", "subjectAltName=DNS:pin.example.com"], check=True, capture_output=True)
    sni_seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(str(crt), str(key))
    sctx.sni_callback = lambda sock, name, c: sni_seen.append(name)
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv.server_address[1], str(crt), sni_seen
    srv.shutdown()
    srv.server_close()


def test_pinned_tls_verifies_certificate_against_name(tls_server):
    import ssl
    port, crt, sni_seen = tls_server
    cctx = ssl.create_default_context(cafile=crt)
    t = recon._PinnedTransport({"pin.example.com": "127.0.0.1"})
    t.inner = httpx.AsyncHTTPTransport(verify=cctx, retries=0)

    async def go():
        async with httpx.AsyncClient(transport=t) as c:
            return await c.get(f"https://pin.example.com:{port}/")
    assert run(go()).status_code == 200
    assert sni_seen == ["pin.example.com"]  # SNI carried the name, not 127.0.0.1


def test_pinned_tls_wrong_name_fails_verification(tls_server):
    import ssl
    port, crt, _ = tls_server
    cctx = ssl.create_default_context(cafile=crt)
    t = recon._PinnedTransport({"other.example.com": "127.0.0.1"})
    t.inner = httpx.AsyncHTTPTransport(verify=cctx, retries=0)

    async def go():
        async with httpx.AsyncClient(transport=t) as c:
            await c.get(f"https://other.example.com:{port}/")
    with pytest.raises(httpx.ConnectError, match="(?i)certificate|hostname"):
        run(go())
