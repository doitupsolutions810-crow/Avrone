"""v0.31: agent-pack import caps the decompressed stream itself. Before: quota checks ran on
header-declared member sizes, but tarfile reads PAX extended headers into memory first, so a
290 KB bundle with a 300 MB PAX header drove the importer to ~1.2 GB RSS (a 50 MB bundle, still
under the bundle cap, would ask for hundreds of GB). Now: PackError at the cap, RSS ~87 MB."""
import gzip
import io
import tarfile
import tracemalloc

import pytest

from avrone import agentpack as ap


def _pax_bomb(n: int, name: str = "manifest.json", key: str = "comment") -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        ti = tarfile.TarInfo(name)
        ti.pax_headers = {key: "A" * n}
        ti.size = 2
        tf.addfile(ti, io.BytesIO(b"{}"))
    return gzip.compress(raw.getvalue(), 9)


@pytest.fixture
def small_quota(monkeypatch):
    monkeypatch.setattr(ap, "quota_bytes", lambda: 256 * 1024)
    monkeypatch.setattr(ap, "MAX_MEMBERS", 50)


def test_pax_header_bomb_refused_without_buffering_it(small_quota):
    b = _pax_bomb(40_000_000)
    assert len(b) < 100_000  # tiny on the wire
    tracemalloc.start()
    try:
        with pytest.raises(ap.PackError, match="expands past the quota"):
            ap.read_pack(b)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, peak  # was ~40 MB+ (the whole header)


def test_gnu_longname_bomb_refused(small_quota):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tf:
        ti = tarfile.TarInfo("home/" + "a" * 5_000_000)
        ti.size = 0
        tf.addfile(ti, io.BytesIO(b""))
    with pytest.raises(ap.PackError):
        ap.read_pack(gzip.compress(raw.getvalue(), 9))


def test_gzip_of_plain_zeros_refused(small_quota):
    with pytest.raises(ap.PackError):
        ap.read_pack(gzip.compress(b"\0" * 20_000_000, 9))


def test_truncated_and_non_gzip_still_pack_errors(small_quota):
    good = _pax_bomb(10)
    for junk in (good[: len(good) // 2], b"not gzip at all", b""):
        with pytest.raises(ap.PackError):
            ap.read_pack(junk)


def test_capped_stream_exact_boundary():
    s = ap._CappedStream(io.BytesIO(b"x" * 10), 10)
    buf = bytearray(4)
    got = b""
    while (n := s.readinto(buf)):
        got += bytes(buf[:n])
    assert got == b"x" * 10 and s.total == 10 and s.readable()
    s2 = ap._CappedStream(io.BytesIO(b"x" * 11), 10)
    with pytest.raises(ap.PackError):
        while s2.readinto(bytearray(4)):
            pass
