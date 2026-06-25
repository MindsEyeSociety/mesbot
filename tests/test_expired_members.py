"""The expired-member audit log and the once-per-day gate helpers.

`expired_members` is created on startup (no migration system) and appended whenever a
membership lapses, so there's a durable record of who expired. The `daily_tasks` gate
helpers were extracted so each daily section can be marked done only on completion.
"""
import datetime

import main


def test_ensure_schema_creates_expired_members(fake_db):
    main.MyClient._ensure_schema(object())
    assert any("CREATE TABLE IF NOT EXISTS expired_members" in s for s in fake_db.sql)


def test_record_expired_member_upserts(fake_db):
    exp = datetime.date(2025, 2, 1)
    main.MyClient._record_expired_member(object(), 629679322210369537, "US2002023241", exp)

    inserts = [(s, p) for s, p in fake_db.executed
               if "expired_members" in s and "INSERT" in s.upper()]
    assert inserts, "no INSERT into expired_members"
    sql, params = inserts[0]
    assert "ON DUPLICATE KEY UPDATE" in sql  # one row per user, refreshed on re-expiry
    assert params == (629679322210369537, "US2002023241", exp)


def test_gate_should_run_true_when_never_run(fake_db):
    fake_db.responses = {}  # daily_tasks lookup returns nothing
    assert main.MyClient._gate_should_run(object(), "daily_maintenance") is True


def test_gate_should_run_false_when_done_today(fake_db):
    fake_db.responses = {"last_run_date FROM daily_tasks": [(datetime.date.today(),)]}
    assert main.MyClient._gate_should_run(object(), "daily_maintenance") is False


def test_gate_mark_done_upserts_today(fake_db):
    main.MyClient._gate_mark_done(object(), "expiration_notifications")

    rows = [(s, p) for s, p in fake_db.executed
            if "daily_tasks" in s and "INSERT" in s.upper()]
    assert rows
    sql, params = rows[0]
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert params[0] == "expiration_notifications"
    assert params[1] == datetime.date.today()
