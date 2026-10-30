"""Round 16 coverage: a fleet checkpoint whose owner stamp fails is released (not left claimed), and the
replay cache's temp file is removed when its replace fails."""
import asyncio
from unittest.mock import MagicMock

import pytest

from avrone import checkpoint as ckm
from avrone import fleet as fleet_mod
from avrone.fleet import FleetManager, sign


class _Core:
    def __init__(self):
        self.codex = type("C", (), {"emit": lambda *a, **k: None})()

    async def squad(self, task, n=None, **kw):
        return {"answer": "a", "run_id": "r"}


@pytest.fixture
def fm(tmp_path, monkeypatch):
    for k in ("AVRONE_FLEET_PARENT", "AVRONE_FLEET_ALLOW", "AVRONE_PUBLIC_URL", "AVRONE_FLEET_REQUIRE_ALLOW",
              "AVRONE_FLEET_MAX_HOPS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "hub")
    return FleetManager(_Core(), tmp_path)


def test_stamp_failure_releases_checkpoint(fm, tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_mod, "stamp_owner", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        asyncio.run(fm.handle_run({"mode": "squad", "prompt": "p", "hops": 1, "from": "kid",
                                   "checkpoint_id": "s1"}))
    path = ckm.managed_path(tmp_path, ckm.fleet_key("kid", "s1"))
    assert not ckm.is_busy(path)  # a retry is not blocked by a stale claim


def test_nonce_save_replace_failure_cleans_tmp(fm, tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_mod.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("ro")))
    fm.verify("POST", "/p", b"", sign("tok", "POST", "/p", b""))  # still accepted
    assert not list(tmp_path.glob("fleet_nonces.json.*.tmp"))
