"""v0.34: codex_search streams logs line-bounded. Before: `for line in fh` loaded a newline-less
log whole (60 MB log -> 120 MB peak), and a deeply nested line raised RecursionError out of the
scan instead of being skipped."""
import asyncio
import json
from types import SimpleNamespace as S

from avrone import codex as codex_mod
from avrone import tools_extra as te


def mk(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(te, "_admin_codex", lambda c: c.core.codex)
    return runs, S(core=S(settings=S(data_dir=tmp_path), codex=S(log_dir=runs)), name="Admin", run_id="r")


def test_overlong_and_nested_lines_skipped(tmp_path, monkeypatch):
    runs, ctx = mk(tmp_path, monkeypatch)
    monkeypatch.setattr(codex_mod.iter_lines, "__defaults__", (256,))
    good = json.dumps({"run_id": "a", "kind": "tool", "agent": "x", "data": {"m": "needle"}, "ts": 2}) + "\n"
    (runs / "a.jsonl").write_text('{"x": "' + "y" * 5000 + '"}\n' + "[" * 200 + "\n" + good)
    (runs / "b.jsonl").write_text('{"x": "' + "z" * 5000)  # newline-less
    out = asyncio.run(te.t_codex_search(ctx, {"query": "needle"}))
    assert "needle" in out
    out = asyncio.run(te.t_codex_search(ctx, {"query": "zzzz"}))
    assert out.startswith("No CodeX events match")


def test_deep_nesting_does_not_escape(tmp_path, monkeypatch):
    runs, ctx = mk(tmp_path, monkeypatch)
    (runs / "a.jsonl").write_text("[" * 100_000 + "]" * 100_000 + "\n")
    out = asyncio.run(te.t_codex_search(ctx, {"query": "q"}))
    assert out.startswith("No CodeX events match")
