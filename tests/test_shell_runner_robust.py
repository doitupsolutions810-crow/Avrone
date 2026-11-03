"""Round 26: the shell runner and its model-facing output. Covers `server &` holding the pipes
(used to stall to the timeout), partial output on timeout, process-group kill, stdin
inheritance, unbounded capture, signal exit labels, and head+tail truncation that keeps the
error at the end, stderr's share, clipped command echo, ANSI and CR progress noise."""
import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import sovereign
from avrone.sovereign import CommandResult, LocalRunner, clean_output, exit_label, _Capture


def run(c):
    return asyncio.run(c)


@pytest.fixture
def runner(tmp_path):
    s = SimpleNamespace(workspace=tmp_path / "ws", command_timeout=10, runner="local",
                        allow_network=False, docker_image="img")
    r = LocalRunner(s)
    run(r.start())
    return r


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return True


# ------------------------------------------------------------------ runner behaviour
def test_background_child_does_not_stall(runner, tmp_path):
    t = time.time()
    res = run(runner.run("sleep 20 & echo $! > bg.pid; echo started", timeout=10))
    took = time.time() - t
    pid = int((runner.workspace / "bg.pid").read_text())
    try:
        assert took < 4 and not res.timed_out and res.exit_code == 0
        assert res.stdout == "started\n"
        assert "background process still holds the output pipe" in res.stderr
    finally:
        os.kill(pid, 9)


def test_background_with_redirect_has_no_note(runner):
    res = run(runner.run("sleep 20 > /dev/null 2>&1 & echo $! > bg.pid; echo ok"))
    os.kill(int((runner.workspace / "bg.pid").read_text()), 9)
    assert res.stdout == "ok\n" and res.stderr == ""


def test_timeout_keeps_partial_output(runner):
    res = run(runner.run("echo first; echo warn >&2; sleep 9", timeout=1))
    assert res.timed_out and res.exit_code == -1
    assert res.stdout == "first\n"
    assert res.stderr.startswith("warn\n") and res.stderr.endswith("command timed out after 1s")


def test_timeout_kills_whole_process_group(runner):
    res = run(runner.run("sleep 30 & echo $! > g.pid; wait", timeout=1))
    assert res.timed_out
    pid = int((runner.workspace / "g.pid").read_text())
    for _ in range(40):
        if not _alive(pid):
            break
        time.sleep(0.05)
    assert not _alive(pid), "grandchild survived the timeout"


def test_stdin_is_not_inherited(runner):
    t = time.time()
    res = run(runner.run("cat; read x; echo done", timeout=5))
    assert time.time() - t < 3 and not res.timed_out and res.stdout == "done\n"


def test_capture_is_bounded(runner):
    res = run(runner.run("yes | head -c 6000000"))
    limit = sovereign.OUTPUT_HEAD_BYTES + sovereign.OUTPUT_TAIL_BYTES + 200
    assert len(res.stdout) <= limit
    assert "bytes of output not kept" in res.stdout and res.stdout.endswith("y\n")


def test_signal_exit_label(runner):
    res = run(runner.run("kill -9 $$"))
    assert res.exit_code == -9 and "[exit -9 SIGKILL]" in res.text()


def test_bash_reported_signal_label(runner):
    res = run(runner.run("bash -c 'kill -9 $$'; exit $?"))
    assert res.exit_code == 137
    assert "[exit 137 = killed by SIGKILL, often out of memory]" in res.text()


def test_non_utf8_output_decodes(runner):
    res = run(runner.run("printf 'caf\\xe9\\n'"))
    assert res.stdout == "caf\ufffd\n"


def test_normal_command_unchanged(runner):
    res = run(runner.run("echo hi; echo err >&2; exit 3"))
    assert (res.exit_code, res.stdout, res.stderr) == (3, "hi\n", "err\n")


def test_cancel_kills_grandchild(runner):
    async def go():
        task = asyncio.ensure_future(runner.run("sleep 30 & echo $! > c.pid; wait", timeout=20))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if (runner.workspace / "c.pid").exists():
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(go())
    pid = int((runner.workspace / "c.pid").read_text())
    for _ in range(40):
        if not _alive(pid):
            break
        time.sleep(0.05)
    assert not _alive(pid)


@pytest.mark.parametrize("val,exp", [("junk", 0.5), ("-3", 0.0), ("99", 10.0), ("0.2", 0.2)])
def test_pipe_grace_env(monkeypatch, val, exp):
    monkeypatch.setenv("AVRONE_SHELL_PIPE_GRACE", val)
    assert sovereign._pipe_grace() == exp


def test_reap_without_pid_or_group():
    class P:
        returncode = None
        def kill(self):
            raise ProcessLookupError
        async def wait(self):
            return 0
    run(sovereign._reap(P()))


# ------------------------------------------------------------------ capture unit
def test_capture_small_is_exact():
    c = _Capture(); c.feed(b"abc"); c.feed(b"def")
    assert c.text() == "abcdef" and c.total == 6


def test_capture_split_multibyte_at_boundary(monkeypatch):
    monkeypatch.setattr(sovereign, "OUTPUT_HEAD_BYTES", 4)
    monkeypatch.setattr(sovereign, "OUTPUT_TAIL_BYTES", 100)
    c = _Capture(); c.feed("aéb".encode() + b"cd")
    assert c.text() == "aébcd"


def test_capture_middle_counted(monkeypatch):
    monkeypatch.setattr(sovereign, "OUTPUT_HEAD_BYTES", 3)
    monkeypatch.setattr(sovereign, "OUTPUT_TAIL_BYTES", 3)
    c = _Capture()
    for ch in (b"aaa", b"bbbb", b"ccc"):
        c.feed(ch)
    assert c.text() == "aaa\n...[4 bytes of output not kept]...\nccc"


# ------------------------------------------------------------------ text() view
def test_tail_error_survives_truncation():
    out = "".join(f"line {i}\n" for i in range(3000)) + "Traceback: ValueError: the real problem\n"
    t = CommandResult("python x.py", 1, out, "").text()
    assert len(t) <= 6000 + len("\n...[truncated]")
    assert "ValueError: the real problem" in t and "line 0\n" in t and "chars cut" in t
    assert t.endswith("...[truncated]")


def test_stderr_keeps_share_under_huge_stdout():
    t = CommandResult("make", 2, "o" * 50000, "error: missing header foo.h\n").text()
    assert "[stderr]\nerror: missing header foo.h" in t


def test_huge_stderr_keeps_its_tail():
    err = "noise\n" * 5000 + "FATAL: disk full\n"
    t = CommandResult("x", 1, "ok\n", err).text()
    assert "ok\n" in t and "FATAL: disk full" in t


def test_stdout_gets_unused_stderr_budget():
    t = CommandResult("x", 1, "a" * 7000, "e").text()
    assert t.count("a") > 5000


def test_long_command_echo_is_clipped():
    cmd = "cat > big.py <<'EOF'\n" + "x = 1\n" * 3000 + "EOF"
    t = CommandResult(cmd, 0, "done\n", "").text()
    assert "command clipped" in t and t.endswith("done\n") and len(t) < 700


def test_tiny_limit_keeps_old_shape():
    t = CommandResult("c", 1, "o" * 100, "bad", timed_out=True).text(limit=40)
    assert t.startswith("$ c\n[exit 1 TIMEOUT]") and t.endswith("...[truncated]")
    assert len(t) <= 40 + len("\n...[truncated]")


def test_ansi_and_progress_cleaned():
    raw = "\x1b[32mOK\x1b[0m\nDownloading  10%\rDownloading  55%\rDownloading 100%\nDone\x1b]0;title\x07\n"
    assert clean_output(raw) == "OK\nDownloading 100%\nDone\n"


def test_crlf_output_kept_as_lines():
    assert clean_output("a\r\nb\r\n") == "a\nb\n"


def test_clean_output_in_text():
    t = CommandResult("pip install x", 0, "\x1b[1m50%\r100%\x1b[0m\n", "").text()
    assert t.endswith("100%\n") and "\x1b" not in t


@pytest.mark.parametrize("code,to,exp", [
    (0, False, "exit 0"), (1, False, "exit 1"), (-1, True, "exit -1 TIMEOUT"), (-1, False, "exit -1"),
    (-15, False, "exit -15 SIGTERM"), (139, False, "exit 139 = killed by SIGSEGV"),
    (130, False, "exit 130 = killed by SIGINT"), (127, False, "exit 127"), (200, False, "exit 200")])
def test_exit_label(code, to, exp):
    assert exit_label(code, to) == exp


# ------------------------------------------------------------------ shell tool
def test_shell_tool_rejects_empty_cmd(tmp_path):
    from avrone.tools import t_shell
    ctx = SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(agents_can_shell=True)))
    for v in ("", "   ", None):
        assert run(t_shell(ctx, {"cmd": v})) == "ERROR: shell needs a non-empty 'cmd'"
