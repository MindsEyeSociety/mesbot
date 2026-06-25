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

    await main.MyClient.check_user_states.coro(client)

    member.add_roles.assert_awaited_once_with(role)
    client.log_message.assert_awaited()
    guild.fetch_member.assert_not_called()   # cache only — no REST
    client.fetch_guild.assert_not_awaited()  # cache only — no REST


async def test_scan_is_idempotent_when_role_present(fake_db, discord_factories):
    member = discord_factories.member(DISCORD_ID, roles=[])
    role, guild, client = _wire(fake_db, discord_factories, member=member)
    member.roles = [role]  # already holds the role

    await main.MyClient.check_user_states.coro(client)

    member.add_roles.assert_not_awaited()
    guild.fetch_member.assert_not_called()


async def test_scan_skips_attendee_not_in_guild(fake_db, discord_factories):
    # get_member returns None for an attendee who left / never joined the guild. The
    # scan must skip them WITHOUT a REST fetch — repeatedly REST-fetching such
    # not-in-guild attendees every 60s was the rate-limit storm this change fixes.
    member = discord_factories.member(DISCORD_ID, roles=[])
    role, guild, client = _wire(fake_db, discord_factories, member=member, in_guild=False)

    await main.MyClient.check_user_states.coro(client)

    member.add_roles.assert_not_awaited()
    guild.fetch_member.assert_not_called()


async def test_scan_no_configured_events_does_nothing(fake_db, discord_factories):
    client = discord_factories.client()
    fake_db.responses = {}  # server_event_roles query yields no rows

    await main.MyClient.check_user_states.coro(client)

    client.get_guild.assert_not_called()
