"""v0.30: the fleet replay cache survives a restart. Before: nonces lived only in memory, so a signed
request captured in flight (a remote run, even priv:shell) was accepted again by the same node after a
restart, as long as its timestamp was still inside the 60 s window."""
import json
import time
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import WINDOW_S, FleetManager, GatewayRefused, sign


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


def node(tmp_path):
    c = MagicMock()
    c.active = {}
    return FleetManager(c, tmp_path)


BODY = b'{"mode":"chat","task":"rm -rf"}'


def test_replay_refused_after_restart(tmp_path):
    h = sign("tok", "POST", "/api/fleet/run", BODY)
    node(tmp_path).verify("POST", "/api/fleet/run", BODY, h)
    restarted = node(tmp_path)  # same data dir, new process
    with pytest.raises(GatewayRefused) as e:
        restarted.verify("POST", "/api/fleet/run", BODY, h)
    assert e.value.code == "replay"
    restarted.verify("POST", "/api/fleet/run", BODY, sign("tok", "POST", "/api/fleet/run", BODY))  # fresh ok


def test_expired_and_junk_entries_dropped_on_load(tmp_path):
    now = time.time()
    (tmp_path / "fleet_nonces.json").write_text(json.dumps(
        {"live": now + 100, "old": now - 1, "bad nonce!": now + 100, "flag": True, "s": "x", "x" * 200: now + 9}))
    fm = node(tmp_path)
    assert set(fm._nonces) == {"live"} and fm._boot_guard is None


@pytest.mark.parametrize("content", ["{torn", "[1,2]", "null"])
def test_unreadable_cache_turns_on_boot_guard(tmp_path, content):
    (tmp_path / "fleet_nonces.json").write_text(content)
    fm = node(tmp_path)
    assert fm._boot_guard is not None
    old = sign("tok", "POST", "/api/fleet/run", BODY, ts=str(int(time.time()) - 5))
    with pytest.raises(GatewayRefused) as e:
        fm.verify("POST", "/api/fleet/run", BODY, old)
    assert "before this node restarted" in str(e.value)
    fm.verify("POST", "/api/fleet/run", BODY, sign("tok", "POST", "/api/fleet/run", BODY,
                                                   ts=str(int(time.time()) + 1)))


def test_boot_guard_expires(tmp_path, monkeypatch):
    (tmp_path / "fleet_nonces.json").write_text("{torn")
    fm = node(tmp_path)
    fm._boot_guard = time.time() - 2 * WINDOW_S - 1
    fm.verify("POST", "/p", b"", sign("tok", "POST", "/p", b""))
    assert fm._boot_guard is None


def test_persist_failure_never_refuses(tmp_path, monkeypatch):
    fm = node(tmp_path)
    from pathlib import Path
    monkeypatch.setattr(Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    fm.verify("POST", "/p", b"", sign("tok", "POST", "/p", b""))
    assert not list(tmp_path.glob("*.tmp"))


def test_cache_file_holds_no_secrets(tmp_path):
    fm = node(tmp_path)
    fm.verify("POST", "/p", BODY, sign("tok", "POST", "/p", BODY, nonce="abc123"))
    raw = (tmp_path / "fleet_nonces.json").read_text()
    assert "abc123" in raw and "tok" not in raw and "rm -rf" not in raw
