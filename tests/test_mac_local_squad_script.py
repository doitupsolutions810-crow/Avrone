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



def test_mac_local_squad_bounds_setup_and_baseline_stages():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "mac-local-squad.sh").read_text(encoding="utf-8")
    assert 'timeout "$LANE_TIMEOUT" ollama pull "$MODEL"' in script
    assert "timeout 900 ollama run" in script
    assert 'timeout 1800 pip install' in script
    assert 'timeout "$LANE_TIMEOUT" python -m pytest' in script



def test_mac_local_squad_enforces_two_accepted_proposals_per_lane():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "mac-local-squad.sh").read_text(encoding="utf-8")
    assert 'python -m avrone.cli evolve --json "$proposal_report"' in script
    assert 'accepted_count -lt 2' in script
    assert 'AVRONE_DATA_DIR="$lane_data"' in script
    assert 'exit 1' in script
    assert "python -m avrone.cli evolve list" not in script



def test_mac_local_squad_rejects_duplicate_lane_names():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "mac-local-squad.sh").read_text(encoding="utf-8")
    assert 'seen_lanes=" "' in script
    assert 'Duplicate lane: $selected_lane' in script


def test_mac_local_squad_counts_only_current_run_proposals():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "mac-local-squad.sh").read_text(encoding="utf-8")
    assert 'RUN_STARTED_EPOCH="$(python -c' in script
    assert 'r["ts"] >= started' in script
    assert 'accepted_count -lt 2' in script


def test_mac_local_squad_bounds_brew_install_stages():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "mac-local-squad.sh").read_text(encoding="utf-8")
    assert "timeout 900 brew install coreutils" in script
    assert "timeout 1800 brew install ollama" in script
