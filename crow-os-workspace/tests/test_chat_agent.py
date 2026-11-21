"""Local agent tools stay in the worktree, and a patch cannot verify itself."""

from pathlib import Path

import pytest

from crow_os.chat_agent import resolve_inside


def test_paths_cannot_escape(tmp_path: Path):
    root = tmp_path / "wt"
    root.mkdir()
    (root / "ok.txt").write_text("hi", encoding="utf-8")
    assert resolve_inside(root, "ok.txt").name == "ok.txt"
    with pytest.raises(ValueError):
        resolve_inside(root, "../secret")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        resolve_inside(root, "link/file")


def test_agent_patch_stays_unmerged_and_unverified(workspace, monkeypatch):
    import crow_os.chat_agent as chat_agent

    monkeypatch.setattr(
        chat_agent,
        "probe_ollama",
        lambda *args, **kwargs: {
            "state": "reachable",
            "host": "http://ollama.test",
            "models": ["fake-model"],
            "detail": "1 model",
            "error": "",
        },
    )
    script = []
    script.append([
        {"type": "tool", "call": {"name": "write_file", "arguments": {
            "path": "tests/test_agent_edge.py",
            "content": "def test_agent_edge():\n    assert True\n",
        }}},
        {"type": "done"},
    ])
    script.append([
        {"type": "tool", "call": {"name": "propose_patch", "arguments": {"message": "add agent edge"}}},
        {"type": "done"},
    ])
    script.append([
        {"type": "tool", "call": {"name": "run_tests", "arguments": {"paths": ["tests/test_agent_edge.py"]}}},
        {"type": "done"},
    ])
    script.append([
        {"type": "token", "text": "The patch is unmerged and unverified."},
        {"type": "done"},
    ])
    cursor = {"n": 0}

    def fake_chat(host, model, messages, tools=None):
        events = script[min(cursor["n"], len(script) - 1)]
        cursor["n"] += 1
        yield from events

    monkeypatch.setattr(chat_agent, "iter_chat", fake_chat)
    agent = workspace.chat.create_agent(name="Local", backend="ollama", model="fake-model")
    thread = workspace.chat.create_thread(agent["id"], "edge patch")
    detail = workspace.chat.post_message(
        thread["id"],
        "Add tests/test_agent_edge.py, commit it, and run it.",
        background=False,
    )
    assert any("unmerged and unverified" in message["content"] for message in detail["messages"])
    assert detail["change_id"]
    evaluation = workspace.evaluation(detail["change_id"])
    assert "unmerged" in evaluation["chips"]
    assert "unverified" in evaluation["chips"]
    assert "verified" not in evaluation["chips"]
    assert evaluation["can_verify"] is False
    with pytest.raises(Exception):
        workspace.verify(detail["change_id"])
    kinds = [item["kind"] for item in detail["activities"]]
    assert "file" in kinds
    assert "patch" in kinds
    assert "test" in kinds
    workspace.attest_gate(detail["change_id"], "operator_accepts_evidence", True)
    verified = workspace.verify(detail["change_id"])
    assert verified["can_verify"] is True
    assert "verified" in verified["chips"]
    assert "unmerged" in verified["chips"]


def test_unreachable_ollama_does_not_invent_a_reply(workspace, monkeypatch):
    import crow_os.chat_agent as chat_agent

    def explode(*args, **kwargs):
        raise AssertionError("model was called")

    monkeypatch.setattr(chat_agent, "iter_chat", explode)
    monkeypatch.setattr(
        chat_agent,
        "probe_ollama",
        lambda *args, **kwargs: {
            "state": "unreachable",
            "host": "http://127.0.0.1:9",
            "models": [],
            "detail": "Ollama unreachable",
            "error": "ConnectError",
        },
    )
    monkeypatch.setattr(chat_agent, "ollama_binary", lambda: {"found": "no", "path": "", "version": ""})
    agent = workspace.chat.create_agent(name="Local", backend="ollama")
    thread = workspace.chat.create_thread(agent["id"], "offline")
    detail = workspace.chat.post_message(thread["id"], "run the tests", background=False)
    text = "\n".join(message["content"] for message in detail["messages"] if message["role"] == "assistant")
    assert "Ollama unreachable" in text
    assert "not on PATH" in text
    assert detail.get("change_id") in {"", None}
    assert detail.get("change") is None
