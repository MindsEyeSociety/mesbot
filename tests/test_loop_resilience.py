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
