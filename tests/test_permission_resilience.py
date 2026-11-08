"""A Discord permission/API error affecting one channel/member/guild must not abort a whole pass.

Production incident (2026-10-09): in one guild the bot could neither manage a role nor post in the
configured logging channel. The second ``Forbidden`` escaped ``log_message``, killed
``_daily_task_once`` and skipped every later guild for the day. These tests drive the REAL
``log_message``, ``_daily_task_once`` and ``_check_user_states_once`` on a real ``MyClient`` with
only Discord I/O and the database faked.
"""
import datetime
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import mysql.connector
import pytest

import main

G1, R1, G2, R2 = 111, 1111, 222, 2222
U1, U2 = 901, 902
LOG_CHANNEL = 5555


async def _aiter(items):
    for item in items:
        yield item


def _real_client():
    client = main.MyClient(intents=discord.Intents.none())
    client.wait_until_ready = AsyncMock()
    client.on_member_join = AsyncMock()
    client.fetch_user = AsyncMock()
    client.get_ver_channel = AsyncMock(return_value=None)
    return client


def _channel(send_error=None):
    channel = MagicMock(name="log_channel")
    channel.send = AsyncMock(side_effect=send_error)
    return channel


# --- log_message is best-effort ----------------------------------------------------------

@pytest.fixture
def logging_configured(fake_db):
    fake_db.responses = {"FROM server_logging": [(LOG_CHANNEL,)]}
    return fake_db


@pytest.mark.parametrize("exc_type", [discord.Forbidden, discord.NotFound, discord.HTTPException])
async def test_log_message_send_failure_is_swallowed_and_logged(
        logging_configured, discord_factories, caplog, exc_type):
    guild = discord_factories.guild(G1, roles=[])
    guild.fetch_channel = AsyncMock(return_value=_channel(discord_factories.http_error(exc_type)))
    client = _real_client()
    client.fetch_guild = AsyncMock(return_value=guild)

    with caplog.at_level(logging.WARNING, logger="main"):
        await main.MyClient.log_message(client, G1, "something important happened")

    text = " ".join(r.getMessage() for r in caplog.records)
    assert str(G1) in text and str(LOG_CHANNEL) in text
    assert "something important happened" in text


@pytest.mark.parametrize("exc_type", [discord.Forbidden, discord.NotFound])
async def test_log_message_fetch_channel_failure_is_swallowed(
        logging_configured, discord_factories, caplog, exc_type):
    guild = discord_factories.guild(G1, roles=[])
    guild.fetch_channel = AsyncMock(side_effect=discord_factories.http_error(exc_type))
    client = _real_client()
    client.fetch_guild = AsyncMock(return_value=guild)

    with caplog.at_level(logging.WARNING, logger="main"):
        await main.MyClient.log_message(client, G1, "hello")

    assert any(str(LOG_CHANNEL) in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exc_type", [discord.Forbidden, discord.NotFound])
async def test_log_message_fetch_guild_failure_is_swallowed(
        logging_configured, discord_factories, caplog, exc_type):
    client = _real_client()
    client.fetch_guild = AsyncMock(side_effect=discord_factories.http_error(exc_type))

    with caplog.at_level(logging.WARNING, logger="main"):
        await main.MyClient.log_message(client, G1, "hello")

    assert any(str(G1) in r.getMessage() for r in caplog.records)


async def test_log_message_success_posts_message(logging_configured, discord_factories):
    channel = _channel()
    guild = discord_factories.guild(G1, roles=[])
    guild.fetch_channel = AsyncMock(return_value=channel)
    client = _real_client()
    client.fetch_guild = AsyncMock(return_value=guild)

    await main.MyClient.log_message(client, G1, "hello")

    channel.send.assert_awaited_once_with("hello")


async def test_log_message_does_not_swallow_programming_errors(logging_configured, discord_factories):
    guild = discord_factories.guild(G1, roles=[])
    guild.fetch_channel = AsyncMock(return_value=_channel(TypeError("bug")))
    client = _real_client()
    client.fetch_guild = AsyncMock(return_value=guild)

    with pytest.raises(TypeError):
        await main.MyClient.log_message(client, G1, "hello")


async def test_log_message_db_errors_still_propagate(monkeypatch):
    def boom():
        raise mysql.connector.Error("db down")

    monkeypatch.setattr(main, "get_cursor", boom)
    with pytest.raises(mysql.connector.Error):
        await main.MyClient.log_message(_real_client(), G1, "hello")


# --- daily task: the production scenario -------------------------------------------------

def _daily_guild(discord_factories, guild_id, role_id, user_id, remove_error=None):
    role = discord_factories.role(role_id)
    member = discord_factories.member(user_id, roles=[role])
    if remove_error:
        member.remove_roles.side_effect = remove_error
    guild = discord_factories.guild(guild_id, roles=[role])
    guild.fetch_members = lambda: _aiter([member])
    guild.fetch_member = AsyncMock(return_value=member)
    return guild, member, role


def _two_days_ago():
    return datetime.datetime.now() - datetime.timedelta(days=2)


async def test_daily_task_survives_forbidden_role_removal_and_forbidden_log_channel(
        fake_db, discord_factories):
    """Guild 1: can't remove the role AND can't post in the log channel. Guild 2 must still run."""
    forbidden = discord_factories.http_error(discord.Forbidden)
    guild1, member1, role1 = _daily_guild(discord_factories, G1, R1, U1, remove_error=forbidden)
    guild1.fetch_channel = AsyncMock(return_value=_channel(discord_factories.http_error(discord.Forbidden)))
    guild2, member2, role2 = _daily_guild(discord_factories, G2, R2, U2)
    ok_channel = _channel()
    guild2.fetch_channel = AsyncMock(return_value=ok_channel)

    client = _real_client()  # REAL log_message
    client.fetch_guild = AsyncMock(side_effect=lambda gid: guild1 if gid == G1 else guild2)
    fake_db.responses = {
        "FROM server_roles": [(G1, R1), (G2, R2)],
        "FROM server_logging": [(LOG_CHANNEL,)],
        "notified_at FROM unauthorized_users": [(_two_days_ago(),)],
    }

    await main.MyClient._daily_task_once(client)

    member1.remove_roles.assert_awaited_once()
    member2.remove_roles.assert_awaited_once_with(role2)  # later guild still processed
    # Guild 2's flag was cleared after its successful removal; guild 1's was kept (forbidden).
    deletes = [p for s, p in fake_db.executed if "DELETE FROM unauthorized_users" in s]
    assert deletes == [(U2, G2)]
    ok_channel.send.assert_awaited()  # guild 2's own logging still works


async def test_daily_task_skips_guild_whose_fetch_is_forbidden(fake_db, discord_factories):
    guild2, member2, role2 = _daily_guild(discord_factories, G2, R2, U2)
    client = _real_client()
    client.log_message = AsyncMock()

    async def fetch_guild(gid):
        if gid == G1:
            raise discord_factories.http_error(discord.Forbidden)
        return guild2

    client.fetch_guild = fetch_guild
    fake_db.responses = {
        "FROM server_roles": [(G1, R1), (G2, R2)],
        "notified_at FROM unauthorized_users": [(_two_days_ago(),)],
    }

    await main.MyClient._daily_task_once(client)

    member2.remove_roles.assert_awaited_once_with(role2)


async def test_daily_task_skips_guild_whose_member_listing_fails(fake_db, discord_factories):
    guild1, member1, _ = _daily_guild(discord_factories, G1, R1, U1)

    async def failing_members():
        raise discord_factories.http_error(discord.Forbidden)
        yield  # pragma: no cover - makes this an async generator

    guild1.fetch_members = failing_members
    guild2, member2, role2 = _daily_guild(discord_factories, G2, R2, U2)
    client = _real_client()
    client.log_message = AsyncMock()
    client.fetch_guild = AsyncMock(side_effect=lambda gid: guild1 if gid == G1 else guild2)
    fake_db.responses = {
        "FROM server_roles": [(G1, R1), (G2, R2)],
        "notified_at FROM unauthorized_users": [(_two_days_ago(),)],
    }

    await main.MyClient._daily_task_once(client)

    member1.remove_roles.assert_not_awaited()
    member2.remove_roles.assert_awaited_once_with(role2)


@pytest.mark.parametrize("exc_type", [discord.NotFound, discord.Forbidden])
async def test_daily_task_member_gone_before_removal_does_not_abort(fake_db, discord_factories, exc_type):
    guild1, member1, _ = _daily_guild(discord_factories, G1, R1, U1)
    guild1.fetch_member = AsyncMock(side_effect=discord_factories.http_error(exc_type))
    guild2, member2, role2 = _daily_guild(discord_factories, G2, R2, U2)
    client = _real_client()
    client.log_message = AsyncMock()
    client.fetch_guild = AsyncMock(side_effect=lambda gid: guild1 if gid == G1 else guild2)
    fake_db.responses = {
        "FROM server_roles": [(G1, R1), (G2, R2)],
        "notified_at FROM unauthorized_users": [(_two_days_ago(),)],
    }

    await main.MyClient._daily_task_once(client)

    member1.remove_roles.assert_not_awaited()
    member2.remove_roles.assert_awaited_once_with(role2)


# --- 60s user-state loop -----------------------------------------------------------------

def _state_guild(discord_factories, guild_id, role_id, user_id):
    role = discord_factories.role(role_id)
    member = discord_factories.member(user_id, roles=[])
    member.mention = f"<@{user_id}>"
    guild = discord_factories.guild(guild_id, roles=[role])
    guild.fetch_member = AsyncMock(return_value=member)
    return guild, member, role


def _state_client(guilds):
    client = _real_client()
    client.log_message = AsyncMock()
    client.fetch_guild = AsyncMock(side_effect=lambda gid: guilds[gid])
    return client


def _deleted_state_users(fake_db):
    return [p[0] for s, p in fake_db.executed if "DELETE FROM user_states WHERE user_id" in s]


@pytest.mark.parametrize("exc_type", [discord.NotFound, discord.Forbidden])
async def test_user_states_member_left_is_reported_and_next_row_processed(
        fake_db, discord_factories, exc_type):
    guild1, _, _ = _state_guild(discord_factories, G1, R1, U1)
    guild1.fetch_member = AsyncMock(side_effect=discord_factories.http_error(exc_type))
    guild2, member2, role2 = _state_guild(discord_factories, G2, R2, U2)
    client = _state_client({G1: guild1, G2: guild2})
    fake_db.responses = {"JOIN user_states us": [(U1, G1, R1), (U2, G2, R2)]}

    await main.MyClient._check_user_states_once(client)

    member2.add_roles.assert_awaited_once_with(role2)
    assert _deleted_state_users(fake_db) == [U1, U2]  # the dead row is cleared, not retried forever
    assert any("not found" in c.args[1] for c in client.log_message.await_args_list)


async def test_user_states_guild_not_found_does_not_abort(fake_db, discord_factories):
    guild2, member2, role2 = _state_guild(discord_factories, G2, R2, U2)
    client = _real_client()
    client.log_message = AsyncMock()

    async def fetch_guild(gid):
        if gid == G1:
            raise discord_factories.http_error(discord.NotFound)
        return guild2

    client.fetch_guild = fetch_guild
    fake_db.responses = {"JOIN user_states us": [(U1, G1, R1), (U2, G2, R2)]}

    await main.MyClient._check_user_states_once(client)

    member2.add_roles.assert_awaited_once_with(role2)
    assert _deleted_state_users(fake_db) == [U1, U2]


async def test_user_states_transient_error_keeps_row_and_continues(fake_db, discord_factories):
    guild1, _, _ = _state_guild(discord_factories, G1, R1, U1)
    guild1.fetch_member = AsyncMock(side_effect=discord_factories.http_error(discord.HTTPException))
    guild2, member2, role2 = _state_guild(discord_factories, G2, R2, U2)
    client = _state_client({G1: guild1, G2: guild2})
    fake_db.responses = {"JOIN user_states us": [(U1, G1, R1), (U2, G2, R2)]}

    await main.MyClient._check_user_states_once(client)

    member2.add_roles.assert_awaited_once_with(role2)
    assert _deleted_state_users(fake_db) == [U2]  # U1 retried next pass


@pytest.mark.parametrize("exc_type", [discord.NotFound, discord.Forbidden])
async def test_expired_link_edit_failure_still_cleans_up(fake_db, discord_factories, exc_type):
    dm_channel = MagicMock()
    dm_channel.fetch_message = AsyncMock(side_effect=discord_factories.http_error(exc_type))
    user = MagicMock()
    user.dm_channel = dm_channel
    client = _real_client()
    client.fetch_user = AsyncMock(return_value=user)
    fake_db.responses = {
        "SELECT * FROM user_states WHERE timestamp": [("state-1", U1), ("state-2", U2)],
        "SELECT message_id FROM auth_messages": [(777,)],
    }

    await main.MyClient._check_user_states_once(client)

    deleted = [p[0] for s, p in fake_db.executed if "DELETE FROM auth_messages" in s]
    assert deleted == ["state-1", "state-2"]  # second link processed despite the first failing
