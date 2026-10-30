# Set test env before any avrone module (and its global Settings) is imported.
import os

os.environ["AVRONE_LLM_MODE"] = "mock"
os.environ["AVRONE_RUNNER"] = "local"
os.environ["AVRONE_ADMIN_TOKEN"] = "test-token"
# The health governor reads real host sensors; keep the suite deterministic on a busy CI box.
# tests/test_governor.py turns it on explicitly with injected (fake) readers.
os.environ.setdefault("AVRONE_GOVERNOR", "off")
os.environ.setdefault("AVRONE_RECON_ALLOWLIST", "*")  # v0.21 recon is default-deny; suites exercise any-public
# The learning bridge's cross-examination round and lessons injection change squad LLM traffic;
# keep the classic squad for the suite. tests/test_bridge.py turns them on explicitly.
os.environ.setdefault("AVRONE_BRIDGE_ROUNDS", "0")
os.environ.setdefault("AVRONE_LESSONS", "off")
# v0.27 guards /api/chat, run history and /ws/events to loopback by default; TestClient's peer is
# "testclient", so the suite opts into the old open behaviour. test_robust_v027_access tests the guard.
os.environ.setdefault("AVRONE_PUBLIC_ACCESS", "all")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
