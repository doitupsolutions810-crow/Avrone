"""Offline tests for avrone-cli: parser, event formatting, and every subcommand against a fake core."""
import asyncio
import json

import pytest

from avrone import cli
from avrone.codex import Event


def ev(kind, **data):
    return Event(run_id="r1", kind=kind, agent="Lead", ts=0.0, data=data) if "ts" in Event.__dataclass_fields__ \
        else Event(run_id="r1", kind=kind, agent="Lead", data=data)


@pytest.mark.parametrize("kind,data,needle", [
    ("run.start", {"mode": "squad", "squad_size": 3}, "mode=squad size=3"),
    ("run.start", {"mode": "chat"}, "size=-"),
    ("agent.spawn", {"role": "Engineer", "parent": "Lead"}, "Engineer (under Lead)"),
    ("agent.spawn", {"role": "Engineer"}, "Engineer"),
    ("tool.call", {"tool": "shell", "args": {"cmd": "ls"}}, 'shell {"cmd": "ls"}'),
    ("tool.result", {"output": "a\nb"}, "a b"),
    ("agent.done", {"result": "x" * 300}, "x" * 100),
    ("lead.planned", {"subtasks": [{"role": "A"}, {"role": "B"}]}, "A, B"),
    ("error", {"message": "bad"}, "bad"),
    ("run.done", {}, "done"),
    ("custom.kind", {"k": 1}, '{"k": 1}'),
])
def test_event_line(kind, data, needle):
    line = cli._event_line(ev(kind, **data))
    assert line.startswith("[r1] ") and needle in line
    if kind == "agent.done":
        assert "x" * 101 not in line


def test_parser_requires_subcommand_and_validates():
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args([])
    with pytest.raises(SystemExit):
        p.parse_args(["deep", "q", "--first-answer", "a", "--first-answer-file", "f"])
    with pytest.raises(SystemExit):
        p.parse_args(["map", "--format", "svg"])
    a = p.parse_args(["squad", "go", "-n", "4", "--max-steps", "7", "-q", "--no-extras"])
    assert (a.cmd, a.prompt, a.n, a.max_steps, a.quiet, a.no_extras) == ("squad", "go", 4, 7, True, True)


# ------------------------------------------------------------ fake core wiring
class FakeCodex:
    def __init__(self):
        self.subs = []

    def subscribe(self):
        q = asyncio.Queue()
        self.subs.append(q)
        q.put_nowait(Event(run_id="r1", kind="run.start", agent="Lead", data={"mode": "chat"})
                     if "ts" not in Event.__dataclass_fields__ else
                     Event(run_id="r1", kind="run.start", agent="Lead", ts=0.0, data={"mode": "chat"}))
        return q

    def unsubscribe(self, q):
        self.subs.remove(q)

    def runs(self, limit):
        return [{"run_id": f"r{i}", "prompt": "p" * 200} for i in range(limit)]


class FakeTools:
    def list(self):
        return [{"name": "shell", "source": "builtin", "args": "{cmd}", "description": "run a command"}]


class FakeCore:
    def __init__(self, settings):
        self.settings = settings
        self.codex = FakeCodex()
        self.tools = FakeTools()
        self.seen = {}

    async def squad(self, prompt, n):
        self.seen["squad"] = (prompt, n)
        return {"answer": "SQUAD ANSWER", "run_id": "r1"}

    async def chat(self, prompt):
        return {"answer": "CHAT ANSWER"}

    async def deep(self, prompt, first, n):
        self.seen["deep"] = (prompt, first, n)
        return {"report": "DEEP REPORT"}


@pytest.fixture
def fake(monkeypatch):
    state = {"cleaned": 0, "core": None, "extras": None}

    async def build_core(settings, extras=True):
        state["core"] = FakeCore(settings)
        state["extras"] = extras
        return state["core"], ["c"]

    async def run_cleanups(c):
        state["cleaned"] += 1
    monkeypatch.setattr(cli, "build_core", build_core)
    monkeypatch.setattr(cli, "run_cleanups", run_cleanups)
    return state


def test_tools_and_json(fake, tmp_path, capsys):
    out = tmp_path / "t.json"
    assert cli.main(["tools", "--json", str(out), "--no-extras"]) == 0
    assert "shell" in capsys.readouterr().out
    assert json.loads(out.read_text())[0]["name"] == "shell"
    assert fake["extras"] is False and fake["cleaned"] == 1


def test_runs_limit(fake, capsys):
    assert cli.main(["runs", "--limit", "3"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3 and len(lines[0]) <= 4 + 90


def test_squad_quiet_and_max_steps(fake, capsys):
    assert cli.main(["squad", "build", "-n", "2", "-q", "--max-steps", "5"]) == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == "SQUAD ANSWER" and cap.err == ""
    assert fake["core"].seen["squad"] == ("build", 2)
    assert fake["core"].settings.max_steps == 5
    assert fake["core"].codex.subs == []


def test_chat_streams_events_to_stderr(fake, capsys):
    assert cli.main(["chat", "hello"]) == 0
    cap = capsys.readouterr()
    assert "CHAT ANSWER" in cap.out
    assert "run.start" in cap.err
    assert fake["core"].codex.subs == []  # unsubscribed in finally


def test_deep_reads_first_answer_file(fake, tmp_path, capsys):
    f = tmp_path / "a.txt"
    f.write_text("my draft")
    assert cli.main(["deep", "is it?", "--first-answer-file", str(f), "-n", "3", "-q"]) == 0
    assert "DEEP REPORT" in capsys.readouterr().out
    assert fake["core"].seen["deep"] == ("is it?", "my draft", 3)


def test_missing_file_is_clean_error(fake, capsys):
    assert cli.main(["deep", "q", "--first-answer-file", "/nonexistent/x", "-q"]) == 1
    assert "avrone-cli: error:" in capsys.readouterr().err
    assert fake["cleaned"] == 1  # cleanup still ran


def test_keyboard_interrupt_exit_code(monkeypatch):
    async def interrupted(args):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "_run", interrupted)
    assert cli.main(["tools"]) == 130


def test_telemetry_and_map(fake, monkeypatch, capsys):
    import avrone.telemetry as tel
    monkeypatch.setattr(tel, "telemetry", lambda core, secs: {"runs": 0, "window": secs})
    monkeypatch.setattr(tel, "system_map", lambda core, secs: {"nodes": [], "edges": []})
    monkeypatch.setattr(tel, "to_mermaid", lambda data: "flowchart LR")
    assert cli.main(["telemetry", "--hours", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["window"] == 7200
    assert cli.main(["map"]) == 0
    assert capsys.readouterr().out.strip() == "flowchart LR"
    assert cli.main(["map", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"nodes": [], "edges": []}


def test_evolve_listing(fake, monkeypatch, capsys):
    import avrone.evolve as evo
    monkeypatch.setattr(evo, "list_accepted", lambda s: [])
    assert cli.main(["evolve"]) == 0
    assert "no accepted proposals" in capsys.readouterr().out
    monkeypatch.setattr(evo, "list_accepted",
                        lambda s: [{"pid": "p1", "branch": "evolve/p1", "goal": "g", "files": ["a.py", "b.py"]}])
    assert cli.main(["evolve"]) == 0
    assert "p1 evolve/p1  g  (a.py, b.py)" in capsys.readouterr().out
