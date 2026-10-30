"""Round 17 coverage: pack_list survives a pack whose stat fails; memory refuses an empty key."""
import asyncio
from types import SimpleNamespace as S

import pytest

from avrone import agentpack
from avrone import tools_packpeer as tp
from avrone.memstore import MemoryRefused, MemoryStore


def test_pack_list_stat_error(tmp_path, monkeypatch):
    d = agentpack.pack_dir(tmp_path / "data")
    d.mkdir(parents=True)
    (d / "a-1.tar.gz").write_bytes(b"junk")
    real_stat = tp.Path.stat

    def stat(self, *a, **k):
        if self.name == "a-1.tar.gz" and k.get("follow_symlinks", True):  # lstat (is_symlink) still works
            raise OSError("gone")
        return real_stat(self, *a, **k)
    monkeypatch.setattr(tp.Path, "stat", stat)
    ctx = S(core=S(settings=S(data_dir=tmp_path / "data")))
    out = asyncio.run(tp.t_pack_list(ctx, {}))
    assert "a-1.tar.gz (-1 bytes): REFUSED" in out


def test_memory_empty_key_refused(tmp_path):
    m = MemoryStore(tmp_path / "mem.json")
    with pytest.raises(MemoryRefused, match="key is required"):
        m.save("ns", "", "v")
