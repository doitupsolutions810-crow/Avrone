"""Round 20 coverage: a deep run that distills lessons reports how many it learned."""
import asyncio

from tests.test_bridge import _core


def test_deep_reports_lessons_learned(tmp_path, monkeypatch):
    class L:
        async def chat(self, messages, **kw):
            if "DEEP LEAD" in messages[0]["content"]:
                return "## Verdict\nConfirmed. Confidence: High\n## Corrections\nNone\n## Bottom line\nok"
            return '{"final": "fine"}'
    core = _core(tmp_path, L())
    monkeypatch.setattr(core, "_distill", lambda run_id, task: 2)
    out = asyncio.run(core.deep("q?", first_answer="a", n=2, run_id="dl"))
    assert out["lessons_learned"] == 2
    monkeypatch.setattr(core, "_distill", lambda run_id, task: 0)
    assert "lessons_learned" not in asyncio.run(core.deep("q?", first_answer="a", n=2, run_id="dl2"))
