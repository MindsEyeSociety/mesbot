"""The on-member-join path: ``assign_event_role``.

This is the second assignment path — invoked from ``on_member_join`` for someone
who registered *before* joining the server. It mirrors the periodic scan's logic
(same EventAttendee + valid-membership gate, same idempotent add_roles), so its
behavior matrix is asserted directly here.
"""
import discord
import main

GUILD_ID = 111
EVENT_ID = 222
ROLE_ID = 333
MEMBER_ID = 444


def _member_with_guild(discord_factories, role, *, member_roles):
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    member = discord_factories.member(MEMBER_ID, roles=member_roles)
    member.guild = guild
    return member


async def test_assign_no_config_returns_early(fake_db, discord_factories):
    role = discord_factories.role(ROLE_ID)
    member = _member_with_guild(discord_factories, role, member_roles=[])
    client = discord_factories.client()
    fake_db.responses = {}  # no event configured for this guild

    await main.MyClient.assign_event_role(client, member)

    member.add_roles.assert_not_awaited()


async def test_assign_role_already_present_is_idempotent(fake_db, discord_factories):
    role = discord_factories.role(ROLE_ID)
    member = _member_with_guild(discord_factories, role, member_roles=[role])
    client = discord_factories.client()
    fake_db.responses = {"server_event_roles": [(EVENT_ID, ROLE_ID)]}

    await main.MyClient.assign_event_role(client, member)

    member.add_roles.assert_not_awaited()


async def test_assign_grants_role_to_attendee(fake_db, discord_factories):
    role = discord_factories.role(ROLE_ID)
    member = _member_with_guild(discord_factories, role, member_roles=[])
    client = discord_factories.client()
    fake_db.responses = {
        "server_event_roles": [(EVENT_ID, ROLE_ID)],
        "EventAttendee": [(1,)],  # is a registered attendee with valid membership
    }

    await main.MyClient.assign_event_role(client, member)

    member.add_roles.assert_awaited_once_with(role)
    client.log_message.assert_awaited()


async def test_assign_skips_non_attendee(fake_db, discord_factories):
    role = discord_factories.role(ROLE_ID)
    member = _member_with_guild(discord_factories, role, member_roles=[])
    client = discord_factories.client()
    fake_db.responses = {
        "server_event_roles": [(EVENT_ID, ROLE_ID)],
        # EventAttendee query yields no rows -> not an attendee
    }

    await main.MyClient.assign_event_role(client, member)

    member.add_roles.assert_not_awaited()


async def test_assign_handles_forbidden_without_raising(fake_db, discord_factories):
    role = discord_factories.role(ROLE_ID)
    member = _member_with_guild(discord_factories, role, member_roles=[])
    member.add_roles.side_effect = discord_factories.http_error(discord.Forbidden)
    client = discord_factories.client()
    fake_db.responses = {
        "server_event_roles": [(EVENT_ID, ROLE_ID)],
        "EventAttendee": [(1,)],
    }

    await main.MyClient.assign_event_role(client, member)  # must not raise

    member.add_roles.assert_awaited_once_with(role)
    assert client.log_message.await_count >= 1  # error reported
