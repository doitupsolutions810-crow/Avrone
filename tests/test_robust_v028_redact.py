"""v0.28: secrets never reach the run log or the live event stream. Before: an agent that ran `env`
or echoed a key had it written verbatim to data/runs/<id>.jsonl and pushed to /ws/events."""
import asyncio
import json

from avrone.codex import Codex, _scrub


def test_env_secret_and_patterns_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-SuperSecretValue1234567890")
    monkeypatch.setenv("AVRONE_CODEX_REDACT", "on")
    cx = Codex(tmp_path)
    q = cx.subscribe()
    cx.emit("r1", "tool.result", "A-1", tool="shell",
            output="XAI_API_KEY=xai-SuperSecretValue1234567890\nAuthorization: Bearer abcdefghijklmnop\n"
                   "git clone https://bob:hunter2pass@github.com/x/y ghp_" + "a" * 36,
            nested={"lst": ["token=zzzzzzzzzz", 5, None]})
    raw = (tmp_path / "runs" / "r1.jsonl").read_text()
    for leak in ("SuperSecretValue", "abcdefghijklmnop", "hunter2pass", "a" * 36, "zzzzzzzzzz"):
        assert leak not in raw, leak
    ev = asyncio.run(q.get())
    assert "SuperSecretValue" not in json.dumps(ev.data) and ev.data["nested"]["lst"][1] == 5
    assert "[REDACTED]" in ev.data["output"]


def test_redaction_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CODEX_REDACT", "off")
    cx = Codex(tmp_path)
    cx.emit("r2", "tool.result", "A", output="password=letmein123")
    assert "letmein123" in (tmp_path / "runs" / "r2.jsonl").read_text()


def test_scrub_shapes():
    assert _scrub({"a": ("x", 1)}) == {"a": ["x", 1]}
    assert _scrub("short") == "short" and _scrub(3.5) == 3.5
    deep = cur = {}
    for _ in range(12):
        cur["n"] = {}
        cur = cur["n"]
    cur["s"] = "secret=abcdefgh123"
    out = json.dumps(_scrub(deep))
    assert "abcdefgh123" not in out
