"""Map-like refs must never be silently interpreted as reference identifiers."""
from collections import UserDict

import pytest

from avrone.bridge import Bridge


@pytest.mark.parametrize("refs", [
    {"post-id": "untrusted"},
    {"__proto__": "injection"},
    UserDict({"post-id": "untrusted"}),
    {},
])
def test_dictionary_refs_do_not_impersonate_sequence(tmp_path, refs):
    bridge = Bridge("dict-refs", tmp_path)
    record = bridge.post("agent", "finding", "kept", refs=refs)
    assert record["refs"] == []


def test_actual_list_refs_still_work(tmp_path):
    bridge = Bridge("valid-refs", tmp_path)
    first = bridge.post("agent", "finding", "parent")
    follow = bridge.post("agent", "answer", "reply", refs=[first["id"]])
    assert follow["refs"] == [first["id"]]
