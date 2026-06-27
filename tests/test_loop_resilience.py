"""The background loops must self-recover from a hung iteration.

Each `@tasks.loop` method wraps its body in `asyncio.wait_for`, so if a Discord await hangs
(e.g. during a gateway reconnect) the iteration is cancelled and the loop continues on its next
tick instead of freezing forever. These tests drive the loop *wrappers* (`.coro`) with a body
that sleeps past a tiny monkeypatched timeout and assert the wrapper returns (doesn't raise or
hang) and logs the stall.
"""
import asyncio
import logging
import types

from unittest.mock import AsyncMock

import main


async def _hang():
    await asyncio.sleep(30)  # far longer than the monkeypatched timeout


async def test_check_user_states_recovers_from_a_hung_iteration(monkeypatch, caplog):
    monkeypatch.setattr(main, "CHECK_USER_STATES_TIMEOUT", 0.01)
    fake = types.SimpleNamespace(_check_user_states_once=_hang)

    with caplog.at_level(logging.ERROR, logger="main"):
        await main.MyClient.check_user_states.coro(fake)  # must return, not raise/hang

    assert any("stalled" in r.message for r in caplog.records)


async def test_daily_task_recovers_from_a_hung_iteration(monkeypatch, caplog):
    monkeypatch.setattr(main, "DAILY_TASK_TIMEOUT", 0.01)
    fake = types.SimpleNamespace(_daily_task_once=_hang, wait_until_ready=AsyncMock())

    with caplog.at_level(logging.ERROR, logger="main"):
        await main.MyClient.daily_task.coro(fake)

    assert any("stalled" in r.message for r in caplog.records)


# --- watchdog: heartbeats + the stall decision ------------------------------------------
# The per-iteration timeout above can't cancel a non-cancellable block or revive a stopped
# loop; the watchdog catches those by os._exit-ing when a heartbeat goes stale. These pin the
# pure decision and that each wrapper stamps its heartbeat every pass.

def _hb(cus=None, daily=None):
    return types.SimpleNamespace(_cus_last_ok=cus, _daily_last_ok=daily)


def test_loops_stalled_none_heartbeat_never_stalled():
    # Before a loop's first pass its heartbeat is None — must never trigger a boot restart-loop.
    assert main.MyClient._loops_stalled(_hb(None, None), 10_000.0) is False


def test_loops_stalled_fresh_heartbeats_ok():
    now = 10_000.0
    assert main.MyClient._loops_stalled(_hb(cus=now - 5, daily=now - 5), now) is False


def test_loops_stalled_scan_heartbeat_stale():
    now = 10_000.0
    stale = now - (main.CHECK_USER_STATES_STALL_LIMIT + 1)
    assert main.MyClient._loops_stalled(_hb(cus=stale, daily=now - 5), now) is True


def test_loops_stalled_daily_heartbeat_stale():
    now = 10_000.0
    stale = now - (main.DAILY_TASK_STALL_LIMIT + 1)
    assert main.MyClient._loops_stalled(_hb(cus=now - 5, daily=stale), now) is True


async def test_check_user_states_stamps_heartbeat_on_completion():
    fake = types.SimpleNamespace(_check_user_states_once=AsyncMock())
    await main.MyClient.check_user_states.coro(fake)
    assert isinstance(fake._cus_last_ok, float)


async def test_check_user_states_stamps_heartbeat_after_timeout(monkeypatch):
    # Heartbeat must advance even when the pass timed out — the loop is still alive/recovering.
    monkeypatch.setattr(main, "CHECK_USER_STATES_TIMEOUT", 0.01)
    fake = types.SimpleNamespace(_check_user_states_once=_hang)
    await main.MyClient.check_user_states.coro(fake)
    assert isinstance(fake._cus_last_ok, float)


async def test_daily_task_stamps_heartbeat_on_completion():
    fake = types.SimpleNamespace(_daily_task_once=AsyncMock(), wait_until_ready=AsyncMock())
    await main.MyClient.daily_task.coro(fake)
    assert isinstance(fake._daily_last_ok, float)
