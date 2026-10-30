"""v0.31: fleet peer urls are validated. Before: anything starting with http:// was stored, so a token
holder could register a cloud metadata endpoint (169.254.169.254) or a url with credentials/CRLF, and
this node would POST signed payloads there and echo the reply into dispatch errors."""
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import check_node_url


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest", "http://[fd00:ec2::254]", "http://metadata.google.internal/",
    "http://METADATA.GOOGLE.INTERNAL./", "http://100.100.100.200", "http://169.254.1.1:80",
    "http://[::ffff:169.254.169.254]", "http://[fe80::1]", "http://224.0.0.1", "http://0.0.0.0",
    "http://user:pw@node.lan", "http://@node.lan", "http://node.lan/?x=1", "http://node.lan/#f",
    "http://node.lan\r\nX-Evil: 1", "http://node .lan", "http://nöde.lan", "ftp://node.lan", "http://",
    "http://node.lan:0", "http://node.lan:99999", "http://[::1", "", None, 5, "http://a/" + "x" * 600,
])
def test_bad_urls_refused(url):
    with pytest.raises(ValueError):
        check_node_url(url)


@pytest.mark.parametrize("url,out", [
    ("http://kid.test", "http://kid.test"), ("https://node.example.com:8443/", "https://node.example.com:8443"),
    ("http://127.0.0.1:8710", "http://127.0.0.1:8710"), ("http://192.168.1.20:8000/avrone/", "http://192.168.1.20:8000/avrone"),
    ("http://[::1]:8710", "http://[::1]:8710"), ("http://10.0.0.5", "http://10.0.0.5"),
])
def test_lan_and_public_urls_kept(url, out):
    assert check_node_url(url) == out


def test_register_uses_check(tmp_path, monkeypatch):
    for k in ("AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL", "AVRONE_FLEET_ALLOW",
              "AVRONE_FLEET_REQUIRE_ALLOW"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    c = MagicMock()
    c.active = {}
    fm = fleet_mod.FleetManager(c, tmp_path)
    with pytest.raises(ValueError, match="metadata"):
        fm.register({"node_id": "kid", "url": "http://169.254.169.254"})
    assert "kid" not in fm.nodes
    fm.register({"node_id": "kid", "url": "http://10.0.0.7:8710/"})
    assert fm.nodes["kid"]["url"] == "http://10.0.0.7:8710"
