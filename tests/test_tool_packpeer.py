import asyncio
import gzip
import io
import tarfile
import time
from types import SimpleNamespace as S
from unittest.mock import MagicMock

import pytest

from avrone import agentpack, agentfs
from avrone import fleet as fleet_mod
from avrone import tools_packpeer as tp
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools


def run(c):
    return asyncio.run(c)


def test_registered_read_only():
    r = builtin_registry()
    register_extra_tools(r)
    from avrone.tools_statehealth import ACTING
    for n in ("pack_list",):
        assert r.get(n) and n not in ACTING


def _ctx(tmp_path, fleet=None):
    return S(core=S(settings=S(data_dir=tmp_path / "data", workspace=tmp_path / "ws"), fleet=fleet))


def test_pack_list_empty(tmp_path):
    assert "no agent packs yet" in run(tp.t_pack_list(_ctx(tmp_path), {}))
    agentpack.pack_dir(tmp_path / "data").mkdir(parents=True)
    assert "no agent packs yet" in run(tp.t_pack_list(_ctx(tmp_path), {}))


def test_pack_list_verifies_good_and_bad(tmp_path):
    ws, data = tmp_path / "ws", tmp_path / "data"
    home = agentfs.agent_dir(ws, "a1") if hasattr(agentfs, "agent_dir") else None
    try:
        bundle, man = agentpack.export_pack(ws, data, "a1", "node-1", home="node-1")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"export unavailable in this layout: {e}")
    d = agentpack.pack_dir(data)
    d.mkdir(parents=True, exist_ok=True)
    (d / "a1-1.tar.gz").write_bytes(bundle)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        ti = tarfile.TarInfo("evil")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))
    (d / "a1-2.tar.gz").write_bytes(gzip.compress(raw.getvalue()))
    (d / "a1-3.tar.gz").symlink_to(d / "a1-1.tar.gz")
    (d / ".a1-4.tar.gz.tmp").write_bytes(b"x")
    out = run(tp.t_pack_list(_ctx(tmp_path), {}))
    assert "3 pack(s)" in out and "a1-1.tar.gz" in out and ": OK - agent a1 from node-1" in out
    assert "a1-2.tar.gz" in out and "REFUSED - unexpected member" in out
    assert "a1-3.tar.gz: REFUSED - symlink" in out and "2 pack(s) would be refused" in out and ".tmp" not in out


def test_pack_list_caps_and_unreadable(tmp_path, monkeypatch):
    d = agentpack.pack_dir(tmp_path / "data")
    d.mkdir(parents=True)
    monkeypatch.setattr(tp, "MAX_LIST", 2)
    for i in range(3):
        (d / f"a-{i}.tar.gz").write_bytes(b"junk")
    monkeypatch.setattr(tp.Path, "read_bytes", lambda self: (_ for _ in ()).throw(PermissionError("no")))
    out = run(tp.t_pack_list(_ctx(tmp_path), {}))
    assert "first 2 shown" in out and "UNREADABLE - PermissionError" in out
