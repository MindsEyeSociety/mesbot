#!/usr/bin/python3
"""One-off remediation: grant the configured event role to everyone who registered.

Why this exists
---------------
The live bot (``main.py``) assigns the event role configured via ``!setevent`` to verified
Discord members whose stored MES membership number
(``user_authorizations.access_token``) matches the free-text
``mes-portal.EventAttendee.membershipNumberSubmitted`` for the event. A buyer who typed
the wrong number (or an email instead of a number) on the Zeffy form is missed by that
path.

How the email match is realised
-------------------------------
``EventAttendee.user_id`` is a foreign key to ``mes-portal.User.id`` that the portal's
Zeffy import populates by resolving the buyer to a real account (including by the email
they entered). Matching on ``user_id`` therefore *subsumes* email-matching: it catches
buyers whose resolved account is known even if their typed membership number was wrong or
blank. This script matches on ``user_id`` **or** ``membershipNumberSubmitted`` to maximise
coverage, then maps the resolved account to a Discord user via the permanent
``user_authorizations`` table.

Scope / safety
--------------
- Keys off the **permanent** ``user_authorizations`` map (Discord id -> MES number); it
  creates **no** new Discord links — only members who already verified are eligible.
- Keeps the membership-validity filter (``membershipExpiration >= CURDATE()``); lapsed
  members are skipped.
- **Idempotent**: members who already hold the role are skipped, so it is safe to re-run.
- Runs against the event(s) configured in ``server_event_roles`` (one today).
- Defaults to **DRY-RUN** (reports only). Pass ``--apply`` to actually grant + notify.

Usage
-----
    python scripts/backfill_event_roles.py            # dry run: print the plan
    python scripts/backfill_event_roles.py --apply    # grant roles + send welcomes

Reads the same ``.env`` as the bot (``TOKEN``, ``DB_*``).
"""

import argparse
import asyncio
import logging
import os
import sys

import discord
import mysql.connector
from dotenv import load_dotenv

# Make the project root (where name_utils.py lives) importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from name_utils import format_member_name  # noqa: E402

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger("backfill_event_roles")


def connect_db():
    """Open a MySQL connection to the bot database using the bot's ``.env`` credentials.

    The connection's default database is ``DB_DATABASE`` (which holds
    ``user_authorizations``, ``server_event_roles``, etc.); portal tables are referenced
    explicitly as ``mes-portal.<Table>``, mirroring how ``main.py`` queries them.

    @return An open ``mysql.connector`` connection.
    @throws mysql.connector.Error if the database cannot be reached.
    """
    return mysql.connector.connect(
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        host=os.getenv('DB_SERVER'),
        database=os.getenv('DB_DATABASE'),
    )


def compute_grants(cursor, event_id, ticketing_id=None, include_zeffy_email=False):
    """Return the verified members eligible for the event role for ``event_id``.

    Matches the permanent ``user_authorizations`` map against the event's attendees by the
    portal-resolved ``EventAttendee.user_id`` (which incorporates the buyer-email match)
    OR by the free-text ``membershipNumberSubmitted``, keeping the membership-validity
    guard. Each returned member is annotated with how they matched so the audit can show
    which grants rely on the resolved account link versus the submitted number.

    When ``include_zeffy_email`` is set (and ``ticketing_id`` is given), it ALSO matches
    raw ``ZeffyDonation`` rows for the event by email (``ZeffyDonation.email`` ->
    ``User.emailAddress``), including ``error``-status donations. This recovers members who
    registered on Zeffy but whose registration failed the portal's processing and so never
    produced an ``EventAttendee`` row. Use deliberately: ``error`` rows include failed/voided
    payments, so review the audit before applying.

    @param ticketing_id: The event's ``PortalEvent.zeffyTicketingId`` (required for the
        Zeffy-email path).
    @return List of dicts: ``discord_user_id``, ``membership_number``, ``name``,
        ``by_userid`` (matched via the portal-resolved account link),
        ``by_number`` (matched via the submitted membership number),
        ``by_zeffy`` (matched via raw ZeffyDonation email, error-recovery path).
    """
    params = [event_id, event_id]
    if include_zeffy_email and ticketing_id:
        zeffy_expr = (
            "EXISTS(SELECT 1 FROM `mes-portal`.ZeffyDonation z "
            "WHERE z.ticketing_id = %s AND z.status IN ('completed','error') "
            "AND LOWER(z.email) = LOWER(u.emailAddress))"
        )
        params.append(ticketing_id)
    else:
        zeffy_expr = "0"

    query = f"""
        SELECT ua.discord_user_id, u.membershipNumber, u.firstName, u.lastName,
               EXISTS(SELECT 1 FROM `mes-portal`.EventAttendee ea
                      WHERE ea.event_id = %s AND ea.user_id = u.id) AS by_userid,
               EXISTS(SELECT 1 FROM `mes-portal`.EventAttendee ea
                      WHERE ea.event_id = %s
                        AND ea.membershipNumberSubmitted = ua.access_token) AS by_number,
               {zeffy_expr} AS by_zeffy, u.nickname
        FROM user_authorizations ua
        JOIN `mes-portal`.User u ON ua.access_token = u.membershipNumber
        WHERE u.membershipExpiration >= CURDATE()
        HAVING by_userid OR by_number OR by_zeffy
    """
    cursor.execute(query, tuple(params))
    grants = []
    for discord_user_id, membership_number, first, last, by_userid, by_number, by_zeffy, nickname in cursor.fetchall():
        grants.append({
            "discord_user_id": discord_user_id,
            "membership_number": membership_number,
            "name": format_member_name(first, last, nickname) or "Unknown",
            "by_userid": bool(by_userid),
            "by_number": bool(by_number),
            "by_zeffy": bool(by_zeffy),
        })
    return grants


def count_unlinked_valid(cursor, event_id):
    """Count valid-membership attendees who have no linked Discord account (cannot be granted).

    These are people who paid for the event and whose membership is current, but who have
    never verified on Discord, so there is no ``user_authorizations`` row to map them to a
    Discord user. They are surfaced for awareness only; the live bot will grant them the
    role automatically within ~60s once they verify.

    @return Integer count of such attendees for ``event_id``.
    """
    cursor.execute("""
        SELECT COUNT(DISTINCT u.id)
        FROM `mes-portal`.EventAttendee ea
        JOIN `mes-portal`.User u ON u.id = ea.user_id
        LEFT JOIN user_authorizations ua ON ua.access_token = u.membershipNumber
        WHERE ea.event_id = %s
          AND u.membershipExpiration >= CURDATE()
          AND ua.discord_user_id IS NULL
    """, (event_id,))
    value = cursor.fetchone()[0]
    cursor.fetchall()
    return value


def build_plan(cnx, include_zeffy_email=False):
    """Build the per-guild remediation plan from the database (no Discord calls).

    Reads every configured event in ``server_event_roles`` and, for each, the eligible
    grant set plus the guild's verification/logging channel ids (so the apply phase needs
    no further DB access) and the count of valid-but-unlinked attendees.

    @param include_zeffy_email: When set, broadens matching to raw ZeffyDonation emails
        (including error-status) via the event's ``PortalEvent.zeffyTicketingId``.
    @return List of plan entries (dicts) with keys: ``guild_id``, ``event_id``,
        ``role_id``, ``ver_channel_id``, ``log_channel_id``, ``grants``, ``unlinked``.
    """
    cursor = cnx.cursor()
    cursor.execute("SELECT guild_id, event_id, role_id FROM server_event_roles")
    events = cursor.fetchall()

    plan = []
    for guild_id, event_id, role_id in events:
        ticketing_id = None
        if include_zeffy_email:
            cursor.execute("SELECT zeffyTicketingId FROM `mes-portal`.PortalEvent WHERE id = %s", (event_id,))
            row = cursor.fetchone()
            cursor.fetchall()
            ticketing_id = row[0] if row else None
        grants = compute_grants(cursor, event_id, ticketing_id, include_zeffy_email)
        unlinked = count_unlinked_valid(cursor, event_id)

        cursor.execute("SELECT channel_id FROM server_verification WHERE guild_id = %s", (guild_id,))
        ver_row = cursor.fetchone()
        cursor.fetchall()
        cursor.execute("SELECT channel_id FROM server_logging WHERE guild_id = %s", (guild_id,))
        log_row = cursor.fetchone()
        cursor.fetchall()

        plan.append({
            "guild_id": guild_id,
            "event_id": event_id,
            "role_id": role_id,
            "ver_channel_id": ver_row[0] if ver_row else None,
            "log_channel_id": log_row[0] if log_row else None,
            "grants": grants,
            "unlinked": unlinked,
        })
    cursor.close()
    return plan


def print_plan(plan):
    """Print the dry-run audit: per event, every candidate and how they matched."""
    if not plan:
        print("No events configured in server_event_roles. Nothing to do.")
        return
    for entry in plan:
        grants = entry["grants"]
        zeffy_only = [g for g in grants if g["by_zeffy"] and not g["by_userid"] and not g["by_number"]]
        print(f"\n=== Guild {entry['guild_id']} | event {entry['event_id']} | "
              f"role {entry['role_id']} ===")
        print(f"  Eligible verified members: {len(grants)} "
              f"(recovered only via raw Zeffy email / error-status: {len(zeffy_only)})")
        print(f"  Valid attendees with no linked Discord (cannot grant yet): {entry['unlinked']}")
        for g in grants:
            if g["by_userid"] or g["by_number"]:
                how = "account+number" if (g["by_userid"] and g["by_number"]) else (
                    "account-link" if g["by_userid"] else "submitted#-only")
            else:
                how = "zeffy-email (error-recovered)"
            print(f"    - {g['name']} (MES {g['membership_number']}, "
                  f"discord {g['discord_user_id']}) [{how}]")


class Backfiller(discord.Client):
    """Discord client that applies a prebuilt remediation plan once, then disconnects.

    For each eligible member it skips anyone who already holds the role (idempotent),
    otherwise assigns it and sends the welcome notifications (member DM, verification
    channel post, admin log line), throttled to respect Discord rate limits.
    """

    def __init__(self, plan, throttle, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.plan = plan
        self.throttle = throttle
        self.totals = {"granted": 0, "already": 0, "missing_member": 0, "errors": 0}

    async def on_ready(self):
        logger.info("Logged in as %s — applying plan.", self.user)
        try:
            for entry in self.plan:
                await self._process_entry(entry)
        finally:
            logger.info("Done. Summary: %s", self.totals)
            await self.close()

    async def _send_channel(self, channel_id, guild, text):
        """Best-effort post to a configured guild channel; never raises."""
        if not channel_id or not guild:
            return
        try:
            channel = await guild.fetch_channel(channel_id)
            if channel:
                await channel.send(text)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
            logger.warning("Could not post to channel %s: %s", channel_id, e)

    async def _process_entry(self, entry):
        guild_id = entry["guild_id"]
        try:
            guild = await self.fetch_guild(guild_id)
        except (discord.NotFound, discord.HTTPException) as e:
            logger.error("Guild %s not reachable: %s", guild_id, e)
            return
        role = discord.utils.get(guild.roles, id=entry["role_id"])
        if not role:
            logger.error("Role %s not found in guild %s; skipping.", entry["role_id"], guild_id)
            return

        for g in entry["grants"]:
            user_id = g["discord_user_id"]
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.HTTPException):
                self.totals["missing_member"] += 1
                logger.info("Member %s not on guild %s; skipping.", user_id, guild_id)
                continue

            if role in member.roles:
                self.totals["already"] += 1
                continue

            try:
                await member.add_roles(role)
                self.totals["granted"] += 1
                logger.info("Granted %s to %s (%s) in %s", role.name, member.display_name,
                            g["membership_number"], guild.name)
            except (discord.Forbidden, discord.HTTPException) as e:
                self.totals["errors"] += 1
                logger.error("Failed to grant %s to %s: %s", role.name, user_id, e)
                await self._send_channel(entry["log_channel_id"], guild,
                                         f"❌ Failed to assign event role {role.name} to {member.display_name}: {e}")
                continue

            welcome = (f"Welcome {member.mention} {g['name']} — you've been granted the "
                       f"event role '{role.name}' on {guild.name}.")
            try:
                await member.send(
                    f"✅ You've been assigned the event role '{role.name}' on {guild.name}.")
            except (discord.Forbidden, discord.HTTPException):
                logger.info("Could not DM %s.", user_id)
            await self._send_channel(entry["ver_channel_id"], guild, welcome)
            await self._send_channel(entry["log_channel_id"], guild,
                                     f"✅ Assigned event role {role.name} to {member.display_name}.")

            await asyncio.sleep(self.throttle)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Actually assign roles and send notifications (default: dry run).")
    parser.add_argument("--throttle", type=float, default=1.0,
                        help="Seconds to sleep between member grants (default: 1.0).")
    parser.add_argument("--include-error-registrants", action="store_true",
                        help="Also recover members from raw ZeffyDonation by email, including "
                             "error-status registrations (failed/voided payments included). "
                             "Review the audit before applying.")
    args = parser.parse_args()

    try:
        cnx = connect_db()
    except mysql.connector.Error as e:
        logger.critical("Database connection failed: %s", e)
        sys.exit(1)

    try:
        plan = build_plan(cnx, args.include_error_registrants)
    finally:
        cnx.close()

    total = sum(len(e["grants"]) for e in plan)
    logger.info("Built plan: %d event(s), %d eligible member-grants.", len(plan), total)
    print_plan(plan)

    if not args.apply:
        print("\nDRY RUN — no roles assigned. Re-run with --apply to grant + notify.")
        return
    if total == 0:
        print("Nothing to apply.")
        return

    token = os.getenv('TOKEN')
    if not token:
        logger.critical("TOKEN not set; cannot connect to Discord.")
        sys.exit(1)

    intents = discord.Intents.default()
    intents.members = True
    intents.guilds = True
    client = Backfiller(plan, args.throttle, intents=intents)
    client.run(token)


if __name__ == "__main__":
    main()
