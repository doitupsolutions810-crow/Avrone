import asyncio
import json
import time
from types import SimpleNamespace as S
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod
from avrone import tools_gatewayview as gv
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools


def run(c):
    return asyncio.run(c)


@pytest.fixture
def fm(tmp_path, monkeypatch):
    for k in ("AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)
    c = MagicMock()
    c.active = {}
    return fleet_mod.FleetManager(c, tmp_path)


def test_registered_read_only():
    r = builtin_registry()
    register_extra_tools(r)
    from avrone.tools_statehealth import ACTING
    for n in ("replay_guard",):
        assert r.get(n) and n not in ACTING


def test_replay_guard_ok_then_persisted(fm):
    ctx = S(core=S(fleet=fm))
    out = run(gv.t_replay_guard(ctx, {}))
    assert out.startswith("replay_guard: OK - 0/100000") and "persisted=not yet" in out and "boot guard: off" in out
    fm.verify("POST", "/p", b"", fleet_mod.sign("tok", "POST", "/p", b"", nonce="n1"))
    out = run(gv.t_replay_guard(ctx, {}))
    assert "1/100000" in out and "persisted=yes" in out and "n1" not in out


def test_replay_guard_degraded(fm, monkeypatch):
    (fm._nonce_path).write_text("{torn")
    fm._boot_guard = time.time()
    monkeypatch.setattr(fleet_mod, "NONCE_CACHE_MAX", 2)
    fm._nonces = {"a": time.time() + 99, "b": time.time() + 99}
    out = run(gv.t_replay_guard(S(core=S(fleet=fm)), {}))
    assert out.startswith("replay_guard: DEGRADED") and "unreadable" in out and "near its cap" in out
    assert "boot guard: active, 1" in out


def test_replay_guard_memory_only_and_off():
    bare = S(enabled=True, _nonces={"x": "junk"})
    out = run(gv.t_replay_guard(S(core=S(fleet=bare)), {}))
    assert "DEGRADED" in out and "memory-only" in out and "0/" in out
    assert "OFF" in run(gv.t_replay_guard(S(core=S(fleet=None)), {}))
    assert "OFF" in run(gv.t_replay_guard(S(core=None), {}))
