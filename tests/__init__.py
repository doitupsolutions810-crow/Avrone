# Marks `tests` as a package so pytest puts the repo root on sys.path and
# cross-file imports like `from tests.test_evolve_loop import ...` resolve under every
# invocation style (console-script pytest, python -m pytest, IDE runners).
