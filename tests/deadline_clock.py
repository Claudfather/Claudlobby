"""One clock for the plane owner HTTP deadline tests (#2388, #2406, #2409, #2412).

These doors end a response deadline with the event loop's timers and check it
against their module's time.monotonic(). A test that shortens the deadline so it
passes quickly races the real work it holds: on a slow runner the deadline
answers before the held call is entered. move_clock() keeps the production
deadline and moves both clocks past it at the step the scenario names.
"""
import asyncio
import contextlib
import threading
import time
import types


async def bounded(awaitable, what, state, seconds=5):
    """Wait at most `seconds` of real time, so a wait that cannot end fails with its
    state, read before the wait is cancelled. Real time, because move_clock's
    paused() stops the loop's own timers."""
    task = asyncio.ensure_future(awaitable)
    expired = threading.Event()
    timer = asyncio.get_running_loop().run_in_executor(None, expired.wait, seconds)
    try:
        await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        if not task.done():
            raise AssertionError(f"waited {seconds} s for {what}: {state()}")
        return task.result()
    finally:
        # Not awaited: returning a thread hop later would give other tasks time to
        # finish work that a check after this wait should still find pending (#2406).
        expired.set()
        timer.cancel()
        task.cancel()


def status(task):
    """pending, done or cancelled: a cancelled task is done() as well."""
    return "cancelled" if task.cancelled() else "done" if task.done() else "pending"


def move_clock(monkeypatch, *modules):
    """Put the event loop's timers and the named modules' deadline reads on one
    clock, real time plus an offset. Only those modules' time.monotonic() moves:
    their other `time` attributes are the real ones, and a module not named keeps
    the real clock. advance(seconds) adds to the offset, so a deadline passes where
    the scenario says, not where a stall puts it. Inside paused() the clock stands
    still. Past its deadline the answer needs no clock time, nor the drain beyond
    the work it waits for, so one that needs some never ends and fails its bound;
    a stall shorter than the bound cannot fail it (#2406)."""
    offset, still = [0.0], [None]
    def now():
        held = still[0]  # read once: a worker thread can call this as paused() exits (#2413)
        return (time.monotonic() if held is None else held) + offset[0]
    clock = types.SimpleNamespace(**vars(time))
    clock.monotonic = now
    monkeypatch.setattr(asyncio.get_running_loop(), "time", now)
    for module in modules:
        monkeypatch.setattr(module, "time", clock)
    def advance(seconds):
        offset[0] += seconds
    @contextlib.contextmanager
    def paused():
        still[0] = time.monotonic()
        try:
            yield
        finally:
            still[0] = None
    return advance, paused
