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
            if flag("serve_hang_after") and n[0] > int(open(os.path.join(D, "serve_hang_after")).read()):
                import time; time.sleep(600)
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
    if flag("run_hang"):
        import time; time.sleep(300)
    print("ready"); sys.exit(0)
sys.exit(0)
'''


STUB_PYTHON = r'''#!/bin/sh
# wraps the real interpreter; `python -m avrone.cli chat ...` can be made to misbehave via flag files
case " $* " in *" avrone.cli chat "*)
  S="$DRIVE/avrone-state"
  [ -e "$STUB_DIR/chat_delete_stage" ] && rm -f "$S/reintro-stage.txt"
  [ -e "$STUB_DIR/chat_corrupt_plan" ] && printf '{' > "$S/reintro-plan.json"
  # unplug right after the agent process ends (AVRONE itself may mkdir its data dir while running;
  # on a real Mac /Volumes is root-owned so that fails — what we test is that the autopilot never recreates it)
  if [ -e "$STUB_DIR/chat_unplug" ]; then "@PY@" "$@"; rc=$?; mv "$DRIVE" "$DRIVE.unplugged"; exit $rc; fi
  [ -e "$STUB_DIR/chat_remove_model" ] && : > "$STUB_DIR/models"
  [ -e "$STUB_DIR/chat_readonly_state" ] && chmod 555 "$S"
  if [ -e "$STUB_DIR/chat_flood" ]; then head -c 3000000 /dev/zero | tr '\0' 'x'; head -c 3000000 /dev/zero | tr '\0' 'y' >&2; fi
  [ -e "$STUB_DIR/chat_crash" ] && { echo "Traceback: boom" >&2; exit 3; }
  [ -e "$STUB_DIR/chat_hang" ] && exec sleep 300
  if [ -e "$STUB_DIR/chat_smoke_pass" ]; then
    for t in $(echo "$AVRONE_TOOLS_ONLY" | tr , ' '); do echo "tool.call agent-1 $t {}" >&2; done
    echo "OK"; exit 0
  fi;;
esac
exec "@PY@" "$@"
'''


def path_without(tmp: Path, *names: str) -> str:
    """A PATH holding symlinks to every executable on the current PATH except `names`."""
    d = tmp / ("path-without-" + "-".join(names)); d.mkdir(exist_ok=True)
    for pdir in os.environ["PATH"].split(":"):
        if not os.path.isdir(pdir):
            continue
        for f in os.listdir(pdir):
            if f in names or (d / f).exists():
                continue
            src = os.path.join(pdir, f)
            if os.access(src, os.X_OK) and not os.path.isdir(src):
                (d / f).symlink_to(src)
    return str(d)


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
    def __init__(self, tmp: Path, repo: Path, drive_name="drive", repo_name="repo"):
        self.tmp = tmp
        self.repo = tmp / repo_name
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
            "python": STUB_PYTHON.replace("@PY@", sys.executable),
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


# =====================================================================================
# Round 2: deeper edges
# =====================================================================================
import importlib.util

_spec = importlib.util.spec_from_file_location("smoke_judge", ROOT / "scripts/avrone-state/smoke_judge.py")
smoke_judge = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(smoke_judge)


def _judge(tmp_path, log: str | bytes | None, nxt: str, stage="7"):
    errf = tmp_path / "smoke.err"
    if log is not None:
        errf.write_bytes(log if isinstance(log, bytes) else log.encode())
    sf = tmp_path / "stage.txt"; sf.write_text(stage)
    r = subprocess.run([sys.executable, str(ROOT / "scripts/avrone-state/smoke_judge.py"), str(errf), nxt, str(sf), stage],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout, sf.read_text(), (tmp_path / "stage.txt.bak")


def _calls(*tools): return "\n".join(f"12:00 tool.call agent-1 {t} {{}}" for t in tools)


class TestSmokeJudge:
    def test_all_called_advances_and_backs_up(self, tmp_path):
        out_, st, bak = _judge(tmp_path, _calls("a", "b", "c"), "a,b,c")
        assert "ADVANCE" in out_ and st == "8" and bak.read_text() == "8"
        assert not list(tmp_path.glob("*.tmp.*")), "atomic write leaves no temp files"

    def test_exactly_sixty_percent_advances(self, tmp_path):
        assert "ADVANCE" in _judge(tmp_path, _calls("a", "b", "c"), "a,b,c,d,e")[0]

    def test_below_sixty_percent_holds(self, tmp_path):
        out_, st, bak = _judge(tmp_path, _calls("a", "b"), "a,b,c,d,e")
        assert "HOLD" in out_ and st == "7" and not bak.exists()

    def test_too_many_malformed_holds(self, tmp_path):
        log = _calls("a", "b") + "\naction.malformed\n" * 4
        assert "HOLD" in _judge(tmp_path, log, "a,b")[0]

    def test_unknown_tool_counts_as_bad(self, tmp_path):
        log = _calls("a") + "\nError: Unknown tool 'x'\n" * 4
        assert "malformed/unknown=4" in _judge(tmp_path, log, "a")[0]

    def test_three_bad_still_advances(self, tmp_path):
        log = _calls("a") + "\naction.malformed" * 3
        assert "ADVANCE" in _judge(tmp_path, log, "a")[0]

    def test_missing_log_holds(self, tmp_path):
        out_, st, _ = _judge(tmp_path, None, "a,b")
        assert "HOLD" in out_ and st == "7"

    def test_binary_garbage_log_holds(self, tmp_path):
        assert "HOLD" in _judge(tmp_path, os.urandom(200_000), "a,b")[0]

    def test_empty_next_is_noop(self, tmp_path):
        out_, st, _ = _judge(tmp_path, _calls("a"), "-")
        assert "HOLD" in out_ and st == "7"

    def test_duplicate_next_names_counted_once(self, tmp_path):
        assert "1/1 exercised" in _judge(tmp_path, _calls("a"), "a,a,a")[0]

    def test_prefix_names_do_not_match(self, tmp_path):
        out_ = _judge(tmp_path, _calls("bridge_post_extra", "spawnx"), "bridge_post,spawn")[0]
        assert "0/2" in out_ and "HOLD" in out_

    def test_non_integer_stage_never_crashes(self, tmp_path):
        out_, st, _ = _judge(tmp_path, _calls("a"), "a", stage="x")
        assert "ADVANCE" in out_ and st == "x"

    def test_wrong_argc_is_hold(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/avrone-state/smoke_judge.py")], capture_output=True, text=True)
        assert r.returncode == 0 and "HOLD" in r.stdout

    def test_judge_function_direct(self):
        assert smoke_judge.judge("", []) == (False, [], 0)
        ok, hit, bad = smoke_judge.judge(_calls("x", "y"), ["x", "y", " "])
        assert ok and hit == ["x", "y"] and bad == 0


# ---------------- progression across cycles ----------------

def test_smoke_pass_advances_stage_end_to_end(env):
    env.flag("chat_smoke_pass")
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "ADVANCE" in out(r) and env.stage() == "8"
    assert (env.state / "reintro-stage.txt.bak").read_text() == "8"


def test_multi_cycle_reaches_full_registry(env):
    env.flag("chat_smoke_pass")
    r = env.run(CYCLES="3", ONCE="0")
    assert r.returncode == 0, out(r)
    assert env.stage() == "9" and "reintro complete: 9/9, all 75 tools active" in out(r)


def test_stage_deleted_mid_run_restored_from_backup(env):
    env.flag("chat_smoke_pass"); env.flag("chat_delete_stage")
    r = env.run(CYCLES="2", ONCE="0", LANES="tools")
    assert r.returncode == 0, out(r)
    assert "stage file vanished — restoring 8 from backup" in out(r)


def test_stage_deleted_without_backup_falls_back_to_repo(env):
    env.flag("chat_delete_stage")
    r = env.run(CYCLES="2", ONCE="0", LANES="tools")
    assert r.returncode == 0 and "restoring 7 from repo" in out(r)


def test_plan_corrupted_mid_run_is_reseeded(env):
    env.flag("chat_corrupt_plan")
    r = env.run(CYCLES="2", ONCE="0")
    assert r.returncode == 0, out(r)
    assert "plan corrupt — reseeding" in out(r) and out(r).count("=== cycle") == 2


@pytest.mark.parametrize("content,expect", [("0000007", "7"), ("99999999999999999999", "7"), ("1e3", "7"), ("७", "7"), ("8\r\n", "8")])
def test_more_stage_encodings(env, content, expect):
    env.state.mkdir(parents=True); (env.state / "reintro-stage.txt").write_text(content)
    r = env.run()
    assert r.returncode == 0, out(r)
    assert str(int(env.stage())) == expect


@pytest.mark.parametrize("plan", ['{"core":["a","a"],"batches":[["b"]]}', '{"core":["a"],"batches":[["a"]]}',
                                  '{"core":["Bad Name"],"batches":[["b"]]}', '{"core":["a"],"batches":[[]]}',
                                  '{"core":["a"],"batches":[["b",3]]}', '{"core":["' + "x" * 65 + '"],"batches":[["b"]]}'])
def test_semantically_bad_plans_reseeded(env, plan):
    env.state.mkdir(parents=True); (env.state / "reintro-plan.json").write_text(plan)
    r = env.run()
    assert r.returncode == 0 and "plan corrupt" in out(r)


# ---------------- lanes misbehaving ----------------

def test_hanging_lane_times_out_and_loop_continues(env):
    env.flag("chat_hang")
    t0 = time.time()
    r = env.run(LANES="tools robust", LANE_TIMEOUT="2", SMOKE_TIMEOUT="2")
    assert r.returncode == 0, out(r)
    assert "lane tools rc=124 (timed out)" in out(r) and "lane robust rc=124 (timed out)" in out(r)
    assert "HOLD" in out(r) and (env.repo / "MERGE_ME.md").exists()
    assert time.time() - t0 < 60


def test_crashing_lane_does_not_stop_the_loop(env):
    env.flag("chat_crash")
    r = env.run(LANES="tools", CYCLES="2", ONCE="0")
    assert r.returncode == 0, out(r)
    assert out(r).count("lane tools rc=3") == 2 and "done." in out(r)


def test_perl_timeout_fallback_when_no_timeout_binary(env, tmp_path):
    if shutil.which("perl") is None:
        pytest.skip("no perl")
    env.flag("chat_hang")
    p = path_without(tmp_path, "timeout", "gtimeout", "brew")
    r = env.run(PATH=f"{env.bin}:{p}", LANES="tools", LANE_TIMEOUT="2", SMOKE_TIMEOUT="2")
    assert r.returncode == 0, out(r)
    assert "lane tools rc=124" in out(r)


# ---------------- git / working tree ----------------

def test_dirty_tree_blocks_patching_and_preserves_edits(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["unpatched"])
    t = e.repo / "avrone/trinity.py"
    t.write_text(t.read_text() + "\n# MY PRECIOUS UNCOMMITTED EDIT\n")
    r = e.run()
    assert r.returncode == 1 and "uncommitted changes" in out(r)
    assert "MY PRECIOUS UNCOMMITTED EDIT" in t.read_text()
    assert "def coerce_args" not in t.read_text()
    e.kill_server()


def test_dirty_tree_fine_when_no_patch_needed(env):
    t = env.repo / "README.md"; t.write_text(t.read_text() + "\nlocal note\n")
    r = env.run()
    assert r.returncode == 0 and "local note" in t.read_text()


def test_git_missing(env, tmp_path):
    r = env.run(PATH=f"{env.bin}:{path_without(tmp_path, 'git')}")
    assert r.returncode == 1 and "git is required" in out(r)


# ---------------- paths ----------------

def test_repo_path_with_spaces_and_unicode(tmp_path, base_repos):
    e = Env(tmp_path, base_repos["patched"], drive_name="Drïve ü 2", repo_name="my repö dir")
    r = e.run(LANES="tools")
    assert r.returncode == 0, out(r)
    assert 'AVRONE_REPO_DIR="' + str(e.repo) + '"' in (e.state / "env.sh").read_text()
    assert "lane tools rc=0" in out(r)
    e.kill_server()


def test_symlinked_drive(env, tmp_path):
    link = tmp_path / "Volumes-AI"; link.symlink_to(env.drive)
    r = env.run(DRIVE=str(link))
    assert r.returncode == 0, out(r)
    assert (env.drive / "avrone-state" / "env.sh").exists()


# ---------------- config parsing ----------------

@pytest.mark.parametrize("ctx,ok", [("1024", False), ("99999999", False), ("2048", True), ("0", False)])
def test_ctx_bounds(env, ctx, ok):
    r = env.run(CTX=ctx)
    assert (r.returncode == 0) == ok, out(r)


@pytest.mark.parametrize("fmt", ["http://127.0.0.1:{p}", "http://127.0.0.1:{p}/", "127.0.0.1:{p}/v1"])
def test_ollama_host_formats(env, fmt):
    r = env.run(OLLAMA_HOST=fmt.format(p=env.port))
    assert r.returncode == 0, out(r)
    assert f'AVRONE_LLM_BASE_URL="http://127.0.0.1:{env.port}/v1"' in (env.state / "env.sh").read_text()


def test_model_prefix_is_not_a_match(env):
    (env.stub / "models").write_text("stub-model-big\n")
    r = env.run()
    assert r.returncode == 0 and "ollama pull stub-model" in env.calls()


def test_model_with_regex_chars_matched_literally(env):
    (env.stub / "models").write_text("a.b+c:1\n")
    r = env.run(MODEL="a.b+c:1")
    assert r.returncode == 0 and "ollama pull" not in env.calls()


def test_low_ram_warns_for_big_model(env):
    (env.stub / "models").write_text("gpt-oss:120b\n")
    r = env.run(MODEL="gpt-oss:120b", AUTOPILOT_RAM_GB="16")
    assert r.returncode == 0 and "wants ~72GB RAM" in out(r)


def test_enough_ram_no_warning(env):
    (env.stub / "models").write_text("gpt-oss:120b\n")
    r = env.run(MODEL="gpt-oss:120b", AUTOPILOT_RAM_GB="128")
    assert r.returncode == 0 and "RAM" not in out(r)


# ---------------- signals ----------------

def _start_and_wait_cycle(env, **kw):
    p = subprocess.Popen(["bash", str(env.repo / SCRIPT_REL)], env=env.env(CYCLES="0", ONCE="0", CYCLE_SLEEP="60", **kw),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 90; log = ""
    while time.time() < deadline and "summary written" not in log:
        time.sleep(0.3)
        runs = list((env.drive / "avrone-runs").glob("*/autopilot.log"))
        log = runs[0].read_text() if runs else ""
    assert "summary written" in log
    return p


def test_sigint_exits_130_and_cleans(env):
    p = _start_and_wait_cycle(env)
    p.send_signal(signal.SIGINT); p.communicate(timeout=20)
    assert p.returncode == 130 and not (env.state / ".autopilot.lock").exists()


def test_sighup_is_survived(env):
    p = _start_and_wait_cycle(env)
    p.send_signal(signal.SIGHUP); time.sleep(1.5)
    assert p.poll() is None, "terminal closing must not kill the autopilot"
    p.send_signal(signal.SIGTERM); o, _ = p.communicate(timeout=20)
    assert p.returncode == 143 and "SIGHUP" in o


# =====================================================================================
# Round 3: hostile environment
# =====================================================================================

# ---------------- injection / input validation ----------------

@pytest.mark.parametrize("model", ['x"; touch /tmp/pwned_avrone; "', "a b", "$(id)", "m`id`", "a;b", "x\ny"])
def test_model_injection_rejected(env, model):
    Path("/tmp/pwned_avrone").unlink(missing_ok=True)
    r = env.run(MODEL=model)
    assert r.returncode == 1 and ("illegal characters" in out(r) or "MODEL" in out(r)), out(r)
    assert not Path("/tmp/pwned_avrone").exists()
    assert not (env.state / "env.sh").exists()


@pytest.mark.parametrize("model", ["gpt-oss:120b", "qwen3-coder-next:q4_K_M", "hf.co/org/Model-GGUF:Q4_K_M", "a.b+c:1"])
def test_legit_model_names_accepted(env, model):
    (env.stub / "models").write_text(model + "\n")
    r = env.run(MODEL=model, AUTOPILOT_RAM_GB="512")
    assert r.returncode == 0, out(r)
    assert f'AVRONE_LLM_MODEL="{model}"' in (env.state / "env.sh").read_text()


@pytest.mark.parametrize("lanes", ["tools; rm -rf /", "bogus", "tools $(id)"])
def test_bad_lanes_rejected(env, lanes):
    r = env.run(LANES=lanes)
    assert r.returncode == 1 and "unknown lane" in out(r)


@pytest.mark.parametrize("host", ['127.0.0.1:1"; id; "', "h ost:1", "$(id):1"])
def test_ollama_host_injection_rejected(env, host):
    r = env.run(OLLAMA_HOST=host)
    assert r.returncode == 1 and "illegal characters" in out(r)


def test_drive_root_rejected(env):
    r = env.run(DRIVE="/")
    assert r.returncode == 1 and ("filesystem root" in out(r) or "not writable" in out(r))


def test_drive_inside_repo_rejected(env):
    d = env.repo / "scratch-drive"; d.mkdir()
    r = env.run(DRIVE=str(d))
    assert r.returncode == 1 and "inside the repo" in out(r)
    assert not (d / "avrone-state").exists()


def test_drive_symlink_into_repo_rejected(env, tmp_path):
    link = tmp_path / "sneaky"; link.symlink_to(env.repo)
    r = env.run(DRIVE=str(link))
    assert r.returncode == 1 and "inside the repo" in out(r)


def test_drive_is_home_rejected(env):
    r = env.run(HOME=str(env.drive))
    assert r.returncode == 1 and "home folder" in out(r)


def test_root_refused_unless_allowed(env):
    (env.bin / "id").write_text('#!/bin/sh\n[ "$1" = -u ] && echo 0 || /usr/bin/id "$@"\n'); (env.bin / "id").chmod(0o755)
    r = env.run()
    assert r.returncode == 1 and "do not run as root" in out(r)
    r = env.run(ALLOW_ROOT="1")
    assert r.returncode == 0, out(r)


# ---------------- the drive misbehaving ----------------

def test_drive_unplugged_mid_run_stops_cleanly(env):
    env.flag("chat_unplug")
    r = env.run(LANES="tools", CYCLES="3", ONCE="0", timeout=120)
    assert r.returncode == 1 and "disappeared or was swapped" in out(r)
    assert not env.drive.exists(), "must not recreate the unplugged mount point"
    assert out(r).count("=== cycle") == 1


def test_drive_swapped_mid_run_detected(env):
    # another drive with the same mount name: token no longer matches
    env.flag("chat_smoke_pass")
    p = subprocess.Popen(["bash", str(env.repo / SCRIPT_REL)], env=env.env(CYCLES="0", ONCE="0", CYCLE_SLEEP="3"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 90
    while time.time() < deadline and not (env.state / ".autopilot.lock" / "token").exists():
        time.sleep(0.2)
    time.sleep(1)
    (env.state / ".autopilot.lock" / "token").write_text("someone-elses-drive")
    o, _ = p.communicate(timeout=90)
    env.kill_server()
    assert p.returncode == 1 and "disappeared or was swapped" in o


def test_state_goes_readonly_while_advancing(env):
    env.flag("chat_smoke_pass"); env.flag("chat_readonly_state")
    try:
        r = env.run()
    finally:
        os.chmod(env.state, 0o755)
    assert r.returncode == 0, out(r)
    assert "could not save stage" in out(r) and env.stage() == "7"


def test_stage_path_is_a_directory(env):
    (env.state / "reintro-stage.txt").mkdir(parents=True)
    r = env.run()
    assert r.returncode == 0, out(r)
    assert "directory or unreadable" in out(r) and env.stage() == "7"


def test_stage_file_unreadable(env):
    env.state.mkdir(parents=True); f = env.state / "reintro-stage.txt"; f.write_text("8"); f.chmod(0)
    try:
        r = env.run()
    finally:
        for g in env.state.glob("reintro-stage.txt*"):
            g.chmod(0o644)
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    assert r.returncode == 0, out(r)
    assert "unreadable" in out(r) and env.stage() in {"7", "8"}


def test_plan_path_is_a_directory(env):
    (env.state / "reintro-plan.json").mkdir(parents=True)
    r = env.run()
    assert r.returncode == 0, out(r)
    assert (env.state / "reintro-plan.json").is_file()


def test_stale_tmp_files_from_killed_write_ignored(env):
    env.state.mkdir(parents=True)
    (env.state / "reintro-stage.txt").write_text("8")
    (env.state / "reintro-stage.txt.tmp.99999").write_text("garbage")
    r = env.run()
    assert r.returncode == 0 and env.stage() == "8"


# ---------------- ollama misbehaving ----------------

def test_our_own_hung_ollama_is_killed_and_restarted(env):
    env.flag("serve_hang_after", "2")
    r = env.run(CYCLES="2", ONCE="0", timeout=150)
    assert r.returncode == 0, out(r)
    assert "ollama is down — restarting" in out(r)


def test_foreign_hung_listener_detected_not_waited_forever(env):
    import socket
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", env.port)); srv.listen(16)   # accepts connections, never answers
    try:
        t0 = time.time()
        r = env.run(timeout=120)
    finally:
        srv.close()
    assert r.returncode == 1 and "does not answer (hung ollama?)" in out(r), out(r)
    assert time.time() - t0 < 60


def test_warmup_hang_times_out(env):
    env.flag("run_hang")
    t0 = time.time()
    r = env.run(WARM_TIMEOUT="3")
    assert r.returncode == 1 and "no output on warm-up within 3s" in out(r)
    assert time.time() - t0 < 60


def test_model_deleted_mid_run_is_repulled(env):
    env.flag("chat_remove_model")
    r = env.run(LANES="tools", CYCLES="2", ONCE="0")
    assert r.returncode == 0, out(r)
    assert "ollama pull stub-model" in env.calls() and "pulling stub-model" in out(r)


def test_model_deleted_and_disk_full_mid_run(env):
    env.flag("chat_remove_model")
    r = env.run(LANES="tools", CYCLES="2", ONCE="0", MIN_FREE_GB="999999")
    assert r.returncode == 1 and "not enough space" in out(r)


# ---------------- floods ----------------

def test_output_flood_from_lanes_is_contained(env):
    env.flag("chat_flood")
    r = env.run(LANES="tools robust")
    assert r.returncode == 0, out(r)
    assert len(r.stdout) < 200_000, "lane output must go to files, not the terminal"
    run = next((env.drive / "avrone-runs").iterdir())
    assert (run / "tools.txt").stat().st_size >= 3_000_000
