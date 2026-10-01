"""Failed member DMs are reported to the guild's logging channel, not just the server log.

Covers the pure formatter ``main.format_dm_failure_message`` and every DM path in ``main``:
the ``on_member_join`` auth-link DM, the daily sweep's removal notice and validation
reminder, and the post-assign notice (where a DM failure must not be misreported as a
role-assignment failure).
"""
import datetime
from unittest.mock import AsyncMock, MagicMock

import discord
import main

GUILD_ID, ROLE_ID, USER_ID = 111, 333, 444


def _forbidden(code=50278, text="Cannot send messages to this user due to having no mutual guilds"):
    response = MagicMock()
    response.status = 403
    response.reason = "Forbidden"
    return discord.Forbidden(response, {"code": code, "message": text})


def _real_client():
    client = main.MyClient(intents=discord.Intents.none())
    client.log_message = AsyncMock()
    client.assign_event_role = AsyncMock()
    client.get_ver_channel = AsyncMock(return_value=None)
    return client


def _logged(client):
    return [call.args[1] for call in client.log_message.await_args_list]


async def _aiter(items):
    for item in items:
        yield item


# --- formatter --------------------------------------------------------------------------

def test_format_forbidden_dm_block_names_member_and_advice():
    text = main.format_dm_failure_message("Sam", 42, "the verification link", _forbidden(), "then run !auth again")
    assert "Sam" in text and "<@42>" in text and "42" in text
    assert "the verification link" in text
    assert "block DMs" in text
    assert "Direct Messages" in text and "Privacy Settings" in text
    assert text.endswith("then run !auth again.")


def test_format_forbidden_50007_also_reported_as_blocked():
    text = main.format_dm_failure_message("Sam", 42, "x", _forbidden(code=50007, text="Cannot send"))
    assert "block DMs" in text


def test_format_other_error_uses_exception_text_and_no_retry_hint():
    text = main.format_dm_failure_message("Sam", 42, "the notice", RuntimeError("boom"))
    assert "boom" in text
    assert "block DMs" not in text
    assert "!auth" not in text


def test_format_blank_exception_falls_back_to_type_name():
    assert "RuntimeError" in main.format_dm_failure_message("Sam", 42, "the notice", RuntimeError())


# --- on_member_join auth-link DM ---------------------------------------------------------

async def test_on_member_join_dm_forbidden_logs_to_channel(fake_db, discord_factories):
    guild = discord_factories.guild(GUILD_ID, roles=[])
    member = discord_factories.member(USER_ID, roles=[], display_name="Sam Tester")
    member.guild = guild
    member.send.side_effect = _forbidden()
    client = _real_client()
    # no user_authorizations row -> the auth-link path

    await main.MyClient.on_member_join(client, member)

    messages = _logged(client)
    assert len(messages) == 1
    assert "Sam Tester" in messages[0] and str(USER_ID) in messages[0]
    assert "verification link" in messages[0]
    assert "!auth" in messages[0]
    assert client.log_message.await_args_list[0].args[0] == GUILD_ID
    assert not any("INSERT INTO auth_messages" in s for s in fake_db.sql)


# --- daily sweep -------------------------------------------------------------------------

def _sweep_client(guild):
    client = _real_client()
    client.wait_until_ready = AsyncMock()
    client.on_member_join = AsyncMock()
    client.fetch_user = AsyncMock()
    client.fetch_guild = AsyncMock(return_value=guild)
    return client


def _sweep_guild(discord_factories, member, role):
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    guild.fetch_members = lambda: _aiter([member])
    guild.fetch_member = AsyncMock(return_value=member)
    return guild


async def test_sweep_removal_notice_dm_failure_logged(fake_db, discord_factories):
    role = discord_factories.role(ROLE_ID)
    member = discord_factories.member(USER_ID, roles=[role], display_name="Sam Tester")
    member.send.side_effect = _forbidden()
    client = _sweep_client(_sweep_guild(discord_factories, member, role))
    two_days_ago = datetime.datetime.now() - datetime.timedelta(days=2)
    fake_db.responses = {
        "FROM server_roles": [(GUILD_ID, ROLE_ID)],
        "notified_at FROM unauthorized_users": [(two_days_ago,)],
    }

    await main.MyClient._daily_task_once(client)

    member.remove_roles.assert_awaited_once_with(role)  # removal itself still happened
    failures = [m for m in _logged(client) if "Could not DM" in m]
    assert len(failures) == 1
    assert "role-removal notice" in failures[0] and "Sam Tester" in failures[0]
    # the flag is still cleared after a successful removal
    assert any("DELETE FROM unauthorized_users" in s for s in fake_db.sql)


async def test_sweep_removal_flag_cleared_even_when_logging_channel_broken(fake_db, discord_factories):
    role = discord_factories.role(ROLE_ID)
    member = discord_factories.member(USER_ID, roles=[role], display_name="Sam Tester")
    member.send.side_effect = _forbidden()
    client = _sweep_client(_sweep_guild(discord_factories, member, role))

    async def broken_for_dm_failures(guild_id, message):
        if "Could not DM" in message:
            raise discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Channel")

    client.log_message = AsyncMock(side_effect=broken_for_dm_failures)
    two_days_ago = datetime.datetime.now() - datetime.timedelta(days=2)
    fake_db.responses = {
        "FROM server_roles": [(GUILD_ID, ROLE_ID)],
        "notified_at FROM unauthorized_users": [(two_days_ago,)],
    }

    await main.MyClient._daily_task_once(client)

    member.remove_roles.assert_awaited_once_with(role)
    assert any("Could not DM" in m for m in _logged(client))  # the post was attempted
    assert any("DELETE FROM unauthorized_users" in s for s in fake_db.sql)


async def test_sweep_validation_dm_failure_logs_once_and_skips_auth_link(fake_db, discord_factories):
    role = discord_factories.role(ROLE_ID)
    member = discord_factories.member(USER_ID, roles=[role], display_name="Sam Tester")
    member.send.side_effect = _forbidden()
    client = _sweep_client(_sweep_guild(discord_factories, member, role))
    fake_db.responses = {"FROM server_roles": [(GUILD_ID, ROLE_ID)]}  # first-time flag

    await main.MyClient._daily_task_once(client)

    failures = [m for m in _logged(client) if "Could not DM" in m]
    assert len(failures) == 1 and "validation reminder" in failures[0]
    client.on_member_join.assert_not_awaited()  # would only fail (and log) a second time


async def test_sweep_validation_dm_success_then_auth_link(fake_db, discord_factories):
    role = discord_factories.role(ROLE_ID)
    member = discord_factories.member(USER_ID, roles=[role])
    client = _sweep_client(_sweep_guild(discord_factories, member, role))
    fake_db.responses = {"FROM server_roles": [(GUILD_ID, ROLE_ID)]}

    await main.MyClient._daily_task_once(client)

    member.send.assert_awaited_once()
    client.on_member_join.assert_awaited_once_with(member)
    assert not any("Could not DM" in m for m in _logged(client))


# --- post-assign notice ------------------------------------------------------------------

async def test_post_assign_dm_failure_is_not_a_role_failure(fake_db, discord_factories):
    role = discord_factories.role(ROLE_ID)
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    member = discord_factories.member(USER_ID, roles=[], display_name="Sam Tester")
    member.guild = guild
    member.mention = f"<@{USER_ID}>"
    member.send.side_effect = _forbidden()
    client = _real_client()
    fake_db.responses = {
        "SELECT last_authorized FROM user_authorizations": [(datetime.datetime(2026, 1, 1),)],
        "FROM server_roles": [(ROLE_ID,)],
    }

    await main.MyClient.on_member_join(client, member)

    member.add_roles.assert_awaited_once_with(role)
    messages = _logged(client)
    assert not any("Unable to assign" in m for m in messages)
    assert sum("Could not DM" in m for m in messages) == 1
    assert any("membership verified" in m for m in messages)  # the success log still posted
    client.assign_event_role.assert_awaited_once_with(member)


async def test_post_assign_real_role_failure_still_reports_error(fake_db, discord_factories):
    role = discord_factories.role(ROLE_ID)
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    member = discord_factories.member(USER_ID, roles=[], display_name="Sam Tester")
    member.guild = guild
    member.add_roles.side_effect = _forbidden(code=50013, text="Missing Permissions")
    client = _real_client()
    fake_db.responses = {
        "SELECT last_authorized FROM user_authorizations": [(datetime.datetime(2026, 1, 1),)],
        "FROM server_roles": [(ROLE_ID,)],
    }

    await main.MyClient.on_member_join(client, member)

    assert any("Unable to assign" in m for m in _logged(client))
    member.send.assert_not_awaited()
