"""v0.20 robustness: signed dispatch gateway + governor edge cases (offline).

Each test pins a bug fixed in this round (or proves an edge already holds):
  * signed-header shape: non-ASCII / signed / padded / newline-terminated timestamps, unbounded or
    junk nonces, non-ASCII signatures (compare_digest raised TypeError -> 500)
  * signature window boundaries and replay at the window edge under clock skew
  * replay-cache growth bound (fails closed, never evicts a live nonce)
  * malformed signed bodies: deep nesting (RecursionError -> 500), NUL/unicode/very long ids,
    non-integer hop counts (Infinity -> OverflowError -> 500, 1.9 silently truncated)
  * the privileged tier stays deny-by-default under every malformed variant
  * governor: NaN/inf thresholds disabled the thermal guard; backwards clock pinned the cache
"""
import json
import math
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod
from avrone.fleet import FleetManager, GatewayRefused, WINDOW_S, sign
from avrone.governor import Governor

PATH = "/api/fleet/run"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT",
              "AVRONE_PUBLIC_URL", "AVRONE_FLEET_HEARTBEAT", "AVRONE_NODE_ID", "AVRONE_FLEET_REQUIRE_ALLOW",
              "AVRONE_FLEET_PRIVILEGED", "AVRONE_PRIVILEGED_NODES", "AVRONE_FLEET_PRIVILEGED_MAX_HOPS",
              "AVRONE_THERMAL_MAX_C"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)


@pytest.fixture
def core():
    c = MagicMock()
    c.active = {}
    c.codex.emit = MagicMock()
    return c


@pytest.fixture
def fm(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "edge")
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", "router")
    return FleetManager(core, tmp_path)


@pytest.fixture
def clock(monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(fleet_mod.time, "time", lambda: now[0])
    return now


def good(**over):
    return {"route": [], "mode": "chat", "prompt": "hi", "hops": 1, "from": "router", **over}


def gw(fm, payload=None, body=None, **kw):
    if body is None:
        body = json.dumps(payload).encode()
    h = sign("tok", "POST", PATH, body, **kw)
    return fm.dispatch_gateway("run", "POST", PATH, body, h)


def code_of(fn, *a, **kw):
    with pytest.raises(GatewayRefused) as ei:
        fn(*a, **kw)
    return ei.value.code


# ---------------------------------------------------------------- header shape
@pytest.mark.parametrize("ts_fmt", ["+{}", " {}", "{} ", "{}\n", "0{}", "{}_", "\u0661{}", "{}.0"])
def test_timestamp_must_be_plain_ascii_digits(fm, clock, ts_fmt):
    ts = ts_fmt.format(int(clock[0]))
    if ts_fmt == "0{}":  # leading zero is still plain digits: accepted (same integer)
        assert gw(fm, good(), ts=ts)["from"] == "router"
        return
    body = json.dumps(good()).encode()
    h = sign("tok", "POST", PATH, body, ts=ts)  # validly signed over the odd string
    assert code_of(fm.dispatch_gateway, "run", "POST", PATH, body, h) == "bad_timestamp"


def test_fullwidth_digit_timestamp_refused(fm, clock):
    ts = "".join(chr(0xFF10 + int(c)) for c in str(int(clock[0])))
    assert int(ts) == int(clock[0])  # int() alone would have accepted it
    body = json.dumps(good()).encode()
    assert code_of(fm.dispatch_gateway, "run", "POST", PATH, body,
                   sign("tok", "POST", PATH, body, ts=ts)) == "bad_timestamp"


def test_huge_timestamp_refused_without_crash(fm, clock):
    body = b"{}"
    h = sign("tok", "POST", PATH, body, ts="9" * 5000)  # int() of >4300 digits raises in py3.11+
    assert code_of(fm.verify, "POST", PATH, body, h) == "bad_timestamp"


@pytest.mark.parametrize("nonce", ["x" * 129, "x" * 100_000, "n\x00n", "n\u00e9", "n n", "n/../n", "nonce\n"])
def test_bad_nonce_refused_and_not_cached(fm, clock, nonce):
    body = json.dumps(good()).encode()
    h = sign("tok", "POST", PATH, body, nonce=nonce)
    e = pytest.raises(GatewayRefused, fm.dispatch_gateway, "run", "POST", PATH, body, h).value
    assert e.code == "bad_nonce" and e.auth
    assert fm._nonces == {}


def test_max_length_nonce_accepted(fm, clock):
    assert gw(fm, good(), nonce="a" * 128)["from"] == "router"


@pytest.mark.parametrize("sig", ["\u00e9" * 64, "\x00" * 64, "", "0" * 64])
def test_non_ascii_or_wrong_signature_is_401_not_500(fm, clock, sig):
    body = b"{}"
    h = sign("tok", "POST", PATH, body)
    h["x-avrone-sig"] = sig
    assert code_of(fm.verify, "POST", PATH, body, h) == "bad_signature"


# ---------------------------------------------------------------- window + replay
@pytest.mark.parametrize("skew,ok", [(WINDOW_S, True), (-WINDOW_S, True), (WINDOW_S + 1, False),
                                     (-WINDOW_S - 1, False)])
def test_window_boundaries(fm, clock, skew, ok):
    ts = str(int(clock[0]) + skew)
    if ok:
        assert gw(fm, good(), ts=ts)
    else:
        body = json.dumps(good()).encode()
        assert code_of(fm.dispatch_gateway, "run", "POST", PATH, body,
                       sign("tok", "POST", PATH, body, ts=ts)) == "stale"


def test_future_skewed_nonce_not_replayable_at_window_edge(fm, clock):
    """Request stamped WINDOW_S in the future: valid until now + 2*WINDOW_S. The old prune
    (seen-time + 2*WINDOW_S, strict) forgot the nonce at exactly that instant -> replay."""
    t0 = clock[0]
    body = b"{}"
    h = sign("tok", "POST", PATH, body, ts=str(int(t0) + WINDOW_S), nonce="skewed")
    fm.verify("POST", PATH, body, h)
    for dt in (1, WINDOW_S, 2 * WINDOW_S):
        clock[0] = t0 + dt
        assert code_of(fm.verify, "POST", PATH, body, h) == "replay"
    clock[0] = t0 + 2 * WINDOW_S + 1  # outside the window now: stale, and the nonce may go
    assert code_of(fm.verify, "POST", PATH, body, h) == "stale"


def test_nonce_survives_backward_clock_step(fm, clock):
    t0 = clock[0]
    body = b"{}"
    h = sign("tok", "POST", PATH, body, ts=str(int(t0)), nonce="stepback")
    fm.verify("POST", PATH, body, h)
    clock[0] = t0 - 30  # NTP steps the clock back: request is in-window again
    assert code_of(fm.verify, "POST", PATH, body, h) == "replay"


def test_nonce_cache_is_bounded_and_fails_closed(fm, clock, monkeypatch):
    monkeypatch.setattr(fleet_mod, "NONCE_CACHE_MAX", 5)
    body = b"{}"
    for i in range(5):
        fm.verify("POST", PATH, body, sign("tok", "POST", PATH, body, nonce=f"n{i}"))
    assert code_of(fm.verify, "POST", PATH, body, sign("tok", "POST", PATH, body, nonce="n5")) == "replay"
    assert len(fm._nonces) == 5 and "n0" in fm._nonces  # nothing live was evicted
    for i in range(5):  # every cached nonce still blocks its replay
        assert code_of(fm.verify, "POST", PATH, body, sign("tok", "POST", PATH, body, nonce=f"n{i}")) == "replay"
    clock[0] += 3 * WINDOW_S  # all expired: capacity comes back
    fm.verify("POST", PATH, body, sign("tok", "POST", PATH, body, nonce="n5"))
    assert list(fm._nonces) == ["n5"]


# ---------------------------------------------------------------- malformed signed bodies
def test_deeply_nested_body_is_bad_body_not_crash(fm, clock):
    body = b"[" * 200_000 + b"]" * 200_000
    assert code_of(gw, fm, body=body) == "bad_body"
    body = b'{"a":' * 100_000 + b"1" + b"}" * 100_000
    assert code_of(gw, fm, body=body) == "bad_body"


@pytest.mark.parametrize("body", [b"\xff\xfe\x00", b"\x00", b"null", b"[1]", b'"s"', b"1e999"])
def test_garbage_bodies_are_bad_body(fm, clock, body):
    assert code_of(gw, fm, body=body) == "bad_body"


@pytest.mark.parametrize("hops", [float("inf"), float("-inf"), float("nan"), 1.9, True, False, None,
                                  "1 ", "\u0661", "1e3", [1], {"n": 1}])
def test_non_integer_hops_refused(fm, clock, hops):
    body = json.dumps(good(hops=hops)).encode()  # json emits Infinity/NaN; json.loads accepts them
    assert code_of(gw, fm, body=body) == "bad_hops"


@pytest.mark.parametrize("hops,expect", [(1, 1), (1.0, 1), ("2", 2), (0, 0)])
def test_integral_hops_still_accepted(fm, clock, hops, expect):
    assert gw(fm, good(hops=hops))["hops"] == hops


@pytest.mark.parametrize("frm", ["router\x00", "rou\u200bter", "ROUTER", "r" * 100_000, "\u0440outer",
                                 ["router"], {"id": "router"}, 7, ""])
def test_odd_sender_ids_never_match_allowlist(fm, clock, frm):
    assert code_of(gw, fm, good(**{"from": frm})) in {"sender_not_allowed", "sender_missing"}


@pytest.mark.parametrize("cap", ["priv:shell\x00", "PRIV:SHELL", "priv:shell ", ["priv:shell"], "spawn:chat\x00",
                                 "x" * 50_000])
def test_odd_capabilities_refused(fm, clock, cap):
    assert code_of(gw, fm, good(capability=cap)) == "capability"


# ---------------------------------------------------------------- privileged stays deny-by-default
@pytest.fixture
def priv_fm(core, tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "edge")
    monkeypatch.setenv("AVRONE_FLEET_ALLOW", "router")
    monkeypatch.setenv("AVRONE_FLEET_PRIVILEGED", "allow")
    monkeypatch.setenv("AVRONE_PRIVILEGED_NODES", "router")
    return FleetManager(core, tmp_path)


@pytest.mark.parametrize("flag", [1, "true", "1", [True], {"x": 1}, None])
def test_privileged_flag_must_be_real_bool(priv_fm, clock, flag):
    assert code_of(gw, priv_fm, good(capability="priv:shell", privileged=flag)) in {"capability",
                                                                                     "privileged_capability"}


@pytest.mark.parametrize("hops", [float("inf"), 1.5, "\u0661", True])
def test_privileged_with_junk_hops_refused_before_gate(priv_fm, clock, hops):
    body = json.dumps(good(capability="priv:shell", privileged=True, hops=hops)).encode()
    assert code_of(gw, priv_fm, body=body) == "bad_hops"
    assert not [c for c in priv_fm.core.codex.emit.call_args_list if c.args[1] == "fleet.privileged.accept"]


def test_privileged_header_with_odd_case_still_refused(priv_fm, clock):
    body = json.dumps(good(capability="priv:shell", privileged=True)).encode()
    h = sign("tok", "POST", PATH, body)
    h["X-AVRONE-PRIVILEGED"] = "1"
    assert code_of(priv_fm.dispatch_gateway, "run", "POST", PATH, body, h) == "privileged_unsigned"


def test_privileged_still_off_by_default(fm, clock):
    assert code_of(gw, fm, good(capability="priv:shell", privileged=True)) == "privileged_disabled"


# ---------------------------------------------------------------- governor
def readers(temp=None, load=None, mem=None, disk=None):
    return {"temp_c": lambda: temp, "load": lambda: load, "mem_pct": lambda: mem, "disk_free_pct": lambda: disk}


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "NaN", "junk", ""])
def test_thermal_max_env_non_finite_falls_back(monkeypatch, bad):
    monkeypatch.setenv("AVRONE_THERMAL_MAX_C", bad)
    g = Governor(4, readers=readers(temp=99.0))
    assert g.thermal_max_c == 85.0 and g.is_hot() and g.target() == 1


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "nan"])
def test_thermal_max_arg_non_finite_falls_back(bad):
    g = Governor(4, thermal_max_c=bad, readers=readers(temp=99.0))
    assert math.isfinite(g.thermal_max_c) and g.is_hot()


@pytest.mark.parametrize("val", [float("nan"), "nan", "garbage", object(), [1], {"t": 1}])
def test_garbage_readings_are_ignored_and_recorded(val):
    g = Governor(4, readers=readers(temp=val, load=val, mem=val, disk=val))
    h = g.read()
    assert h.temp_c is None and h.load is None and set(h.errors) == {"temp_c", "load", "mem_pct", "disk_free_pct"}
    assert g.target() == 4 and g.last_reason == "no readable sensors" and not g.is_hot()


def test_raising_and_missing_readers_never_break_target():
    def boom():
        raise OSError("sensor gone")
    g = Governor(3, readers={"temp_c": boom, "load": lambda: None, "mem_pct": boom, "disk_free_pct": lambda: None})
    assert g.target() == 3 and not g.is_hot() and "OSError" in g.read().errors["temp_c"]


def test_extreme_readings_fail_safe():
    assert Governor(4, readers=readers(temp=float("inf"))).target() == 1
    assert Governor(4, readers=readers(disk=float("-inf"))).target() == 1
    assert Governor(4, readers=readers(temp=float("inf"))).is_hot()


def test_backwards_clock_does_not_pin_stale_cache():
    t = [1000.0]
    temp = [40.0]
    g = Governor(4, readers=readers(temp=None) | {"temp_c": lambda: temp[0]}, ttl_s=2.0, clock=lambda: t[0])
    assert not g.is_hot()
    temp[0], t[0] = 99.0, 500.0  # clock stepped backwards; the old code kept the 40C reading forever
    assert g.is_hot()


def test_target_edge_wants():
    g = Governor(4, readers=readers())
    assert g.target(0) == 1 and g.target(-5) == 1 and g.target(10**9) == 4
