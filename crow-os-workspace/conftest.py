"""Keep Avrone's root pytest run from importing this app.

The workspace suite is run from this directory (`python -m pytest`).
The parent checkout's CI invokes pytest at the repository root, where
`crow_os` is not an installed package.
"""

from __future__ import annotations

from pathlib import Path


def pytest_ignore_collect(collection_path, config):
    root = Path(str(config.rootpath)).resolve()
    here = Path(__file__).resolve().parent
    if root == here:
        return None
    path = Path(str(collection_path)).resolve()
    tests = here / "tests"
    if path == tests or tests in path.parents:
        return True
    return None
