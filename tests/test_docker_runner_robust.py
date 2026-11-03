"""Round 27: the Docker runner. Covers in-container deadlines (killing the docker client never
killed the command inside the container), images without bash, a shared environment container or
agent home that died (daemon restart, OOM, `docker rm`), leaked `docker run --rm` containers after
a timeout or cancel, auto mode falling back to local when the docker health check fails, and the
exit-125 label. Runs offline against a fake `docker` shim."""
import asyncio
import os
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import agentcontainer as ac
from avrone import agentfs, sovereign
from avrone.config import Settings
from avrone.sovereign import CommandResult, DockerRunner

FAKE_DOCKER = r'''#!/bin/bash
# fake docker: state dir $FAKE_DOCKER_STATE, call log $FAKE_DOCKER_STATE/log
S=$FAKE_DOCKER_STATE; echo "$*" >> "$S/log"
sub=$1; shift
case "$sub" in
  inspect) n=${@: -1}; [ -f "$S/c-$n" ] && { echo true; exit 0; }; echo "Error: No such object: $n" >&2; exit 1;;
  rm) n=${@: -1}; rm -f "$S/c-$n"; echo "$n"; exit 0;;
  exec)
    while [ $# -gt 0 ]; do case "$1" in -w|-e) shift 2;; -*) shift;; *) break;; esac; done
    n=$1; shift
    if [ ! -f "$S/c-$n" ]; then echo "Error response from daemon: No such container: $n" >&2; exit 1; fi
    exec "$@";;
  run)
    detach=0; name=""
    while [ $# -gt 0 ]; do case "$1" in
      -d) detach=1; shift;; --rm) shift;;
      --name) name=$2; shift 2;;
      -v|-w|-e|--cap-drop|--security-opt|--pids-limit|--memory|--cpus|--network|--label) shift 2;;
      -*) shift;; *) break;; esac; done
    img=$1; shift
    if [ "$detach" = 1 ]; then
      [ -f "$S/fail-run" ] && { echo "docker: Error response from daemon: boom." >&2; exit 125; }
      touch "$S/c-$name"; echo "id-$name"; exit 0
    fi
    [ -n "$name" ] && touch "$S/c-$name"
    "$@"; rc=$?; [ -n "$name" ] && rm -f "$S/c-$name"; exit $rc;;
esac
exit 0
'''


def run(c):
    return asyncio.run(c)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"; bindir.mkdir()
    d = bindir / "docker"; d.write_text(FAKE_DOCKER); d.chmod(0o755)
    state = tmp_path / "state"; state.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.delenv("AVRONE_AGENT_DOCKER_ISOLATE", raising=False)
    monkeypatch.delenv("AVRONE_AGENT_CONTAINER", raising=False)
    monkeypatch.setenv("AVRONE_NODE_ID", "n1")
    return SimpleNamespace(state=state, log=lambda: (state / "log").read_text().splitlines()
                           if (state / "log").exists() else [])


def _settings(tmp_path, **kw):
    s = Settings()
    s.workspace = tmp_path / "ws"; s.data_dir = tmp_path / "data"
    s.command_timeout = 10; s.docker_image = "img:1"; s.allow_network = False; s.runner = "docker"
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ------------------------------------------------------------------ launcher / GUARD
def test_launcher_without_timeout_is_historical():
    assert ac.launcher("ls") == ["bash", "-lc", "ls"]


@pytest.mark.parametrize("t,dl", [(10, "12"), (1, "3"), (0, "0"), (-5, "0"), ("junk", "0"), (2.9, "4")])
def test_launcher_deadline(t, dl):
    argv = ac.launcher("ls", t)
    assert argv[:3] == ["sh", "-c", ac.GUARD] and argv[3] == dl and argv[4] == "ls"


def _guard(cmd, t, path=None):
    env = dict(os.environ, PATH=path) if path else None
    return subprocess.run(ac.launcher(cmd, t), capture_output=True, text=True, env=env, timeout=30)


def test_guard_runs_bash_and_passes_exit_code():
    r = _guard('echo "$BASH_VERSION" | cut -c1; exit 7', 10)
    assert r.returncode == 7 and r.stdout.strip().isdigit()


def test_guard_quotes_and_dollars_survive():
    r = _guard("""printf '%s|' "a b" '$HOME' "it's"; echo""", 10)
    assert r.stdout == "a b|$HOME|it's|\n"


def test_guard_deadline_kills_inside_container():
    t = time.time()
    r = _guard("sleep 20", 1)
    assert time.time() - t < 8 and r.returncode != 0


def _minimal_bin(tmp_path, tools, extra=None):
    b = tmp_path / "minbin"; b.mkdir()
    for tool in tools:
        src = subprocess.run(["sh", "-c", f"command -v {tool}"], capture_output=True, text=True).stdout.strip()
        os.symlink(src, b / tool)
    for name, body in (extra or {}).items():
        p = b / name; p.write_text(body); p.chmod(0o755)
    return str(b)


def test_guard_image_without_bash_uses_sh(tmp_path):
    path = _minimal_bin(tmp_path, ["sh", "echo", "timeout", "true"])
    r = _guard("echo via-sh; [ -z \"$BASH_VERSION\" ] && echo nobash", 10, path)
    assert r.returncode == 0 and r.stdout == "via-sh\nnobash\n"


def test_guard_image_without_timeout_still_runs(tmp_path):
    path = _minimal_bin(tmp_path, ["sh", "echo"])
    r = _guard("echo ran", 10, path)
    assert r.returncode == 0 and r.stdout == "ran\n"


def test_guard_old_busybox_timeout_is_skipped(tmp_path):
    # pre-1.30 busybox: `timeout [-t SECS] PROG`; `-s KILL 5 true` fails, so it must not be used
    old = "#!/bin/sh\necho 'timeout: unrecognized option' >&2\nexit 1\n"
    path = _minimal_bin(tmp_path, ["sh", "echo", "true"], {"timeout": old})
    r = _guard("echo once", 10, path)
    assert r.returncode == 0 and r.stdout == "once\n"


def test_guard_zero_deadline_runs_without_timeout(tmp_path):
    path = _minimal_bin(tmp_path, ["sh", "echo"], {"timeout": "#!/bin/sh\necho used >&2\nexit 9\n"})
    r = _guard("echo plain", 0, path)
    assert r.stdout == "plain\n" and "used" not in r.stderr


# ------------------------------------------------------------------ container_gone
@pytest.mark.parametrize("code,out,err,exp", [
    (1, "", "Error response from daemon: No such container: avrone-env-1", True),
    (126, "", "Error response from daemon: container 3f2a is not running", True),
    (1, "", "Error response from daemon: Container abc is paused, unpause the container before exec", True),
    (1, "", "Error response from daemon: Container abc is restarting, wait until the container is running", True),
    (1, "partial", "Error response from daemon: No such container: x", False),   # command ran
    (0, "", "Error response from daemon: No such container: x", False),
    (2, "", "Error response from daemon: No such container: x", False),
    (1, "", "grep: No such file or directory", False),
    (1, "", "", False),
])
def test_container_gone(code, out, err, exp):
    assert ac.container_gone(CommandResult("c", code, out, err), "x") is exp


# ------------------------------------------------------------------ DockerRunner with fake docker
def test_commands_carry_in_container_deadline(tmp_path, fake):
    r = DockerRunner(_settings(tmp_path)); run(r.start())
    res = run(r.run("echo hi", timeout=7))
    assert res.stdout == "hi\n"
    execs = [l for l in fake.log() if l.startswith("exec")]
    assert " 9 echo hi" in execs[-1]


def test_shared_container_died_is_recreated(tmp_path, fake):
    r = DockerRunner(_settings(tmp_path)); run(r.start())
    (fake.state / f"c-{r.container}").unlink()                   # daemon restart / OOM
    res = run(r.run("echo back"))
    assert res.exit_code == 0 and res.stdout == "back\n" and r.restarts == 1
    log = fake.log()
    assert sum(l.startswith("run -d") for l in log) == 2 and f"rm -f {r.container}" in log


def test_shared_container_restart_failure_is_reported(tmp_path, fake):
    r = DockerRunner(_settings(tmp_path)); run(r.start())
    (fake.state / f"c-{r.container}").unlink(); (fake.state / "fail-run").touch()
    res = run(r.run("echo x"))
    assert res.exit_code == 1 and "No such container" in res.stderr
    assert "could not restart the environment container" in res.stderr


def test_command_output_mentioning_dead_container_is_not_retried(tmp_path, fake):
    r = DockerRunner(_settings(tmp_path)); run(r.start())
    res = run(r.run("echo done; echo 'Error response from daemon: No such container: q' >&2; exit 1"))
    assert res.stdout == "done\n" and r.restarts == 0


def test_persistent_home_died_is_recreated(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_CONTAINER", "persistent")
    s = _settings(tmp_path); r = DockerRunner(s); run(r.start())
    d = agentfs.agent_dir(s.workspace, "Coder")
    assert run(r.run("echo one", cwd=d)).stdout == "one\n"
    name = ac.container_name("n1", "coder")
    (fake.state / f"c-{name}").unlink()
    res = run(r.run("echo two", cwd=d))
    assert res.stdout == "two\n" and r.restarts == 1 and name in r.agent_pool.homes


def test_isolated_run_is_named_and_removed_on_timeout(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_DOCKER_ISOLATE", "1")
    s = _settings(tmp_path); r = DockerRunner(s); run(r.start())
    d = agentfs.agent_dir(s.workspace, "Coder")
    res = run(r.run("sleep 30", cwd=d, timeout=1))
    assert res.timed_out
    log = fake.log()
    names = [l.split("--name ")[1].split()[0] for l in log if l.startswith("run --rm --name")]
    assert names and names[0].startswith("avrone-run-") and f"rm -f {names[0]}" in log


def test_isolated_run_not_removed_when_it_finished(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_DOCKER_ISOLATE", "1")
    s = _settings(tmp_path); r = DockerRunner(s); run(r.start())
    res = run(r.run("echo ok", cwd=agentfs.agent_dir(s.workspace, "Coder")))
    assert res.stdout == "ok\n" and not any(l.startswith("rm -f avrone-run-") for l in fake.log())


def test_isolated_run_removed_on_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_AGENT_DOCKER_ISOLATE", "1")
    s = _settings(tmp_path); r = DockerRunner(s)
    calls = []

    async def fake_exec(argv, cmd, timeout, cwd=None, env=None):
        calls.append(argv)
        if argv[1] == "run":
            await asyncio.sleep(30)
        return CommandResult(cmd, 0, "", "")
    monkeypatch.setattr(r, "_exec", fake_exec)

    async def go():
        t = asyncio.ensure_future(r.run("x", cwd=agentfs.agent_dir(s.workspace, "A")))
        await asyncio.sleep(0.05); t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    run(go())
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[-1] == ["docker", "rm", "-f", name]


def test_exec_argv_compat_without_timeout(tmp_path):
    r = DockerRunner(_settings(tmp_path))
    assert r.exec_argv("ls") == ["docker", "exec", r.container, "bash", "-lc", "ls"]
    assert ac.exec_argv("c1", "ls")[-3:] == ["bash", "-lc", "ls"]


# ------------------------------------------------------------------ docker_run tool
def test_docker_run_tool_removes_container_on_timeout(tmp_path, monkeypatch):
    from avrone import tools_extra as te
    monkeypatch.setattr(te.shutil, "which", lambda n: "/usr/bin/docker")
    calls = []

    class R:
        async def _exec(self, argv, cmd, timeout):
            calls.append(argv)
            return CommandResult(cmd, -1, "", "command timed out", timed_out=argv[1] == "run")
    s = SimpleNamespace(agents_can_shell=True, allow_network=False, command_timeout=1, workspace=tmp_path)
    ctx = SimpleNamespace(core=SimpleNamespace(settings=s, runner=R()), agent_dir=None)
    monkeypatch.setattr(te, "_ws", lambda c: tmp_path)
    out = run(te.t_docker_run(ctx, {"image": "alpine", "cmd": "sleep 999"}))
    name = calls[0][calls[0].index("--name") + 1]
    assert name.startswith("avrone-dr-") and calls[1] == ["docker", "rm", "-f", name] and "TIMEOUT" in out


# ------------------------------------------------------------------ establish / labels
def test_auto_falls_back_to_local_when_docker_health_check_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(sovereign.shutil, "which", lambda n: "/usr/bin/docker")
    stopped = []

    async def start(self):
        return None

    async def drun(self, cmd, timeout=None, cwd=None):
        return CommandResult(cmd, 127, "", "exec: \"sh\": executable file not found in $PATH")

    async def stop(self):
        stopped.append(1)
    monkeypatch.setattr(DockerRunner, "start", start)
    monkeypatch.setattr(DockerRunner, "run", drun)
    monkeypatch.setattr(DockerRunner, "stop", stop)
    r = run(sovereign.establish(_settings(tmp_path, runner="auto")))
    assert r.kind == "local" and stopped == [1]


def test_explicit_docker_health_failure_still_raises(tmp_path, monkeypatch):
    async def start(self):
        return None

    async def drun(self, cmd, timeout=None, cwd=None):
        return CommandResult(cmd, 127, "", "no shell")
    monkeypatch.setattr(DockerRunner, "start", start)
    monkeypatch.setattr(DockerRunner, "run", drun)
    with pytest.raises(RuntimeError, match="health check failed"):
        run(sovereign.establish(_settings(tmp_path, runner="docker")))


def test_exit_125_label():
    t = CommandResult("docker run x", 125, "", "docker: Error response from daemon: pull access denied.").text()
    assert "[exit 125 = docker failed before the command ran]" in t
    assert "[exit 125]" in CommandResult("myprog", 125, "", "custom failure").text()
