"""The valid-membership gate that excludes expired members.

The "only paid-up members get the role" rule is enforced in SQL
(``u.membershipExpiration >= CURDATE()``), so it cannot be exercised through the
fakes without a live database. Instead this test captures the exact query the scan
sends and asserts the gate (and the EventAttendee join it depends on) is present —
i.e. the production code really filters on membership validity at the DB layer.
"""
import main

GUILD_ID = 111
EVENT_ID = 222
ROLE_ID = 333
DISCORD_ID = 444


async def test_attendee_query_enforces_valid_membership(fake_db, discord_factories):
    role = discord_factories.role(ROLE_ID)
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    member = discord_factories.member(DISCORD_ID, roles=[])
    guild.get_member.return_value = member
    client = discord_factories.client()
    client.get_guild.return_value = guild
    fake_db.responses = {
        "server_event_roles": [(GUILD_ID, EVENT_ID, ROLE_ID)],
        "EventAttendee": [(DISCORD_ID,)],
    }

    await main.MyClient._check_user_states_once(client)

    attendee_queries = [s for s in fake_db.sql if "EventAttendee" in s]
    assert attendee_queries, "scan never queried EventAttendee"
    query = attendee_queries[0]
    assert "membershipExpiration >= CURDATE()" in query
    assert "JOIN `mes-portal`.EventAttendee" in query
