"""A guild the bot has left must have its config purged, but only when it is certainly gone.

These tests drive the REAL ``purge_guild_config``, ``on_guild_remove`` and ``_daily_task_once`` on a
real ``MyClient``; only Discord I/O and the database are faked.
"""
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import mysql.connector
import pytest

import main

G1, R1, G2, R2 = 111, 1111, 222, 2222
TABLES = [
    "server_roles", "server_logging", "server_verification",
    "server_event_roles", "unauthorized_users", "user_states",
]


async def _aiter(items):
    for item in items:
        yield item


def _real_client():
    client = main.MyClient(intents=discord.Intents.none())
    client.wait_until_ready = AsyncMock()
    client.log_message = AsyncMock()
    return client


def _purge_deletes(fake_db):
    return [(s, p) for s, p in fake_db.executed if s.startswith("DELETE FROM")
            and "WHERE guild_id" in s]


@pytest.fixture
def cnx(monkeypatch):
    fake = MagicMock(name="cnx")
    monkeypatch.setattr(main, "cnx", fake)
    return fake


def _live_guild(discord_factories, guild_id, role_id):
    role = discord_factories.role(role_id)
    guild = discord_factories.guild(guild_id, roles=[role])
    guild.fetch_members = lambda: _aiter([])
    return guild


# --- purge_guild_config ------------------------------------------------------------------

def test_purge_deletes_every_table_for_that_guild_only_and_commits_once(fake_db, cnx):
    main.purge_guild_config(G1)

    deletes = _purge_deletes(fake_db)
    assert [s.split()[2] for s, _ in deletes] == TABLES
    assert all(s.endswith("WHERE guild_id = %s") for s, _ in deletes)
    assert all(p == (G1,) for _, p in deletes)
    cnx.commit.assert_called_once()
    cnx.rollback.assert_not_called()


def test_purge_logs_one_warning_naming_guild_and_tables(fake_db, cnx, caplog):
    with caplog.at_level(logging.WARNING, logger="main"):
        main.purge_guild_config(G1)

    records = [r for r in caplog.records if str(G1) in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    assert all(f"{t}=0" in records[0].getMessage() for t in TABLES)


def test_purge_reports_rows_deleted_per_table(monkeypatch, cnx):
    counts = iter([1, 0, 0, 0, 9, 0])

    class CountingCursor:
        rowcount = 0

        def execute(self, sql, params=None):
            self.rowcount = next(counts)

        def close(self):
            pass

    monkeypatch.setattr(main, "get_cursor", CountingCursor)

    result = main.purge_guild_config(G1)

    assert result == {"server_roles": 1, "server_logging": 0, "server_verification": 0,
                      "server_event_roles": 0, "unauthorized_users": 9, "user_states": 0}


def test_purge_db_error_rolls_back_and_propagates(monkeypatch, cnx):
    class FailingCursor:
        rowcount = 0

        def execute(self, sql, params=None):
            if "server_event_roles" in sql:
                raise mysql.connector.Error("boom")

        def close(self):
            pass

    monkeypatch.setattr(main, "get_cursor", FailingCursor)

    with pytest.raises(mysql.connector.Error):
        main.purge_guild_config(G1)

    cnx.rollback.assert_called_once()
    cnx.commit.assert_not_called()


# --- on_guild_remove ---------------------------------------------------------------------

async def test_on_guild_remove_purges_that_guild(fake_db, cnx, discord_factories):
    guild = discord_factories.guild(G1, roles=[])

    await main.MyClient.on_guild_remove(_real_client(), guild)

    assert {p for _, p in _purge_deletes(fake_db)} == {(G1,)}
    assert len(_purge_deletes(fake_db)) == len(TABLES)


async def test_on_guild_remove_swallows_db_error(monkeypatch, cnx, discord_factories, caplog):
    def boom():
        raise mysql.connector.Error("db down")

    monkeypatch.setattr(main, "get_cursor", boom)
    guild = discord_factories.guild(G1, roles=[])

    with caplog.at_level(logging.ERROR, logger="main"):
        await main.MyClient.on_guild_remove(_real_client(), guild)

    assert any(str(G1) in r.getMessage() for r in caplog.records)


# --- daily task --------------------------------------------------------------------------

async def test_daily_not_found_and_uncached_purges(fake_db, cnx, discord_factories):
    client = _real_client()
    client.get_guild = MagicMock(return_value=None)
    client.fetch_guild = AsyncMock(side_effect=discord_factories.http_error(discord.NotFound))
    fake_db.responses = {"FROM server_roles": [(G1, R1)]}

    await main.MyClient._daily_task_once(client)

    assert {p for _, p in _purge_deletes(fake_db)} == {(G1,)}
    assert len(_purge_deletes(fake_db)) == len(TABLES)
    cnx.start_transaction.assert_called_once()  # one purge transaction (the gate also commits)


async def test_daily_fetch_returns_none_and_uncached_purges(fake_db, cnx):
    client = _real_client()
    client.get_guild = MagicMock(return_value=None)
    client.fetch_guild = AsyncMock(return_value=None)
    fake_db.responses = {"FROM server_roles": [(G1, R1)]}

    await main.MyClient._daily_task_once(client)

    assert len(_purge_deletes(fake_db)) == len(TABLES)


async def test_daily_not_found_but_still_cached_does_not_purge(fake_db, cnx, discord_factories):
    client = _real_client()
    client.get_guild = MagicMock(return_value=discord_factories.guild(G1, roles=[]))
    client.fetch_guild = AsyncMock(side_effect=discord_factories.http_error(discord.NotFound))
    fake_db.responses = {"FROM server_roles": [(G1, R1)]}

    await main.MyClient._daily_task_once(client)

    assert _purge_deletes(fake_db) == []
    cnx.start_transaction.assert_not_called()


@pytest.mark.parametrize("exc_type", [discord.Forbidden, discord.HTTPException])
async def test_daily_forbidden_and_generic_http_errors_never_purge(
        fake_db, cnx, discord_factories, exc_type):
    client = _real_client()
    client.get_guild = MagicMock(return_value=None)  # even with an empty cache
    client.fetch_guild = AsyncMock(side_effect=discord_factories.http_error(exc_type))
    fake_db.responses = {"FROM server_roles": [(G1, R1)]}

    await main.MyClient._daily_task_once(client)

    assert _purge_deletes(fake_db) == []


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_daily_server_errors_never_purge(fake_db, cnx, status):
    response = MagicMock()
    response.status = status
    response.reason = "Server Error"
    client = _real_client()
    client.get_guild = MagicMock(return_value=None)
    client.fetch_guild = AsyncMock(side_effect=discord.HTTPException(response, "down"))
    fake_db.responses = {"FROM server_roles": [(G1, R1)]}

    await main.MyClient._daily_task_once(client)

    assert _purge_deletes(fake_db) == []


async def test_daily_other_guilds_still_processed_after_purge(fake_db, cnx, discord_factories):
    guild2 = _live_guild(discord_factories, G2, R2)

    async def fetch_guild(gid):
        if gid == G1:
            raise discord_factories.http_error(discord.NotFound)
        return guild2

    client = _real_client()
    client.get_guild = MagicMock(return_value=None)
    client.fetch_guild = fetch_guild
    fake_db.responses = {"FROM server_roles": [(G1, R1), (G2, R2)]}
    members_fetched = []
    guild2.fetch_members = lambda: (members_fetched.append(G2), _aiter([]))[1]

    await main.MyClient._daily_task_once(client)

    assert {p for _, p in _purge_deletes(fake_db)} == {(G1,)}  # G2 never purged
    assert members_fetched == [G2]  # G2 still reconciled


async def test_daily_db_error_during_purge_reaches_fatal_db_handler(
        monkeypatch, fake_db, cnx, discord_factories):
    """The purge error is re-raised, so the daily task's fatal-DB handler (os._exit) sees it."""
    exits = []
    monkeypatch.setattr(main.os, "_exit", exits.append)
    client = _real_client()
    client.get_guild = MagicMock(return_value=None)
    client.fetch_guild = AsyncMock(side_effect=discord_factories.http_error(discord.NotFound))
    fake_db.responses = {"FROM server_roles": [(G1, R1)]}
    cnx.start_transaction.side_effect = mysql.connector.Error("tx failed")

    await main.MyClient._daily_task_once(client)

    cnx.rollback.assert_called_once()
    assert exits == [1]
