"""Matching semantics of the shared event-attendee SQL, run against a real SQL engine.

The ``fake_db`` fixture keys canned rows by SQL substring, so it never executes the query. Here
the real ``main.EVENT_ATTENDEE_MATCH_SQL`` (via ``build_event_attendee_query``) runs on in-memory
sqlite (``%s`` -> ``?``, ``CURDATE`` registered, ``mes-portal`` attached), and BOTH production
paths -- the periodic scan and ``assign_event_role`` -- are driven end to end through it. Only the
Discord objects and non-attendee queries (``server_event_roles`` etc.) are faked.

Regression covered: a buyer typed ``US20Q`` into the Zeffy form, but the portal resolved the
registration to her account in ``EventAttendee.user_id``; the old typed-number-only join missed her.
"""
import random
import sqlite3
from datetime import date, timedelta

import pytest

import main
from conftest import FakeCursor

GUILD_ID = 111
EVENT_ID = 222
OTHER_EVENT_ID = 999
ROLE_ID = 333

FUTURE = (date.today() + timedelta(days=200)).isoformat()
PAST = (date.today() - timedelta(days=5)).isoformat()


class SqliteBackedDB:
    """Routes attendee-match queries to sqlite; everything else to the canned FakeCursor map."""

    def __init__(self, responses):
        self.conn = sqlite3.connect(":memory:")
        self.conn.create_function("CURDATE", 0, lambda: date.today().isoformat())
        self.conn.execute(
            "CREATE TABLE user_authorizations (discord_user_id INTEGER, access_token TEXT)")
        self.conn.execute("ATTACH DATABASE ':memory:' AS \"mes-portal\"")
        self.conn.execute(
            "CREATE TABLE \"mes-portal\".User "
            "(id INTEGER PRIMARY KEY, membershipNumber TEXT, membershipExpiration TEXT)")
        self.conn.execute(
            "CREATE TABLE \"mes-portal\".EventAttendee "
            "(id INTEGER PRIMARY KEY, event_id INTEGER, user_id INTEGER, "
            "membershipNumberSubmitted TEXT)")
        self.responses = responses
        self.executed = []

    def add_member(self, user_id, number, discord_id, expires=FUTURE, authorized=True):
        self.conn.execute('INSERT INTO "mes-portal".User VALUES (?, ?, ?)', (user_id, number, expires))
        if authorized:
            self.conn.execute("INSERT INTO user_authorizations VALUES (?, ?)", (discord_id, number))

    def add_registration(self, event_id=EVENT_ID, user_id=None, typed=None):
        self.conn.execute(
            'INSERT INTO "mes-portal".EventAttendee (event_id, user_id, membershipNumberSubmitted) '
            "VALUES (?, ?, ?)", (event_id, user_id, typed))

    def cursor(self):
        return _RoutingCursor(self)


class _RoutingCursor:
    def __init__(self, db):
        self._db = db
        self._fake = FakeCursor(db.responses, db.executed)
        self._real = None

    def execute(self, sql, params=None):
        if "EventAttendee" in sql and "user_authorizations" in sql:
            self._db.executed.append((sql, params))
            self._real = self._db.conn.execute(sql.replace("%s", "?"), params or ())
        else:
            self._real = None
            self._fake.execute(sql, params)

    def fetchall(self):
        return self._real.fetchall() if self._real is not None else self._fake.fetchall()

    def fetchone(self):
        return self._real.fetchone() if self._real is not None else self._fake.fetchone()

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    database = SqliteBackedDB({
        "SELECT guild_id, event_id, role_id FROM server_event_roles": [(GUILD_ID, EVENT_ID, ROLE_ID)],
        "SELECT event_id, role_id FROM server_event_roles": [(EVENT_ID, ROLE_ID)],
    })
    monkeypatch.setattr(main, "get_cursor", database.cursor)
    return database


def _member_ids_from_query(db):
    """Run the shared scan query directly and return the matched discord ids (with duplicates)."""
    cur = db.cursor()
    cur.execute(main.build_event_attendee_query("SELECT DISTINCT ua.discord_user_id"), (EVENT_ID,))
    return [row[0] for row in cur.fetchall()]


async def _scan_grants(db, discord_factories, discord_ids):
    """Drive the real scan; return the set of discord ids that were granted the role."""
    role = discord_factories.role(ROLE_ID)
    guild = discord_factories.guild(GUILD_ID, roles=[role])
    members = {i: discord_factories.member(i, roles=[]) for i in discord_ids}
    guild.get_member.side_effect = members.get
    client = discord_factories.client()
    client.get_guild.return_value = guild
    await main.MyClient._check_user_states_once(client)
    return {i for i, m in members.items() if m.add_roles.await_count}


async def _join_grants(db, discord_factories, discord_id):
    """Drive the real on-join path for one member; return True if the role was granted."""
    role = discord_factories.role(ROLE_ID)
    member = discord_factories.member(discord_id, roles=[])
    member.guild = discord_factories.guild(GUILD_ID, roles=[role])
    client = discord_factories.client()
    await main.MyClient.assign_event_role(client, member)
    return bool(member.add_roles.await_count)


async def _both_paths(db, discord_factories, discord_id):
    scan = discord_id in await _scan_grants(db, discord_factories, [discord_id])
    join = await _join_grants(db, discord_factories, discord_id)
    assert scan == join, "scan and on-join paths must agree"
    return scan


async def test_garbage_typed_number_matched_via_resolved_user_id(db, discord_factories):
    db.add_member(1, "US2019010018", 5001)
    db.add_registration(user_id=1, typed="US20Q")  # the real regression
    assert await _both_paths(db, discord_factories, 5001) is True


async def test_correct_typed_number_without_user_id_still_matches(db, discord_factories):
    db.add_member(1, "US2019010018", 5001)
    db.add_registration(user_id=None, typed="US2019010018")  # fallback kept
    assert await _both_paths(db, discord_factories, 5001) is True


async def test_other_event_registration_not_matched(db, discord_factories):
    db.add_member(1, "US2019010018", 5001)
    db.add_registration(event_id=OTHER_EVENT_ID, user_id=1, typed="US2019010018")
    assert await _both_paths(db, discord_factories, 5001) is False


async def test_expired_membership_not_matched(db, discord_factories):
    db.add_member(1, "US2019010018", 5001, expires=PAST)
    db.add_registration(user_id=1, typed="US2019010018")
    assert await _both_paths(db, discord_factories, 5001) is False


async def test_member_not_in_user_authorizations_not_matched(db, discord_factories):
    db.add_member(1, "US2019010018", 5001, authorized=False)
    db.add_registration(user_id=1, typed="US2019010018")
    assert await _both_paths(db, discord_factories, 5001) is False


async def test_someone_else_user_id_does_not_match_this_member(db, discord_factories):
    db.add_member(1, "US2019010018", 5001)
    db.add_member(2, "US2019010019", 5002)
    db.add_registration(user_id=2, typed="US20Q")  # belongs to member 2 only
    assert await _scan_grants(db, discord_factories, [5001, 5002]) == {5002}


def test_row_matching_both_ways_appears_once_in_scan(db):
    db.add_member(1, "US2019010018", 5001)
    db.add_registration(user_id=1, typed="US2019010018")  # matches by id AND number
    db.add_registration(user_id=1, typed="US2019010018")  # even duplicate rows
    assert _member_ids_from_query(db) == [5001]


@pytest.mark.parametrize("seed", range(25))
async def test_random_populations_match_independent_expectation(seed, db, discord_factories):
    rng = random.Random(seed)
    expected = set()
    for n in range(1, 13):
        number = f"US{n:010d}"
        discord_id = 6000 + n
        expires = rng.choice([FUTURE, PAST])
        authorized = rng.random() < 0.8
        db.add_member(n, number, discord_id, expires=expires, authorized=authorized)
        registered_this_event = False
        for _ in range(rng.randint(0, 2)):
            event = rng.choice([EVENT_ID, OTHER_EVENT_ID])
            style = rng.choice(["id_only", "number_only", "both", "garbage"])
            user_id = n if style in ("id_only", "both") else None
            typed = {"number_only": number, "both": number, "garbage": "US20Q"}.get(style)
            db.add_registration(event_id=event, user_id=user_id, typed=typed)
            # "garbage" with no user_id matches nobody; every other style matches this member.
            if event == EVENT_ID and style != "garbage":
                registered_this_event = True
        if registered_this_event and expires == FUTURE and authorized:
            expected.add(discord_id)

    all_ids = [6000 + n for n in range(1, 13)]
    assert set(_member_ids_from_query(db)) == expected
    assert len(_member_ids_from_query(db)) == len(expected)  # no duplicates
    assert await _scan_grants(db, discord_factories, all_ids) == expected
    for discord_id in all_ids:
        assert await _join_grants(db, discord_factories, discord_id) == (discord_id in expected)
