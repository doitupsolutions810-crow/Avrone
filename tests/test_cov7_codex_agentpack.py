"""Round-7 coverage: Codex bus/log edge branches and agentpack_tools refusal/success paths."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from avrone import agentfs, agentpack
from avrone import agentpack_tools as apt
from avrone.codex import Codex
from avrone.config import Settings


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ Codex
def test_emit_drops_for_full_subscriber_but_feeds_others(tmp_path):
    cx = Codex(tmp_path)
    full = asyncio.Queue(maxsize=1)
    full.put_nowait("stale")
    cx._subscribers.add(full)
    ok = cx.subscribe()
    ev = cx.emit("r1", "agent.think", "A", text="hi")
    assert full.qsize() == 1 and full.get_nowait() == "stale"   # full queue: event dropped, no raise
    assert ok.get_nowait() is ev
    assert cx.replay("r1")[0]["data"] == {"text": "hi"}          # log still written
    cx.unsubscribe(ok)
    cx.unsubscribe(ok)                                            # idempotent
    cx.emit("r1", "x", "A")
    assert ok.empty()


def test_emit_serialises_non_json_values(tmp_path):
    cx = Codex(tmp_path)
    cx.emit("r1", "tool.result", "A", path=tmp_path, obj=object())
    rec = cx.replay("r1")[0]
    assert rec["data"]["path"] == str(tmp_path) and isinstance(rec["data"]["obj"], str)


def test_runs_skips_unreadable_and_bad_first_lines(tmp_path):
    cx = Codex(tmp_path)
    cx.emit("good", "run.start", "sys", prompt="do it")
    cx.emit("noprompt", "run.start", "sys")
    (cx.log_dir / "blank.jsonl").write_text("\n" + json.dumps({"ts": 1}) + "\n")
    (cx.log_dir / "list.jsonl").write_text("[1, 2]\n")
    (cx.log_dir / "datastr.jsonl").write_text(json.dumps({"ts": 2, "data": "nope"}) + "\n")
    (cx.log_dir / "dir.jsonl").mkdir()                             # open() raises IsADirectoryError
    runs = {r["run_id"]: r for r in cx.runs()}
    assert set(runs) == {"good", "noprompt", "datastr"}
    assert runs["good"]["prompt"] == "do it" and runs["noprompt"]["prompt"] == ""
    assert runs["datastr"] == {"run_id": "datastr", "started": 2, "prompt": ""}


def test_runs_limit_newest_first(tmp_path):
    cx = Codex(tmp_path)
    for i, rid in enumerate(["old", "mid", "new"]):
        cx.emit(rid, "run.start", "sys", prompt=rid)
        os.utime(cx.log_dir / f"{rid}.jsonl", (1000 + i, 1000 + i))
    assert [r["run_id"] for r in cx.runs(limit=2)] == ["new", "mid"]


def test_replay_unreadable_and_escaping_logs(tmp_path):
    cx = Codex(tmp_path)
    (cx.log_dir / "isdir.jsonl").mkdir()
    assert cx.replay("isdir") == []                               # read_text OSError
    assert cx.replay("missing") == []
    outside = tmp_path / "evil.jsonl"
    outside.write_text(json.dumps({"kind": "x"}) + "\n")
    os.symlink(outside, cx.log_dir / "link.jsonl")
    assert cx.replay("link") == []                                # resolves outside log_dir
    assert cx.replay("../evil") == []


def test_parse_line_rejects_deeply_nested(tmp_path):
    assert Codex._parse_line("[" * 100000 + "]" * 100000) is None
    assert Codex._parse_line('{"a": 1') is None
    assert Codex._parse_line('  {"a": 1}  ') == {"a": 1}


# ------------------------------------------------------------------ agentpack_tools
class RecCodex:
    def __init__(self):
        self.events = []

    def emit(self, run_id, kind, agent, **data):
        self.events.append((kind, data))


def _ctx(tmp_path, aid="builder", fleet=None, codex="rec"):
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data")
    core = SimpleNamespace(settings=s)
    if codex == "rec":
        core.codex = RecCodex()
    elif codex is not None:
        core.codex = codex
    if fleet is not None:
        core.fleet = fleet
    d = agentfs.agent_dir(s.workspace, aid) if aid else None
    return SimpleNamespace(core=core, run_id="r1", name="Builder-1", depth=0, agent_id=aid, agent_dir=d)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AVRONE_AGENT_MIGRATE", "AVRONE_NODE_ID", "AVRONE_AGENT_FS_QUOTA_MB"):
        monkeypatch.delenv(k, raising=False)


def test_settings_and_node_fallbacks(tmp_path, monkeypatch):
    from pathlib import Path
    bare = SimpleNamespace(core=SimpleNamespace())
    assert apt._settings(bare) == (Path("./workspace"), Path("./data"))
    monkeypatch.setattr(apt.agentcontainer, "local_node_id", lambda: "local-n")
    assert apt._node(bare) == "local-n"
    for nid in (None, "", 7):
        assert apt._node(SimpleNamespace(core=SimpleNamespace(fleet=SimpleNamespace(node_id=nid)))) == "local-n"
    assert apt._node(SimpleNamespace(core=SimpleNamespace(fleet=SimpleNamespace(node_id="edge-1")))) == "edge-1"


def test_export_without_codex_and_with_broken_codex(tmp_path):
    class Boom:
        def emit(self, *a, **kw):
            raise RuntimeError("down")

    for cx in (None, Boom()):
        ctx = _ctx(tmp_path, codex=cx)
        (ctx.agent_dir / "n.txt").write_text("x")
        out = run(apt.t_agent_pack_export(ctx, {}))
        assert out.startswith("packed agent builder:"), out
    no_home = _ctx(tmp_path, aid=None, codex=Boom())
    assert "no agent home" in run(apt.t_agent_pack_export(no_home, {}))


def test_export_writes_pack_atomically_and_audits(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.agent_dir / "notes.txt").write_text("hello")
    out = run(apt.t_agent_pack_export(ctx, {}))
    pdir = agentpack.pack_dir(ctx.core.settings.data_dir)
    packs = [p.name for p in pdir.iterdir()]
    assert len(packs) == 1 and apt.PACK_NAME_RE.match(packs[0]) and packs[0] in out
    assert not any(n.endswith(".tmp") for n in os.listdir(pdir))
    kind, data = ctx.core.codex.events[-1]
    assert kind == "agentpack.export" and data["name"] == packs[0] and len(data["sha256"]) == 64
    assert f"sha256={data['sha256']}" in out


@pytest.mark.parametrize("name", ["", "builder", "builder-1.tar.gz.bak", "Builder-1.tar.gz",
                                  "builder-abc.tar.gz", "coder-1.tar.gz", "builder-1234567890123.tar.gz"])
def test_import_rejects_bad_names(tmp_path, name):
    ctx = _ctx(tmp_path)
    out = run(apt.t_agent_pack_import(ctx, {"name": name}))
    assert out.startswith("ERROR: name must be one of YOUR packs")
    kind, data = ctx.core.codex.events[-1]
    assert kind == "agentpack.refused" and data["tool"] == "agent_pack_import"


def test_import_missing_pack_and_symlinked_pack(tmp_path):
    ctx = _ctx(tmp_path)
    assert run(apt.t_agent_pack_import(ctx, {"name": "builder-1.tar.gz"})) == "ERROR: no such pack: builder-1.tar.gz"
    pdir = agentpack.pack_dir(ctx.core.settings.data_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "elsewhere.tar.gz"
    real.write_bytes(b"x")
    os.symlink(real, pdir / "builder-2.tar.gz")
    assert run(apt.t_agent_pack_import(ctx, {"name": "builder-2.tar.gz"})) == "ERROR: no such pack: builder-2.tar.gz"
    (pdir / "builder-3.tar.gz").mkdir()
    assert "no such pack" in run(apt.t_agent_pack_import(ctx, {"name": " builder-3.tar.gz "}))


def test_import_corrupt_pack_is_refused(tmp_path):
    ctx = _ctx(tmp_path)
    pdir = agentpack.pack_dir(ctx.core.settings.data_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "builder-5.tar.gz").write_bytes(b"not a tarball")
    out = run(apt.t_agent_pack_import(ctx, {"name": "builder-5.tar.gz"}))
    assert out.startswith("ERROR")
    assert ctx.core.codex.events[-1][0] == "agentpack.refused"


class FakeFleet:
    def __init__(self, enabled=True, result=None, exc=None):
        self.enabled = enabled
        self.node_id = "edge-a"
        self.result, self.exc, self.calls = result, exc, []

    async def migrate_agent(self, aid, target):
        self.calls.append((aid, target))
        if self.exc:
            raise self.exc
        return self.result


def test_migrate_needs_enabled_fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    assert "AVRONE_FLEET_TOKEN" in run(apt.t_agent_migrate(_ctx(tmp_path), {}))
    assert "AVRONE_FLEET_TOKEN" in run(apt.t_agent_migrate(_ctx(tmp_path, fleet=FakeFleet(enabled=False)), {}))


def test_migrate_success_and_target_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    f = FakeFleet(result={"agent_id": "builder", "target": "edge-b", "files": 3, "bytes": 42})
    out = run(apt.t_agent_migrate(_ctx(tmp_path, fleet=f), {"target": ""}))
    assert out == "migrated agent builder home to edge-b (3 files, 42 bytes)"
    run(apt.t_agent_migrate(_ctx(tmp_path, fleet=f), {"target": "edge-c"}))
    assert f.calls == [("builder", "auto"), ("builder", "edge-c")]


def test_migrate_failure_and_no_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    f = FakeFleet(exc=RuntimeError("peer unreachable"))
    out = run(apt.t_agent_migrate(_ctx(tmp_path, fleet=f), {}))
    assert out.startswith("ERROR: migration refused or failed") and "peer unreachable" in out
    out = run(apt.t_agent_migrate(_ctx(tmp_path, aid=None, fleet=FakeFleet()), {}))
    assert "home stays" in out and "no agent home" in out


# ------------------------------------------------------------------ osint (rollup robustness, audit failure)
from avrone import osint, recon  # noqa: E402
from tests.test_osint import ctx as osint_ctx  # noqa: E402


def test_osint_metrics_skips_corrupt_and_odd_lines(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    lines = [json.dumps({"kind": "recon.dns", "data": {"host": "a.com"}}),
             "{truncated", "", "[1, 2]", "42", json.dumps("str"),
             json.dumps({"kind": 7}),
             json.dumps({"kind": "recon.tls", "data": "notadict"}),
             json.dumps({"kind": "recon.http", "data": {"host": ["x"]}}),
             json.dumps({"kind": recon.REFUSED_KIND, "data": None}),
             json.dumps({"kind": recon.REFUSED_KIND, "data": {"tool": ["weird"]}}),
             json.dumps({"kind": recon.REFUSED_KIND, "data": {"tool": "dns_enum"}}),
             "[" * 50000]
    (runs / "r1.jsonl").write_text("\n".join(lines))
    (runs / "dir.jsonl").mkdir()                                     # unreadable: skipped, not counted
    out = run(osint.t_osint_metrics(osint_ctx(tmp_path), {}))
    assert out.startswith("recon/OSINT metrics across 1 run logs")
    assert "recon.dns: 1" in out and "recon.tls: 1" in out and "recon.http: 1" in out
    assert "a.com: 1" in out and "total probes: 3" in out
    assert "refused by guardrails: 3" in out and "dns_enum: 1" in out and "?: 1" in out


def test_osint_metrics_refusals_only(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r.jsonl").write_text(json.dumps({"kind": recon.REFUSED_KIND, "data": {"tool": "osint_domain"}}))
    out = run(osint.t_osint_metrics(osint_ctx(tmp_path), {}))
    assert "total probes: 0" in out and "refused by guardrails: 1 (osint_domain: 1)" in out


def test_osint_domain_survives_codex_failure_and_empty_ct(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "*")  # v0.21 recon default-deny

    async def sec(c, a):
        return "ok"

    async def no_ct(c, a):
        return ""

    for name in ("t_dns_enum", "t_tls_inspect", "t_http_fingerprint"):
        monkeypatch.setattr(recon, name, sec)
    monkeypatch.setattr(recon, "t_cert_transparency", no_ct)
    c = osint_ctx()

    def boom(*a, **k):
        raise RuntimeError("codex down")

    c.core.codex = SimpleNamespace(emit=boom)
    out = run(osint.t_osint_domain(c, {"host": "example.com"}))
    assert out.startswith("OSINT profile — example.com") and out.endswith("[Subdomains] ")


def test_osint_domain_ct_without_count_is_clipped(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "*")  # v0.21 recon default-deny

    async def sec(c, a):
        return "ok"

    async def ct(c, a):
        return "ERROR: crt.sh unavailable " + "z" * 500

    for name in ("t_dns_enum", "t_tls_inspect", "t_http_fingerprint"):
        monkeypatch.setattr(recon, name, sec)
    monkeypatch.setattr(recon, "t_cert_transparency", ct)
    out = run(osint.t_osint_domain(osint_ctx(), {"host": "example.com"}))
    tail = out.split("[Subdomains] ", 1)[1]
    assert tail.startswith("ERROR: crt.sh unavailable") and len(tail) == 200
