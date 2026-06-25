"""The bot-side cadence that bounds event-role latency.

A member already on the server receives their event role on the next tick of
``check_user_states`` (every 60s); ``daily_task`` is the hourly self-heal net.
These two intervals are the bot half of the documented "~1-2 minute" worst case
from registration to role, so they are pinned here.
"""
import main


def _interval_seconds(loop):
    """Total period of a discord.py ``tasks.Loop`` in seconds.

    Sums the seconds/minutes/hours components so the assertion holds regardless of
    how discord.py chooses to store the interval internally.
    """
    return loop.seconds + loop.minutes * 60 + loop.hours * 3600


def test_check_user_states_runs_every_60_seconds():
    assert _interval_seconds(main.MyClient.check_user_states) == 60


def test_daily_task_runs_hourly():
    assert _interval_seconds(main.MyClient.daily_task) == 3600
