import asyncio
import httpx
import pytest

from avrone import netdiag, tools_extra


def run(c):
    return asyncio.run(c)


@pytest.fixture()
def mock_http(monkeypatch):
    def handler(req: httpx.Request):
        u = str(req.url)
        if "dns-query" in u:
            return httpx.Response(200, json={"Status": 0, "Answer": [{"name": "ex.com", "TTL": 60, "data": "1.2.3.4"}]})
        if "rdap.org/domain" in u:
            return httpx.Response(200, json={"ldhName": "EX.COM", "events": [{"eventAction": "registration", "eventDate": "2000-01-01"}],
                                             "nameservers": [{"ldhName": "NS1.EX.COM"}],
                                             "entities": [{"roles": ["registrar"], "vcardArray": ["vcard", [["fn", {}, "text", "Reg Inc"]]]}]})
        if "ipwho.is" in u:
            return httpx.Response(200, json={"success": True, "ip": "1.2.3.4", "city": "Flint", "region": "MI", "country": "US",
                                             "latitude": 43, "longitude": -83, "connection": {"asn": 1, "org": "Org", "isp": "ISP"}})
        return httpx.Response(404)
    monkeypatch.setattr(tools_extra, "HTTP_TRANSPORT", httpx.MockTransport(handler))


def test_dns_doh(mock_http):
    out = run(netdiag.t_dns_lookup(None, {"host": "ex.com", "type": "MX"}))
    assert "NOERROR" in out and "1.2.3.4" in out


def test_rdap(mock_http):
    out = run(netdiag.t_rdap_whois(None, {"query": "https://ex.com/path"}))
    assert "EX.COM" in out and "Reg Inc" in out and "NS1.EX.COM" in out


def test_geo(mock_http):
    out = run(netdiag.t_ip_geo(None, {"host": "1.2.3.4"}))
    assert "Flint" in out and "AS1" in out


def test_geo_private():
    assert "private" in run(netdiag.t_ip_geo(None, {"host": "192.168.1.1"}))


def test_validation():
    with pytest.raises(ValueError):
        run(netdiag.t_tcp_ping(None, {"host": "-oProxy=evil"}))
    assert run(netdiag.t_dns_lookup(None, {"host": "ex.com", "type": "ANY"})).startswith("ERROR")


def test_tcp_ping_local():
    async def go():
        srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            return await netdiag.t_tcp_ping(None, {"host": "127.0.0.1", "port": port, "count": 2})
        finally:
            srv.close()
    assert "2/2 ok" in run(go())


def test_registered():
    from avrone.tools import ToolRegistry
    reg = ToolRegistry()
    tools_extra.register_extra_tools(reg)
    assert {"dns_lookup", "tcp_ping", "traceroute", "rdap_whois", "ip_geo"} <= set(reg.names())
