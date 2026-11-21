"""Round comparison uses stored outcomes only."""

from crow_os.compare import compare_rounds


def test_compare_fixed_regressed_and_new_failures():
    left = {
        "id": "a",
        "commit_sha": "1",
        "cases": [
            {"file": "tests/a.py", "name": "test_one", "outcome": "failed"},
            {"file": "tests/a.py", "name": "test_two", "outcome": "passed"},
            {"file": "tests/b.py", "name": "test_keep", "outcome": "failed"},
        ],
    }
    right = {
        "id": "b",
        "commit_sha": "2",
        "cases": [
            {"file": "tests/a.py", "name": "test_one", "outcome": "passed"},
            {"file": "tests/a.py", "name": "test_two", "outcome": "error"},
            {"file": "tests/b.py", "name": "test_keep", "outcome": "failed"},
            {"file": "tests/c.py", "name": "test_new", "outcome": "failed"},
        ],
    }
    report = compare_rounds(left, right)
    assert report["fixed"] == ["tests/a.py::test_one"]
    assert report["regressed"] == ["tests/a.py::test_two"]
    assert report["new_failures"] == ["tests/c.py::test_new"]
    assert report["still_failing"] == ["tests/b.py::test_keep"]
    assert report["pass_delta"] == 0
    assert report["fail_delta"] == 1
    assert report["commit_a"] == "1"
    assert report["commit_b"] == "2"
