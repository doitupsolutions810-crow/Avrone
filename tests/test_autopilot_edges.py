"""Edge-case tests for scripts/mac-avrone-autopilot.sh.

Each test runs the real script against a throwaway copy of the repo, a fake drive and stubbed
`brew` / `launchctl` / `ollama` (a tiny HTTP server on a free port), with AVRONE in mock LLM mode
so lanes finish instantly. Marked `integration`: excluded from the self-evolve gate, run in CI.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(sys.platform == "win32", reason="bash script")]

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "scripts/mac-avrone-autopilot.sh"
BASE_COMMIT = "1121d26"   # upstream main before the carried patches

STUB_OLLAMA = r'''#!/usr/bin/env python3
import os, sys, json, http.server, threading
D = os.environ["STUB_DIR"]
def flag(n): return os.path.exists(os.path.join(D, n))
def log(m): open(os.path.join(D, "calls.log"), "a").write(m + "\n")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
log("ollama " + " ".join(sys.argv[1:]))
models = os.path.join(D, "models")
if cmd == "serve":
    if flag("serve_fail"): sys.exit(1)
    open(os.path.join(D, "serve.pid"), "w").write(str(os.getpid()))
    host, port = os.environ["OLLAMA_HOST"].rsplit(":", 1)
    maxreq = int(open(os.path.join(D, "max_req")).read()) if flag("max_req") else 0
    n = [0]
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            n[0] += 1
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"version":"stub"}')
            if maxreq and n[0] >= maxreq:
                threading.Thread(target=srv.shutdown).start()
        def log_message(self, *a): pass
    srv = http.server.HTTPServer((host, int(port)), H)
    srv.serve_forever(); sys.exit(0)
if cmd == "list":
    print("NAME ID SIZE MODIFIED")
    if os.path.exists(models):
        for m in open(models).read().split():
            print(f"{m} abc 1GB now")
    sys.exit(0)
if cmd == "pull":
    if flag("pull_fail"): sys.exit(1)
    open(models, "a").write(sys.argv[2] + "\n"); sys.exit(0)
if cmd == "run":
    if flag("run_silent"): sys.exit(0)
    print("ready"); sys.exit(0)
sys.exit(0)
'''


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


@pytest.fixture(scope="session")
def base_repos(tmp_path_factory):
    """Two pristine repo copies: 'patched' (current HEAD) and 'unpatched' (upstream base)."""
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("needs the git checkout")
    out = {}
    for name, ref in (("patched", "HEAD"), ("unpatched", BASE_COMMIT)):
        d = tmp_path_factory.mktemp(name) / "repo"
        r = subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(d)], capture_output=True, text=True)
        if r.returncode:
            pytest.skip(f"clone failed: {r.stderr}")
        if _git(d, "checkout", "-q", ref).returncode:
            pytest.skip(f"{ref} not available")
        # always test the CURRENT script + state, even on the old base
        shutil.copytree(ROOT / "scripts", d / "scripts", dirs_exist_ok=True)
        _git(d, "config", "user.email", "t@t"); _git(d, "config", "user.name", "t")
        out[name] = d
    return out


class Env:
    def __init__(self, tmp: Path, repo: Path, drive_name="drive"):
        self.tmp = tmp
        self.repo = tmp / "repo"
        shutil.copytree(repo, self.repo, symlinks=True)
        # fake venv: real >=3.11 interpreter, empty activate (no pip install in tests)
        venv = self.repo / ".venv" / "bin"
        shutil.rmtree(self.repo / ".venv", ignore_errors=True)
        venv.mkdir(parents=True)
        (venv / "activate").write_text("")
        (venv / "python").symlink_to(sys.executable)
        self.drive = tmp / drive_name
        self.drive.mkdir()
        self.stub = tmp / "stub"; self.stub.mkdir()
        self.bin = tmp / "bin"; self.bin.mkdir()
        (self.stub / "models").write_text("stub-model\n")
        for name, body in {
            "ollama": STUB_OLLAMA,
            "brew": '#!/bin/sh\necho "brew $*" >> "$STUB_DIR/calls.log"\n[ "$1" = --prefix ] && echo /usr\nexit 0\n',
            "launchctl": "#!/bin/sh\nexit 0\n",
            "python": f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
        }.items():
            p = self.bin / name; p.write_text(body); p.chmod(0o755)
        self.port = _free_port()

    def flag(self, name, content=""):
        (self.stub / name).write_text(content)

    def env(self, **kw):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("AVRONE_", "OLLAMA_"))}
        e.update(PATH=f"{self.bin}:{e['PATH']}", STUB_DIR=str(self.stub), DRIVE=str(self.drive),
                 OLLAMA_HOST=f"127.0.0.1:{self.port}", MODEL="stub-model", ONCE="1", LANES="",
                 SKIP_GATE="1", AUTOPILOT_NO_INSTALL="1", AVRONE_LLM_MODE="mock",
                 SMOKE_TIMEOUT="60", LANE_TIMEOUT="60", CYCLE_SLEEP="0", MIN_FREE_GB="0",
                 PYTHONDONTWRITEBYTECODE="1")
        e.update({k: str(v) for k, v in kw.items()})
        return {k: v for k, v in e.items() if v is not None and v != "__unset__"}

    def run(self, timeout=180, **kw):
        r = subprocess.run(["bash", str(self.repo / SCRIPT_REL)], cwd=self.tmp, env=self.env(**kw),
                           capture_output=True, text=True, timeout=timeout)
        self.kill_server()
        return r

    def kill_server(self):
        pidf = self.stub / "serve.pid"
        if pidf.exists():
            try: os.kill(int(pidf.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError): pass
            pidf.unlink()

    @property
    def state(self): return self.drive / "avrone-state"

    def stage(self): return (self.state / "reintro-stage.txt").read_text().strip()

    def calls(self):
        p = self.stub / "calls.log"; return p.read_text() if p.exists() else ""


@pytest.fixture
def env(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["patched"])
    yield e
    e.kill_server()


def out(r): return r.stdout + r.stderr


# ---------------- argument / drive validation ----------------

def test_no_drive_fails_cleanly(env):
    r = env.run(DRIVE="__unset__")
    assert r.returncode == 1 and "set DRIVE" in out(r)


def test_missing_drive(env):
    r = env.run(DRIVE=str(env.tmp / "nope"))
    assert r.returncode == 1 and "not mounted" in out(r)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_readonly_drive(env):
    env.drive.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        r = env.run()
    finally:
        env.drive.chmod(0o755)
    assert r.returncode == 1 and "not writable" in out(r)


def test_trailing_slash_drive(env):
    r = env.run(DRIVE=str(env.drive) + "/")
    assert r.returncode == 0, out(r)
    assert (env.drive / "avrone-state" / "env.sh").exists()


@pytest.mark.parametrize("bad", [("CYCLES", "abc"), ("CYCLES", "-1"), ("CTX", "big")])
def test_bad_numeric_env(env, bad):
    k, v = bad
    r = env.run(ONCE="0", **{k: v})
    assert r.returncode == 1 and k in out(r)


def test_not_a_checkout(env, tmp_path):
    fake = tmp_path / "fake" / "scripts"; fake.mkdir(parents=True)
    shutil.copy(env.repo / SCRIPT_REL, fake / "x.sh")
    r = subprocess.run(["bash", str(fake / "x.sh")], env=env.env(), capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "not an AVRONE checkout" in out(r)


# ---------------- happy path ----------------

def test_happy_path_with_spaces_in_drive(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["patched"], drive_name="AI Drive (USB)")
    r = e.run()
    assert r.returncode == 0, out(r)
    envsh = (e.state / "env.sh").read_text()
    assert "AVRONE_PROVIDER=ollama" in envsh and "AVRONE_RECON_ALLOWLIST=127.0.0.1,localhost" in envsh
    assert f"127.0.0.1:{e.port}/v1" in envsh and 'AVRONE_LLM_MODEL="stub-model"' in envsh
    assert (e.repo / "MERGE_ME.md").exists() and "Reintro stage" in (e.repo / "MERGE_ME.md").read_text()
    assert "=== cycle 1 ===" in out(r) and "done." in out(r)
    assert not (e.state / ".autopilot.lock").exists(), "lock must be released"
    e.kill_server()


def test_lanes_run_in_mock_mode_and_report(env):
    r = env.run(LANES="tools robust")
    assert r.returncode == 0, out(r)
    assert "lane tools start" in out(r) and "lane robust start" in out(r)
    assert "lane tools rc=0" in out(r)
    assert "lane-tools" in (env.repo / "MERGE_ME.md").read_text()


def test_resume_uses_drive_state_not_repo_seed(env):
    env.state.mkdir(parents=True)
    shutil.copy(env.repo / "scripts/avrone-state/reintro-plan.json", env.state)
    (env.state / "reintro-stage.txt").write_text("8\n")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "stage=8/9" in out(r) and "reintro stage 8/9" in out(r)


def test_full_stage_reports_complete(env):
    env.state.mkdir(parents=True); (env.state / "reintro-stage.txt").write_text("9")
    r = env.run()
    assert r.returncode == 0 and "reintro complete: 9/9, all 75 tools active" in out(r)


# ---------------- corrupted state ----------------

@pytest.mark.parametrize("content", ["", "abc", "7.5", "-3", "  \n"])
def test_corrupt_stage_is_reseeded(env, content):
    env.state.mkdir(parents=True); (env.state / "reintro-stage.txt").write_text(content)
    r = env.run()
    assert r.returncode == 0, out(r)
    assert env.stage() == "7"


def test_stage_beyond_plan_is_clamped(env):
    env.state.mkdir(parents=True); (env.state / "reintro-stage.txt").write_text("99")
    r = env.run()
    assert r.returncode == 0 and "clamping" in out(r) and env.stage() == "9"


@pytest.mark.parametrize("plan", ["{not json", '{"core": [], "batches": []}', '{"core": ["a"]}', "[]"])
def test_corrupt_plan_is_reseeded(env, plan):
    env.state.mkdir(parents=True); (env.state / "reintro-plan.json").write_text(plan)
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "plan corrupt" in out(r)
    assert list(env.state.glob("reintro-plan.json.bad-*")), "corrupt plan kept for forensics"


def test_corrupt_mcp_json_is_replaced(env):
    env.state.mkdir(parents=True); (env.state / "mcp.json").write_text("{oops")
    assert env.run().returncode == 0
    assert (env.state / "mcp.json").read_text().strip() == '{"servers":{}}'


# ---------------- repo hygiene + patches ----------------

def test_patches_apply_once_on_upstream_base(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    r = e.run()
    assert r.returncode == 0, out(r)
    assert "applying carried patches" in out(r)
    assert "def coerce_args" in (e.repo / "avrone/trinity.py").read_text()
    e.kill_server()
    r2 = e.run()
    assert r2.returncode == 0 and "applying carried patches" not in out(r2), "must be idempotent"


def test_patch_conflict_restores_tree(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    b = e.repo / "avrone/bootstrap.py"
    lines = b.read_text().splitlines(True)
    b.write_text("".join(lines[:195]) + "# conflicting local edit\n" * 3 + "".join(lines[210:]))
    _git(e.repo, "commit", "-qam", "conflict")
    r = e.run()
    assert r.returncode == 1 and "patch apply failed" in out(r)
    assert not (e.repo / ".git/rebase-apply").exists(), "git am must be aborted"
    e.kill_server()


def test_leftover_git_am_is_aborted(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    (e.repo / ".git/rebase-apply").mkdir()
    r = e.run()
    assert "leftover git am" in out(r)
    e.kill_server()


def test_runtime_agents_dir_kept_but_git_ignored(env):
    (env.repo / "agents").mkdir(); (env.repo / "agents/x.txt").write_text("x")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert (env.repo / "agents/x.txt").exists(), "agent homes are runtime state, never deleted"
    st = _git(env.repo, "status", "--short").stdout
    assert "agents" not in st and "MERGE_ME" not in st, st
    r2 = env.run()
    assert (env.repo / ".git/info/exclude").read_text().count("/agents/") == 1, "exclude must be idempotent"


def test_tracked_agents_kept(env):
    (env.repo / "agents").mkdir(); (env.repo / "agents/keep.py").write_text("x=1\n")
    _git(env.repo, "add", "agents"); _git(env.repo, "commit", "-qm", "agents")
    r = env.run()
    assert r.returncode == 0 and (env.repo / "agents/keep.py").exists() and "tracked in git" in out(r)


def test_ignored_agents_kept(env):
    (env.repo / "agents").mkdir(); (env.repo / "agents/data.bin").write_text("x")
    with open(env.repo / ".gitignore", "a") as f: f.write("\nagents/\n")
    r = env.run()
    assert r.returncode == 0 and (env.repo / "agents/data.bin").exists()


# ---------------- ollama / model ----------------

def test_ollama_fails_to_start(env):
    env.flag("serve_fail")
    r = env.run()
    assert r.returncode == 1 and "ollama did not start" in out(r)


def test_model_pulled_when_missing(env):
    (env.stub / "models").write_text("")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "ollama pull stub-model" in env.calls()


def test_pull_retries_then_fails(env):
    (env.stub / "models").write_text(""); env.flag("pull_fail")
    r = env.run(timeout=240)
    assert r.returncode == 1 and "after 3 attempts" in out(r)
    assert env.calls().count("ollama pull") == 3


def test_low_disk_blocks_pull(env):
    (env.stub / "models").write_text("")
    r = env.run(MIN_FREE_GB="999999999")
    assert r.returncode == 1 and "not enough space" in out(r)
    assert "ollama pull" not in env.calls()


def test_disk_check_skipped_when_model_present(env):
    assert env.run(MIN_FREE_GB="999999999").returncode == 0


def test_silent_model_warmup_fails(env):
    env.flag("run_silent")
    r = env.run()
    assert r.returncode == 1 and "no output on warm-up" in out(r)


def test_ollama_dies_mid_run_is_restarted(env):
    env.flag("max_req", "3")   # stub server exits after 3 health checks
    r = env.run(CYCLES="2", ONCE="0")
    assert r.returncode == 0, out(r)
    assert "ollama is down — restarting" in out(r)
    assert out(r).count("=== cycle") == 2


def test_ollama_restart_storm_gives_up(env):
    env.flag("max_req", "1")
    r = env.run(CYCLES="0", ONCE="0", timeout=240)
    assert r.returncode == 1 and "keeps dying" in out(r)


def test_already_running_ollama_is_reused(env):
    env.flag("max_req", "0")
    srv = subprocess.Popen([str(env.bin / "ollama"), "serve"], env=env.env())
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", env.port), 0.2).close(); break
            except OSError:
                time.sleep(0.1)
        r = subprocess.run(["bash", str(env.repo / SCRIPT_REL)], env=env.env(), capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, out(r)
        assert "starting ollama serve" not in out(r)
        assert srv.poll() is None, "must not kill an ollama it did not start"
    finally:
        srv.kill()


def test_no_brew_needed_when_everything_installed(env):
    (env.bin / "brew").unlink()
    assert env.run().returncode == 0


def test_no_brew_and_no_ollama(env, tmp_path):
    (env.bin / "brew").unlink(); (env.bin / "ollama").unlink()
    path = ":".join(p for p in os.environ["PATH"].split(":") if not (Path(p) / "ollama").exists() and not (Path(p) / "brew").exists())
    r = env.run(PATH=f"{env.bin}:{path}")
    assert r.returncode == 1 and "Homebrew is required" in out(r)


# ---------------- gate ----------------

def test_red_gate_stops_before_any_cycle(env):
    r = env.run(SKIP_GATE="0", GATE_CMD="echo '1 failed'; exit 1")
    assert r.returncode == 1 and "baseline RED" in out(r) and "=== cycle" not in out(r)


def test_green_gate_logged(env):
    r = env.run(SKIP_GATE="0", GATE_CMD="echo '5 passed in 0.1s'")
    assert r.returncode == 0 and "baseline: 5 passed" in out(r)


# ---------------- concurrency + signals ----------------

def test_second_instance_refused(env):
    (env.state / ".autopilot.lock").mkdir(parents=True)
    (env.state / ".autopilot.lock/pid").write_text(str(os.getpid()))   # alive
    r = env.run()
    assert r.returncode == 2 and "already running" in out(r)
    assert (env.state / ".autopilot.lock").exists(), "must not steal a live lock"


def test_stale_lock_is_recovered(env):
    p = subprocess.Popen(["true"]); p.wait()
    (env.state / ".autopilot.lock").mkdir(parents=True)
    (env.state / ".autopilot.lock/pid").write_text(str(p.pid))   # dead
    r = env.run()
    assert r.returncode == 0 and "stale lock" in out(r)


def test_two_concurrent_runs_one_wins(env):
    e = env.env(CYCLES="0", ONCE="0", CYCLE_SLEEP="2")
    a = subprocess.Popen(["bash", str(env.repo / SCRIPT_REL)], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.05)
    b = subprocess.run(["bash", str(env.repo / SCRIPT_REL)], env=e, capture_output=True, text=True, timeout=60)
    a.send_signal(signal.SIGTERM)
    try: a.communicate(timeout=30)
    except subprocess.TimeoutExpired: a.kill(); raise
    assert b.returncode == 2 or a.returncode == 2


def test_sigterm_cleans_up_lock_and_ollama(env):
    p = subprocess.Popen(["bash", str(env.repo / SCRIPT_REL)], env=env.env(CYCLES="0", ONCE="0", CYCLE_SLEEP="60"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log = ""
    deadline = time.time() + 90
    while time.time() < deadline and "=== cycle 1 ===" not in log:
        time.sleep(0.5)
        runs = list((env.drive / "avrone-runs").glob("*/autopilot.log"))
        log = runs[0].read_text() if runs else ""
    assert "=== cycle 1 ===" in log
    time.sleep(1)
    p.send_signal(signal.SIGTERM)
    t0 = time.time(); p.communicate(timeout=20)
    assert time.time() - t0 < 15, "must not wait out the 60s cycle sleep"
    assert p.returncode == 143
    assert not (env.state / ".autopilot.lock").exists()
    pid = int((env.stub / "serve.pid").read_text())
    time.sleep(0.5)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_rerun_after_success_is_clean(env):
    assert env.run().returncode == 0
    r = env.run()
    assert r.returncode == 0, out(r)
    assert len(list((env.drive / "avrone-runs").iterdir())) == 2, "each run gets its own results dir"
