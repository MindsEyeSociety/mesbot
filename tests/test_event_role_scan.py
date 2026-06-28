"""The real "already on the server" path: ``check_user_states``' event-role scan.

Drives the production coroutine (``main.MyClient._check_user_states_once``) with the fake DB
returning empty result sets for the OAuth-state and cleanup queries, so only the event-role
scan does work. It grants the configured role to attendees present in the guild.

The scan reads members from the gateway-populated cache (``get_member``) first — zero HTTP in
steady state. On a cache miss it falls back to a SINGLE ``fetch_member`` (REST), throttled by a
negative cache so not-in-guild attendees aren't re-fetched every pass (the original 429 storm).
It must NOT use ``query_members`` — that shares discord.py's chunk machinery and deadlocks
against the gateway's automatic guild re-chunking (the multi-hour stall this design fixes).
"""
from unittest.mock import AsyncMock, MagicMock

import discord
import main

GUILD_ID = 111
EVENT_ID = 222
ROLE_ID = 333
DISCORD_ID = 444


def _real_client():
    """A real MyClient (so ``_fetch_attendee`` and the negative cache are the real ones)."""
    client = main.MyClient(intents=discord.Intents.none())
    client.log_message = AsyncMock()
    return client


def _wire(fake_db, discord_factories, *, member, in_guild=True):
    """One-event, one-attendee scan via a fake client; member is in cache unless in_guild=False."""
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


def _wire_real(fake_db, discord_factories, *, guild):
    """One-event scan via a REAL client — needed to exercise the fetch_member fallback path."""
    client = _real_client()
    client.get_guild = MagicMock(return_value=guild)
    fake_db.responses = {
        "server_event_roles": [(GUILD_ID, EVENT_ID, ROLE_ID)],
        "EventAttendee": [(DISCORD_ID,)],
    }
    return client


# --- cache-hit paths: zero HTTP ---------------------------------------------------------

async def test_scan_assigns_role_to_cached_attendee(fake_db, discord_factories):
    member = discord_factories.member(DISCORD_ID, roles=[])  # in guild, lacks role
    role, guild, client = _wire(fake_db, discord_factories, member=member)

    await main.MyClient._check_user_states_once(client)

    member.add_roles.assert_awaited_once_with(role)
    client.log_message.assert_awaited()
    client._announce_event_role.assert_awaited_once()  # public announcement on grant
    guild.fetch_member.assert_not_called()    # cache hit — no REST
    guild.query_members.assert_not_called()   # never the chunk-colliding path


async def test_scan_is_idempotent_when_role_present(fake_db, discord_factories):
    member = discord_factories.member(DISCORD_ID, roles=[])
    role, guild, client = _wire(fake_db, discord_factories, member=member)
    member.roles = [role]  # already holds the role

    await main.MyClient._check_user_states_once(client)

    member.add_roles.assert_not_awaited()
    client._announce_event_role.assert_not_awaited()  # nothing granted -> no announcement
    guild.fetch_member.assert_not_called()


async def test_scan_no_configured_events_does_nothing(fake_db, discord_factories):
    client = discord_factories.client()
    fake_db.responses = {}  # server_event_roles query yields no rows

    await main.MyClient._check_user_states_once(client)

    client.get_guild.assert_not_called()


# --- cache-miss fallback: bounded, negative-cached fetch_member ------------------------

async def test_scan_fetches_and_assigns_uncached_in_guild_attendee(fake_db, discord_factories):
    # get_member misses, but the attendee IS in the guild — one REST fetch finds and assigns them.
    role = discord_factories.role(ROLE_ID)
    member = discord_factories.member(DISCORD_ID, roles=[])
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(return_value=member)
    client = _wire_real(fake_db, discord_factories, guild=guild)

    await main.MyClient._check_user_states_once(client)

    guild.fetch_member.assert_awaited_once_with(DISCORD_ID)
    member.add_roles.assert_awaited_once_with(role)
    guild.query_members.assert_not_called()


async def test_scan_negative_caches_not_in_guild_attendee(fake_db, discord_factories):
    # get_member misses and fetch_member 404s -> skip + remember absent; a second pass within the
    # TTL must NOT re-fetch (the repeated per-attendee REST was the 429 storm).
    role = discord_factories.role(ROLE_ID)
    member = discord_factories.member(DISCORD_ID, roles=[])
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(side_effect=discord_factories.http_error(discord.NotFound))
    client = _wire_real(fake_db, discord_factories, guild=guild)

    await main.MyClient._check_user_states_once(client)  # pass 1: fetch -> NotFound -> cache absent
    await main.MyClient._check_user_states_once(client)  # pass 2: within TTL -> no fetch

    guild.fetch_member.assert_awaited_once()  # exactly one REST call across both passes
    member.add_roles.assert_not_awaited()


# --- the _fetch_attendee helper directly ----------------------------------------------

async def test_fetch_attendee_returns_member_when_present(discord_factories):
    client = _real_client()
    member = discord_factories.member(DISCORD_ID, roles=[])
    guild = discord_factories.guild(GUILD_ID, roles=[])
    guild.fetch_member = AsyncMock(return_value=member)

    got = await main.MyClient._fetch_attendee(client, guild, GUILD_ID, DISCORD_ID)

    assert got is member


async def test_fetch_attendee_absent_is_cached_then_skipped(discord_factories):
    client = _real_client()
    guild = discord_factories.guild(GUILD_ID, roles=[])
    guild.fetch_member = AsyncMock(side_effect=discord_factories.http_error(discord.NotFound))

    assert await main.MyClient._fetch_attendee(client, guild, GUILD_ID, DISCORD_ID) is None
    assert await main.MyClient._fetch_attendee(client, guild, GUILD_ID, DISCORD_ID) is None

    guild.fetch_member.assert_awaited_once()  # second call short-circuited by the negative cache


# --- public announcement on grant (#arrivals / verification channel) --------------------

async def test_scan_announces_in_verification_channel_on_assignment(fake_db, discord_factories):
    # On a real grant the scan posts a public note to the verification channel.
    role = discord_factories.role(ROLE_ID)
    member = discord_factories.member(DISCORD_ID, roles=[])
    member.mention = f"<@{DISCORD_ID}>"
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    guild.get_member = MagicMock(return_value=member)
    ver_channel = MagicMock(name="arrivals")
    ver_channel.send = AsyncMock()
    guild.get_channel = MagicMock(return_value=ver_channel)
    client = _real_client()
    client.get_guild = MagicMock(return_value=guild)
    fake_db.responses = {
        "server_event_roles": [(GUILD_ID, EVENT_ID, ROLE_ID)],
        "EventAttendee": [(DISCORD_ID,)],
        "server_verification": [(999,)],  # get_ver_channel -> this channel
    }

    await main.MyClient._check_user_states_once(client)

    member.add_roles.assert_awaited_once_with(role)
    ver_channel.send.assert_awaited_once()


async def test_announce_event_role_silent_without_verification_channel(fake_db, discord_factories):
    # No verification channel configured -> get_ver_channel returns None -> no public post.
    client = _real_client()
    member = discord_factories.member(DISCORD_ID, roles=[])
    member.mention = f"<@{DISCORD_ID}>"
    guild = discord_factories.guild(GUILD_ID, roles=[])
    ver_channel = MagicMock()
    ver_channel.send = AsyncMock()
    guild.get_channel = MagicMock(return_value=ver_channel)
    fake_db.responses = {}  # server_verification empty -> get_ver_channel None

    await main.MyClient._announce_event_role(client, guild, member, discord_factories.role(ROLE_ID))

    ver_channel.send.assert_not_awaited()
