---
name: test-coverage-sweep
description: Find AVRONE's weakest-covered modules with pytest --cov, write offline tests for them, pass the gate, and propose them via evolve_propose.
---
1. task_board add one card per phase (measure, write, gate, propose) so other agents see what you are doing.
2. shell `python -m pytest -q -m "not integration" --cov=avrone --cov-report=term-missing` from the repo root.
   If it says "unrecognized arguments: --cov", env_install pytest-cov (pip) and re-run. Note the TOTAL percentage.
3. think: rank modules by missed statements (not just percent). Pick the 2-3 with the most missed lines that hold
   pure logic: parsers, validators, path/arg checks, error branches. Skip code that only wraps a live network or Docker call.
4. read_file each chosen module and search_files tests/ for the module name so you extend, not duplicate, what exists.
5. write_file new tests under tests/ (e.g. tests/test_<module>_cov.py). Rules: offline only. No real network, sockets,
   Docker or LLM calls: use tmp_path, monkeypatch, SimpleNamespace contexts and fake transports, as tests/test_agentkit.py does.
   Test behaviour and error strings, not private implementation details. Never edit existing tests to make them pass.
6. shell `python -m pytest -q <new test file>` until it passes, then the full gate
   `python -m pytest -q -m "not integration"`. If a new test exposes a real bug, keep the test and fix the code minimally.
   If the same failure repeats more than twice, stop and report it instead of looping.
7. shell the coverage command again and record before -> after for each module and the TOTAL.
8. evolve_propose with goal "coverage: <modules> <before>% -> <after>%" and edits = every new or changed file, each
   with its FULL contents. Then evolve_list to confirm it passed; evolve_diff it to double-check the change set.
9. memory_save key "coverage.last_sweep" with the date, modules and numbers; artifact_save a short report.
10. Report: modules targeted, tests added (count and file names), coverage before/after, bugs found, and the proposal id.
    Never push, bump versions, or touch the live tree: evolve_propose is the only way your change leaves this run.
