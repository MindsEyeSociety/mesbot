"""The verification welcome message honors the member's preferred name.

Drives the OAuth member-role path in ``main.MyClient._check_user_states_once`` (a newly
authorized member picked up from ``user_states``). Asserts the PUBLIC verification-channel
post shows the portal ``nickname`` + surname and keeps the MES number, while the INTERNAL
log line keeps the legal name. No prior test mocked the welcome name queries; this is
net-new coverage for the handbook-alignment change.
"""
from unittest.mock import AsyncMock, MagicMock

import discord
import main

GUILD_ID = 111
ROLE_ID = 333
DISCORD_ID = 444
MEMBERSHIP = "US2016090038"


def _real_client():
    client = main.MyClient(intents=discord.Intents.none())
    client.log_message = AsyncMock()
    return client


def _wire(fake_db, discord_factories, *, nickname):
    """One newly-authorized member whose portal row carries ``nickname``."""
    role = discord_factories.role(ROLE_ID)
    member = discord_factories.member(DISCORD_ID, roles=[], display_name="Tester")
    member.mention = f"<@{DISCORD_ID}>"
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    guild.fetch_member = AsyncMock(return_value=member)
    ver_channel = MagicMock(name="verification")
    ver_channel.send = AsyncMock()
    guild.fetch_channel = AsyncMock(return_value=ver_channel)

    client = _real_client()
    client.fetch_guild = AsyncMock(return_value=guild)

    fake_db.responses = {
        # The "new authorization" join that yields (discord_user_id, guild_id, role_id).
        "JOIN user_states us": [(DISCORD_ID, GUILD_ID, ROLE_ID)],
        # discord_user_id -> membership number (access_token quirk).
        "SELECT access_token FROM user_authorizations WHERE discord_user_id": [(MEMBERSHIP,)],
        # The name lookup, now including nickname as the 4th column.
        "SELECT firstName, lastName, membershipNumber, nickname": [
            ("Andrew", "Sutton", MEMBERSHIP, nickname)
        ],
        # get_ver_channel -> channel id.
        "server_verification": [(999,)],
    }
    return member, ver_channel, client


async def test_welcome_prefers_nickname_and_keeps_membership_number(fake_db, discord_factories):
    member, ver_channel, client = _wire(fake_db, discord_factories, nickname="Andy")

    await main.MyClient._check_user_states_once(client)

    member.add_roles.assert_awaited_once()
    ver_channel.send.assert_awaited_once()
    public = ver_channel.send.await_args.args[0]
    assert "Andy Sutton" in public          # preferred name shown
    assert MEMBERSHIP in public              # MES number retained (handbook)
    assert "Andrew" not in public            # legal first name not broadcast

    # Internal log keeps the legal name for moderation.
    logged = " ".join(str(c.args[-1]) for c in client.log_message.await_args_list)
    assert "Andrew Sutton" in logged


async def test_welcome_falls_back_to_legal_name_when_nickname_blank(fake_db, discord_factories):
    member, ver_channel, client = _wire(fake_db, discord_factories, nickname="")

    await main.MyClient._check_user_states_once(client)

    ver_channel.send.assert_awaited_once()
    public = ver_channel.send.await_args.args[0]
    assert "Andrew Sutton" in public
    assert MEMBERSHIP in public
