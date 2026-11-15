"""edgepack: persistent per-agent containers, agent packs, thermal migration. Fully offline:
docker is never invoked (subprocess/_exec mocked) and fleet HTTP goes through MockTransport."""
import asyncio
import gzip
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from avrone import agentcontainer as ac
from avrone import agentfs, agentpack, memstore
from avrone.agentpack import PackError
from avrone.config import Settings
from avrone.sovereign import CommandResult, DockerRunner

EDGE_ENVS = ("AVRONE_AGENT_CONTAINER", "AVRONE_AGENT_MEM_MB", "AVRONE_AGENT_CPUS", "AVRONE_AGENT_IDLE_SECS",
             "AVRONE_AGENT_DOCKER_ISOLATE", "AVRONE_AGENT_FS_QUOTA_MB", "AVRONE_AGENT_MIGRATE",
             "AVRONE_NODE_ID", "AVRONE_THERMAL_MAX_C")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_edge(monkeypatch):
    for k in EDGE_ENVS:
        monkeypatch.delenv(k, raising=False)


def _settings(tmp_path, **kw):
    return Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data", **kw)


def _fake_inspect_from_run(argv, running=True):
    def option(flag, default=None):
        try:
            return argv[argv.index(flag) + 1]
        except (ValueError, IndexError):
            return default
    labels = {}
    for i, value in enumerate(argv[:-1]):
        if value == "--label":
            key, val = argv[i + 1].split("=", 1)
            labels[key] = val
    env = []
    for i, value in enumerate(argv[:-1]):
        if value in ("--env", "-e"):
            env.append(argv[i + 1])
    bind = option("-v")
    source = bind.split(":", 1)[0]
    memory = int(option("--memory", "512m").removesuffix("m")) * 1024 * 1024
    swap = int(option("--memory-swap", "512m").removesuffix("m")) * 1024 * 1024
    return {
        "State": {"Running": running},
        "Image": argv[-3],
        "Config": {"User": option("--user"), "Image": argv[-3], "WorkingDir": option("-w"),
                   "Env": env, "Labels": labels},
        "HostConfig": {
            "Binds": [bind], "ReadonlyRootfs": "--read-only" in argv,
            "Tmpfs": {"/tmp": option("--tmpfs", "")}, "CapDrop": ["ALL"], "CapAdd": [],
            "SecurityOpt": [option("--security-opt", "")],
            "NetworkMode": option("--network", "bridge"), "Privileged": False,
            "PortBindings": None, "Devices": [], "DeviceRequests": [],
            "PidMode": "", "IpcMode": "", "AutoRemove": False,
            "Memory": memory, "MemorySwap": swap,
            "NanoCpus": int(float(option("--cpus", "1.0")) * 1_000_000_000),
            "PidsLimit": int(option("--pids-limit", "512")),
        },
        "Mounts": [{"Type": "bind", "Source": source, "Destination": "/agent", "RW": True}],
    }


class FakeDocker:
    """Records argv and returns hardened JSON from docker inspect."""

    def __init__(self, running: set[str] | None = None, fail_run: bool = False):
        self.calls: list[list[str]] = []
        self.running = running or set()
        self.fail_run = fail_run
        self.inspect_payloads: dict[str, dict] = {}
        self.image_id = "sha256:" + "a" * 64
        self.fail_image_inspect = False
        self.fail_rm_names: set[str] = set()
        self.raise_rm_names: set[str] = set()

    def seed(self, argv):
        name = argv[argv.index("--name") + 1]
        self.running.add(name)
        self.inspect_payloads[name] = _fake_inspect_from_run(argv)

    async def __call__(self, argv, timeout):
        self.calls.append(argv)
        name = argv[-1]
        if len(argv) > 2 and argv[1:3] == ["image", "inspect"]:
            if self.fail_image_inspect:
                return CommandResult(" ".join(argv), 1, "", "image missing")
            return CommandResult(" ".join(argv), 0, self.image_id, "")
        if argv[1] == "inspect":
            if name in self.running and name in self.inspect_payloads:
                return CommandResult(" ".join(argv), 0, json.dumps(self.inspect_payloads[name]), "")
            return CommandResult(" ".join(argv), 1, "", f"No such object: {name}")
        if argv[1] == "run":
            if self.fail_run:
                return CommandResult("", 125, "", "image missing")
            name = argv[argv.index("--name") + 1]
            self.running.add(name)
            self.inspect_payloads[name] = _fake_inspect_from_run(argv)
        if argv[1] == "rm":
            if name in self.raise_rm_names:
                self.raise_rm_names.remove(name)
                raise RuntimeError("temporary docker rm transport error")
            if name in self.fail_rm_names:
                self.fail_rm_names.remove(name)
                return CommandResult(" ".join(argv), 1, "", "temporary docker rm failure")
            if name not in self.running:
                return CommandResult(" ".join(argv), 1, "", f"No such container: {name}")
            self.running.discard(name)
            self.inspect_payloads.pop(name, None)
        return CommandResult(" ".join(argv), 0, "", "")

    def verbs(self):
        return [c[1] for c in self.calls]

# ------------------------------------------------------------------ 1. persistent container argv
def test_defaults_off():
    assert ac.container_mode() == "off"
    assert ac.mem_mb() == 512 and ac.cpus() == 1.0 and ac.idle_secs() == 600.0


def test_mode_and_limits_env(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "Persistent")
    monkeypatch.setenv("AVRONE_AGENT_MEM_MB", "1024")
    monkeypatch.setenv("AVRONE_AGENT_CPUS", "2.5")
    assert ac.container_mode() == "persistent" and ac.mem_mb() == 1024 and ac.cpus() == 2.5
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "yes-please")
    monkeypatch.setenv("AVRONE_AGENT_MEM_MB", "junk")
    monkeypatch.setenv("AVRONE_AGENT_CPUS", "-3")
    assert ac.container_mode() == "off" and ac.mem_mb() == 512 and ac.cpus() == 1.0
    monkeypatch.setenv("AVRONE_AGENT_MEM_MB", "1")
    assert ac.mem_mb() == ac.MIN_MEM_MB


def test_container_name_is_docker_safe():
    assert ac.container_name("Edge 7!", "Builder") == "avrone-agent-edge-7-builder"
    assert ac.container_name("", "../x") == "avrone-agent-node-x"


def test_persistent_run_argv_hardening(tmp_path):
    d = agentfs.agent_dir(tmp_path / "ws", "Coder")
    argv = ac.persistent_run_argv("avrone-agent-n-coder", d, "img:1", allow_network=False, mem=256, cpu=0.5,
                                  agent_id="coder")
    assert argv[:4] == ["docker", "run", "-d", "--name"] and argv[4] == "avrone-agent-n-coder"
    assert "--rm" not in argv                                     # long-lived, reused
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--memory") + 1] == "256m" and argv[argv.index("--cpus") + 1] == "0.5"
    assert argv[argv.index("--network") + 1] == "none"
    assert argv.count("-v") == 1 and argv[argv.index("-v") + 1] == f"{d}:/agent"   # ONLY the agent dir
    assert argv[argv.index("-w") + 1] == "/agent" and "AVRONE_AGENT_DIR=/agent" in argv
    assert argv[-3:] == ["img:1", "sleep", "infinity"]
    net = ac.persistent_run_argv("n", d, "img", allow_network=True)
    assert "--network" not in net and net[net.index("--memory") + 1] == "512m"


def test_exec_inspect_rm_argv():
    assert ac.exec_argv("c1", "ls") == ["docker", "exec", "-w", "/agent", "-e", "AVRONE_AGENT_DIR=/agent",
                                        "c1", "bash", "-lc", "ls"]
    assert ac.inspect_argv("c1")[:2] == ["docker", "inspect"] and ac.rm_argv("c1") == ["docker", "rm", "-f", "c1"]


def test_isolate_path_shares_hardening(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_DOCKER_ISOLATE", "1")
    s = _settings(tmp_path, allow_network=False)
    r = DockerRunner(s)
    argv = r.exec_argv("make", r._agent_cwd(agentfs.agent_dir(s.workspace, "Coder")))
    assert argv[argv.index("--cap-drop") + 1] == "ALL" and "no-new-privileges" in argv
    assert argv[argv.index("--network") + 1] == "none" and argv.count("-v") == 1


# ------------------------------------------------------------------ pool: lazy, reused, reaped
def test_pool_lazy_create_and_reuse(tmp_path):
    fake, now = FakeDocker(), [100.0]
    pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: now[0], idle=60)
    d = agentfs.agent_dir(tmp_path / "ws", "builder")
    assert fake.calls == []                                       # nothing until first use
    name = run(pool.ensure(d))
    assert name == "avrone-agent-edge-builder"
    assert fake.verbs() == ["image", "inspect", "rm", "run", "inspect"] and pool.created == 1
    now[0] = 130.0
    assert run(pool.ensure(d)) == name
    assert fake.verbs() == ["image", "inspect", "rm", "run", "inspect"]               # reused: no new docker calls
    assert pool.state()["homes"][name]["uses"] == 2


def test_pool_adopts_only_a_policy_compliant_running_leftover(tmp_path):
    d = agentfs.agent_dir(tmp_path / "ws", "builder")
    fake = FakeDocker()
    argv = ac.persistent_run_argv(
        "avrone-agent-edge-builder", d, fake.image_id, False, agent_id="builder"
    )
    fake.seed(argv)
    pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: 0.0)
    run(pool.ensure(d))
    assert fake.verbs() == ["image", "inspect"] and pool.created == 0


def test_pool_pins_mutable_tag_to_immutable_image_id(tmp_path):
    fake = FakeDocker()
    first_id = fake.image_id
    d = agentfs.agent_dir(tmp_path / "ws", "builder")
    first_pool = ac.AgentContainerPool(fake, "img:stable", False, node="edge", clock=lambda: 0.0)
    name = run(first_pool.ensure(d))
    first_payload = fake.inspect_payloads[name]
    assert first_payload["Config"]["Image"] == first_id
    assert first_payload["Image"] == first_id
    first_run = next(call for call in fake.calls if len(call) > 1 and call[1] == "run")
    assert first_run[-3] == first_id

    second_id = "sha256:" + "b" * 64
    fake.image_id = second_id  # the tag now points at a different local image
    restarted_pool = ac.AgentContainerPool(fake, "img:stable", False, node="edge", clock=lambda: 1.0)
    run(restarted_pool.ensure(d))
    run_calls = [call for call in fake.calls if len(call) > 1 and call[1] == "run"]
    assert len(run_calls) == 2
    assert run_calls[1][-3] == second_id
    final_payload = fake.inspect_payloads[name]
    assert final_payload["Config"]["Image"] == second_id
    assert final_payload["Image"] == second_id
    assert ["docker", "rm", "-f", name] in fake.calls


@pytest.mark.parametrize("bad_image_id,fail", [
    ("not-an-image-id", False),
    ("", True),
])
def test_pool_fails_closed_when_image_identity_cannot_be_resolved(tmp_path, bad_image_id, fail):
    fake = FakeDocker()
    fake.image_id = bad_image_id
    fake.fail_image_inspect = fail
    d = agentfs.agent_dir(tmp_path / "ws", "builder")
    pool = ac.AgentContainerPool(fake, "img:missing", False, node="edge", clock=lambda: 0.0)
    with pytest.raises(RuntimeError, match="resolve configured Docker image|invalid immutable image ID"):
        run(pool.ensure(d))
    assert fake.verbs() == ["image"]  # no inspect, remove, or create without a verified image


def test_pool_close_waits_for_active_lease_and_rejects_new_work(tmp_path):
    async def go():
        fake = FakeDocker()
        pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: 0.0)
        home = agentfs.agent_dir(tmp_path / "ws", "busy")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def worker():
            async with pool.lease(home) as name:
                entered.set()
                await release.wait()
                return name

        worker_task = asyncio.create_task(worker())
        await entered.wait()
        name = next(iter(pool.homes))
        close_task = asyncio.create_task(pool.close())
        await asyncio.sleep(0)
        assert not pool._closed
        assert ["docker", "rm", "-f", name] not in fake.calls
        with pytest.raises(RuntimeError, match="closing or closed"):
            await pool.ensure(agentfs.agent_dir(tmp_path / "ws", "late"))
        release.set()
        assert await worker_task == name
        await close_task
        assert pool._closed
        assert pool.homes == {}
        assert ["docker", "rm", "-f", name] in fake.calls

    run(go())


def test_pool_close_is_idempotent_and_rejects_work_after_close(tmp_path):
    async def go():
        fake = FakeDocker()
        pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: 0.0)
        home = agentfs.agent_dir(tmp_path / "ws", "done")
        name = await pool.ensure(home)
        await asyncio.gather(pool.close(), pool.close(), pool.close())
        await pool.close()
        assert fake.calls.count(["docker", "rm", "-f", name]) == 1
        with pytest.raises(RuntimeError, match="closing or closed"):
            await pool.ensure(home)

    run(go())


def test_pool_close_waiter_cancellation_does_not_cancel_cleanup(tmp_path):
    async def go():
        fake = FakeDocker()
        pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: 0.0)
        home = agentfs.agent_dir(tmp_path / "ws", "cancel-waiter")
        name = await pool.ensure(home)
        original_exec = pool._exec
        rm_entered = asyncio.Event()
        allow_rm = asyncio.Event()

        async def slow_rm(argv, timeout):
            if len(argv) > 1 and argv[1] == "rm":
                rm_entered.set()
                await allow_rm.wait()
            return await original_exec(argv, timeout)

        pool._exec = slow_rm
        waiter = asyncio.create_task(pool.close())
        await rm_entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not pool._closed
        second_waiter = asyncio.create_task(pool.close())
        allow_rm.set()
        await second_waiter
        assert pool._closed
        assert pool.homes == {}
        assert ["docker", "rm", "-f", name] in fake.calls

    run(go())


def test_pool_close_forces_removal_after_drain_deadline(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(ac, "MAX_AGENT_CONTAINER_CLOSE_DRAIN_SECONDS", 0.01)
        fake = FakeDocker()
        pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: 0.0)
        home = agentfs.agent_dir(tmp_path / "ws", "stuck")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def worker():
            async with pool.lease(home):
                entered.set()
                await release.wait()

        worker_task = asyncio.create_task(worker())
        await entered.wait()
        name = next(iter(pool.homes))
        await asyncio.wait_for(pool.close(), timeout=1)
        assert pool._closed
        assert name not in pool.homes
        assert ["docker", "rm", "-f", name] in fake.calls
        release.set()
        await worker_task

    run(go())



def test_pool_close_preserves_failed_removals_and_retries_only_remaining_homes(tmp_path):
    async def go():
        fake = FakeDocker()
        pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: 0.0)
        first = await pool.ensure(agentfs.agent_dir(tmp_path / "ws", "first"))
        second = await pool.ensure(agentfs.agent_dir(tmp_path / "ws", "second"))
        fake.fail_rm_names.add(first)

        with pytest.raises(RuntimeError, match="could not remove managed agent container"):
            await pool.close()

        assert not pool._closed
        assert pool._closing
        assert set(pool.homes) == {first}
        assert fake.calls.count(["docker", "rm", "-f", first]) == 1
        assert fake.calls.count(["docker", "rm", "-f", second]) == 1
        assert second not in fake.running

        await pool.close()
        assert pool._closed
        assert pool.homes == {}
        assert first not in fake.running and second not in fake.running
        assert fake.calls.count(["docker", "rm", "-f", first]) == 2
        assert fake.calls.count(["docker", "rm", "-f", second]) == 1

    run(go())


def test_pool_close_retries_after_docker_rm_transport_exception(tmp_path):
    async def go():
        fake = FakeDocker()
        pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: 0.0)
        name = await pool.ensure(agentfs.agent_dir(tmp_path / "ws", "retry-exception"))
        fake.raise_rm_names.add(name)

        with pytest.raises(RuntimeError, match="could not remove managed agent container"):
            await pool.close()
        assert not pool._closed and name in pool.homes

        await pool.close()
        assert pool._closed and pool.homes == {}
        assert name not in fake.running
        assert fake.calls.count(["docker", "rm", "-f", name]) == 2

    run(go())


def test_pool_close_treats_an_externally_removed_container_as_cleaned(tmp_path):
    async def go():
        fake = FakeDocker()
        pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: 0.0)
        name = await pool.ensure(agentfs.agent_dir(tmp_path / "ws", "externally-removed"))
        fake.running.discard(name)
        fake.inspect_payloads.pop(name, None)

        await pool.close()
        assert pool._closed and pool.homes == {}
        assert fake.calls.count(["docker", "rm", "-f", name]) == 1

    run(go())


def test_pool_idle_reap(tmp_path):
    fake, now = FakeDocker(), [0.0]
    pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: now[0], idle=60)
    a = run(pool.ensure(agentfs.agent_dir(tmp_path / "ws", "alpha")))
    now[0] = 50.0
    b = run(pool.ensure(agentfs.agent_dir(tmp_path / "ws", "beta")))
    now[0] = 70.0                                                 # alpha idle 70s, beta 20s
    assert run(pool.reap_idle()) == [a]
    assert ["docker", "rm", "-f", a] in fake.calls and a not in pool.homes and b in pool.homes
    now[0] = 200.0
    c = run(pool.ensure(agentfs.agent_dir(tmp_path / "ws", "gamma")))   # ensure reaps stale beta first
    assert b not in pool.homes and c in pool.homes and pool.reaped == 2
    run(pool.close())
    assert pool.homes == {} and fake.calls[-1] == ["docker", "rm", "-f", c]


def test_pool_idle_env(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_IDLE_SECS", "120")
    assert ac.AgentContainerPool(FakeDocker(), "i", False, node="n").idle_after == 120.0


def test_pool_start_failure_raises(tmp_path):
    pool = ac.AgentContainerPool(FakeDocker(fail_run=True), "img", False, node="n", clock=lambda: 0.0)
    with pytest.raises(RuntimeError, match="could not start agent container"):
        run(pool.ensure(agentfs.agent_dir(tmp_path / "ws", "x")))
    assert pool.homes == {}


def test_docker_runner_persistent_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "persistent")
    monkeypatch.setenv("AVRONE_NODE_ID", "edge1")
    s = _settings(tmp_path, allow_network=False)
    r = DockerRunner(s)
    seen = []

    fake = FakeDocker()
    async def fake_exec(argv, cmd, timeout, cwd=None, env=None):
        seen.append(argv)
        return await fake(argv, timeout)
    monkeypatch.setattr(r, "_exec", fake_exec)
    d = agentfs.agent_dir(s.workspace, "Researcher")
    run(r.run("ls", cwd=d))
    run(r.run("pwd", cwd=d))
    runs = [a for a in seen if a[1] == "run"]
    assert len(runs) == 1 and "avrone-agent-edge1-researcher" in runs[0]
    execs = [a for a in seen if a[1] == "exec"]
    assert [a[-1] for a in execs] == ["ls", "pwd"] and all("avrone-agent-edge1-researcher" in a for a in execs)
    run(r.run("echo hi"))                                         # non-agent command: shared container
    assert seen[-1] == ["docker", "exec", r.container, "bash", "-lc", "echo hi"]
    run(r.stop())
    assert ["docker", "rm", "-f", "avrone-agent-edge1-researcher"] in seen


def test_docker_runner_off_by_default_unchanged(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    r = DockerRunner(s)
    seen = []

    async def fake_exec(argv, cmd, timeout, cwd=None, env=None):
        seen.append(argv)
        return CommandResult(cmd, 0, "", "")
    monkeypatch.setattr(r, "_exec", fake_exec)
    run(r.run("ls", cwd=agentfs.agent_dir(s.workspace, "Researcher")))
    assert seen[0][:2] == ["docker", "exec"] and r.container in seen[0] and r.agent_pool is None


# ------------------------------------------------------------------ 2. agent packs
def _home(tmp_path, aid="builder", files=None, mem=None):
    ws, data = tmp_path / "ws", tmp_path / "data"
    d = agentfs.agent_dir(ws, aid)
    for rel, content in (files if files is not None else {"notes.txt": "hello", "sub/plan.md": "# plan"}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        os.utime(p, (1_700_000_000, 1_700_000_000))
    store = memstore.MemoryStore(data / "memory.jsonl")
    for k, v in (mem if mem is not None else {"goal": "ship"}).items():
        rec = store.save(aid, k, v, agent="Builder-1")
    return ws, data, d


def _repack(bundle, mutate):
    """Rewrite a bundle's members through mutate(name, data) -> (name, data) | None."""
    src = tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for m in src.getmembers():
            data = src.extractfile(m).read() if m.isreg() else b""
            got = mutate(m.name, data)
            if got is None:
                continue
            ti = tarfile.TarInfo(got[0])
            ti.size = len(got[1])
            tf.addfile(ti, io.BytesIO(got[1]))
    return gzip.compress(raw.getvalue())


def test_pack_manifest_and_determinism(tmp_path):
    ws, data, _ = _home(tmp_path)
    b1, man = agentpack.export_pack(ws, data, "builder", "edge")
    b2, _ = agentpack.export_pack(ws, data, "builder", "edge")
    assert b1 == b2                                               # byte-identical
    assert man["agent_id"] == "builder" and man["origin"] == "edge" and man["home"] == "edge"
    assert [f["path"] for f in man["files"]] == ["notes.txt", "sub/plan.md"]
    assert man["files"][0]["size"] == 5 and len(man["files"][0]["sha256"]) == 64
    assert man["memory"]["namespaces"] == ["builder"] and man["memory"]["entries"] == 1
    assert man["created"] == agentpack.content_stamp(ws, data, "builder")
    names = tarfile.open(fileobj=io.BytesIO(b1), mode="r:gz").getnames()
    assert names == ["manifest.json", "memory.json", "home/notes.txt", "home/sub/plan.md"]


def test_pack_roundtrip_to_fresh_node(tmp_path):
    ws, data, _ = _home(tmp_path / "a")
    bundle, man = agentpack.export_pack(ws, data, "builder", "edge")
    ws2, data2 = tmp_path / "b" / "ws", tmp_path / "b" / "data"
    res = agentpack.import_pack(ws2, data2, bundle, local_node="cool")
    assert res["files"] == 2 and res["memory"]["merged"] == 1
    d2 = ws2.resolve() / "agents" / "builder"
    assert (d2 / "sub" / "plan.md").read_text() == "# plan"
    assert memstore.MemoryStore(data2 / "memory.jsonl").load("builder")["goal"]["value"] == "ship"
    assert agentpack.load_index(data2)["builder"]["home"] == "edge"
    # re-export from the new node preserves where the agent was born
    b3, m3 = agentpack.export_pack(ws2, data2, "builder", "cool",
                                   home=agentpack.home_node(data2, "builder", "cool"))
    assert m3["home"] == "edge" and m3["origin"] == "cool" and m3["files"] == man["files"]


def test_hash_tamper_refused(tmp_path):
    ws, data, _ = _home(tmp_path / "a")
    bundle, _ = agentpack.export_pack(ws, data, "builder", "edge")
    bad = _repack(bundle, lambda n, d: (n, b"evil!") if n == "home/notes.txt" else (n, d))
    with pytest.raises(PackError, match="tampered"):
        agentpack.import_pack(tmp_path / "b" / "ws", tmp_path / "b" / "data", bad, "cool")
    badmem = _repack(bundle, lambda n, d: (n, b"[]") if n == "memory.json" else (n, d))
    with pytest.raises(PackError, match="memory hash"):
        agentpack.read_pack(badmem)
    extra = _repack(bundle, lambda n, d: (n, d))
    extra = _repack(extra, lambda n, d: (n, d) if n != "home/sub/plan.md" else None)
    with pytest.raises(PackError, match="do not match"):
        agentpack.read_pack(extra)
    assert not (tmp_path / "b" / "ws" / "agents" / "builder").exists()


@pytest.mark.parametrize("evil", ["home/../../etc/passwd", "home//abs", "home/a/../../x", "/abs/path", "other.txt"])
def test_path_traversal_refused(tmp_path, evil):
    ws, data, _ = _home(tmp_path / "a")
    bundle, _ = agentpack.export_pack(ws, data, "builder", "edge")
    bad = _repack(bundle, lambda n, d: (evil, d) if n == "home/notes.txt" else (n, d))
    with pytest.raises(PackError):
        agentpack.import_pack(tmp_path / "b" / "ws", tmp_path / "b" / "data", bad, "cool")
    assert not (tmp_path / "etc").exists()


def test_symlink_member_refused(tmp_path):
    ws, data, _ = _home(tmp_path / "a")
    bundle, _ = agentpack.export_pack(ws, data, "builder", "edge")
    src = tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for m in src.getmembers():
            tf.addfile(m, src.extractfile(m))
        ln = tarfile.TarInfo("home/link")
        ln.type, ln.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tf.addfile(ln)
    with pytest.raises(PackError, match="non-regular"):
        agentpack.read_pack(gzip.compress(raw.getvalue()))


def test_export_refuses_symlink_and_special(tmp_path):
    ws, data, d = _home(tmp_path)
    (d / "escape").symlink_to("/etc/passwd")
    with pytest.raises(PackError, match="symlink"):
        agentpack.export_pack(ws, data, "builder", "edge")
    (d / "escape").unlink()
    os.mkfifo(d / "pipe")
    with pytest.raises(PackError, match="special file"):
        agentpack.export_pack(ws, data, "builder", "edge")


def test_quota_enforced_both_ways(tmp_path, monkeypatch):
    ws, data, _ = _home(tmp_path / "a", files={"big.bin": "x" * (2 * 1024 * 1024)})
    bundle, _ = agentpack.export_pack(ws, data, "builder", "edge")
    monkeypatch.setenv("AVRONE_AGENT_FS_QUOTA_MB", "1")
    with pytest.raises(PackError, match="quota"):
        agentpack.export_pack(ws, data, "builder", "edge")
    with pytest.raises(PackError, match="quota"):
        agentpack.import_pack(tmp_path / "b" / "ws", tmp_path / "b" / "data", bundle, "cool")


def test_never_overwrites_another_agents_home(tmp_path):
    ws, data, _ = _home(tmp_path / "a")
    bundle, _ = agentpack.export_pack(ws, data, "builder", "edge")
    # the receiver has its OWN native "builder" (home = cool)
    ws2, data2, d2 = _home(tmp_path / "b", files={"mine.txt": "keep"})
    with pytest.raises(PackError, match="another agent"):
        agentpack.import_pack(ws2, data2, bundle, "cool")
    assert (d2 / "mine.txt").read_text() == "keep" and not (d2 / "notes.txt").exists()


def test_same_agent_only_if_newer(tmp_path):
    ws, data, d = _home(tmp_path / "a")
    old_bundle, old = agentpack.export_pack(ws, data, "builder", "edge")
    ws2, data2 = tmp_path / "b" / "ws", tmp_path / "b" / "data"
    agentpack.import_pack(ws2, data2, old_bundle, "cool")
    with pytest.raises(PackError, match="not newer"):              # same pack again
        agentpack.import_pack(ws2, data2, old_bundle, "cool")
    (d / "notes.txt").write_text("hello v2")
    os.utime(d / "notes.txt", (2_100_000_000, 2_100_000_000))
    new_bundle, new = agentpack.export_pack(ws, data, "builder", "edge")
    assert new["created"] > old["created"]
    agentpack.import_pack(ws2, data2, new_bundle, "cool")
    assert (ws2.resolve() / "agents" / "builder" / "notes.txt").read_text() == "hello v2"
    with pytest.raises(PackError, match="not newer"):              # the stale one can't roll it back
        agentpack.import_pack(ws2, data2, old_bundle, "cool")


def test_import_expectations(tmp_path):
    ws, data, _ = _home(tmp_path / "a")
    bundle, _ = agentpack.export_pack(ws, data, "builder", "edge")
    with pytest.raises(PackError, match="not 'coder'"):
        agentpack.import_pack(tmp_path / "b" / "ws", tmp_path / "b" / "data", bundle, "cool", expect_agent="coder")
    with pytest.raises(PackError, match="origin"):
        agentpack.import_pack(tmp_path / "b" / "ws", tmp_path / "b" / "data", bundle, "cool", expect_origin="mallory")
    with pytest.raises(PackError, match="not a valid"):
        agentpack.read_pack(b"not a tarball")
    with pytest.raises(PackError, match="invalid agent id"):
        agentpack.export_pack(ws, data, "../x", "edge")


def test_empty_home_exports(tmp_path):
    b, man = agentpack.export_pack(tmp_path / "ws", tmp_path / "data", "ghost", "edge")
    assert man["files"] == [] and man["memory"]["entries"] == 0 and man["created"] == 0
    assert agentpack.read_pack(b)[0]["agent_id"] == "ghost"


# ------------------------------------------------------------------ 3. fleet migration
import base64  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import httpx  # noqa: E402

from avrone import doctor  # noqa: E402
from avrone import fleet as fleet_mod  # noqa: E402
from avrone import agentpack_tools as apt  # noqa: E402
from avrone.fleet import AGENTPACK_PATH, FleetManager, GatewayRefused, sign  # noqa: E402
from avrone.governor import Governor  # noqa: E402

FLEET_ENVS = ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT", "AVRONE_FLEET_REQUIRE_ALLOW",
              "AVRONE_FLEET_PRIVILEGED", "AVRONE_PRIVILEGED_NODES", "AVRONE_FLEET_MAX_HOPS", "AVRONE_PUBLIC_URL")


@pytest.fixture
def fleet_env(monkeypatch):
    for k in FLEET_ENVS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)
    return monkeypatch


def mkfleet(mp, root, node, allow="", temp=None):
    mp.setenv("AVRONE_FLEET_TOKEN", "tok-0123456789abcdef")
    mp.setenv("AVRONE_NODE_ID", node)
    mp.setenv("AVRONE_FLEET_ALLOW", allow)
    core = MagicMock()
    core.active = {}
    core.codex.emit = MagicMock()
    core.settings = Settings(workspace=root / "ws", data_dir=root / "data")
    core.governor = Governor(4, thermal_max_c=80, readers={
        "temp_c": (lambda: temp[0]) if temp else (lambda: None), "load": lambda: None,
        "mem_pct": lambda: None, "disk_free_pct": lambda: None}, ttl_s=0)
    return FleetManager(core, root / "data")


def events(fm, kind):
    return [c.kwargs for c in fm.core.codex.emit.call_args_list if c.args[1] == kind]


def seed(fm, aid="builder", text="hello", mtime=1_700_000_000):
    d = agentfs.agent_dir(fm.core.settings.workspace, aid)
    (d / "notes.txt").write_text(text)
    os.utime(d / "notes.txt", (mtime, mtime))
    return d


def online(fm, nid, url=None, health=None, load=0):
    fm.nodes[nid] = {"node_id": nid, "url": url or f"http://{nid}", "last_seen": time.time(), "load": load,
                     **({"health": health} if health else {})}


def signed(fm, payload, path=AGENTPACK_PATH, headers=None):
    body = json.dumps(payload).encode()
    return body, {**sign(fm.token, "POST", path, body), **(headers or {})}


def transport_to(receivers):
    """MockTransport standing in for peers' HTTP servers (same gateway + status mapping as server.py)."""
    def handler(request: httpx.Request) -> httpx.Response:
        rx = receivers[request.url.host]
        try:
            payload = rx.dispatch_gateway("agentpack", request.method, request.url.path, request.content,
                                          {k.lower(): v for k, v in request.headers.items()})
            return httpx.Response(200, json=rx.receive_agentpack(payload))
        except GatewayRefused as e:
            return httpx.Response(401 if e.auth else 403, json={"detail": str(e)})
        except PermissionError as e:
            return httpx.Response(403, json={"detail": str(e)})
        except ValueError as e:
            return httpx.Response(400, json={"detail": str(e)})
    return httpx.MockTransport(handler)


def pack_payload(fm, aid="builder", **extra):
    bundle, _ = agentpack.export_pack(fm.core.settings.workspace, fm.data_dir, aid, fm.node_id)
    return {"from": fm.node_id, "hops": 1, "capability": "agent:migrate", "agent_id": aid,
            "bundle": base64.b64encode(bundle).decode(), **extra}


def test_capability_registered():
    assert fleet_mod.kind_capabilities("agentpack") == {"agent:migrate"}
    assert not fleet_mod.migrate_enabled()


def test_gateway_refuses_open_allowlist(tmp_path, fleet_env):
    rx = mkfleet(fleet_env, tmp_path / "rx", "cool")                    # open allowlist
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge")
    seed(tx)
    with pytest.raises(GatewayRefused) as e:
        rx.dispatch_gateway("agentpack", "POST", AGENTPACK_PATH, *signed(rx, pack_payload(tx)))
    assert e.value.code == "sender_not_allowed"


def test_gateway_refuses_unlisted_sender(tmp_path, fleet_env):
    rx = mkfleet(fleet_env, tmp_path / "rx", "cool", allow="other")
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge")
    seed(tx)
    with pytest.raises(GatewayRefused) as e:
        rx.dispatch_gateway("agentpack", "POST", AGENTPACK_PATH, *signed(rx, pack_payload(tx)))
    assert e.value.code == "sender_not_allowed" and events(rx, "fleet.refused")


def test_gateway_refuses_wrong_capability(tmp_path, fleet_env):
    rx = mkfleet(fleet_env, tmp_path / "rx", "cool", allow="edge")
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge")
    seed(tx)
    for cap in ("spawn:squad", "memory:sync"):
        with pytest.raises(GatewayRefused) as e:
            rx.dispatch_gateway("agentpack", "POST", AGENTPACK_PATH, *signed(rx, pack_payload(tx, capability=cap)))
        assert e.value.code == "capability"
    # and agent:migrate is not accepted on the run endpoint
    with pytest.raises(GatewayRefused):
        rx.dispatch_gateway("run", "POST", "/api/fleet/run",
                            *signed(rx, {"from": "edge", "hops": 1, "capability": "agent:migrate"}, "/api/fleet/run"))


def test_gateway_refuses_privileged(tmp_path, fleet_env):
    fleet_env.setenv("AVRONE_FLEET_PRIVILEGED", "allow")
    fleet_env.setenv("AVRONE_PRIVILEGED_NODES", "edge")
    rx = mkfleet(fleet_env, tmp_path / "rx", "cool", allow="edge")
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge")
    seed(tx)
    with pytest.raises(GatewayRefused) as e:
        rx.dispatch_gateway("agentpack", "POST", AGENTPACK_PATH, *signed(rx, pack_payload(tx, privileged=True)))
    assert e.value.code == "privileged_capability" and events(rx, "fleet.privileged.refused")
    with pytest.raises(GatewayRefused) as e:
        rx.dispatch_gateway("agentpack", "POST", AGENTPACK_PATH,
                            *signed(rx, pack_payload(tx), headers={"x-avrone-privileged": "1"}))
    assert e.value.code == "privileged_unsigned"


def test_receiver_needs_migrate_on(tmp_path, fleet_env):
    rx = mkfleet(fleet_env, tmp_path / "rx", "cool", allow="edge")
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge")
    seed(tx)
    payload = rx.dispatch_gateway("agentpack", "POST", AGENTPACK_PATH, *signed(rx, pack_payload(tx)))
    with pytest.raises(PermissionError, match="disabled"):
        rx.receive_agentpack(payload)
    assert events(rx, "agent.migrate_refused")
    fleet_env.setenv("AVRONE_AGENT_MIGRATE", "on")
    assert rx.receive_agentpack(payload)["files"] == 1
    # the pack's origin must be the signed sender
    spoof = {**pack_payload(tx), "from": "mallory"}
    with pytest.raises(ValueError, match="origin"):
        rx.receive_agentpack(spoof)


def test_migrate_end_to_end(tmp_path, fleet_env):
    fleet_env.setenv("AVRONE_AGENT_MIGRATE", "on")
    rx = mkfleet(fleet_env, tmp_path / "rx", "cool", allow="edge")
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge")
    d = seed(tx)
    memstore.MemoryStore(tx.data_dir / "memory.jsonl").save("builder", "goal", "ship")
    online(tx, "cool")
    fleet_env.setattr(fleet_mod, "HTTP_TRANSPORT", transport_to({"cool": rx}))
    res = run(tx.migrate_agent("builder"))
    assert res["ok"] and res["target"] == "cool" and res["remote"]["memory"]["merged"] == 1
    got = rx.core.settings.workspace.resolve() / "agents" / "builder" / "notes.txt"
    assert got.read_text() == "hello" and (d / "notes.txt").exists()     # copied; source home kept
    assert events(tx, "agent.migrate")[0]["target"] == "cool"
    assert agentpack.load_index(tx.data_dir)["builder"]["migrated_to"] == "cool"
    assert agentpack.load_index(rx.data_dir)["builder"]["home"] == "edge"


def test_migrate_disabled_by_default(tmp_path, fleet_env):
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge")
    seed(tx)
    online(tx, "cool")
    with pytest.raises(PermissionError, match="disabled"):
        run(tx.migrate_agent("builder"))
    assert events(tx, "agent.migrate_refused")
    assert run(tx.thermal_migrate()) == []


def test_pick_target_prefers_cool_then_fallback(tmp_path, fleet_env):
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge")
    with pytest.raises(RuntimeError, match="no online nodes"):
        tx.pick_migration_target()
    online(tx, "a", load=3)
    online(tx, "b", load=1)
    assert tx.pick_migration_target()["node_id"] == "b"             # no governor numbers: least loaded
    online(tx, "hot", health={"temp_c": 90, "load": 0.1})
    online(tx, "warm", health={"temp_c": 60, "load": 0.5})
    online(tx, "cool", health={"temp_c": 45, "load": 1.5})
    assert tx.pick_migration_target()["node_id"] == "cool"          # coolest under thermal max
    tx.nodes["cool"]["last_seen"] = 0                                # offline peers are never picked
    assert tx.pick_migration_target()["node_id"] == "warm"
    assert tx.pick_migration_target("a")["node_id"] == "a"


def test_migrate_failure_stays_put(tmp_path, fleet_env):
    fleet_env.setenv("AVRONE_AGENT_MIGRATE", "on")
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge")
    d = seed(tx)
    online(tx, "down")

    def boom(request):
        raise httpx.ConnectError("unreachable")
    fleet_env.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(boom))
    with pytest.raises(httpx.ConnectError):
        run(tx.migrate_agent("builder"))
    assert events(tx, "agent.migrate_failed") and (d / "notes.txt").read_text() == "hello"
    assert "migrated_to" not in agentpack.load_index(tx.data_dir).get("builder", {})
    # a peer that refuses (403) is audited as refused
    rx = mkfleet(fleet_env, tmp_path / "rx", "down")                 # open allowlist -> refuses packs
    tx2 = mkfleet(fleet_env, tmp_path / "tx2", "edge")
    seed(tx2)
    online(tx2, "down")
    fleet_env.setattr(fleet_mod, "HTTP_TRANSPORT", transport_to({"down": rx}))
    with pytest.raises(RuntimeError, match="403"):
        run(tx2.migrate_agent("builder"))
    assert events(tx2, "agent.migrate_refused")


def test_thermal_hook_moves_idle_agents_only(tmp_path, fleet_env):
    fleet_env.setenv("AVRONE_AGENT_MIGRATE", "on")
    temp = [70.0]
    rx = mkfleet(fleet_env, tmp_path / "rx", "cool", allow="edge")
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge", temp=temp)
    seed(tx, "idler")
    seed(tx, "busy")
    seed(tx, "fresh", mtime=int(time.time()))                        # touched just now: not idle
    tx.core.active = {"Busy-2": {"role": "Busy", "status": "working"}}
    online(tx, "cool")
    fleet_env.setattr(fleet_mod, "HTTP_TRANSPORT", transport_to({"cool": rx}))
    assert run(tx.thermal_migrate()) == []                           # 70C: not hot
    temp[0] = 92.0
    assert tx.core.governor.is_hot()
    out = run(tx.thermal_migrate())
    assert [o["agent_id"] for o in out] == ["idler"] and out[0]["ok"]
    assert run(tx.thermal_migrate()) == []                           # unchanged since: not re-sent


def test_thermal_hook_failure_is_contained(tmp_path, fleet_env):
    fleet_env.setenv("AVRONE_AGENT_MIGRATE", "on")
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge", temp=[99.0])
    seed(tx, "a")
    seed(tx, "b")
    out = run(tx.thermal_migrate())                                  # no peers at all
    assert out == [{"agent_id": "a", "ok": False, "error": "no online nodes to migrate to"}]
    assert events(tx, "agent.migrate_failed") and (agentfs.agents_root(tx.core.settings.workspace) / "a").is_dir()


def test_governor_is_hot_unreadable():
    g = Governor(2, thermal_max_c=80, readers={"temp_c": lambda: None, "load": lambda: None,
                                               "mem_pct": lambda: None, "disk_free_pct": lambda: None})
    assert g.is_hot() is False


def test_register_keeps_health(tmp_path, fleet_env):
    rx = mkfleet(fleet_env, tmp_path / "rx", "router")
    rx.register({"node_id": "e1", "url": "http://e1", "health": {"temp_c": 55.5, "load": 0.2, "junk": 1}})
    assert rx.nodes["e1"]["health"] == {"temp_c": 55.5, "load": 0.2}  # unified sanitize_health (edgeroute) drops junk + unknowns


def test_server_endpoint_registered():
    from avrone.server import app
    assert AGENTPACK_PATH in {r.path for r in app.routes}


# ------------------------------------------------------------------ 4. tools + doctor
def tool_ctx(fm, aid="builder"):
    return SimpleNamespace(core=fm.core, run_id="r1", name="Builder-1", depth=0, agent_id=aid,
                           agent_dir=agentfs.agent_dir(fm.core.settings.workspace, aid))


def test_tools_export_import_own_only(tmp_path, fleet_env):
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge")
    d = seed(tx)
    out = run(apt.t_agent_pack_export(tool_ctx(tx), {}))
    assert out.startswith("packed agent builder: builder-1700000000.tar.gz")
    name = "builder-1700000000.tar.gz"
    assert (agentpack.pack_dir(tx.data_dir) / name).is_file() and d.is_dir()
    assert "not newer" in run(apt.t_agent_pack_import(tool_ctx(tx), {"name": name}))   # same copy: no-op refusal
    # another agent can't import builder's pack, and paths/URLs are refused
    for bad in (name, "../x.tar.gz", "http://evil/builder-1.tar.gz", "/etc/passwd"):
        assert run(apt.t_agent_pack_import(tool_ctx(tx, "coder"), {"name": bad})).startswith("ERROR")
    assert events(tx, "agentpack.export") and events(tx, "agentpack.refused")


def test_tool_import_restores_into_empty_home(tmp_path, fleet_env):
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge")
    d = seed(tx)
    run(apt.t_agent_pack_export(tool_ctx(tx), {}))
    import shutil
    shutil.rmtree(d)
    out = run(apt.t_agent_pack_import(tool_ctx(tx), {"name": "builder-1700000000.tar.gz"}))
    assert out.startswith("restored agent builder") and (d / "notes.txt").read_text() == "hello"


def test_tool_migrate_gated(tmp_path, fleet_env):
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge")
    seed(tx)
    assert "AVRONE_AGENT_MIGRATE=on" in run(apt.t_agent_migrate(tool_ctx(tx), {}))
    fleet_env.setenv("AVRONE_AGENT_MIGRATE", "on")
    assert "home stays" in run(apt.t_agent_migrate(tool_ctx(tx), {}))     # no peers
    no_home = SimpleNamespace(core=tx.core, run_id="r", name="X", depth=0, agent_id=None)
    assert run(apt.t_agent_pack_export(no_home, {})).startswith("ERROR")


def test_tools_registered():
    from avrone.tools import ToolRegistry
    from avrone.tools_extra import register_extra_tools
    r = ToolRegistry()
    register_extra_tools(r)
    assert {"agent_pack_export", "agent_pack_import", "agent_migrate"} <= set(r.names())


def _doc(env, settings):
    return doctor.run_doctor(settings, env=env, which=lambda _: None, python_version=(3, 12, 0))


def _checks(rep, name):
    return [c for c in rep["checks"] if c["name"] == name]


def test_doctor_migrate_open_allowlist(tmp_path):
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data")
    base = {"AVRONE_FLEET_TOKEN": "x" * 32}
    rep = _doc(base, s)
    assert rep["toggles"]["agent_migrate"] == "off" and rep["toggles"]["agent_container"] == "off"
    assert not _checks(rep, "security.agent_migrate")
    rep = _doc({**base, "AVRONE_AGENT_MIGRATE": "on"}, s)
    assert [c["status"] for c in _checks(rep, "security.agent_migrate")] == ["warn"]
    rep = _doc({**base, "AVRONE_AGENT_MIGRATE": "on", "AVRONE_FLEET_ALLOW": "a,b",
                "AVRONE_AGENT_CONTAINER": "persistent"}, s)
    assert [c["status"] for c in _checks(rep, "security.agent_migrate")] == ["info"]
    assert rep["toggles"]["agent_container"] == "persistent"


def test_heartbeat_carries_health(tmp_path, fleet_env):
    fleet_env.setenv("AVRONE_FLEET_PARENT", "http://router")
    tx = mkfleet(fleet_env, tmp_path / "tx", "edge", temp=[61.0])
    tx.core.tools.names.return_value = []
    tx.core.runner.kind = "local"
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})
    fleet_env.setattr(fleet_mod, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    fleet_env.setattr("avrone.presence.snapshot", lambda core: asyncio.sleep(0, result={}))
    assert run(tx.join_parent_once())["ok"]
    assert seen[0]["health"]["temp_c"] == 61.0



@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "512"])
def test_explicit_memory_limit_rejects_invalid_types_and_nonpositive_values(invalid, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    with pytest.raises(ValueError, match="mem"):
        ac.hardening_args(False, mem=invalid)


def test_explicit_memory_and_cpu_limits_are_clamped_to_safe_bounds(monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    low = ac.hardening_args(False, mem=1, cpu=0.01)
    high = ac.hardening_args(False, mem=10**9, cpu=10**9)
    assert low[low.index("--memory") + 1] == f"{ac.MIN_MEM_MB}m"
    assert low[low.index("--cpus") + 1] == str(ac.MIN_CPUS)
    assert high[high.index("--memory") + 1] == f"{ac.MAX_MEM_MB}m"
    assert high[high.index("--cpus") + 1] == str(ac.MAX_CPUS)


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf"), True, "bad"])
def test_pool_rejects_invalid_explicit_idle_limits(invalid):
    pool = ac.AgentContainerPool(FakeDocker(), "img", False, node="n", idle=invalid)
    with pytest.raises(ValueError, match="idle"):
        _ = pool.idle_after


def test_pool_recreates_a_managed_but_unsafe_leftover(tmp_path):
    d = agentfs.agent_dir(tmp_path / "ws", "builder")
    fake = FakeDocker()
    argv = ac.persistent_run_argv(
        "avrone-agent-edge-builder", d, fake.image_id, False, agent_id="builder"
    )
    fake.seed(argv)
    fake.inspect_payloads["avrone-agent-edge-builder"]["HostConfig"]["NetworkMode"] = "bridge"
    pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: 0.0)
    run(pool.ensure(d))
    assert fake.verbs() == ["image", "inspect", "rm", "run", "inspect"]
    assert fake.inspect_payloads["avrone-agent-edge-builder"]["HostConfig"]["NetworkMode"] == "none"


def test_pool_refuses_to_adopt_or_delete_an_unowned_name(tmp_path):
    d = agentfs.agent_dir(tmp_path / "ws", "builder")
    fake = FakeDocker()
    argv = ac.persistent_run_argv(
        "avrone-agent-edge-builder", d, fake.image_id, False, agent_id="builder"
    )
    fake.seed(argv)
    fake.inspect_payloads["avrone-agent-edge-builder"]["Config"]["Labels"].pop("avrone.agent-home")
    pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: 0.0)
    with pytest.raises(RuntimeError, match="unowned"):
        run(pool.ensure(d))
    assert fake.verbs() == ["image", "inspect"]
    assert "avrone-agent-edge-builder" in fake.running


def test_pool_rejects_container_that_does_not_survive_start(tmp_path):
    class DiesImmediately(FakeDocker):
        async def __call__(self, argv, timeout):
            result = await super().__call__(argv, timeout)
            if argv[1] == "run":
                self.inspect_payloads[argv[argv.index("--name") + 1]]["State"]["Running"] = False
            return result
    fake = DiesImmediately()
    d = agentfs.agent_dir(tmp_path / "ws", "builder")
    pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: 0.0)
    with pytest.raises(RuntimeError, match="failed sandbox verification"):
        run(pool.ensure(d))
    assert pool.homes == {} and "avrone-agent-edge-builder" not in fake.running


def test_pool_parallel_ensure_same_agent_creates_one_container(tmp_path):
    async def main():
        fake = FakeDocker()
        d = agentfs.agent_dir(tmp_path / "ws", "builder")
        pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: 0.0)
        results = await asyncio.gather(*(pool.ensure(d) for _ in range(32)))
        assert len(set(results)) == 1
        assert fake.verbs().count("run") == 1
        assert fake.verbs().count("inspect") == 2
        assert pool.state()["homes"][results[0]]["uses"] == 32
        await pool.close()
    run(main())



@pytest.mark.parametrize("mutate,reason", [
    (lambda p: p["HostConfig"].update(NetworkMode="bridge"), "network access"),
    (lambda p: p["HostConfig"].update(Privileged=True), "privileged"),
    (lambda p: p["HostConfig"].update(ReadonlyRootfs=False), "root filesystem"),
    (lambda p: p["Config"].update(User="0:0"), "non-root agent identity"),
    (lambda p: p["Config"].update(Image="evil:latest"), "configured image"),
    (lambda p: p.update(Image="sha256:" + "b" * 64), "actual image ID"),
    (lambda p: p["Config"].update(WorkingDir="/"), "working directory"),
    (lambda p: p["Config"].update(Env=["HOME=/tmp"]), "environment"),
    (lambda p: p["Config"]["Env"].append("AVRONE_ADMIN_TOKEN=leaked"), "secret-shaped credentials"),
    (lambda p: p["Config"]["Env"].append("PATH=/tmp"), "duplicated variable names"),
    (lambda p: p["HostConfig"].update(CapAdd=["SYS_ADMIN"]), "capabilities"),
    (lambda p: p["HostConfig"].update(CapDrop=[]), "capabilities"),
    (lambda p: p["HostConfig"].update(SecurityOpt=[]), "no-new-privileges"),
    (lambda p: p["HostConfig"].update(
        SecurityOpt=["no-new-privileges", "seccomp=unconfined"]), "seccomp/AppArmor overrides"),
    (lambda p: p["HostConfig"].update(
        SecurityOpt=["no-new-privileges", "apparmor=unconfined"]), "seccomp/AppArmor overrides"),
    (lambda p: p["HostConfig"].update(UTSMode="host"), "UTS namespace"),
    (lambda p: p["HostConfig"].update(CgroupnsMode="host"), "cgroup namespace"),
    (lambda p: p["HostConfig"].update(PidMode="shareable"), "private PID namespace"),
    (lambda p: p["HostConfig"].update(IpcMode="container:peer"), "private IPC namespace"),
    (lambda p: p["HostConfig"].update(UTSMode={"mode": "private"}), "private UTS namespace"),
    (lambda p: p["HostConfig"].update(CgroupnsMode="host-extra"), "private cgroup namespace"),
    (lambda p: p["HostConfig"].update(Runtime="nvidia"), "non-default runtime"),
    (lambda p: p["HostConfig"].update(UsernsMode="host"), "user namespace"),
    (lambda p: p["HostConfig"].update(Sysctls={"net.ipv4.ip_forward": "1"}), "sysctl overrides"),
    (lambda p: p["HostConfig"].update(GroupAdd=["0"]), "supplemental host groups"),
    (lambda p: p["HostConfig"].update(DeviceCgroupRules=["c 1:1 rwm"]), "device cgroup rules"),
    (lambda p: p["HostConfig"].update(PortBindings={"80/tcp": [{"HostPort": "8080"}]}), "publishes ports"),
    (lambda p: p["HostConfig"].update(Devices=[{"PathOnHost": "/dev/sda"}]), "device access"),
    (lambda p: p["HostConfig"].update(DeviceRequests=[{"Count": -1}]), "device access"),
    (lambda p: p["HostConfig"].update(PidMode="host"), "PID namespace"),
    (lambda p: p["HostConfig"].update(IpcMode="host"), "IPC namespace"),
    (lambda p: p["HostConfig"].update(AutoRemove=True), "auto-remove"),
    (lambda p: p["HostConfig"].update(Memory=1), "memory or swap"),
    (lambda p: p["HostConfig"].update(MemorySwap=1), "memory or swap"),
    (lambda p: p["HostConfig"].update(NanoCpus=1), "CPU limit"),
    (lambda p: p["HostConfig"].update(PidsLimit=1), "PID limit"),
    (lambda p: p["HostConfig"].update(Binds=["/tmp:/agent"]), "private /agent bind"),
    (lambda p: p["Mounts"].append({"Type": "bind", "Destination": "/host", "RW": True}), "private /agent bind"),
    (lambda p: p["Mounts"][0].update(RW=False), "private /agent bind"),
    (lambda p: p["HostConfig"]["Tmpfs"].update({"/tmp": "rw,size=8g"}), "bounded /tmp"),
])
def test_existing_container_policy_validator_rejects_each_escape(mutate, reason, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    agent_dir = agentfs.agent_dir(tmp_path / "ws", "builder")
    argv = ac.persistent_run_argv(
        "avrone-agent-edge-builder", agent_dir, "img", allow_network=False, agent_id="builder"
    )
    payload = _fake_inspect_from_run(argv)
    mutate(payload)
    ok, owned, detail = ac._inspect_matches_policy(
        json.dumps(payload), agent_dir, "img", allow_network=False
    )
    assert owned is True
    assert ok is False
    assert reason in detail


def test_existing_container_policy_validator_accepts_only_a_valid_json_object(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    agent_dir = agentfs.agent_dir(tmp_path / "ws", "builder")
    argv = ac.persistent_run_argv(
        "avrone-agent-edge-builder", agent_dir, "img", allow_network=False, agent_id="builder"
    )
    payload = _fake_inspect_from_run(argv)
    ok, owned, detail = ac._inspect_matches_policy(
        json.dumps(payload), agent_dir, "img", allow_network=False
    )
    assert (ok, owned, detail) == (True, True, "compatible")
    for malformed in ("", "not-json", "[]", json.dumps({"State": {"Running": True}})):
        ok, owned, _ = ac._inspect_matches_policy(malformed, agent_dir, "img", allow_network=False)
        assert ok is False and owned is False


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf"), True, "1cpu"])
def test_explicit_cpu_limit_rejects_invalid_values(invalid, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    with pytest.raises(ValueError, match="cpu"):
        ac.hardening_args(False, cpu=invalid)



@pytest.mark.parametrize("invalid", [10**1000, float("inf"), float("nan"), True, "not-a-number"])
def test_cpu_limit_rejects_overflow_nonfinite_and_malformed_values(invalid, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    with pytest.raises(ValueError, match="cpu"):
        ac.hardening_args(False, cpu=invalid)


def test_explicit_idle_limit_is_bounded_but_accepts_valid_extremes():
    minimum = ac.AgentContainerPool(FakeDocker(), "img", False, node="n", idle=0.01)
    maximum = ac.AgentContainerPool(FakeDocker(), "img", False, node="n", idle=10**1000)
    assert minimum.idle_after == ac.MIN_IDLE_SECS
    assert maximum.idle_after == 7 * 86400.0


def test_network_opt_in_never_allows_shared_host_or_container_network_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    d = agentfs.agent_dir(tmp_path / "ws", "builder")
    argv = ac.persistent_run_argv("avrone-agent-edge-builder", d, "img", True, agent_id="builder")
    for mode in ("host", "container:abc123"):
        payload = _fake_inspect_from_run(argv)
        payload["HostConfig"]["NetworkMode"] = mode
        ok, owned, detail = ac._inspect_matches_policy(json.dumps(payload), d, "img", allow_network=True)
        assert owned and not ok and "network namespace" in detail

@pytest.mark.asyncio
async def test_pool_lease_prevents_reap_during_long_running_command(tmp_path):
    now = [0.0]
    fake = FakeDocker()
    pool = ac.AgentContainerPool(fake, "img", False, node="edge", clock=lambda: now[0], idle=60)
    home = agentfs.agent_dir(tmp_path / "ws", "long-worker")
    async with pool.lease(home) as name:
        assert pool.state()["homes"][name]["active"] == 1
        now[0] = 600.0
        assert await pool.reap_idle(now=now[0]) == []
        assert name in pool.homes
    assert pool.state()["homes"][name]["active"] == 0
    assert pool.state()["homes"][name]["idle_s"] == 0.0
    now[0] = 661.0
    assert await pool.reap_idle(now=now[0]) == [name]
    assert name not in pool.homes


@pytest.mark.asyncio
async def test_pool_lease_is_released_when_command_task_is_cancelled(tmp_path):
    import asyncio
    now = [0.0]
    pool = ac.AgentContainerPool(FakeDocker(), "img", False, node="edge",
                                 clock=lambda: now[0], idle=60)
    home = agentfs.agent_dir(tmp_path / "ws", "cancel-worker")
    entered, blocked = asyncio.Event(), asyncio.Event()
    async def worker():
        async with pool.lease(home) as name:
            entered.set()
            await blocked.wait()
            return name
    task = asyncio.create_task(worker())
    await entered.wait()
    name = next(iter(pool.homes))
    assert pool.homes[name].active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.homes[name].active == 0
    now[0] = 61.0
    assert await pool.reap_idle(now=now[0]) == [name]


@pytest.mark.parametrize("mutate,reason", [
    (lambda p: p["Config"].update(Env=["HOME=/agent", "HOME=/tmp", "TMPDIR=/tmp",
                                      "PYTHONDONTWRITEBYTECODE=1", "AVRONE_AGENT_DIR=/agent"]),
     "duplicated or overridden"),
    (lambda p: p["Config"].update(Env=["HOME=/agent", {"TMPDIR": "/tmp"}]), "environment is malformed"),
    (lambda p: p["HostConfig"].update(SecurityOpt=["no-new-privileges=false"]), "no-new-privileges"),
    (lambda p: p["HostConfig"].update(Tmpfs={"/tmp": "notrw,nosuid,nodev,size=128m"}), "bounded /tmp"),
    (lambda p: p["HostConfig"].update(Tmpfs={"/tmp": "rw,nosuid,nodev,size=128m",
                                             "/var/run": "rw,size=16m"}), "bounded /tmp"),
    (lambda p: p["HostConfig"].update(AutoRemove=None), "auto-remove policy"),
    (lambda p: p["HostConfig"].update(AutoRemove="false"), "auto-remove policy"),
    (lambda p: p["HostConfig"].update(Memory=float(512 * 1024 * 1024)), "memory or swap"),
    (lambda p: p["HostConfig"].update(NanoCpus=1_000_000_000.0), "CPU limit"),
    (lambda p: p["HostConfig"].update(PidsLimit=True), "PID limit"),
    (lambda p: p["HostConfig"].update(VolumesFrom=["other-container"]), "external volumes"),
    (lambda p: p["HostConfig"].update(CapDrop={"ALL": True}), "capabilities"),
    (lambda p: p["HostConfig"].update(SecurityOpt={"no-new-privileges": True}), "no-new-privileges"),
])
def test_policy_inspector_rejects_malformed_or_lookalike_fields(mutate, reason, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    home = agentfs.agent_dir(tmp_path / "ws", "strict-worker")
    argv = ac.persistent_run_argv("avrone-agent-edge-strict-worker", home, "img",
                                  False, agent_id="strict-worker")
    payload = _fake_inspect_from_run(argv)
    mutate(payload)
    ok, owned, detail = ac._inspect_matches_policy(json.dumps(payload), home, "img", False)
    assert owned is True and ok is False
    assert reason in detail

@pytest.mark.asyncio
async def test_docker_runner_holds_lease_while_exec_is_in_flight(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "persistent")
    monkeypatch.setenv("AVRONE_AGENT_UID", "501")
    monkeypatch.setenv("AVRONE_AGENT_GID", "20")
    settings = _settings(tmp_path, allow_network=False)
    runner = DockerRunner(settings)
    now = [0.0]
    fake_docker = FakeDocker()
    pool = ac.AgentContainerPool(fake_docker, "img", False, node="edge",
                                 clock=lambda: now[0], idle=60)
    runner.agent_pool = pool
    observed = {}
    async def fake_exec(argv, cmd, timeout, cwd=None, env=None):
        if len(argv) > 1 and argv[1] == "exec":
            now[0] = 100.0
            container_name = argv[6]
            observed["active"] = pool.state()["homes"][container_name]["active"]
            observed["reaped"] = await pool.reap_idle(now=now[0])
            return CommandResult(cmd, 0, "completed", "")
        return await fake_docker(argv, timeout)
    monkeypatch.setattr(runner, "_exec", fake_exec)
    home = agentfs.agent_dir(settings.workspace, "busy-worker")
    result = await runner.run("sleep 90", cwd=home)
    assert result.exit_code == 0
    assert observed == {"active": 1, "reaped": []}
    name = next(iter(pool.homes))
    assert pool.homes[name].active == 0
    now[0] = 161.0
    assert await pool.reap_idle(now=now[0]) == [name]
