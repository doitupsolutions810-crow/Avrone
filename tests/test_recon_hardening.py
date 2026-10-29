"""Regression tests for recon/OSINT input hardening. Fully offline: DNS, sockets and httpx are faked."""
import asyncio
import socket
from types import SimpleNamespace

import httpx
import pytest

from avrone import osint, recon

PUBLIC_IP = "93.184.216.34"


def ctx(events=None):
    def emit(run_id, kind, agent, **data):
        if events is not None:
            events.append((kind, data))
    core = SimpleNamespace(codex=SimpleNamespace(emit=emit), settings=SimpleNamespace(data_dir="/tmp/none"))
    return SimpleNamespace(core=core, run_id="r", name="Recon-1", depth=0)


def run(c):
    return asyncio.run(c)


class NetworkTouched(AssertionError):
    pass


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Any attempt to reach the network fails the test loudly."""
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "*")  # v0.21: recon is default-deny; "*" = any public host

    def no_net(*a, **k):
        raise NetworkTouched(f"network touched: {a!r}")

    async def no_net_async(*a, **k):
        raise NetworkTouched(f"network touched: {a!r}")

    class NoClient:
        def __init__(self, *a, **k):
            raise NetworkTouched("httpx client created")

    monkeypatch.setattr(recon, "_resolve_ips", no_net)
    monkeypatch.setattr(socket, "create_connection", no_net)
    monkeypatch.setattr(socket, "getaddrinfo", no_net)
    monkeypatch.setattr(asyncio, "open_connection", no_net_async)
    monkeypatch.setattr(httpx, "AsyncClient", NoClient)


TOOLS = [
    (recon.t_tls_inspect, {}),
    (recon.t_http_fingerprint, {}),
    (recon.t_port_scan, {"authorized": True}),
    (recon.t_cert_transparency, {}),
    (recon.t_dns_enum, {}),
    (osint.t_osint_domain, {}),
]
TOOL_IDS = ["tls_inspect", "http_fingerprint", "port_scan", "cert_transparency", "dns_enum", "osint_domain"]

PRIVATE_TARGETS = [
    "localhost", "LOCALHOST", "localhost.", "api.localhost", "localhost.localdomain", "ip6-localhost",
    "127.0.0.1", "127.1", "2130706433", "0x7f.0x0.0x0.0x1", "0177.0.0.1", "0.0.0.0",
    "10.1.2.3", "172.16.0.1", "192.168.1.1", "100.64.0.1",
    "169.254.169.254", "http://169.254.169.254/latest/meta-data/", "169.254.1.1",
    "metadata.google.internal", "metadata", "instance-data.ec2.internal",
    "printer.local", "db.internal", "nas.home.arpa", "router.lan", "box.localdomain",
    "10.0.0.1.nip.io", "app.192.168.0.10.sslip.io",
    "::1", "[::1]", "http://[::1]:8080/", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "[::ffff:169.254.169.254]",
]


@pytest.mark.parametrize("target", PRIVATE_TARGETS)
def test_reject_private_and_host_arg_refuse(target):
    host, err = recon._host_arg({"host": target})
    assert err and err.startswith("ERROR"), (target, host, err)


@pytest.mark.parametrize("fn,extra", TOOLS, ids=TOOL_IDS)
@pytest.mark.parametrize("target", ["localhost", "127.0.0.1", "169.254.169.254", "metadata.google.internal",
                                    "192.168.1.1", "[::1]", "printer.local", "10.0.0.1.nip.io", "127.1"])
def test_every_tool_refuses_private_targets_without_network(fn, extra, target):
    out = run(fn(ctx(), {"host": target, **extra}))
    assert out.startswith("ERROR"), out


@pytest.mark.parametrize("target,needle", [
    ("8.8.8.8", "IP literal"),
    ("2606:4700::1111", "IP literal"),
    ("[2606:4700::1111]", "IP literal"),
    ("10.0.0.0/8", "CIDR"),
    ("8.8.8.0/24", "CIDR"),
    ("example.com/24", "CIDR"),
    ("example.com,example.org", "one target"),
    ("example.com example.org", "one target"),
    ("example.com;example.org", "one target"),
    ("example.com\nexample.org", "one target"),
    ("*.example.com", "wildcard"),
    ("example.*", "wildcard"),
    ("{a,b}.example.com", "one target"),
    ("http://user:pw@example.com/", "userinfo"),
    ("ftp://example.com", "scheme"),
    ("file:///etc/passwd", "scheme"),
    ("fileserver", "valid public hostname"),
    ("example.123", "valid public hostname"),
    ("-bad-.example.com", "valid public hostname"),
    ("a" * 64 + ".com", "valid"),
    ("x" * 3000, "too long"),
])
def test_target_syntax_refusals(target, needle):
    host, err = recon._host_arg({"host": target})
    assert err and err.startswith("ERROR") and needle.lower() in err.lower(), (target, err)


@pytest.mark.parametrize("target,expected", [
    ("example.com", "example.com"),
    ("Example.COM.", "example.com"),
    ("https://example.com:443/path?x=1", "example.com"),
    ("http://sub.example.co.uk/", "sub.example.co.uk"),
    ("_dmarc.example.com", "_dmarc.example.com"),
    ("xn--bcher-kva.example", "xn--bcher-kva.example"),
    ("bücher.example", "xn--bcher-kva.example"),
    ("1.2.3.4.example.com", "1.2.3.4.example.com"),  # embedded PUBLIC quad is just a name
])
def test_public_domains_still_accepted(target, expected):
    assert recon._host_arg({"host": target}) == (expected, None)


# ------------------------------------------------------------------ kill switch + allowlist modes
@pytest.mark.parametrize("mode", ["deny", "DENY", " deny ", "off", "false", "0", "no", "disabled",
                                  "denyy", "maybe", "allow-ish"])
def test_kill_switch_fails_closed(monkeypatch, mode):
    monkeypatch.setenv("AVRONE_RECON", mode)
    assert recon._denied() and "disabled" in recon._denied()
    for fn, extra in TOOLS:
        assert "disabled" in run(fn(ctx(), {"host": "example.com", **extra}))


@pytest.mark.parametrize("mode", ["allow", "ALLOW", " allow ", "", "on", "true", "1", "yes"])
def test_kill_switch_allow_values(monkeypatch, mode):
    monkeypatch.setenv("AVRONE_RECON", mode)
    assert recon._denied() is None


def test_kill_switch_unset_is_allow(monkeypatch):
    monkeypatch.delenv("AVRONE_RECON", raising=False)
    assert recon._denied() is None


def test_deny_checked_before_target_parsing(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "deny")
    assert "disabled" in run(recon.t_tls_inspect(ctx(), {"host": "127.0.0.1"}))


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_allowlist_is_default_deny(monkeypatch, raw):
    # v0.21: unset/blank allows nothing; the operator must name their own/authorized domains
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", raw)
    host, err = recon._host_arg({"host": "example.com"})
    assert err and "default-deny" in err and "AVRONE_RECON_ALLOWLIST" in err
    assert recon._host_arg({"host": "localhost"})[1]


def test_unset_allowlist_is_default_deny(monkeypatch):
    monkeypatch.delenv("AVRONE_RECON_ALLOWLIST", raising=False)
    assert recon._host_arg({"host": "example.com"})[1]


def test_star_allowlist_means_any_public_host_but_never_private(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", " * ")
    assert recon._host_arg({"host": "example.com"}) == ("example.com", None)
    for t in ("localhost", "10.0.0.1", "169.254.169.254", "nas.local"):
        assert recon._host_arg({"host": t})[1]


def test_named_allowlist_still_exact_or_true_subdomain(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "mysite.com")
    assert recon._host_arg({"host": "api.mysite.com"})[1] is None
    assert recon._host_arg({"host": "evilmysite.com"})[1]


@pytest.mark.parametrize("raw", [",", " , , ", "*, *.", "*,", ".", "..,"])
def test_set_but_unparseable_allowlist_fails_closed(monkeypatch, raw):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", raw)
    host, err = recon._host_arg({"host": "example.com"})
    assert err and "not in AVRONE_RECON_ALLOWLIST" in err


def test_allowlist_cannot_whitelist_private_targets(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "localhost, 127.0.0.1, internal, 169.254.169.254")
    for t in ("localhost", "127.0.0.1", "db.internal", "169.254.169.254"):
        assert recon._host_arg({"host": t})[1]


def test_allowlist_trailing_dot_normalised(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "mysite.com.")
    assert recon._host_arg({"host": "api.mysite.com."}) == ("api.mysite.com", None)
    assert recon._host_arg({"host": "evilmysite.com"})[1]


# ------------------------------------------------------------------ connect-time resolution guard
@pytest.mark.parametrize("ips", [["127.0.0.1"], ["10.0.0.5"], ["169.254.169.254"], ["::1"],
                                 [PUBLIC_IP, "192.168.0.2"], ["::ffff:10.0.0.1"], []])
def test_connecting_tools_refuse_names_resolving_private(monkeypatch, ips):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: ips)
    for fn, extra in TOOLS[:3]:  # tls, http, port_scan connect to the target itself
        if fn is recon.t_tls_inspect:
            async def thread(f, *a, **k):
                return f(*a, **k)
            monkeypatch.setattr(asyncio, "to_thread", thread)
        out = run(fn(ctx(), {"host": "rebind.example.com", **extra}))
        assert out.startswith("ERROR") and ("resolve" in out), out


def test_port_scan_connects_to_vetted_address_only(monkeypatch):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: [PUBLIC_IP])
    hosts = []

    async def fake_open(host, port):
        hosts.append(host)
        raise ConnectionRefusedError
    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": [80, 443]}))
    assert "open: none" in out and set(hosts) == {PUBLIC_IP}


def test_tls_connects_to_vetted_address_with_sni(monkeypatch):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: [PUBLIC_IP])
    seen = {}

    class Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class SSock(Sock):
        def getpeercert(self):
            return {"notAfter": "x"}

        def version(self):
            return "TLSv1.3"

        def cipher(self):
            return ("C", "TLSv1.3", 256)

    def fake_conn(addr, timeout=None):
        seen["addr"] = addr
        return Sock()

    class FakeCtx:
        def wrap_socket(self, sock, server_hostname=None):
            seen["sni"] = server_hostname
            return SSock()

    async def thread(f, *a, **k):
        return f(*a, **k)
    monkeypatch.setattr(socket, "create_connection", fake_conn)
    monkeypatch.setattr(recon.ssl, "create_default_context", lambda: FakeCtx())
    monkeypatch.setattr(asyncio, "to_thread", thread)
    out = run(recon.t_tls_inspect(ctx(), {"host": "example.com"}))
    assert "TLSv1.3" in out and seen == {"addr": (PUBLIC_IP, 443), "sni": "example.com"}


@pytest.mark.parametrize("port", ["abc", 0, 70000, -1])
def test_tls_port_validated(port):
    assert run(recon.t_tls_inspect(ctx(), {"host": "example.com", "port": port})).startswith("ERROR: port")


@pytest.mark.parametrize("scheme", ["ftp", "file", "gopher", "javascript"])
def test_http_scheme_restricted(scheme):
    assert "scheme must be http or https" in run(recon.t_http_fingerprint(ctx(), {"host": "example.com",
                                                                                   "scheme": scheme}))


def _hooked_client(redirect_to):
    """Fake AsyncClient that fires request hooks for the first URL and a redirect hop."""
    class C:
        def __init__(self, *a, event_hooks=None, **k):
            self.hooks = (event_hooks or {}).get("request", [])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            for u in (url, redirect_to):
                req = httpx.Request("GET", u)
                for h in self.hooks:
                    await h(req)
            return SimpleNamespace(status_code=200, headers=httpx.Headers({}), url=redirect_to)
    return C


@pytest.mark.parametrize("hop", ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8080/",
                                 "http://localhost/admin", "http://[::1]/", "http://db.internal/"])
def test_http_redirect_to_private_target_refused(monkeypatch, hop):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: [PUBLIC_IP])
    monkeypatch.setattr(httpx, "AsyncClient", _hooked_client(hop))
    out = run(recon.t_http_fingerprint(ctx(), {"host": "example.com"}))
    assert out.startswith("ERROR: redirect to") and "refused" in out, out


def test_http_redirect_to_name_resolving_private_refused(monkeypatch):
    monkeypatch.setattr(recon, "_resolve_ips",
                        lambda h: ["10.0.0.9"] if h == "sneaky.example.net" else [PUBLIC_IP])
    monkeypatch.setattr(httpx, "AsyncClient", _hooked_client("https://sneaky.example.net/"))
    out = run(recon.t_http_fingerprint(ctx(), {"host": "example.com"}))
    assert out.startswith("ERROR: redirect to sneaky.example.net refused")


def test_http_redirect_outside_allowlist_refused(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "example.com")
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: [PUBLIC_IP])
    monkeypatch.setattr(httpx, "AsyncClient", _hooked_client("https://other.org/"))
    out = run(recon.t_http_fingerprint(ctx(), {"host": "example.com"}))
    assert "not in AVRONE_RECON_ALLOWLIST" in out


def test_http_public_redirect_still_works(monkeypatch):
    monkeypatch.setattr(recon, "_resolve_ips", lambda h: [PUBLIC_IP])
    monkeypatch.setattr(httpx, "AsyncClient", _hooked_client("https://www.example.com/"))
    out = run(recon.t_http_fingerprint(ctx(), {"host": "example.com"}))
    assert "-> 200" in out and "www.example.com" in out


# ------------------------------------------------------------------ output caps
def _json_client(handler):
    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            data = handler(url, params or {})
            return SimpleNamespace(status_code=200, json=lambda: data, raise_for_status=lambda: None)
    return C


def test_ct_caps_rows_and_names(monkeypatch):
    rows = [{"name_value": "\n".join(f"h{i}-{j}.example.com" for j in range(3))} for i in range(20000)]
    monkeypatch.setattr(httpx, "AsyncClient", _json_client(lambda u, p: rows))
    ev = []
    out = run(recon.t_cert_transparency(ctx(ev), {"host": "example.com"}))
    assert ev[0][1]["found"] == recon.CT_MAX_NAMES
    assert f"{recon.CT_MAX_NAMES} unique" in out
    assert len(out.splitlines()) <= recon.CT_SHOW + 2


def test_ct_rejects_lookalike_suffix(monkeypatch):
    rows = [{"name_value": "evilexample.com\nwww.example.com\nexample.com\n*.example.com"}]
    monkeypatch.setattr(httpx, "AsyncClient", _json_client(lambda u, p: rows))
    out = run(recon.t_cert_transparency(ctx(), {"host": "example.com"}))
    assert "evilexample.com" not in out and "2 unique" in out


@pytest.mark.parametrize("payload", [{"error": "x"}, "oops", [1, "two", None]])
def test_ct_tolerates_bad_shapes(monkeypatch, payload):
    monkeypatch.setattr(httpx, "AsyncClient", _json_client(lambda u, p: payload))
    out = run(recon.t_cert_transparency(ctx(), {"host": "example.com"}))
    assert out.startswith("ERROR") or "no certificate-transparency records" in out


def test_dns_caps_answers_and_length(monkeypatch):
    big = {"Answer": [{"data": "v" * 5000} for _ in range(500)]}
    seen = []

    def h(url, params):
        seen.append(params["type"])
        return big
    monkeypatch.setattr(httpx, "AsyncClient", _json_client(h))
    out = run(recon.t_dns_enum(ctx(), {"host": "example.com", "types": ["TXT"] * 50}))
    assert len(seen) == recon.DNS_MAX_TYPES
    line = out.splitlines()[1]
    assert line.count("v" * recon.DNS_MAX_ANSWER_LEN) == recon.DNS_MAX_ANSWERS
    assert "v" * (recon.DNS_MAX_ANSWER_LEN + 1) not in out


@pytest.mark.parametrize("types", ["A", [1], ["A;rm"], ["A" * 11], [None]])
def test_dns_types_validated(types):
    assert run(recon.t_dns_enum(ctx(), {"host": "example.com", "types": types})).startswith("ERROR: types")


def test_dns_tolerates_non_dict_payload(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _json_client(lambda u, p: ["junk"]))
    out = run(recon.t_dns_enum(ctx(), {"host": "example.com", "types": ["A"]}))
    assert "A: -" in out


def test_module_audit_notes_present():
    assert "AUDIT NOTE" in recon.__doc__ and "_reject_private" in recon.__doc__
    assert "AUDIT NOTE" in osint.__doc__
