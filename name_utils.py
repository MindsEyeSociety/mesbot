"""Name-formatting helpers shared by the bot and its maintenance scripts.

Members sign up with their legal first/last name, but the MES Membership Handbook lets
them use a preferred name or nickname on public correspondence. The portal stores that
choice in ``mes-portal.User.nickname``; this module centralises the one rule for turning
those columns into the string the bot shows in public channels.
"""


def format_member_name(first_name, last_name, nickname):
    """Build the public display name, preferring the member's chosen nickname.

    Used for member-facing text posted to public Discord channels (e.g. the verification
    welcome). When the member has set a nickname it replaces the legal first name while the
    surname is kept for identifiability; otherwise the legal first name is used. All inputs
    are treated as optional free text: ``None`` and whitespace-only values are ignored, and
    the result is trimmed.

    @param first_name The member's legal first name from ``mes-portal.User.firstName``.
    @param last_name The member's legal last name from ``mes-portal.User.lastName``.
    @param nickname The member's preferred name from ``mes-portal.User.nickname``; blank or
        ``None`` falls back to the legal first name.
    @return The display name, e.g. ``"Andy Sutton"`` (nickname set) or ``"Andrew Sutton"``
        (nickname blank). Returns ``""`` when every field is empty.

    Example — nickname preferred over the legal first name:
        >>> format_member_name("Andrew", "Sutton", "Andy")
        'Andy Sutton'

    Example — blank nickname falls back to the legal first name:
        >>> format_member_name("Andrew", "Sutton", "")
        'Andrew Sutton'
    """
    nick = (nickname or "").strip()
    last = (last_name or "").strip()
    if nick:
        return f"{nick} {last}".strip()
    return f"{(first_name or '').strip()} {last}".strip()
