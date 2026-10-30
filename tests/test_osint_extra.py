import asyncio, json
from pathlib import Path
from types import SimpleNamespace
import pytest
from avrone import osint

def ctx(tmp=None):
    settings = SimpleNamespace(data_dir=str(tmp) if tmp else "/tmp/none")
    core = SimpleNamespace(settings=settings, codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r", name="OSINT-1", depth=0)

def run(c): return asyncio.run(c)

def test_metrics_empty_logs(tmp_path):
    assert "no recon/OSINT activity" in run(osint.t_osint_metrics(ctx(tmp_path), {}))

def test_metrics_aggregation_edge_cases(tmp_path):
    runs = Path(tmp_path) / "runs"; runs.mkdir()
    # empty logs
    (runs / "empty.jsonl").write_text("")
    # unknown tool + top-host tie
    evs = [
        {"kind": "recon.dns", "data": {"host": "tie.com"}},
        {"kind": "recon.tls", "data": {"host": "tie.com"}},
        {"kind": "unknown.tool", "data": {"host": "ignore.com"}},
        {"kind": "osint.domain", "data": {"host": "other.com"}},
    ]
    (runs / "r1.jsonl").write_text("\n".join(json.dumps(e) for e in evs))
    out = run(osint.t_osint_metrics(ctx(tmp_path), {}))
    assert "recon.dns: 1" in out and "recon.tls: 1" in out and "osint.domain: 1" in out
    assert "unknown.tool" not in out
    assert "tie.com: 2" in out and "other.com: 1" in out
    assert "total probes: 3" in out
