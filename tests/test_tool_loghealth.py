"""log_health: per-log integrity findings, read budget, never event content."""
import asyncio
import json
import os
from types import SimpleNamespace as S

from avrone import codex as codex_mod
from avrone import tools_limits as tl
from avrone.tools import ToolRegistry
from avrone.tools_limits import register_limits_tools


def ctx(tmp_path):
    return S(core=S(settings=S(data_dir=tmp_path)))


def ev(i, secret="s3cr3t-content"):
    return json.dumps({"kind": "k", "data": {"x": secret}, "ts": i}) + "\n"


def run(c):
    return asyncio.run(c)


def test_no_logs(tmp_path):
    assert run(tl.t_log_health(ctx(tmp_path), {})) == "log_health: no run logs on this node"
    assert run(tl.t_log_health(S(core=None), {})) == "log_health: no run logs on this node"


def test_findings(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setenv("AVRONE_REPLAY_MAX_EVENTS", "3")
    monkeypatch.setattr(codex_mod, "MAX_LINE_BYTES", 64)
    (runs / "clean.jsonl").write_text(ev(1))
    (runs / "big.jsonl").write_text("".join(ev(i) for i in range(5)))
    (runs / "bad.jsonl").write_text(ev(1) + "{not json\n" + "[" * 3000 + "\n" + '{"x":"' + "y" * 500 + '"}\n'
                                    + "\n" + ev(2) + '{"torn": ')
    out = run(tl.t_log_health(ctx(tmp_path), {"runs": "junk"}))
    assert out.startswith("log_health: 3 of 3 run log(s) checked, 2 with findings")
    assert "- big: 5 events, views show the newest 3" in out
    assert "- bad: 1 corrupt line(s); 2 line(s) over 4 MB, skipped by every reader; torn last line" in out
    assert "clean" not in out and "s3cr3t" not in out
    (runs / "big.jsonl").unlink()
    (runs / "bad.jsonl").unlink()
    assert run(tl.t_log_health(ctx(tmp_path), {"runs": 0})).endswith("all checked logs intact")


def test_budget_and_unreadable(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    for i, name in enumerate(("a", "b", "c")):
        (runs / f"{name}.jsonl").write_text("".join(ev(j) for j in range(20)))
        os.utime(runs / f"{name}.jsonl", (100 + i, 100 + i))
    monkeypatch.setattr(tl, "LOG_HEALTH_MAX_BYTES", 300)
    out = run(tl.t_log_health(ctx(tmp_path), {}))
    assert "(stopped at the 256 MB read budget)" in out and "1 of 3" in out
    monkeypatch.setattr(tl, "LOG_HEALTH_MAX_BYTES", 10 ** 9)
    real = tl._scan_log
    monkeypatch.setattr(tl, "_scan_log", lambda p, b: (_ for _ in ()).throw(OSError()) if p.stem == "c" else real(p, b))
    assert "- c: unreadable" in run(tl.t_log_health(ctx(tmp_path), {}))


def test_stat_race_and_registered(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "a.jsonl").write_text(ev(1))
    (runs / "gone.jsonl").symlink_to(tmp_path / "missing")
    out = run(tl.t_log_health(ctx(tmp_path), {}))
    assert out.startswith("log_health: 1 of 1")
    reg = ToolRegistry()
    register_limits_tools(reg)
    assert reg.get("log_health") is not None
