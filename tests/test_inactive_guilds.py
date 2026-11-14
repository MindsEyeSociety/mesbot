"""A guild the bot has left is marked inactive (``server_roles.active``), never deleted.

These tests drive the REAL ``deactivate_guild``, ``reactivate_guild``, ``_migrate_server_roles_active``,
``on_guild_remove``/``on_guild_join``, ``on_message`` (``!role``) and ``_daily_task_once`` on a real
``MyClient``; only Discord I/O and the database are faked.
"""
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import mysql.connector
import pytest

import main

G1, R1, G2, R2 = 111, 1111, 222, 2222


async def _aiter(items):
    for item in items:
        yield item


def _real_client():
    client = main.MyClient(intents=discord.Intents.none())
    client.wait_until_ready = AsyncMock()
    client.log_message = AsyncMock()
    return client


def _deletes(fake_db):
    return [s for s in fake_db.sql if s.lstrip().upper().startswith("DELETE")]


def _updates(fake_db):
    return [(s, p) for s, p in fake_db.executed if s.startswith("UPDATE server_roles")]


@pytest.fixture
def cnx(monkeypatch):
    fake = MagicMock(name="cnx")
    monkeypatch.setattr(main, "cnx", fake)
    return fake


@pytest.fixture
def rowcount_db(monkeypatch, fake_db):
    """fake_db whose cursors report ``fake_db.rowcount`` after each execute (UPDATE results)."""
    fake_db.rowcount = 1
    real_cursor = fake_db.cursor

    def cursor():
        c = real_cursor()
        original = c.execute

        def execute(sql, params=None):
            original(sql, params)
            c.rowcount = fake_db.rowcount

        c.execute = execute
        return c

    monkeypatch.setattr(main, "get_cursor", cursor)
    return fake_db


def _live_guild(discord_factories, guild_id, role_id):
    role = discord_factories.role(role_id)
    guild = discord_factories.guild(guild_id, roles=[role])
    guild.fetch_members = lambda: _aiter([])
    return guild


# --- schema migration --------------------------------------------------------------------

def test_migration_runs_alter_when_column_missing(fake_db, caplog):
    fake_db.responses = {}  # information_schema lookup returns no row
    with caplog.at_level(logging.INFO, logger="main"):
        assert main._migrate_server_roles_active() is True

    lookup = [s for s in fake_db.sql if "information_schema.COLUMNS" in s]
    assert len(lookup) == 1
    assert "TABLE_SCHEMA = DATABASE()" in lookup[0] and "COLUMN_NAME = 'active'" in lookup[0]
    assert "ALTER TABLE server_roles ADD COLUMN active BOOLEAN NOT NULL DEFAULT TRUE" in fake_db.sql
    assert any("active column" in r.getMessage() for r in caplog.records)


def test_migration_skips_alter_when_column_present(fake_db):
    fake_db.responses = {"information_schema.COLUMNS": [("active",)]}

    assert main._migrate_server_roles_active() is False

    assert not any("ALTER TABLE" in s for s in fake_db.sql)


def test_ensure_schema_runs_the_migration(fake_db, cnx):
    main.MyClient._ensure_schema(object())

    assert any("information_schema.COLUMNS" in s for s in fake_db.sql)
    assert any("ALTER TABLE server_roles ADD COLUMN active" in s for s in fake_db.sql)


# --- deactivate_guild / reactivate_guild -------------------------------------------------

def test_deactivate_is_transition_only_update_and_warns(rowcount_db, caplog):
    with caplog.at_level(logging.WARNING, logger="main"):
        assert main.deactivate_guild(G1) is True

    assert _updates(rowcount_db) == [(
        "UPDATE server_roles SET active = FALSE WHERE guild_id = %s AND active = TRUE", (G1,))]
    warnings = [r for r in caplog.records if str(G1) in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "inactive" in warnings[0].getMessage() and "kept" in warnings[0].getMessage()


def test_deactivate_repeat_changes_nothing_and_does_not_warn(rowcount_db, caplog):
    rowcount_db.rowcount = 0  # already inactive: the guarded UPDATE matches no row
    with caplog.at_level(logging.WARNING, logger="main"):
        assert main.deactivate_guild(G1) is False

    assert caplog.records == []


def test_reactivate_is_transition_only_update_and_logs_info(rowcount_db, caplog):
    with caplog.at_level(logging.INFO, logger="main"):
        assert main.reactivate_guild(G1) is True

    assert _updates(rowcount_db) == [(
        "UPDATE server_roles SET active = TRUE WHERE guild_id = %s AND active = FALSE", (G1,))]
    assert [r.levelno for r in caplog.records if str(G1) in r.getMessage()] == [logging.INFO]


def test_reactivate_when_not_inactive_is_silent_noop(rowcount_db, caplog):
    rowcount_db.rowcount = 0
    with caplog.at_level(logging.INFO, logger="main"):
        assert main.reactivate_guild(G1) is False

    assert caplog.records == []


def test_deactivate_and_reactivate_propagate_db_errors(monkeypatch):
    def boom():
        raise mysql.connector.Error("db down")

    monkeypatch.setattr(main, "get_cursor", boom)
    with pytest.raises(mysql.connector.Error):
        main.deactivate_guild(G1)
    with pytest.raises(mysql.connector.Error):
        main.reactivate_guild(G1)


# --- guild events ------------------------------------------------------------------------

async def test_on_guild_remove_deactivates_that_guild_without_deleting(
        rowcount_db, cnx, discord_factories):
    await main.MyClient.on_guild_remove(_real_client(), discord_factories.guild(G1, roles=[]))

    assert [p for _, p in _updates(rowcount_db)] == [(G1,)]
    assert "active = FALSE" in _updates(rowcount_db)[0][0]
    assert _deletes(rowcount_db) == []


async def test_on_guild_remove_swallows_db_error(monkeypatch, discord_factories, caplog):
    def boom():
        raise mysql.connector.Error("db down")

    monkeypatch.setattr(main, "get_cursor", boom)
    with caplog.at_level(logging.ERROR, logger="main"):
        await main.MyClient.on_guild_remove(_real_client(), discord_factories.guild(G1, roles=[]))

    assert any(str(G1) in r.getMessage() for r in caplog.records)


async def test_on_guild_join_reactivates(rowcount_db, discord_factories):
    await main.MyClient.on_guild_join(_real_client(), discord_factories.guild(G1, roles=[]))

    assert [p for _, p in _updates(rowcount_db)] == [(G1,)]
    assert "active = TRUE" in _updates(rowcount_db)[0][0]


async def test_on_guild_join_swallows_db_error(monkeypatch, discord_factories, caplog):
    def boom():
        raise mysql.connector.Error("db down")

    monkeypatch.setattr(main, "get_cursor", boom)
    with caplog.at_level(logging.ERROR, logger="main"):
        await main.MyClient.on_guild_join(_real_client(), discord_factories.guild(G1, roles=[]))

    assert any(str(G1) in r.getMessage() for r in caplog.records)


# --- !role upsert ------------------------------------------------------------------------

async def test_role_command_upsert_sets_active(fake_db, cnx, discord_factories):
    role = discord_factories.role(R1)
    guild = discord_factories.guild(G1, roles=[role])
    message = MagicMock()
    message.author.id = 5
    message.author.guild_permissions.administrator = True
    message.guild = guild
    message.content = f"!role {R1}"
    message.reply = AsyncMock()
    fake_self = MagicMock()
    fake_self.user.id = 999

    await main.MyClient.on_message(fake_self, message)

    upserts = [(s, p) for s, p in fake_db.executed if s.startswith("INSERT INTO server_roles")]
    assert len(upserts) == 1
    sql, params = upserts[0]
    assert "active" in sql and "ON DUPLICATE KEY UPDATE role_id = %s, active = TRUE" in sql
    assert params == (G1, R1, R1)


# --- daily task --------------------------------------------------------------------------

async def test_daily_query_filters_on_active_and_deletes_nothing(fake_db, cnx):
    client = _real_client()
    client.get_guild = MagicMock(return_value=None)

    await main.MyClient._daily_task_once(client)

    main_queries = [s for s in fake_db.sql if s.startswith("SELECT guild_id, role_id FROM server_roles")]
    assert main_queries == ["SELECT guild_id, role_id FROM server_roles WHERE active = TRUE"]
    assert not any("DELETE FROM server_roles" in s or "DELETE FROM server_logging" in s
                   or "DELETE FROM server_verification" in s or "DELETE FROM server_event_roles" in s
                   for s in fake_db.sql)


async def test_daily_reactivates_inactive_guild_back_in_cache(rowcount_db, cnx, discord_factories):
    rowcount_db.responses = {"WHERE active = FALSE": [(G1,), (G2,)]}
    client = _real_client()
    cached = discord_factories.guild(G1, roles=[])
    client.get_guild = MagicMock(side_effect=lambda gid: cached if gid == G1 else None)

    await main.MyClient._daily_task_once(client)

    assert [p for _, p in _updates(rowcount_db)] == [(G1,)]  # G2 is not cached: stays inactive


async def test_daily_reactivate_pass_runs_before_main_query(rowcount_db, cnx, discord_factories):
    rowcount_db.responses = {"WHERE active = FALSE": [(G1,)]}
    client = _real_client()
    client.get_guild = MagicMock(return_value=discord_factories.guild(G1, roles=[]))

    await main.MyClient._daily_task_once(client)

    sql = rowcount_db.sql
    reactivate = next(i for i, s in enumerate(sql) if s.startswith("UPDATE server_roles SET active = TRUE"))
    main_query = sql.index("SELECT guild_id, role_id FROM server_roles WHERE active = TRUE")
    assert reactivate < main_query


async def test_daily_not_found_and_uncached_deactivates(rowcount_db, cnx, discord_factories):
    client = _real_client()
    client.get_guild = MagicMock(return_value=None)
    client.fetch_guild = AsyncMock(side_effect=discord_factories.http_error(discord.NotFound))
    rowcount_db.responses = {"WHERE active = TRUE": [(G1, R1)]}

    await main.MyClient._daily_task_once(client)

    assert [p for _, p in _updates(rowcount_db)] == [(G1,)]
    assert "active = FALSE" in _updates(rowcount_db)[0][0]
    assert _deletes(rowcount_db) == []


async def test_daily_fetch_returns_none_and_uncached_deactivates(rowcount_db, cnx):
    client = _real_client()
    client.get_guild = MagicMock(return_value=None)
    client.fetch_guild = AsyncMock(return_value=None)
    rowcount_db.responses = {"WHERE active = TRUE": [(G1, R1)]}

    await main.MyClient._daily_task_once(client)

    assert [p for _, p in _updates(rowcount_db)] == [(G1,)]


async def test_daily_not_found_but_still_cached_does_not_deactivate(
        rowcount_db, cnx, discord_factories):
    client = _real_client()
    client.get_guild = MagicMock(return_value=discord_factories.guild(G1, roles=[]))
    client.fetch_guild = AsyncMock(side_effect=discord_factories.http_error(discord.NotFound))
    rowcount_db.responses = {"WHERE active = TRUE": [(G1, R1)]}

    await main.MyClient._daily_task_once(client)

    assert not any("active = FALSE" in s for s, _ in _updates(rowcount_db))


@pytest.mark.parametrize("exc_type", [discord.Forbidden, discord.HTTPException])
async def test_daily_forbidden_and_generic_http_errors_never_deactivate(
        rowcount_db, cnx, discord_factories, exc_type):
    client = _real_client()
    client.get_guild = MagicMock(return_value=None)  # even with an empty cache
    client.fetch_guild = AsyncMock(side_effect=discord_factories.http_error(exc_type))
    rowcount_db.responses = {"WHERE active = TRUE": [(G1, R1)]}

    await main.MyClient._daily_task_once(client)

    assert not any("active = FALSE" in s for s, _ in _updates(rowcount_db))


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_daily_server_errors_never_deactivate(rowcount_db, cnx, status):
    response = MagicMock()
    response.status = status
    response.reason = "Server Error"
    client = _real_client()
    client.get_guild = MagicMock(return_value=None)
    client.fetch_guild = AsyncMock(side_effect=discord.HTTPException(response, "down"))
    rowcount_db.responses = {"WHERE active = TRUE": [(G1, R1)]}

    await main.MyClient._daily_task_once(client)

    assert not any("active = FALSE" in s for s, _ in _updates(rowcount_db))


async def test_daily_other_guilds_still_processed_after_deactivation(
        rowcount_db, cnx, discord_factories):
    guild2 = _live_guild(discord_factories, G2, R2)

    async def fetch_guild(gid):
        if gid == G1:
            raise discord_factories.http_error(discord.NotFound)
        return guild2

    client = _real_client()
    client.get_guild = MagicMock(return_value=None)
    client.fetch_guild = fetch_guild
    rowcount_db.responses = {"WHERE active = TRUE": [(G1, R1), (G2, R2)]}
    members_fetched = []
    guild2.fetch_members = lambda: (members_fetched.append(G2), _aiter([]))[1]

    await main.MyClient._daily_task_once(client)

    assert [p for _, p in _updates(rowcount_db)] == [(G1,)]  # G2 never deactivated
    assert members_fetched == [G2]  # G2 still reconciled


async def test_daily_db_error_during_deactivation_reaches_fatal_db_handler(
        monkeypatch, fake_db, cnx, discord_factories):
    """The error is re-raised, so the daily task's fatal-DB handler (os._exit) sees it."""
    exits = []
    monkeypatch.setattr(main.os, "_exit", exits.append)
    client = _real_client()
    client.get_guild = MagicMock(return_value=None)
    client.fetch_guild = AsyncMock(side_effect=discord_factories.http_error(discord.NotFound))
    fake_db.responses = {"WHERE active = TRUE": [(G1, R1)]}
    real_cursor = fake_db.cursor

    def cursor():
        c = real_cursor()
        original = c.execute

        def execute(sql, params=None):
            if sql.startswith("UPDATE server_roles SET active = FALSE"):
                raise mysql.connector.Error("update failed")
            original(sql, params)

        c.execute = execute
        return c

    monkeypatch.setattr(main, "get_cursor", cursor)

    await main.MyClient._daily_task_once(client)

    assert exits == [1]
