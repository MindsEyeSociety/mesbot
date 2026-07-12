"""The OAuth membership gate and the autocommit setting both connections depend on.

Two production defects are pinned here.

**The membership-type gate.** ``oauth_callback`` admitted a hardcoded ``{"Full", "Trial"}``,
which silently rejected ``Monthly`` subscribers — real, paid-up members — with a message telling
them to obtain a membership they already had. The gate is exercised through the pure
``is_membership_type_valid`` rather than the Flask route, so the rule is tested without standing
up the OAuth provider and the portal user API.

**Autocommit.** The server runs REPEATABLE READ, and mysql-connector defaults autocommit to
False, so a connection's first SELECT pins a snapshot that lasts until the transaction ends. The
bot's loops read far more often than they write, so an idle pass never commits, the snapshot
never refreshes, and rows committed by the *other* process become permanently invisible —
stranding members who verified successfully. Like the SQL gate in ``test_membership_gate``, this
cannot be observed without a live database, so the tests assert the connection is *opened* with
the setting that prevents it.
"""
from unittest.mock import MagicMock

import pytest

import main
import server

# Every membershipType the portal actually carries, split by whether it may verify.
GRANTS_ACCESS = ["Full", "Trial", "Monthly"]
DENIES_ACCESS = ["None", "DNR", "Expelled", "Resigned", "0", "", None]


@pytest.mark.parametrize("membership_type", GRANTS_ACCESS)
def test_paying_and_trialling_members_are_admitted(membership_type):
    assert server.is_membership_type_valid(membership_type) is True


@pytest.mark.parametrize("membership_type", DENIES_ACCESS)
def test_non_members_and_removed_members_are_rejected(membership_type):
    assert server.is_membership_type_valid(membership_type) is False


def test_monthly_subscribers_are_admitted():
    """Regression: active Monthly subscribers were locked out of Discord entirely.

    The gate hardcoded {"Full", "Trial"}, so a current Monthly member hit
    "Invalid membership, you must have a Trial or Full membership" on every attempt.
    """
    assert "Monthly" in server.VALID_MEMBERSHIP_TYPES
    assert server.is_membership_type_valid("Monthly") is True


def test_expiry_is_gated_separately_from_type():
    """A valid *type* alone must not grant access — expiry is the other half of the gate."""
    assert server.is_membership_type_valid("Monthly") is True
    assert server.is_membership_expired("2020-01-01") is True


@pytest.mark.parametrize("module", [main, server], ids=["main", "server"])
def test_db_connections_enable_autocommit(module, monkeypatch):
    """Both processes must open their connection with autocommit ON.

    Without it, REPEATABLE READ pins each connection's read snapshot at its first SELECT and the
    read-only loop passes never commit, so one process stops seeing the other's committed rows.
    """
    connect = MagicMock(name="connect")
    monkeypatch.setattr(module.mysql.connector, "connect", connect)

    module.db_connect()

    connect.assert_called_once()
    assert connect.call_args.kwargs["autocommit"] is True


@pytest.mark.parametrize("module", [main, server], ids=["main", "server"])
def test_stale_connection_reconnects_with_autocommit(module, monkeypatch):
    """The ping/reconnect path must not hand back a connection that lost the setting.

    ``get_cursor`` rebuilds the global connection when the old one goes stale; if that path
    bypassed ``db_connect`` the snapshot bug would silently return after any DB blip.
    """
    fresh = MagicMock(name="fresh_cnx")
    monkeypatch.setattr(module, "db_connect", MagicMock(return_value=fresh))

    stale = MagicMock(name="stale_cnx")
    stale.ping.side_effect = module.mysql.connector.Error("server has gone away")
    monkeypatch.setattr(module, "cnx", stale)

    cursor = module.get_cursor()

    module.db_connect.assert_called_once()
    assert cursor is fresh.cursor.return_value
