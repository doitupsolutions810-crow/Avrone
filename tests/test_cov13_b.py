"""Round 13 coverage: access-policy helpers, bridge revise parsing of non-object JSON, trinity agent
dir with no workspace, checkpoint store tmp cleanup when the tmp is already gone."""
from types import SimpleNamespace

import pytest

from avrone import agentfs, bridge, checkpoint, server


@pytest.mark.parametrize("val,want", [(None, "loopback"), ("", "loopback"), (" ADMIN ", "admin"), ("all", "all"),
                                      ("open", "loopback")])
def test_access_policy(monkeypatch, val, want):
    if val is None:
        monkeypatch.delenv("AVRONE_PUBLIC_ACCESS", raising=False)
    else:
        monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", val)
    assert server.access_policy() == want


def test_caller_allowed_matrix(monkeypatch):
    monkeypatch.setattr(server.settings, "admin_token", "t" * 20)
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "loopback")
    assert server.caller_allowed("127.0.0.1", None) and not server.caller_allowed("10.0.0.2", None)
    assert server.caller_allowed("10.0.0.2", "t" * 20) and not server.caller_allowed(None, "nope")
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "admin")
    assert not server.caller_allowed("::1", None) and server.caller_allowed("::1", "t" * 20)


def test_require_local_without_client(monkeypatch):
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "loopback")
    with pytest.raises(server.HTTPException) as e:
        server.require_local(SimpleNamespace(client=None), None)
    assert e.value.status_code == 403


def test_parse_revise_junk():
    assert bridge.parse_revise(None) == ([], "")
    assert bridge.parse_revise('REVISE: ["not", "an", "object"]') == ([], "")
    assert bridge.parse_revise("{not json}") == ([], "")
    deep = "{" + '"a":[' * 5000 + "]" * 5000 + "}"
    assert bridge.parse_revise(deep) == ([], "")  # RecursionError is junk, not a crash


def test_attach_agent_dir_without_workspace(monkeypatch):
    from avrone.trinity import TrinityCore
    monkeypatch.setattr(agentfs, "agent_fs_enabled", lambda: True)
    fake = SimpleNamespace(runner=SimpleNamespace(), settings=SimpleNamespace())
    ctx = SimpleNamespace(agent_dir=None)
    TrinityCore._attach_agent_dir(fake, ctx, "A-1")
    assert ctx.agent_dir is None


def test_checkpoint_tmp_already_gone(tmp_path, monkeypatch):
    st = checkpoint._Store(tmp_path / "c.json")

    def replace(src, dst):
        import os
        os.unlink(src)
        raise OSError("ro")
    monkeypatch.setattr(checkpoint.os, "replace", replace)
    assert st.save() is False and st.write_errors == 1
