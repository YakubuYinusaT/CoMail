"""Idle backoff and the wake that makes it free.

The backoff exists so an always-on instance stops querying Neon every few
seconds. It is only acceptable because enqueue() wakes the loop directly,
so these two are tested together: if the wake stops working, a job could
sit for a minute and nobody would notice until someone timed it.

No database here. run_one is replaced, so what is under test is the
waiting, not the work.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app import worker


@pytest.fixture
def quiet_worker(monkeypatch):
    """A loop that never finds anything, so it always backs off."""
    monkeypatch.setattr(worker, "IDLE_MIN", 0.05)
    monkeypatch.setattr(worker, "IDLE_MAX", 5.0)
    monkeypatch.setattr(worker, "reclaim_stale", lambda: 0)

    calls = {"n": 0}

    async def nothing_to_do():
        calls["n"] += 1
        return False

    monkeypatch.setattr(worker, "run_one", nothing_to_do)
    return calls


async def _run_briefly(seconds: float):
    stop = asyncio.Event()
    task = asyncio.create_task(worker.loop(stop))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


def test_idle_polling_backs_off(quiet_worker):
    """Doubling from 0.05s must not manage many passes in a third of a second."""
    asyncio.run(_run_briefly(0.35))
    # 0.05 + 0.1 + 0.2 already exceeds the window, so four claims at most.
    assert quiet_worker["n"] <= 4, f"polled {quiet_worker['n']} times, not backing off"


def test_nudge_wakes_the_loop_from_another_thread(quiet_worker):
    """enqueue() runs in a request thread, so the wake must cross threads."""

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.loop(stop))
        await asyncio.sleep(0.4)          # let the backoff stretch out
        before = quiet_worker["n"]

        started = time.monotonic()
        threading.Thread(target=worker.nudge).start()
        while quiet_worker["n"] == before and time.monotonic() - started < 2:
            await asyncio.sleep(0.01)
        woke_after = time.monotonic() - started

        stop.set()
        await asyncio.wait_for(task, timeout=2)
        return quiet_worker["n"] > before, woke_after

    woke, delay = asyncio.run(scenario())
    assert woke, "nudge did not wake the loop"
    assert delay < 0.5, f"took {delay:.2f}s to wake, backoff was not interrupted"


def test_nudge_before_the_loop_exists_is_harmless():
    """enqueue() is also called from the CLI, where no loop is running."""
    worker._wake = worker._wake_loop = None
    worker.nudge()
