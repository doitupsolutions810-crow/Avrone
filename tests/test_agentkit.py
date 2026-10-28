import asyncio, os, json
from types import SimpleNamespace
from pathlib import Path
import pytest
from avrone import agentkit
from avrone.sovereign import CommandResult


class Runner:
    def __init__(self, ws): self.workspace, self.kind, self.cmds = ws, "local", []
    async def run(self, cmd, timeout=None):
        self.cmds.append(cmd)
        return CommandResult(cmd, 0, "ok", "")


def ctx(tmp):
    ws = tmp / "ws"; ws.mkdir(exist_ok=True)
    events = []
    core = SimpleNamespace(runner=Runner(ws), settings=SimpleNamespace(data_dir=tmp / "d"),
                           codex=SimpleNamespace(emit=lambda *a, **k: events.append((a, k))))
    return SimpleNamespace(core=core, run_id="r1", name="Agent-1", depth=0), events


def run(c): return asyncio.run(c)


def test_qr_roundtrip(tmp_path):
    zx = pytest.importorskip("zxingcpp")
    from PIL import Image
    c, _ = ctx(tmp_path)
    img = zx.create_barcode('{"hello":"avrone"}', zx.BarcodeFormat.QRCode) if hasattr(zx, "create_barcode") else None
    if img is None:
        pytest.skip("zxing-cpp without writer")
    pil = Image.fromarray(img.to_image(scale=8)) if hasattr(img, "to_image") else None
    if pil is None:
        pytest.skip("writer API differs")
    pil.save(c.core.runner.workspace / "q.png")
    out = run(agentkit.t_image_decode(c, {"path": "q.png"}))
    assert '{"hello":"avrone"}' in out and "QRCode" in out
    info = run(agentkit.t_image_inspect(c, {"path": "q.png"}))
    assert "entropy=" in info and "modules" in info


def test_skills(tmp_path):
    c, _ = ctx(tmp_path)
    assert "barcode-forensics" in run(agentkit.t_skill_list(c, {}))
    assert "image_decode" in run(agentkit.t_skill_load(c, {"name": "barcode-forensics"}))
    assert run(agentkit.t_skill_load(c, {"name": "nope"})).startswith("ERROR")


def test_env_install_policy_and_ledger(tmp_path, monkeypatch):
    c, events = ctx(tmp_path)
    monkeypatch.setenv("AVRONE_SELF_CHANGES", "allow")
    assert "installed" in run(agentkit.t_env_install(c, {"package": "requests"}))
    assert run(agentkit.t_env_install(c, {"package": "x; rm -rf /"})).startswith("ERROR")
    monkeypatch.setenv("AVRONE_SELF_CHANGES", "deny")
    assert "disabled" in run(agentkit.t_env_install(c, {"package": "requests"}))
    led = run(agentkit.t_resources(c, {}))
    assert "pip install requests ok" in led and "denied" in led
    assert any(a[1] == "env.change" for a, _ in events)


def test_task_board(tmp_path):
    c, _ = ctx(tmp_path)
    run(agentkit.t_task_board(c, {"op": "add", "title": "decode"}))
    out = run(agentkit.t_task_board(c, {"op": "update", "id": 1, "status": "done", "note": "QR ok"}))
    assert "#1 [done] decode" in out and "QR ok" in out


def test_install_dedupe_and_weak_lead(tmp_path, monkeypatch):
    c, _ = ctx(tmp_path)
    monkeypatch.setenv("AVRONE_SELF_CHANGES", "allow")
    run(agentkit.t_env_install(c, {"package": "requests"}))
    n = len(c.core.runner.cmds)
    assert "already installed" in run(agentkit.t_env_install(c, {"package": "requests"}))
    assert len(c.core.runner.cmds) == n
    from avrone.trinity import _weak_lead
    assert _weak_lead("I'll inspect the image and decode it directly.", "x" * 5000)
    assert not _weak_lead("The QR decodes to " + "data " * 100, "x" * 5000)
