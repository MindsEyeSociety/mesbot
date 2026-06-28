"""Shared fixtures for the mesbot test suite.

Importing ``main`` runs module-level code that opens a MySQL connection and would,
without a guard, start the Discord bot. We patch ``mysql.connector.connect`` to a
``MagicMock`` *before* importing ``main`` so the suite never touches a real
database, and rely on the ``if __name__ == "__main__"`` guard in ``main`` to keep
``client.run`` from firing on import.

The fixtures here let a test drive ``main``'s two external dependencies with
controlled fakes:
- the database, via a content-aware cursor that maps SQL fragments to result rows;
- Discord I/O, via plain ``MagicMock``/``AsyncMock`` stand-ins for guild, member,
  role and the client coroutines the code under test calls.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Make the project root (where main.py lives) importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Neutralize the module-level DB connect before importing main.
import mysql.connector  # noqa: E402

mysql.connector.connect = MagicMock(name="connect")

import discord  # noqa: E402

# main.py uses datetime.UTC, which only exists on Python 3.11+. The bot runs on a
# 3.11+ host; this shim lets the suite drive the same code under an older local
# interpreter without altering production behavior.
import datetime as _datetime  # noqa: E402

if not hasattr(_datetime, "UTC"):
    _datetime.UTC = _datetime.timezone.utc

import main  # noqa: E402


class FakeCursor:
    """Content-aware stand-in for a ``mysql.connector`` cursor.

    ``execute`` records each ``(sql, params)`` pair into a shared list and selects
    the rows the next fetch returns by matching the SQL against the fragments in
    ``responses`` — the first fragment found as a substring wins. A query that
    matches no fragment yields no rows, so query branches a test does not care
    about behave as empty result sets. Matching is on SQL content, not call order,
    so tests are robust to the order in which the production code issues queries.

    @param responses: Mapping of SQL fragment -> list of result-row tuples.
    @param executed: Shared list every ``execute`` appends ``(sql, params)`` to.
    """

    def __init__(self, responses, executed):
        self._responses = responses
        self._executed = executed
        self._rows = []

    def execute(self, sql, params=None):
        self._executed.append((sql, params))
        self._rows = []
        for fragment, rows in self._responses.items():
            if fragment in sql:
                self._rows = list(rows)
                break

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class FakeDB:
    """Per-test SQL->rows map plus a log of every statement executed.

    ``cursor`` hands out fresh :class:`FakeCursor` instances that share this DB's
    ``responses`` and ``executed`` log, mirroring how the production code opens a
    new cursor for nearly every query.
    """

    def __init__(self):
        self.responses = {}
        self.executed = []

    def cursor(self):
        return FakeCursor(self.responses, self.executed)

    @property
    def sql(self):
        """Every SQL string executed so far — for asserting which queries were sent."""
        return [sql for sql, _ in self.executed]


@pytest.fixture
def fake_db(monkeypatch):
    """Patch ``main.get_cursor`` to hand out content-aware fake cursors.

    Returns the :class:`FakeDB`; set ``.responses`` before invoking the code under
    test and read ``.sql`` / ``.executed`` afterwards.
    """
    db = FakeDB()
    monkeypatch.setattr(main, "get_cursor", db.cursor)
    return db


def _make_role(role_id, name="Event Attendee"):
    role = MagicMock(name=f"role<{role_id}>")
    role.id = role_id
    role.name = name
    return role


def _make_member(member_id, roles, display_name="Tester"):
    member = MagicMock(name=f"member<{member_id}>")
    member.id = member_id
    member.display_name = display_name
    member.name = display_name
    member.roles = list(roles)
    member.add_roles = AsyncMock(name="add_roles")
    member.remove_roles = AsyncMock(name="remove_roles")
    member.send = AsyncMock(name="send")
    return member


def _make_guild(guild_id, roles, name="Test Guild"):
    guild = MagicMock(name=f"guild<{guild_id}>")
    guild.id = guild_id
    guild.name = name
    guild.roles = list(roles)
    # get_member is the cache lookup (synchronous); fetch_member is the bounded REST fallback the
    # event scan uses only on a cache miss (throttled by a negative cache). The scan must never
    # call query_members — it deadlocks against discord.py's automatic guild re-chunking.
    guild.get_member = MagicMock(name="get_member")
    guild.fetch_member = AsyncMock(name="fetch_member")
    guild.query_members = AsyncMock(name="query_members")  # present only to assert it stays unused
    return guild


def _make_client():
    """A fake ``self`` exposing the async client methods the coroutines call."""
    client = MagicMock(name="MyClient")
    # get_guild is the cache lookup (synchronous) the event scan uses; fetch_guild is
    # the REST call it must NOT use — kept so tests can assert it is never awaited.
    client.get_guild = MagicMock(name="get_guild")
    client.fetch_guild = AsyncMock(name="fetch_guild")
    client.fetch_member = AsyncMock(name="fetch_member")
    client.fetch_user = AsyncMock(name="fetch_user")
    client.get_ver_channel = AsyncMock(name="get_ver_channel", return_value=None)
    client.log_message = AsyncMock(name="log_message")
    return client


def _http_error(exc_type=discord.HTTPException):
    """Build a discord HTTP error for simulating a failed fetch/role grant."""
    response = MagicMock()
    response.status = 404
    response.reason = "Not Found"
    return exc_type(response, "simulated")


@pytest.fixture
def discord_factories():
    """Builders for the fake Discord objects, exposed as one namespace.

    Example::

        role = discord_factories.role(333)
        guild = discord_factories.guild(111, roles=[role])
        member = discord_factories.member(444, roles=[])
    """
    return SimpleNamespace(
        role=_make_role,
        member=_make_member,
        guild=_make_guild,
        client=_make_client,
        http_error=_http_error,
    )
