import pytest

from avrone.dispatch import (
    MAX_MESSAGE_CHARS,
    MAX_PROMPT_CHARS,
    validate_message_payload,
    validate_node_url,
    validate_route,
    validate_run_payload,
)


def test_valid_run_payload_is_normalized():
    result = validate_run_payload(
        {"route": ["router1", "edge2"], "hops": 0, "mode": "squad", "prompt": "review",
         "n": 4, "from": "root1", "origin_run": "run-12"},
        max_hops=3,
    )
    assert result["route"] == ["router1", "edge2"]
    assert result["n"] == 4


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"prompt": "x", "mode": "unsupported"}, "mode must be one of"),
        ({"prompt": ""}, "non-empty string"),
        ({"prompt": "x", "hops": True}, "hops must be an integer"),
        ({"prompt": "x", "n": True}, "n must be an integer"),
        ({"prompt": "x", "n": 9}, "n must be between"),
        ({"prompt": "x", "route": "edge2"}, "route must be a list"),
        ({"prompt": "x", "route": ["edge2", "edge2"]}, "cannot visit a node twice"),
        ({"prompt": "x", "route": ["invalid/id"]}, "invalid node ID"),
        ({"prompt": "x", "route": ["a", "b"], "hops": 2}, "configured hop limit"),
        ({"prompt": "x" * (MAX_PROMPT_CHARS + 1)}, "prompt exceeds"),
        ({"prompt": "x", "from": "invalid/id"}, "valid node ID"),
        ({"prompt": "x", "origin_run": "bad\nvalue"}, "printable characters"),
    ],
)
def test_run_payload_rejects_invalid_shape_and_bounds(payload, reason):
    with pytest.raises(ValueError, match=reason):
        validate_run_payload(payload, max_hops=3)


def test_route_requires_valid_ids_and_bounded_length():
    assert validate_route([], max_hops=3) == []
    with pytest.raises(ValueError, match="invalid node ID"):
        validate_route([1], max_hops=3)
    with pytest.raises(ValueError, match="hop limit"):
        validate_route(["a", "b", "c", "d"], max_hops=3)


def test_message_payload_is_strict_and_bounded():
    good = {"route": ["edge2"], "hops": 0, "agent": "Builder-1",
            "message": "inspect test failures", "from": "root1", "wait": False}
    assert validate_message_payload(good, max_hops=3)["agent"] == "Builder-1"
    cases = [
        ({**good, "route": "edge2"}, "route must be a list"),
        ({**good, "wait": "false"}, "wait must be a boolean"),
        ({**good, "agent": "bad\nname"}, "printable name"),
        ({**good, "message": "x" * (MAX_MESSAGE_CHARS + 1)}, "message exceeds"),
    ]
    for payload, reason in cases:
        with pytest.raises(ValueError, match=reason):
            validate_message_payload(payload, max_hops=3)


def test_peer_url_policy(monkeypatch):
    monkeypatch.delenv("AVRONE_FLEET_ALLOW_INSECURE_HTTP", raising=False)
    assert validate_node_url("http://127.0.0.1:8710") == "http://127.0.0.1:8710"
    assert validate_node_url("https://node.example:8710/") == "https://node.example:8710"
    cases = [
        ("ftp://node.example", "http(s)"),
        ("https://user:pass@node.example", "credentials"),
        ("https://node.example/api/fleet", "base URL"),
        ("https://node.example?x=y", "base URL"),
        ("http://10.0.0.4:8710", "must use HTTPS"),
    ]
    for url, reason in cases:
        with pytest.raises(ValueError, match=reason):
            validate_node_url(url)


def test_remote_plain_http_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_ALLOW_INSECURE_HTTP", "true")
    assert validate_node_url("http://10.0.0.4:8710") == "http://10.0.0.4:8710"
