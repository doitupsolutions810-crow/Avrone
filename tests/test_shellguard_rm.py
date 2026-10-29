"""Regression tests for the recursive-remove rule of the opt-in shell guard.

An earlier version only inspected the first target, so wildcard, trailing-slash
and multi-target forms slipped through. Samples are built from fragments so no
complete destructive command appears literally in the source.
"""
import pytest

from avrone import shellguard

RM = "r" + "m"

BLOCKED = [
    f"{RM} -rf /tmp/x ~", f"{RM} -rf /*", f"{RM} -rf $HOME/", f"{RM} -rf ~/", f"{RM} -r -f /",
    f"sudo {RM} -rf /etc", f"echo a; {RM} -rf /", f"{RM} --recursive --force /", f"{RM} -rf '/'",
    f"cd / && {RM} -rf .", f"{RM} -rf /usr/", f"{RM} -Rf ~/*", f"{RM} -rf ${{HOME}}",
    f"{RM} -rf build /", f"nohup {RM} -rf / &",
]

ALLOWED = [
    f"{RM} -rf ./build", f"{RM} -rf /tmp/x", f"{RM} -f a.txt", f"{RM} -rf node_modules dist",
    f"echo {RM} -rf / is bad", f"{RM} -rf workspace/agents/a1", f"{RM} file.txt", f"{RM} -rf build",
    "ls /", "ls -la ~", f"{RM} -f /tmp/lockfile",
]


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AVRONE_SHELL_GUARD", "on")


@pytest.mark.parametrize("cmd", BLOCKED)
def test_rm_blocked(cmd):
    assert shellguard.check(cmd), cmd


@pytest.mark.parametrize("cmd", ALLOWED)
def test_rm_allowed(cmd):
    assert shellguard.check(cmd) is None, cmd
