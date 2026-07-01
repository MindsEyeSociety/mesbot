"""Unit tests for the pure ``format_member_name`` helper.

The MES Membership Handbook lets members show a preferred name/nickname on public
correspondence. ``format_member_name`` encodes that one rule; these cases pin the
nickname-preferred behavior and the legal-name fallback, including the messy inputs the
portal can hand us (``None``, empty, whitespace-only).
"""
import sys
from pathlib import Path

# Make the project root (where name_utils.py lives) importable, mirroring conftest.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from name_utils import format_member_name


def test_nickname_replaces_legal_first_name():
    # A distinct nickname is preferred; the surname is kept for identifiability.
    assert format_member_name("Andrew", "Sutton", "Andy") == "Andy Sutton"


def test_handle_style_nickname_is_used_verbatim():
    assert format_member_name("Izabella", "Konzem", "Toast") == "Toast Konzem"


def test_none_nickname_falls_back_to_legal_first_name():
    assert format_member_name("Andrew", "Sutton", None) == "Andrew Sutton"


def test_empty_nickname_falls_back_to_legal_first_name():
    assert format_member_name("Andrew", "Sutton", "") == "Andrew Sutton"


def test_whitespace_only_nickname_falls_back_to_legal_first_name():
    assert format_member_name("Andrew", "Sutton", "   ") == "Andrew Sutton"


def test_nickname_equal_to_first_name_is_harmless():
    assert format_member_name("Michael", "Fulton", "Michael") == "Michael Fulton"


def test_surrounding_whitespace_is_trimmed():
    assert format_member_name("Andrew", "Sutton", "  Andy  ") == "Andy Sutton"


def test_all_fields_empty_returns_empty_string():
    # Lets callers apply their own fallback (e.g. the backfill's "Unknown").
    assert format_member_name("", "", "") == ""
    assert format_member_name(None, None, None) == ""


def test_missing_surname_with_nickname_returns_nickname_only():
    assert format_member_name("Andrew", "", "Andy") == "Andy"
