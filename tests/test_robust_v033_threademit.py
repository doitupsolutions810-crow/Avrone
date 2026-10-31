"""v0.33: codex events emitted from worker threads reach live subscribers at once. Before: emit()
called asyncio.Queue.put_nowait from the runner/fleet threads; asyncio.Queue is not thread-safe and
that put never woke the event loop, so a waiting /ws/events pump saw the event only when something
else happened on the loop (measured: a thread emit at 0.1 s arrived at 3.0 s, when a timer fired)."""
import asyncio
import threading
import time

from avrone.codex import Codex


def test_thread_emit_wakes_waiting_subscriber(tmp_path):
    async def main():
        cx = Codex(tmp_path)
        q = cx.subscribe()
        t0 = time.monotonic()
        threading.Timer(0.05, lambda: cx.emit("r", "x", "a", n=1)).start()
        ev = await asyncio.wait_for(q.get(), 5)
        return ev, time.monotonic() - t0
    ev, dt = asyncio.run(main())
    assert ev.data == {"n": 1} and dt < 1.5


def test_loop_emit_still_direct_and_counts_drops(tmp_path):
    async def main():
        cx = Codex(tmp_path)
        q = cx.subscribe()
        for i in range(q.maxsize + 3):
            cx.emit("r", "x", "a", i=i)
        return cx, q
    cx, q = asyncio.run(main())
    assert q.qsize() == q.maxsize and cx.dropped == 3


def test_thread_emit_counts_drops_on_full_queue(tmp_path):
    async def main():
        cx = Codex(tmp_path)
        q = cx.subscribe()
        for _ in range(q.maxsize):
            q.put_nowait(None)
        th = threading.Thread(target=lambda: cx.emit("r", "x", "a"))
        th.start()
        th.join()
        await asyncio.sleep(0.05)
        return cx
    assert asyncio.run(main()).dropped == 1


def test_subscriber_of_closed_loop_is_dropped(tmp_path):
    cx = Codex(tmp_path)

    async def sub():
        return cx.subscribe()
    q = asyncio.run(sub())  # its loop is closed once run() returns
    cx.emit("r", "x", "a")
    assert q not in cx._subscribers and q not in cx._loops


def test_closed_between_check_and_call(tmp_path):
    cx = Codex(tmp_path)

    class Racy:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, *a):
            raise RuntimeError("Event loop is closed")
    q = asyncio.Queue()
    cx._subscribers.add(q)
    cx._loops[q] = Racy()
    cx.emit("r", "x", "a")
    assert q not in cx._subscribers


def test_subscribe_outside_loop_and_unsubscribe(tmp_path):
    cx = Codex(tmp_path)
    q = cx.subscribe()  # no running loop: plain put, as before
    cx.emit("r", "x", "a")
    assert q.qsize() == 1 and q not in cx._loops
    cx.unsubscribe(q)
    assert not cx._subscribers
