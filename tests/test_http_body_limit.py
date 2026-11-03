"""Real TCP regression: early 413 must survive a client still sending its body."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest

pytestmark = pytest.mark.integration


def test_oversize_responses_are_complete_and_connection_recovers(tmp_path):
    env = dict(os.environ, AVRONE_ENV_FILE=str(tmp_path / "absent.env"),
               AVRONE_LLM_MODE="mock", AVRONE_RUNNER="local",
               AVRONE_ADMIN_TOKEN="transport-test-token",
               AVRONE_WORKSPACE=str(tmp_path / "workspace"),
               AVRONE_DATA_DIR=str(tmp_path / "data"))
    with socket.socket() as listener, (tmp_path / "server.log").open("w") as log:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        base = "http://127.0.0.1:" + str(listener.getsockname()[1])
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "avrone.server:app", "--fd", str(listener.fileno()),
             "--no-access-log", "--log-level", "warning"],
            cwd=Path(__file__).resolve().parents[1], env=env,
            pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(timeout=3, trust_env=False) as client:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        if client.get(base + "/api/status").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert process.poll() is None, (tmp_path / "server.log").read_text()
                    assert time.monotonic() < deadline, "server did not become ready"
                    time.sleep(.05)
                # Repeated requests reuse a real keep-alive connection. The
                # original Connection: close response raced unread TCP data.
                for size in (4097, 65537, 4097):
                    response = client.post(base + "/api/blackboard", content=b"x" * size)
                    assert response.status_code == 413
                    assert response.json()["detail"] == "request body too large (max 4096 bytes)"
                    assert client.get(base + "/api/status").status_code == 200
                chunked = client.post(base + "/api/blackboard",
                                      content=iter((b"a" * 2000, b"b" * 3000, b"tail")))
                assert chunked.status_code == 413
                assert chunked.json()["detail"] == "request body too large (max 4096 bytes)"
                accepted = client.post(base + "/api/blackboard", json={"id": "after-413", "text": "alive"},
                                       headers={"X-Admin-Token": "transport-test-token"})
                assert accepted.status_code == 200
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
