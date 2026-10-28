"""Tool-call robustness: arg schemas, unknown-tool suggestions, multi-call batches."""
import asyncio
import json

import pytest

from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import CORE_SCHEMAS, Tool, ToolRegistry, builtin_registry, validate_args


def run(c):
    return asyncio.run(c)


# ------------------------------------------------------------------ registry level
def test_validate_args_required_and_types():
    schema = {"required": {"path": "str", "n": "int"}, "optional": {"x": "float", "b": "str|dict"}}
    assert validate_args(schema, {"path": "a", "n": 2}) == []
    assert validate_args(schema, {"path": "a", "n": 2, "x": 1, "b": {"k": 1}, "extra": 1}) == []
    assert validate_args(schema, {"n": 2}) == ["missing 'path'"]
    assert validate_args(schema, {"path": None, "n": 2}) == ["missing 'path'"]
    probs = validate_args(schema, {"path": 3, "n": True, "x": "no", "b": [1]})
    assert "'path' must be str, got int" in probs
    assert "'n' must be int, got bool" in probs  # bools are not ints in a schema
    assert any(p.startswith("'x' must be float") for p in probs)
    assert any(p.startswith("'b' must be str|dict") for p in probs)
    assert validate_args(schema, ["not", "a", "dict"]) == ["args must be a JSON object, got list"]
    assert validate_args(None, "anything") == []
    assert validate_args({"required": {"q": "weird"}}, {"q": 1}) == []  # unknown type word never blocks


def test_core_tools_get_schemas():
    from avrone.tools_extra import register_extra_tools
    r = builtin_registry()
    register_extra_tools(r)
    for name in ("shell", "python", "read_file", "write_file", "edit_file", "fetch", "web_search", "spawn"):
        assert r.get(name).schema == CORE_SCHEMAS[name], name
    # plugin/MCP tools with a core name are not given a builtin schema
    r2 = ToolRegistry()
    r2.register(Tool("shell", "d", "{}", None, source="plugin:x"))
    assert r2.get("shell").schema is None
    # an explicit schema wins over the core one
    r2.register(Tool("fetch", "d", "{}", None, schema={"required": {"u": "str"}}))
    assert r2.get("fetch").schema == {"required": {"u": "str"}}


def test_check_messages():
    r = builtin_registry()
    reason, msg = r.check("read_file", {})
    assert reason == "bad_args"
    assert msg.startswith("ARG ERROR (read_file): missing 'path'; expected {\"path\": str}")
    reason, msg = r.check("shel", {"cmd": "ls"})
    assert reason == "unknown_tool" and "Did you mean: shell?" in msg
    assert "Did you mean: read_file" in r.check("readfile", {})[1]
    assert "Did you mean" not in r.check("zzzzzz", {})[1]
    assert r.check("shell", {"cmd": "ls"}) is None


def test_unknown_tool_call_suggests():
    r = builtin_registry()
    out = run(r.call(None, "Fetch", {}))
    assert out.startswith("ERROR: unknown tool 'Fetch'. Did you mean: fetch?")
    assert "Known:" in out


# ------------------------------------------------------------------ agent loop level
class ScriptLLM:
    """Replies with the scripted strings in order, then a final; records every prompt."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    async def chat(self, messages, **kw):
        self.seen.append([dict(m) for m in messages])
        if self.replies:
            return self.replies.pop(0)
        return '{"final": "done"}'


def _core(tmp_path, llm, tools=None, steps=6):
    from avrone.trinity import TrinityCore
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.max_steps = steps
    return TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir), tools=tools)


def _reg(log=None, delay=0.0):
    """Registry of fake tools: echo (schema'd), slow, boom (raises past the registry)."""
    r = ToolRegistry()

    async def echo(ctx, args):
        if log is not None:
            log.append(("echo", args.get("text")))
        return f"echo:{args['text']}"

    async def slow(ctx, args):
        await asyncio.sleep(delay)
        if log is not None:
            log.append(("slow", args.get("id")))
        return f"slow:{args.get('id')}"

    async def boom(ctx, args):
        raise RuntimeError("kaboom")

    r.register(Tool("echo", "echo text", '{"text": str}', echo, schema={"required": {"text": "str"}}))
    r.register(Tool("slow", "sleep then return", '{"id": int}', slow))
    r.register(Tool("boom", "always crashes", "{}", boom))
    return r


def _batch(*calls):
    return "```json\n" + json.dumps({"tools": [{"tool": t, "args": a} for t, a in calls]}) + "\n```"


def _last_user(llm, i):
    return llm.seen[i][-1]["content"]


def _kinds(core, run_id):
    return [e["kind"] for e in core.codex.replay(run_id)]


def test_parse_action_accepts_batch():
    from avrone.trinity import parse_action
    a = parse_action(_batch(("echo", {"text": "a"})))
    assert a == {"tools": [{"tool": "echo", "args": {"text": "a"}}]}


def test_batch_runs_concurrently_results_in_order(tmp_path):
    log = []
    llm = ScriptLLM([_batch(("slow", {"id": 1}), ("slow", {"id": 2}), ("echo", {"text": "hi"}))])
    core = _core(tmp_path, llm, tools=_reg(log, delay=0.3))
    import time
    t0 = time.monotonic()
    res = run(core.run_agent("b1", "A", "task"))
    assert time.monotonic() - t0 < 0.55  # two 0.3s calls ran at the same time
    assert res == "done"
    fed = _last_user(llm, 1)
    i1, i2, i3 = (fed.index("TOOL RESULT [1/3] (slow):\nslow:1"), fed.index("TOOL RESULT [2/3] (slow):\nslow:2"),
                  fed.index("TOOL RESULT [3/3] (echo):\necho:hi"))
    assert i1 < i2 < i3
    evs = core.codex.replay("b1")
    batch = [e for e in evs if e["kind"] == "tool.batch"]
    assert len(batch) == 1 and batch[0]["data"]["count"] == 3 and batch[0]["data"]["dropped"] == 0
    assert _kinds(core, "b1").count("tool.call") == 3 and _kinds(core, "b1").count("tool.result") == 3


def test_batch_cap_default_and_env(tmp_path, monkeypatch):
    from avrone.trinity import max_calls_per_turn
    monkeypatch.delenv("AVRONE_MAX_CALLS_PER_TURN", raising=False)
    assert max_calls_per_turn() == 4
    for raw, want in (("2", 2), ("99", 8), ("0", 1), ("-3", 1), ("junk", 4)):
        monkeypatch.setenv("AVRONE_MAX_CALLS_PER_TURN", raw)
        assert max_calls_per_turn() == want, raw
    monkeypatch.setenv("AVRONE_MAX_CALLS_PER_TURN", "2")
    log = []
    llm = ScriptLLM([_batch(*[("echo", {"text": str(i)}) for i in range(5)])])
    core = _core(tmp_path, llm, tools=_reg(log))
    run(core.run_agent("cap", "A", "task"))
    assert [x[1] for x in log] == ["0", "1"]
    fed = _last_user(llm, 1)
    assert "TOOL RESULT [2/2]" in fed and "3 more call(s) were skipped (limit 2 per turn)" in fed
    batch = [e for e in core.codex.replay("cap") if e["kind"] == "tool.batch"][0]
    assert batch["data"] == {"count": 2, "dropped": 3, "step": 0}
    # the prompt tells the model the cap
    assert "(up to 2)" in llm.seen[0][0]["content"] and '"tools": [' in llm.seen[0][0]["content"]


def test_crash_in_one_call_is_isolated(tmp_path, monkeypatch):
    llm = ScriptLLM([_batch(("echo", {"text": "a"}), ("boom", {}), ("echo", {"text": "b"}))])
    reg = _reg()
    core = _core(tmp_path, llm, tools=reg)

    # also crash *past* the registry's own guard (e.g. a wrapper that raises)
    orig = reg.call

    async def flaky(ctx, name, args):
        if args.get("text") == "b":
            raise ValueError("wrapper died")
        return await orig(ctx, name, args)

    monkeypatch.setattr(reg, "call", flaky)
    res = run(core.run_agent("crash", "A", "task"))
    assert res == "done"
    fed = _last_user(llm, 1)
    assert "TOOL RESULT [1/3] (echo):\necho:a" in fed
    assert "TOOL RESULT [2/3] (boom):\nERROR: RuntimeError: kaboom" in fed
    assert "TOOL RESULT [3/3] (echo):\nERROR: ValueError: wrapper died" in fed


def test_single_call_and_final_unchanged(tmp_path):
    llm = ScriptLLM(['```json\n{"tool": "echo", "args": {"text": "x"}}\n```', '{"final": "ok"}'])
    core = _core(tmp_path, llm, tools=_reg())
    assert run(core.run_agent("one", "A", "task")) == "ok"
    assert _last_user(llm, 1) == "TOOL RESULT (echo):\necho:x"
    assert "tool.batch" not in _kinds(core, "one")


def test_arg_error_does_not_run_tool_and_model_repairs(tmp_path):
    log = []
    llm = ScriptLLM(['{"tool": "echo", "args": {"txt": "x"}}', '{"tool": "echo", "args": {"text": "x"}}'])
    core = _core(tmp_path, llm, tools=_reg(log))
    run(core.run_agent("ae", "A", "task"))
    assert log == [("echo", "x")]  # only the repaired call ran
    assert _last_user(llm, 1).startswith("TOOL RESULT (echo):\nARG ERROR (echo): missing 'text'; expected {\"text\": str}")
    errs = [e for e in core.codex.replay("ae") if e["kind"] == "tool.arg_error"]
    assert len(errs) == 1 and errs[0]["data"]["reason"] == "bad_args" and errs[0]["data"]["tool"] == "echo"


def test_arg_error_inside_batch_others_still_run(tmp_path):
    log = []
    llm = ScriptLLM([_batch(("echo", {"text": 5}), ("echo", {"text": "ok"}))])
    core = _core(tmp_path, llm, tools=_reg(log))
    run(core.run_agent("bae", "A", "task"))
    assert log == [("echo", "ok")]
    fed = _last_user(llm, 1)
    assert "[1/2] (echo):\nARG ERROR (echo): 'text' must be str, got int" in fed
    assert "[2/2] (echo):\necho:ok" in fed


def test_unknown_tool_suggestion_in_loop(tmp_path):
    llm = ScriptLLM(['{"tool": "ech", "args": {"text": "x"}}'])
    core = _core(tmp_path, llm, tools=_reg())
    run(core.run_agent("unk", "A", "task"))
    fed = _last_user(llm, 1)
    assert "unknown tool 'ech'. Did you mean: echo?" in fed
    errs = [e for e in core.codex.replay("unk") if e["kind"] == "tool.arg_error"]
    assert errs and errs[0]["data"]["reason"] == "unknown_tool"


def test_arg_error_budget_then_continue_normally(tmp_path):
    log = []
    bad = '{"tool": "echo", "args": {"text": 1}}'
    llm = ScriptLLM([bad, bad, bad, bad, '{"tool": "echo", "args": {"txt": 2}}'])
    reg = _reg(log)

    async def lenient(ctx, args):  # the tool's own handling once validation steps aside
        log.append(("echo", args.get("text")))
        return f"echo:{args.get('text')}"

    reg.get("echo").fn = lenient
    core = _core(tmp_path, llm, tools=reg, steps=8)
    assert run(core.run_agent("bud", "A", "task")) == "done"
    # first 3 blocked, then the 4th and 5th run despite bad args
    assert log == [("echo", 1), ("echo", None)]
    errs = [e for e in core.codex.replay("bud") if e["kind"] == "tool.arg_error"]
    assert [e["data"]["consecutive"] for e in errs] == [1, 2, 3]
    assert _last_user(llm, 4) == "TOOL RESULT (echo):\necho:1"


def test_arg_error_budget_resets_after_clean_call(tmp_path):
    log = []
    bad = '{"tool": "echo", "args": {}}'
    good = '{"tool": "echo", "args": {"text": "g"}}'
    llm = ScriptLLM([bad, bad, good, bad, bad])
    core = _core(tmp_path, llm, tools=_reg(log), steps=8)
    run(core.run_agent("rst", "A", "task"))
    assert log == [("echo", "g")]  # the later bad calls are still blocked: count reset
    errs = [e for e in core.codex.replay("rst") if e["kind"] == "tool.arg_error"]
    assert [e["data"]["consecutive"] for e in errs] == [1, 2, 1, 2]


def test_bad_batch_shapes(tmp_path):
    llm = ScriptLLM(['{"tools": []}', '{"tools": "shell"}', '{"tools": [42, {"tool": "echo", "args": {"text": "y"}}]}'])
    core = _core(tmp_path, llm, tools=_reg())
    assert run(core.run_agent("shape", "A", "task")) == "done"
    assert "must be a non-empty list" in _last_user(llm, 1)
    assert "must be a non-empty list" in _last_user(llm, 2)
    fed = _last_user(llm, 3)
    assert "[1/2] (?):\nARG ERROR (?)" in fed and "[2/2] (echo):\necho:y" in fed


def test_mock_llm_squad_still_works(tmp_path):
    from avrone.llm import LLM
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    from avrone.trinity import TrinityCore
    core = TrinityCore(s, LLM(s), LocalRunner(s), Codex(s.data_dir))
    out = run(core.chat("hello", run_id="mk"))
    assert out["answer"]
    assert "tool.arg_error" not in _kinds(core, "mk")
