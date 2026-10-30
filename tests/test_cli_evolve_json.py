"""CLI contract tests for accepted evolve-proposal JSON output."""
import asyncio
import json
from types import SimpleNamespace

from avrone import cli, evolve


def test_evolve_json_option_writes_the_report_file(tmp_path, monkeypatch, capsys):
    report = tmp_path / "accepted.json"
    rows = [
        {"pid": "fresh-1", "branch": "evolve/fresh-1",
         "files": ["tests/test_one.py"], "ts": 123.5},
    ]

    async def fake_build_core(settings, extras=True):
        return SimpleNamespace(settings=settings, codex=SimpleNamespace()), []

    async def fake_cleanups(_cleanups):
        return None

    monkeypatch.setattr(cli, "build_core", fake_build_core)
    monkeypatch.setattr(cli, "run_cleanups", fake_cleanups)
    monkeypatch.setattr(evolve, "list_accepted", lambda _settings: rows)

    args = cli.build_parser().parse_args(["evolve", "--json", str(report)])
    result = asyncio.run(cli._run(args))

    assert result == 0
    assert json.loads(report.read_text(encoding="utf-8")) == rows
    assert json.loads(capsys.readouterr().out) == rows
