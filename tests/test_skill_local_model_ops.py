"""local-model-ops skill: valid, every tool and env var it names is real, and its route promise holds."""
import re
from pathlib import Path

from avrone import evolve
from avrone.gateway import is_loopback_url
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools
from avrone.tools_routerstatus import route_of

ROOT = Path(__file__).resolve().parent.parent
BODY = (ROOT / "skills" / "local-model-ops" / "SKILL.md").read_text()


def test_valid_and_grounded():
    assert evolve.check_skill("local-model-ops", BODY) is None
    reg = builtin_registry()
    register_extra_tools(reg)
    for tool in ("router_status", "env_report"):
        assert tool in BODY and reg.get(tool) is not None
    src = "".join(p.read_text() for p in (ROOT / "avrone").glob("*.py"))
    for env in set(re.findall(r"AVRONE_[A-Z_]+", BODY)):
        assert env in src, env
    assert (ROOT / "tools" / "model_bench.py").exists()
    assert "--base" in (ROOT / "tools" / "model_bench.py").read_text()


def test_route_promise(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://p:1")
    monkeypatch.setenv("http_proxy", "http://p:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    for url in ("http://127.0.0.1:11434/v1", "http://localhost:11434/v1"):
        assert is_loopback_url(url) and route_of(url) == "direct"
    assert route_of("http://models.example.com/v1") == "env-proxy"
