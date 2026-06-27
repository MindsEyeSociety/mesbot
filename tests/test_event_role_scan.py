"""The real "already on the server" path: ``check_user_states``' event-role scan.

Drives the actual production coroutine (``main.MyClient.check_user_states.coro``)
with the fake DB returning empty result sets for the OAuth-state and cleanup
queries, so only the event-role scan does work. The scan reads the guild and its
members from the gateway-populated cache (``get_guild``/``get_member``) — never the
REST API — and grants the configured role to attendees who are present in the guild
and don't already hold it. These tests pin that behaviour, including the assertion
that no REST member fetch is attempted (the per-attendee REST fetch was the cause of
the rate-limit storm this scan was changed to avoid).
"""
from unittest.mock import AsyncMock

import main

GUILD_ID = 111
EVENT_ID = 222
ROLE_ID = 333
DISCORD_ID = 444


def _wire(fake_db, discord_factories, *, member, in_guild=True):
    """Set up a one-event, one-attendee scan; member is in cache unless in_guild=False."""
    role = discord_factories.role(ROLE_ID)
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    guild.get_member.return_value = member if in_guild else None
    client = discord_factories.client()
    client.get_guild.return_value = guild
    fake_db.responses = {
        "server_event_roles": [(GUILD_ID, EVENT_ID, ROLE_ID)],
        "EventAttendee": [(DISCORD_ID,)],
    }
    return role, guild, client


async def test_scan_assigns_role_to_cached_attendee(fake_db, discord_factories):
    member = discord_factories.member(DISCORD_ID, roles=[])  # in guild, lacks role
    role, guild, client = _wire(fake_db, discord_factories, member=member)

    await main.MyClient._check_user_states_once(client)

    member.add_roles.assert_awaited_once_with(role)
    client.log_message.assert_awaited()
    guild.fetch_member.assert_not_called()   # cache only — no REST
    client.fetch_guild.assert_not_awaited()  # cache only — no REST


async def test_scan_is_idempotent_when_role_present(fake_db, discord_factories):
    member = discord_factories.member(DISCORD_ID, roles=[])
    role, guild, client = _wire(fake_db, discord_factories, member=member)
    member.roles = [role]  # already holds the role

    await main.MyClient._check_user_states_once(client)

    member.add_roles.assert_not_awaited()
    guild.fetch_member.assert_not_called()


async def test_scan_skips_attendee_not_in_guild(fake_db, discord_factories):
    # get_member returns None for an attendee who left / never joined the guild. The
    # scan must skip them WITHOUT a REST fetch — repeatedly REST-fetching such
    # not-in-guild attendees every 60s was the rate-limit storm this change fixes.
    member = discord_factories.member(DISCORD_ID, roles=[])
    role, guild, client = _wire(fake_db, discord_factories, member=member, in_guild=False)

    await main.MyClient._check_user_states_once(client)

    member.add_roles.assert_not_awaited()
    guild.fetch_member.assert_not_called()


async def test_scan_no_configured_events_does_nothing(fake_db, discord_factories):
    client = discord_factories.client()
    fake_db.responses = {}  # server_event_roles query yields no rows

    await main.MyClient._check_user_states_once(client)

    client.get_guild.assert_not_called()


# --- the get_member cache-gap fix: refresh the cache over the gateway -------------------

async def test_scan_queries_then_assigns_uncached_in_guild_attendee(fake_db, discord_factories):
    # Attendee is in the guild but missing from the member cache: get_member returns None
    # until query_members (gateway) populates it. The scan must refresh, then assign.
    role = discord_factories.role(ROLE_ID)
    member = discord_factories.member(DISCORD_ID, roles=[])
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    member_cache = {}
    guild.get_member = lambda did: member_cache.get(did)

    async def _query(*, user_ids, cache=True):
        # the member IS in the guild, so the gateway returns + caches it
        if DISCORD_ID in user_ids:
            member_cache[DISCORD_ID] = member
            return [member]
        return []
    guild.query_members = AsyncMock(side_effect=_query)

    client = discord_factories.client()
    client.get_guild.return_value = guild
    fake_db.responses = {
        "server_event_roles": [(GUILD_ID, EVENT_ID, ROLE_ID)],
        "EventAttendee": [(DISCORD_ID,)],
    }

    await main.MyClient._check_user_states_once(client)

    guild.query_members.assert_awaited_once()
    assert DISCORD_ID in guild.query_members.await_args.kwargs["user_ids"]
    member.add_roles.assert_awaited_once_with(role)
    guild.fetch_member.assert_not_called()  # gateway refresh, never REST


async def test_scan_skips_uncached_not_in_guild_after_query(fake_db, discord_factories):
    # Cache miss for someone NOT in the guild: query_members returns nothing and doesn't
    # cache them, so they stay skipped — and crucially no REST fetch happens.
    role = discord_factories.role(ROLE_ID)
    member = discord_factories.member(DISCORD_ID, roles=[])
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    guild.get_member = lambda did: None
    guild.query_members = AsyncMock(return_value=[])

    client = discord_factories.client()
    client.get_guild.return_value = guild
    fake_db.responses = {
        "server_event_roles": [(GUILD_ID, EVENT_ID, ROLE_ID)],
        "EventAttendee": [(DISCORD_ID,)],
    }

    await main.MyClient._check_user_states_once(client)

    guild.query_members.assert_awaited_once()
    member.add_roles.assert_not_awaited()
    guild.fetch_member.assert_not_called()


async def test_scan_cached_attendee_makes_no_gateway_call(fake_db, discord_factories):
    # Steady state: the attendee is already cached, so no gateway refresh is issued.
    member = discord_factories.member(DISCORD_ID, roles=[])
    role, guild, client = _wire(fake_db, discord_factories, member=member)

    await main.MyClient._check_user_states_once(client)

    member.add_roles.assert_awaited_once_with(role)
    guild.query_members.assert_not_awaited()
