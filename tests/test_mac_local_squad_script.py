"""Static safety contract tests for the manual Apple-Silicon squad launcher."""
from pathlib import Path


def test_mac_local_squad_launcher_fails_closed_before_lanes():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "mac-local-squad.sh").read_text(encoding="utf-8")
    assert "Python 3.11 or 3.12 is required" in script
    assert "could not create .venv" in script
    assert "if ! pip install -q -e" in script
    assert "install.log" in script
    assert "LANE_TIMEOUT must be between 60 and 43200 seconds" in script
    assert "Unknown lane:" in script


def test_mac_local_squad_propagates_every_lane_failure():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "mac-local-squad.sh").read_text(encoding="utf-8")
    assert 'pids=("$!")' in script
    assert 'for pid in "${pids[@]}"; do' in script
    assert 'wait "$pid"' in script
    assert 'if [ "$lane_status" -ne 0 ]; then' in script
    assert 'exit "$lane_status"' in script



def test_mac_local_squad_uses_unique_run_directory_and_real_timeout_wrapper():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "mac-local-squad.sh").read_text(encoding="utf-8")
    assert 'OUT="$DRIVE/avrone-runs/round5-$(date +%Y%m%d-%H%M%S)-$$"' in script
    assert 'timeout() { command gtimeout "$@"; }' in script
    assert "alias timeout=gtimeout" not in script
