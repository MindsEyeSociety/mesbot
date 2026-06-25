"""Daily-task cleanup of members who hold the role but are no longer authorized.

Covers the bug fixed here: removal must run **per guild** (a member in several servers is
stripped in every one, not just the first encountered), the `unauthorized_users` lookup
must be **guild-scoped**, and the grace-period policy lives in the pure
`_classify_unauthorized` helper so it can be tested without Discord or a database.

The integration tests drive ``daily_task`` on a REAL ``MyClient`` instance (so the helper
methods it calls are the real ones) with only the Discord I/O patched.
"""
import datetime
from unittest.mock import AsyncMock

import discord
import main

G1, R1, G2, R2, USER_ID = 111, 1111, 222, 2222, 999


async def _aiter(items):
    """Minimal async iterator so a fake guild.fetch_members() can be `async for`-ed."""
    for item in items:
        yield item


def _real_client():
    """A real MyClient (real helper methods) with Discord I/O stubbed out.

    `guilds` is a read-only property that returns an empty cache on an unconnected
    client, so the hourly ban/unban loops are naturally no-ops.
    """
    client = main.MyClient(intents=discord.Intents.none())
    client.wait_until_ready = AsyncMock()
    client.log_message = AsyncMock()
    client.on_member_join = AsyncMock()
    client.fetch_user = AsyncMock()
    return client


# --- pure decision helper ---------------------------------------------------------------

def test_classify_no_flag_inserts():
    assert main.MyClient._classify_unauthorized(None, datetime.datetime(2026, 6, 25)) == "insert"


def test_classify_within_grace_waits():
    now = datetime.datetime(2026, 6, 25, 12, 0, 0)
    assert main.MyClient._classify_unauthorized((now - datetime.timedelta(hours=5),), now) == "wait"


def test_classify_one_day_old_removes():
    now = datetime.datetime(2026, 6, 25, 12, 0, 0)
    assert main.MyClient._classify_unauthorized((now - datetime.timedelta(days=1),), now) == "remove"


def test_classify_long_overdue_removes():
    now = datetime.datetime(2026, 6, 25, 12, 0, 0)
    assert main.MyClient._classify_unauthorized((now - datetime.timedelta(days=400),), now) == "remove"


# --- the actual daily_task removal path -------------------------------------------------

async def test_daily_task_strips_unauthorized_in_every_guild(fake_db, discord_factories):
    role1 = discord_factories.role(R1)
    role2 = discord_factories.role(R2)
    member1 = discord_factories.member(USER_ID, roles=[role1])
    member2 = discord_factories.member(USER_ID, roles=[role2])
    guild1 = discord_factories.guild(G1, roles=[role1])
    guild2 = discord_factories.guild(G2, roles=[role2])
    guild1.fetch_members = lambda: _aiter([member1])
    guild2.fetch_members = lambda: _aiter([member2])
    guild1.fetch_member = AsyncMock(return_value=member1)
    guild2.fetch_member = AsyncMock(return_value=member2)

    client = _real_client()
    client.fetch_guild = AsyncMock(side_effect=lambda gid: guild1 if gid == G1 else guild2)

    two_days_ago = datetime.datetime.now() - datetime.timedelta(days=2)
    fake_db.responses = {
        "FROM server_roles": [(G1, R1), (G2, R2)],
        "notified_at FROM unauthorized_users": [(two_days_ago,)],
        # everything else (auth checks, expiry scan, gates) defaults to empty
    }

    await main.MyClient.daily_task.coro(client)

    # Removed in BOTH servers — the old shared-dedup bug would have skipped the second.
    member1.remove_roles.assert_awaited_once_with(role1)
    member2.remove_roles.assert_awaited_once_with(role2)

    # The unauthorized lookup is guild-scoped (regression guard).
    lookups = [s for s in fake_db.sql if "notified_at FROM unauthorized_users" in s]
    assert lookups and all("guild_id" in s for s in lookups)
    # The flag is cleared with a guild-scoped DELETE after each removal.
    assert any("DELETE FROM unauthorized_users WHERE user_id = %s AND guild_id = %s" in s
               for s in fake_db.sql)


async def test_daily_task_waits_within_grace_period(fake_db, discord_factories):
    role1 = discord_factories.role(R1)
    member1 = discord_factories.member(USER_ID, roles=[role1])
    guild1 = discord_factories.guild(G1, roles=[role1])
    guild1.fetch_members = lambda: _aiter([member1])
    guild1.fetch_member = AsyncMock(return_value=member1)

    client = _real_client()
    client.fetch_guild = AsyncMock(return_value=guild1)

    five_hours_ago = datetime.datetime.now() - datetime.timedelta(hours=5)
    fake_db.responses = {
        "FROM server_roles": [(G1, R1)],
        "notified_at FROM unauthorized_users": [(five_hours_ago,)],
    }

    await main.MyClient.daily_task.coro(client)

    member1.remove_roles.assert_not_awaited()  # still inside the grace window


async def test_daily_task_forbidden_keeps_flag_and_reports_permissions(fake_db, discord_factories):
    role1 = discord_factories.role(R1)
    member1 = discord_factories.member(USER_ID, roles=[role1])
    member1.remove_roles.side_effect = discord_factories.http_error(discord.Forbidden)
    guild1 = discord_factories.guild(G1, roles=[role1])
    guild1.fetch_members = lambda: _aiter([member1])
    guild1.fetch_member = AsyncMock(return_value=member1)

    client = _real_client()
    client.fetch_guild = AsyncMock(return_value=guild1)

    two_days_ago = datetime.datetime.now() - datetime.timedelta(days=2)
    fake_db.responses = {
        "FROM server_roles": [(G1, R1)],
        "notified_at FROM unauthorized_users": [(two_days_ago,)],
    }

    await main.MyClient.daily_task.coro(client)

    member1.remove_roles.assert_awaited_once()  # no retry on a permission error
    # Flag is kept (no DELETE) so it retries and re-reports next run.
    assert not any("DELETE FROM unauthorized_users" in s for s in fake_db.sql)
    messages = [call.args[1] for call in client.log_message.await_args_list]
    assert any("permission" in m.lower() for m in messages)


# --- the remove-with-retry helper -------------------------------------------------------

async def test_remove_retry_success_first_try(discord_factories):
    role = discord_factories.role(R1)
    member = discord_factories.member(USER_ID, roles=[role])
    result = await main.MyClient._remove_role_with_retry(member, role, attempts=3, delay=0)
    assert result == "removed"
    member.remove_roles.assert_awaited_once_with(role)


async def test_remove_retry_forbidden_does_not_retry(discord_factories):
    role = discord_factories.role(R1)
    member = discord_factories.member(USER_ID, roles=[role])
    member.remove_roles.side_effect = discord_factories.http_error(discord.Forbidden)
    result = await main.MyClient._remove_role_with_retry(member, role, attempts=3, delay=0)
    assert result == "forbidden"
    assert member.remove_roles.await_count == 1  # permission error reported immediately


async def test_remove_retry_transient_gives_up_after_attempts(discord_factories):
    role = discord_factories.role(R1)
    member = discord_factories.member(USER_ID, roles=[role])
    member.remove_roles.side_effect = discord_factories.http_error(discord.HTTPException)
    result = await main.MyClient._remove_role_with_retry(member, role, attempts=3, delay=0)
    assert result == "api_error"
    assert member.remove_roles.await_count == 3  # retried up to the limit, then reported


async def test_remove_retry_transient_then_succeeds(discord_factories):
    role = discord_factories.role(R1)
    member = discord_factories.member(USER_ID, roles=[role])
    err = discord_factories.http_error(discord.HTTPException)
    member.remove_roles.side_effect = [err, None]  # one transient failure, then success
    result = await main.MyClient._remove_role_with_retry(member, role, attempts=3, delay=0)
    assert result == "removed"
    assert member.remove_roles.await_count == 2


async def test_daily_task_flags_first_time_without_removing(fake_db, discord_factories):
    role1 = discord_factories.role(R1)
    member1 = discord_factories.member(USER_ID, roles=[role1])
    guild1 = discord_factories.guild(G1, roles=[role1])
    guild1.fetch_members = lambda: _aiter([member1])
    guild1.fetch_member = AsyncMock(return_value=member1)

    client = _real_client()
    client.fetch_guild = AsyncMock(return_value=guild1)

    fake_db.responses = {
        "FROM server_roles": [(G1, R1)],
        # no unauthorized_users row yet -> first-time flag, no removal
    }

    await main.MyClient.daily_task.coro(client)

    member1.remove_roles.assert_not_awaited()
    assert any("INSERT INTO unauthorized_users" in s for s in fake_db.sql)


# --- cross-DB membership-validation join guard ------------------------------------------

async def test_membership_validation_joins_are_well_formed(fake_db, discord_factories):
    """Pin the shape of the cross-database membership-validation joins.

    The daily task validates membership by joining `user_authorizations.access_token` to
    `mes-portal`.User.membershipNumber (Section A for expiry, Section B for self-heal). This
    join is mission-critical and lives in inline SQL, so guard it against a silent refactor
    that would break expiry detection. (The 2026-06 "empty join" incident turned out to be a
    query-tool artifact, not a code defect — this locks the real code path regardless.)
    """
    guild = discord_factories.guild(G1, roles=[discord_factories.role(R1)])
    guild.fetch_members = lambda: _aiter([])  # no members; only the validation queries run
    client = _real_client()
    client.fetch_guild = AsyncMock(return_value=guild)
    fake_db.responses = {"FROM server_roles": [(G1, R1)]}

    await main.MyClient.daily_task.coro(client)

    joins = [s for s in fake_db.sql
             if "user_authorizations ua" in s and "`mes-portal`.User u" in s]
    assert joins, "no cross-DB membership-validation join was issued"
    for query in joins:
        assert "ua.access_token = u.membershipNumber" in query
    # Section A keys expiry detection off membershipExpiration through that join.
    assert any("membershipExpiration" in s and "`mes-portal`.User u" in s for s in fake_db.sql)
