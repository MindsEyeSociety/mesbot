#!/usr/bin/python3

import time
import asyncio
import logging
from discord.ext import tasks
import uuid
import datetime
import requests
import mysql.connector
from mysql.connector import errorcode
import os
from dotenv import load_dotenv
import discord
from urllib.parse import urlencode, urlparse, parse_qs
from flask import Flask, request, redirect

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


def get_cursor():
    """Return a DB cursor, reconnecting the global connection if it went stale.

    Tries ping/reconnect first; falls back to a fresh connection. Raises
    mysql.connector.Error if the DB cannot be reached after all attempts.
    """
    global cnx
    try:
        cnx.ping(reconnect=True, attempts=3, delay=2)
    except mysql.connector.Error:
        cnx = mysql.connector.connect(
            user=os.getenv('DB_USER'),
            password=os.getenv('DB_PASSWORD'),
            host=os.getenv('DB_SERVER'),
            database=os.getenv('DB_DATABASE')
        )
    return cnx.cursor()


class MyClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.check_user_states_task = None  # Store the task reference

    async def setup_hook(self):
        """ Ensures tasks are started after the bot is ready """
        self._ensure_schema()
        if not self.check_user_states.is_running():
            self.check_user_states.start()  # Start the background loop safely
            logger.info("✅ check_user_states loop started!")
        if not self.daily_task.is_running():
            self.daily_task.start()
            logger.info("✅ daily_task loop started!")
        await super().setup_hook()

    def _ensure_schema(self):
        """Create bot-owned tables that aren't part of the base schema, if missing.

        Idempotent (CREATE TABLE IF NOT EXISTS), so it's safe to run on every startup;
        this is how new bot tables ship since the project has no migration system. Currently
        ensures `expired_members`, the audit log of memberships that have lapsed.

        @see _record_expired_member
        """
        cursor = get_cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS expired_members (
                discord_user_id BIGINT PRIMARY KEY,
                membership_number VARCHAR(255),
                membership_expiration DATE,
                recorded_at DATETIME NOT NULL
            )
        """)
        cnx.commit()
        cursor.close()

    def _record_expired_member(self, discord_user_id, membership_number, expiration_date):
        """Record (audit) that a member's MES membership has lapsed.

        Upserts one row per Discord user into `expired_members`, refreshed each time the
        membership is found expired. Audit log only: it does not remove Discord roles (the
        daily reconciliation does that). Voluntary `!expire` token removals are deliberately
        NOT recorded here, since those are not membership expirations.

        @param discord_user_id: the Discord user whose membership lapsed.
        @param membership_number: their MES membership number (the stored access_token).
        @param expiration_date: the membershipExpiration date that has now passed.

        Example: self._record_expired_member(629679322210369537, "US2002023241", date(2025,2,1))
        """
        cursor = get_cursor()
        cursor.execute("""
            INSERT INTO expired_members (discord_user_id, membership_number, membership_expiration, recorded_at)
            VALUES (%s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                membership_number = VALUES(membership_number),
                membership_expiration = VALUES(membership_expiration),
                recorded_at = NOW()
        """, (discord_user_id, membership_number, expiration_date))
        cnx.commit()
        cursor.close()

    @staticmethod
    def _classify_unauthorized(existing_entry, now, grace_days=1):
        """Decide what to do with a member who holds the role but isn't authorized.

        Pure decision function (no I/O) so the per-guild cleanup policy is unit-testable in
        isolation from Discord and the database.

        @param existing_entry: the `unauthorized_users` row for THIS (user, guild) as a
            (notified_at,) tuple, or None if not yet flagged in this guild.
        @param now: current datetime, compared against notified_at.
        @param grace_days: days to wait after first flagging before removing the role.
        @returns "insert" to flag + notify, "wait" during the grace window, or "remove".

        Example: MyClient._classify_unauthorized(None, datetime.datetime.now()) -> "insert".
        """
        if not existing_entry:
            return "insert"
        notified_at = existing_entry[0]
        if (now - notified_at).days >= grace_days:
            return "remove"
        return "wait"

    def _gate_should_run(self, task_name):
        """Return True if the once-per-day task `task_name` has not completed today yet.

        @param task_name: the `daily_tasks.task_name` key gating a section of daily work.
        """
        cursor = get_cursor()
        cursor.execute("SELECT last_run_date FROM daily_tasks WHERE task_name = %s", (task_name,))
        result = cursor.fetchone()
        cursor.fetchall()
        cursor.close()
        last_run_date = result[0] if result else datetime.date.min
        return last_run_date < datetime.date.today()

    def _gate_mark_done(self, task_name):
        """Mark `task_name` as completed today.

        Call at the END of a section's work, so an interrupted run leaves the gate open and
        retries on the next tick instead of being marked done before the work finished.

        @param task_name: the `daily_tasks.task_name` key to stamp with today's date.
        """
        cursor = get_cursor()
        cursor.execute("""
            INSERT INTO daily_tasks (task_name, last_run_date)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE last_run_date = %s
        """, (task_name, datetime.date.today(), datetime.date.today()))
        cnx.commit()
        cursor.close()

    @staticmethod
    async def _remove_role_with_retry(member, role, attempts=3, delay=2.0):
        """Remove ``role`` from ``member``, retrying transient Discord API errors.

        Distinguishes the two failure modes the caller must report differently:
        a permission failure (``discord.Forbidden`` — the bot can't manage the role, which
        an admin has to fix) is returned immediately with no retry, while a transient API
        failure (timeout / 5xx) is retried up to ``attempts`` times before giving up.
        discord.py already retries 429 rate limits internally, so the ``HTTPException``s
        that reach here are the ones worth re-attempting.

        @param member: the guild member to remove the role from.
        @param role: the role to remove.
        @param attempts: total tries for transient errors (>= 1).
        @param delay: seconds to wait between transient retries.
        @returns "removed" on success, "forbidden" if the bot lacks permission (no retry),
            or "api_error" if transient errors persisted through every attempt.

        Example: await MyClient._remove_role_with_retry(member, role) -> "removed".
        """
        for attempt in range(1, attempts + 1):
            try:
                await member.remove_roles(role)
                return "removed"
            except discord.Forbidden:
                return "forbidden"
            except discord.HTTPException as e:
                logger.warning(
                    f"Transient Discord API error removing {role.name} from {member.id} "
                    f"(attempt {attempt}/{attempts}): {e}"
                )
                if attempt < attempts:
                    await asyncio.sleep(delay)
        return "api_error"

    @tasks.loop(hours=1) # run every hour
    async def daily_task(self):
        await self.wait_until_ready()  # Waits until the bot is connected
        try:
            # check the banlist
            cursor=get_cursor()
            cursor.execute("SELECT user_id FROM banned_users")
            banned_users = [row[0] for row in cursor.fetchall()]
            logger.info(f"Checking bans for {len(banned_users)} users across all servers.")
            for guild in self.guilds:
                #print(f"Checking guild {guild.name}")
                for user_id in banned_users:
                    try:
                        ban_entry = await guild.fetch_ban(discord.Object(id=user_id)) # fetch a banlist if the user is banned
                        #print(f"✅ User {user_id} is already banned in {guild.name}")
                    except discord.errors.Forbidden:
                        #print(f"Missing permission to manage bans in {guild.name}")
                        pass
                    except discord.NotFound:
                        try:
                            await guild.ban(discord.Object(id=user_id), reason="User is on global ban list")
                            await self.log_message(guild.id, f"🚨 Banned user {user_id}")
                            logger.info(f"🚨 Banned user {user_id} in {guild.name}")
                        except discord.Forbidden:
                            await self.log_message(guild.id, f"⚠️ Missing permissions to ban {user_id} from global banlist")
                            logger.warning(f"⚠️ Missing permissions to ban {user_id} in {guild.name}")
                        except discord.HTTPException as e:
                            await self.log_message(guild.id, f"❌ Failed to ban {user_id}: {e}")
                            logger.error(f"❌ Failed to ban {user_id} in {guild.name}: {e}")
            cursor.close()
            #clearing users from banlist
            cursor=get_cursor()
            cursor.execute("SELECT user_id FROM unbanned_users")
            unbanned_users = [row[0] for row in cursor.fetchall()]
            logger.info(f"Checking unbans for {len(unbanned_users)} users across all servers.")
            for guild in self.guilds:
                for user_id in unbanned_users:
                    try:
                        bans = [ban async for ban in guild.bans()]
                    except discord.Forbidden:
                        #print(f"Missing permissions to fetch bans in guild {guild.name}.")
                        continue
                    except discord.HTTPException as e:
                        logger.error(f"Error fetching bans for guild {guild.name}: {e}")
                        continue
                    banned_user_ids = {ban_entry.user.id for ban_entry in bans}
                    for user_id in unbanned_users:
                        if user_id in banned_user_ids:
                            try:
                                user = discord.Object(id=user_id)
                                await guild.unban(user, reason="User is on unbanned list; removing ban.")
                                logger.info(f"Unbanned user {user_id} in guild {guild.name}.")
                            except discord.Forbidden:
                                logger.warning(f"Missing permissions to unban user {user_id} in guild {guild.name}.")
                            except discord.HTTPException as e:
                                logger.error(f"Error unbanning {user_id} in guild {guild.name}: {e}")
            cursor.close()

            # --- Section A: membership-expiration notices + expired-member audit log ---
            # Gated on its own daily key and marked done only on completion (see
            # _gate_mark_done), so an interrupted run retries next tick rather than being
            # marked done up front, while still not re-sending notices twice the same day.
            if self._gate_should_run('expiration_notifications'):
                logger.info(f"Running expiration notifications for {datetime.date.today()}")
                cursor = get_cursor()
                cursor.execute("""
                    SELECT ua.discord_user_id, ua.access_token, u.membershipExpiration
                    FROM user_authorizations ua
                    JOIN `mes-portal`.User u ON ua.access_token = u.membershipNumber
                """)
                users = cursor.fetchall()
                cursor.close()
                today = datetime.datetime.today().date()
                notify_days = [28,14,7,0] # notify at 4 weeks, 2 weeks, 1 week and day of expiration
                for user in users:
                    user_id = user[0]
                    membership_number = user[1]
                    expiration_date = user[2]

                    if expiration_date is None:
                        logger.warning(f"{user} invalid expiration date")
                        continue

                    days_until_expiration = (expiration_date - today).days

                    logger.debug(f"Discord id:{user_id} member id:{user[1]} expiration date:{expiration_date} days left:{days_until_expiration}")
                    if days_until_expiration in notify_days:
                        try:
                            discord_user = await self.fetch_user(user_id)
                            if discord_user:
                                await discord_user.send(f"🔔 Your membership will expire on {expiration_date}.")
                                logger.info(f"Notified user {user_id} about membership expiration on {expiration_date}.")
                        except:
                            logger.error(f"Could not send message to user {user_id}.")

                    if days_until_expiration < 0:
                        # Expired: record it (audit) BEFORE dropping the authorization.
                        self._record_expired_member(user_id, membership_number, expiration_date)
                        cursor = get_cursor()
                        cursor.execute("DELETE FROM user_authorizations WHERE discord_user_id = %s", (user_id,))
                        cnx.commit()
                        cursor.close()
                        logger.info(f"Recorded and removed expired user {user_id} from user_authorizations.")

                self._gate_mark_done('expiration_notifications')
                logger.info("Expiration notifications complete.")
            else:
                logger.info("Skipping expiration notifications, already complete today.")

            # --- Section B: member-role reconciliation + unauthorized-role cleanup ---
            # Idempotent, so marking it done only on completion lets an interrupted run retry.
            if self._gate_should_run('daily_maintenance'):
                logger.info(f"Running daily maintenance for {datetime.date.today()}")
                cursor = get_cursor()
                cursor.execute("SELECT guild_id, role_id FROM server_roles")
                servers = cursor.fetchall()
                cursor.close()

                # Dedupe only the outbound DMs across guilds in this run; the per-guild role
                # work below always runs, so a member is reconciled in EVERY server they're in
                # rather than only the first one encountered.
                dm_notified = set()

                for server in servers:
                    guild_id = server[0]
                    role_id = server[1]

                    try:
                        guild = await self.fetch_guild(guild_id)
                    except discord.NotFound:
                        logger.warning(f"Guild id {guild_id} not found")
                        continue
                    if not guild:
                        logger.warning(f"Guild id {guild_id} not found")
                        continue

                    role = discord.utils.get(guild.roles, id=role_id)
                    if not role:
                        logger.warning(f"Role id {role_id} not found")
                        continue

                    members = [member async for member in guild.fetch_members()]
                    logger.info(f"Fetched {len(members)} members from {guild.name}")

                    # Self-heal: grant the configured role to any member who is verified with a
                    # currently-valid membership but is missing it. This is the permanent-record
                    # safety net for the per-OAuth assignment path in check_user_states — if that
                    # path is ever interrupted, the daily run reconciles from user_authorizations.
                    # Silent on purpose: no welcome DMs/channel posts during a bulk reconcile.
                    cursor = get_cursor()
                    cursor.execute("""
                        SELECT ua.discord_user_id
                        FROM user_authorizations ua
                        JOIN `mes-portal`.User u ON ua.access_token = u.membershipNumber
                        WHERE u.membershipExpiration >= CURDATE()
                    """)
                    valid_authorized = {row[0] for row in cursor.fetchall()}
                    cursor.close()
                    for member in members:
                        if member.id in valid_authorized and role not in member.roles:
                            try:
                                await member.add_roles(role)
                                logger.info(f"Self-heal: assigned role {role.name} to {member.display_name} in {guild.name}")
                                await self.log_message(guild_id, f"✅ Self-heal: assigned role {role.name} to {member.display_name} (membership verified).")
                            except (discord.Forbidden, discord.HTTPException) as e:
                                logger.error(f"Self-heal: failed to assign {role.name} to {member.id} in {guild.name}: {e}")

                    users_with_role = [member.id for member in members if role in member.roles]

                    logger.info(f"Users with {role_id} are {users_with_role}")
                    for user_id in users_with_role:
                        cursor = get_cursor()
                        cursor.execute("SELECT 1 FROM user_authorizations WHERE discord_user_id = %s", (user_id,))
                        is_authorized = cursor.fetchone()
                        cursor.fetchall()
                        cursor.close()
                        if is_authorized:
                            continue

                        # Unauthorized in THIS guild. Look up the guild-scoped flag (NOT just
                        # by user_id) and decide independently per guild, so a member in
                        # several servers is cleaned up in each one.
                        cursor = get_cursor()
                        cursor.execute(
                            "SELECT notified_at FROM unauthorized_users WHERE user_id = %s AND guild_id = %s",
                            (user_id, guild_id)
                        )
                        existing_entry = cursor.fetchone()
                        cursor.fetchall()
                        cursor.close()

                        action = self._classify_unauthorized(existing_entry, datetime.datetime.now())

                        if action == "insert":
                            cursor = get_cursor()
                            cursor.execute("""
                                INSERT INTO unauthorized_users (user_id, guild_id, notified_at)
                                VALUES (%s, %s, NOW())
                            """, (user_id, guild_id))
                            cnx.commit()
                            cursor.close()
                            logger.info(f"Logged unauthorized user {user_id} in guild {guild.name}.")
                            await self.log_message(guild.id, f"User {user_id} has role {role.name} but has not verified.")
                            member = await guild.fetch_member(user_id)
                            if member and user_id not in dm_notified:
                                dm_notified.add(user_id)  # at most one "please validate" DM per run
                                try:
                                    await member.send("Please follow the steps provided to validate your membership.")
                                    await self.on_member_join(member)
                                except:
                                    await self.log_message(guild.id, f"Can't send PM to user {member.name}")
                                    logger.error(f"Can't send PM to user {user_id}")
                        elif action == "remove":
                            member = await guild.fetch_member(user_id)
                            if member:
                                result = await self._remove_role_with_retry(member, role)
                                if result == "removed":
                                    logger.info(f"Removed role {role.name} from user {user_id} in guild {guild.name}")
                                    await self.log_message(guild.id, f"Removed role {role.name} from {member.name}, as they have not validated their membership.")
                                    if user_id not in dm_notified:
                                        dm_notified.add(user_id)
                                        try:
                                            await member.send(f"Your role '{role.name}' on '{guild.name}' has been removed as you have not validated your membership.")
                                        except:
                                            logger.error(f"Can't send removal PM to user {user_id}")
                                    # Clear the flag only AFTER a successful removal, so any
                                    # failure keeps the flag and retries without re-notifying.
                                    cursor = get_cursor()
                                    cursor.execute("DELETE FROM unauthorized_users WHERE user_id = %s AND guild_id = %s", (user_id, guild_id))
                                    cnx.commit()
                                    cursor.close()
                                elif result == "forbidden":
                                    # Admin-fixable: the bot can't manage this role. Report every run.
                                    logger.error(f"Unable to remove {role.name} from user {user_id} on {guild.name}: bot lacks permission")
                                    await self.log_message(guild.id, f"⚠️ Unable to remove {role.name} from {member.name}: the bot lacks the Manage Roles permission or its role is below {role.name}. An admin must fix the bot's permissions.")
                                else:  # api_error — transient failures persisted through retries
                                    logger.error(f"Failed to remove {role.name} from user {user_id} on {guild.name}: Discord API timed out after retries")
                                    await self.log_message(guild.id, f"❌ Could not remove {role.name} from {member.name}: Discord API timed out after several attempts. Will retry on the next run.")
                            else:
                                logger.warning(f"Unauthorized user {user_id} not found on server {guild.name}")
                self._gate_mark_done('daily_maintenance')
                logger.info("Daily task complete.")
            else:
                logger.info("Skipping daily task, already complete.")
        except mysql.connector.Error as e:
            logger.critical(f"Fatal DB error in daily_task, exiting: {e}")
            os._exit(1)



    @tasks.loop(seconds=60) # run every 60 seconds
    async def check_user_states(self):
        try:
            cursor=get_cursor()

            cursor.execute("SELECT user_id, guild_id FROM user_states")
            cursor.fetchall()
            #print("user_states:", cursor.fetchall())

            cursor.execute("SELECT discord_user_id FROM user_authorizations")
            cursor.fetchall()
            #print("user_authorizations:", cursor.fetchall())

            cursor.execute("SELECT guild_id, role_id FROM server_roles")
            cursor.fetchall()
            #print("server_roles:", cursor.fetchall())


            #print(f"Checking for authorizations and cleaning up state table at {datetime.datetime.now()}")
            # check if we have any new authorizations
            cursor.execute("""
                    SELECT ua.discord_user_id, us.guild_id, sr.role_id
                    FROM user_authorizations ua
                    JOIN user_states us ON ua.discord_user_id = us.user_id
                    JOIN server_roles sr ON us.guild_id = sr.guild_id
                """)
            results = cursor.fetchall()
            cursor.close()
            #print(f"Found {len(results)}")
            for user_id, guild_id, role_id in results:
                logger.info(f"User id: {user_id} guild id: {guild_id} role id: {role_id}")
                guild = await self.fetch_guild(guild_id)
                if guild:
                    member = await guild.fetch_member(user_id)
                    if member:
                        role = discord.utils.get(guild.roles, id=role_id)
                        if role:
                            try:
                                await member.add_roles(role)
                                                        # === Get user info for welcome message ===
                                cursor = get_cursor()
                                cursor.execute("SELECT access_token FROM user_authorizations WHERE discord_user_id = %s", (user_id,))
                                auth_row = cursor.fetchone()
                                cursor.fetchall()

                                if auth_row and auth_row[0]:
                                    cursor.execute("""
                                        SELECT firstName, lastName, membershipNumber
                                        FROM `mes-portal`.User
                                        WHERE membershipNumber = %s
                                    """, (auth_row[0],))
                                    user_row = cursor.fetchone()
                                    cursor.fetchall()
                                    cursor.close()

                                    user_result = f"{user_row[0]} {user_row[1]}" if user_row else "Unknown"
                                    auth_result = f"({user_row[2]})" if user_row else ""
                                else:
                                    user_result = ""
                                    auth_result = ""

                                welcome_text = f"Welcome {member.mention} {user_result} {auth_result} membership verified!"

                                # Send welcome message
                                ver_channel_id = await self.get_ver_channel(guild_id)
                                if ver_channel_id:
                                    ver_channel = await guild.fetch_channel(ver_channel_id)
                                    if ver_channel:
                                        await ver_channel.send(welcome_text)
                                    else:
                                        await self.log_message(guild_id, f"⚠️ Verification channel {ver_channel_id} no longer exists!")
                                else:
                                    await self.log_message(guild_id, welcome_text)

                                await self.log_message(guild_id, f"Member {member.name} {user_result} {auth_result} assigned role {role.name} via stored token.")
                                await member.send(f"✅ You've been assigned the role '{role.name}' on {guild.name} automatically.")
                                logger.info(f"✅ Assigned role {role.name} to {member.display_name} in {guild.name}")
                                await self.log_message(guild_id, f"✅ Assigned role {role.name} to {member.display_name}.")
                            except:
                                logger.error(f"Error assigning role {role.name} to {member.display_name} in {guild.name}")
                                await self.log_message(guild_id, f"Error assigning role {role.name} to {member.display_name}. Please check bot permissions.")
                        else:
                            logger.warning(f"⚠️ Role with ID {role_id} not found in guild {guild.name}.")
                            await self.log_message(guild_id, f"⚠️ Role with ID {role_id} not found.")
                    else:
                        logger.warning(f"⚠️ Member {user_id} not found in guild {guild_id}.")
                        await self.log_message(guild_id, f"⚠️ Member {user_id} not found on this server.")
                else:
                    logger.warning(f"⚠️ Guild {guild_id} not found.")
                cursor=get_cursor()
                cursor.execute("DELETE FROM user_states WHERE user_id = %s", (user_id,))
                cnx.commit()
                cursor.close()

            # removed expired oauth states from the database
            five_minutes_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=15)
            cursor=get_cursor()
            cursor.execute("SELECT * FROM user_states WHERE timestamp < %s", (five_minutes_ago,))
            results=cursor.fetchall()
            cursor.close()
            #if not results:
            #    print(f"No cleanup to do for the state table")
            for result in results:
                logger.debug(f"Removing {result} at {datetime.datetime.now()}")
                cursor=get_cursor()
                cursor.execute("DELETE FROM user_states WHERE timestamp < %s", (five_minutes_ago,))
                cnx.commit()
                cursor.close()
                state = result[0]
                user_id = result[1]
                try:
                    cursor=get_cursor()
                    cursor.execute("SELECT message_id FROM auth_messages WHERE state = %s", (state,))
                    msg_result = cursor.fetchone()
                    cursor.fetchall()
                    cursor.close()
                except:
                    logger.error(f"Error updating expired link in message {state}")
                if msg_result:
                    message_id = msg_result[0]
                    user = await self.fetch_user(user_id)
                    dm_channel = user.dm_channel
                    if dm_channel:
                        message = await dm_channel.fetch_message(message_id)
                        new_content = "[🔗 Link Expired]\nuse command !auth to request a new link"
                        await message.edit(content=new_content)
                        logger.info(f"Link expired message updated {message_id}")
                    else:
                        logger.warning(f"Unable to DM {user.name}")
                else:
                    logger.warning(f"Message {state} not found")
                try:
                    cursor=get_cursor()
                    cursor.execute("DELETE FROM auth_messages where state = %s", (state,))
                    cnx.commit()
                    cursor.close()
                except:
                    logger.error(f"Error removing {state} from auth_messages")
            # --- Event role periodic scan ---
            cursor = get_cursor()
            cursor.execute("SELECT guild_id, event_id, role_id FROM server_event_roles")
            event_configs = cursor.fetchall()
            cursor.close()

            for guild_id, event_id, role_id in event_configs:
                # Read the guild and its members from the gateway-populated cache rather
                # than the REST API. fetch_guild/fetch_member issue an HTTP request per
                # attendee on every 60s pass, which saturates Discord's per-route rate
                # limit as the attendee list grows; get_guild/get_member are in-memory
                # lookups (no HTTP). Requires intents.members, which this bot enables.
                guild = self.get_guild(guild_id)
                if not guild:
                    continue

                role = discord.utils.get(guild.roles, id=role_id)
                if not role:
                    continue

                cursor = get_cursor()
                cursor.execute("""
                    SELECT DISTINCT ua.discord_user_id
                    FROM user_authorizations ua
                    JOIN `mes-portal`.EventAttendee ea ON ua.access_token = ea.membershipNumberSubmitted
                    JOIN `mes-portal`.User u ON ua.access_token = u.membershipNumber
                    WHERE ea.event_id = %s
                      AND u.membershipExpiration >= CURDATE()
                """, (event_id,))
                attendee_ids = [row[0] for row in cursor.fetchall()]
                cursor.close()

                # Some qualifying attendees may be in the guild but absent from the member
                # cache — e.g. they joined before registering (so on_member_join couldn't
                # assign yet) or their join was missed around a gateway reconnect. Refresh
                # those over the gateway (NOT REST), so they get picked up without
                # reintroducing the per-attendee REST scan. IDs not in the guild are simply
                # not returned, and a fully-cached steady state makes zero gateway calls.
                uncached = [d for d in attendee_ids if guild.get_member(d) is None]
                for i in range(0, len(uncached), 100):  # query_members allows <=100 ids/call
                    try:
                        await guild.query_members(user_ids=uncached[i:i + 100], cache=True)
                    except (discord.HTTPException, asyncio.TimeoutError) as e:
                        logger.warning(f"query_members failed for {len(uncached[i:i + 100])} ids in {guild.name}: {e}")

                for discord_id in attendee_ids:
                    # Skip attendees who are not in the guild (None) or already hold the
                    # role — no REST call and no redundant add_roles, so a steady state
                    # where everyone already has the role costs zero HTTP requests.
                    member = guild.get_member(discord_id)
                    if member is None or role in member.roles:
                        continue
                    try:
                        await member.add_roles(role)
                        await self.log_message(guild_id, f"✅ Assigned event role {role.name} to {member.display_name}.")
                        logger.info(f"✅ Assigned event role {role.name} to {member.display_name} in {guild.name}")
                    except (discord.Forbidden, discord.HTTPException) as e:
                        logger.error(f"Failed to assign event role to {discord_id}: {e}")

        except mysql.connector.Error as e:
            logger.critical(f"Fatal DB error in check_user_states, exiting: {e}")
            os._exit(1)


    @check_user_states.before_loop
    async def before_check_user_states(self):
        """ Wait until bot is ready before running the loop """
        await self.wait_until_ready()

    async def on_message(self, message):
        if message.author.id == self.user.id:
            return
        try:
            if message.content.startswith('!help'):
                if isinstance(message.author, discord.Member):
                    if message.author.guild_permissions.administrator:
                        await message.reply("!role to configure what role MESBot will assign to verified members.(Available only to admins)\n!id <user id> to identify name and membership number for a verified member.\n!auth to reauthenticate to the portal, this will extend or create your login token. \n!expire will delete your login token \n !setlog <channel name> to set the logging channel for the bot.(Available only to admins) \n Please visit https://www.modernenigmasociety.org/mesbot-documentation/ for more information.\n!setevent <event_id> <role> to configure event role assignment (admin). !clearevent to remove the event configuration (admin).")
                else:
                    await message.reply("!id <user id> to identify name and membership for a verified member\n!auth to reauthenticate to the portal, this will extend or create your login token. \n!expire will delete your login token \n Please visit https://www.modernenigmasociety.org/mesbot-documentation/ for more information.")
            if message.content.startswith('!auth'):
                if isinstance(message.author, discord.Member):
                    #do something here to authenticate the user like on_join
                    await self.on_member_join(message.author)
                    return
                else:
                    guild = None
                    parts = message.content.split(" ",1)
                    if len(parts) < 2:
                        await message.reply("❌ You must specify a server name or server id with this command. e.g. !auth Modern Enigma Society")
                        return
                    else:
                        server=parts[1]
                        if server.isdigit():
                            guild = await self.fetch_guild(int(server))
                        else:
                            for g in self.guilds:
                                if g.name.lower() == server.lower():
                                    guild = g
                                    break
                        if not guild:
                            await message.reply("❌ Guild not found.")
                            return
                        try:
                            member = await guild.fetch_member(message.author.id)
                            await self.on_member_join(member)
                        except:
                            await message.reply(f"❌ You are not a member of {guild.name}")
                            logger.warning(f"Unable to find member {message.author.id} on server {guild.name}")

            if message.content.startswith('!expire'):
                #delete the user token from user_authorizations
                cursor=get_cursor()
                user_id = message.author.id
                try:
                    cursor.execute("DELETE FROM user_authorizations WHERE discord_user_id = %s", (user_id,))
                    cnx.commit()
                    await message.reply("✅ Token removed, please use !auth to get a new token.")
                except:
                    await message.reply("❌ Token not found.")
                cursor.close()
                return
            if message.content.startswith('!id'):
                cursor=get_cursor()
                cursor.execute("SELECT discord_user_id FROM user_authorizations WHERE discord_user_id = %s", (message.author.id,))
                author=cursor.fetchone()
                cursor.fetchall()
                if not author:
                    await message.reply("Only verified members can use this command.")
                    return
                parts = message.content.split(maxsplit=1)
                if len(parts) < 2:
                    await message.reply("Please specify a user id to identify.")
                else:
                    try:
                        user_id = int(parts[1])
                    except ValueError:
                        await message.reply("❌ Please provide a numeric Discord user ID.")
                        return
                    cursor=get_cursor()
                    cursor.execute("SELECT access_token FROM user_authorizations WHERE discord_user_id = %s", (user_id,))
                    auth_result = cursor.fetchone()
                    cursor.fetchall()
                    if auth_result:
                        cursor.execute("""
                            SELECT firstName, lastName FROM `mes-portal`.User
                            WHERE membershipNumber = (SELECT access_token FROM user_authorizations WHERE discord_user_id = %s)
                        """, (user_id,))
                        user_result = cursor.fetchone()
                        cursor.fetchall()
                        cursor.close()
                        await message.reply(f"User id: {user_id} is {user_result} {auth_result}")
                        return
                    else:
                        await message.reply(f"User id: {user_id} not found")
                        return

            if message.content.startswith('!role'):
                if message.guild is None:
                    await message.reply("❌ This command cannot be used in DMs.")
                    return
                if not message.author.guild_permissions.administrator:
                    await message.reply("❌ You must be an administrator to use this command.")
                    return
                parts = message.content.split(maxsplit=1)
                if len(parts) < 2:
                    cursor=get_cursor()
                    cursor.execute("SELECT role_id from server_roles WHERE guild_id = %s", (message.guild.id,))
                    role_data = cursor.fetchone()
                    cursor.fetchall()
                    if role_data:
                        role = discord.utils.get(message.guild.roles, id=role_data[0])
                        if role:
                            await message.reply(f"Current assigned member role is '{role.name}'")
                            return
                    await message.reply("Please specify a role ID or name")
                    cursor.close()
                    return

                command, role_input = parts[0], parts[1]

                role = None
                try:
                    role_id = int(role_input)
                    role = discord.utils.get(message.guild.roles, id=role_id)
                except ValueError:
                    #if not a value try matching by name
                    role = discord.utils.get(message.guild.roles, name=role_input)

                if role:
                    guild_id = message.guild.id
                    role_id = role.id
                    cursor=get_cursor()
                    try:
                        cursor.execute(
                                "INSERT INTO server_roles (guild_id, role_id) VALUES (%s, %s) "
                                "ON DUPLICATE KEY UPDATE role_id = %s",
                                (guild_id, role_id, role_id)
                        )
                        cnx.commit()
                        await message.reply(f"Role found: {role.name} (ID: {role.id})")
                    except mysql.connector.Error as err:
                        logger.error(f"Database Error: {err}")
                        await message.reply("❌ An error occurred while saving the role.")
                    cursor.close()

                else:
                    logger.warning(f"No matching role {role_input} found.")
                    await message.reply("No matching role found.")

            if message.content.startswith('!setlog'):
                # command to set logging channel for the server
                if message.guild is None:
                    await message.reply("❌ This command cannot be used in DMs.")
                    return
                if not message.author.guild_permissions.administrator:
                    await message.reply("❌ You must be an administrator to use this command.")
                    return
                if isinstance(message.channel, discord.DMChannel):
                    await message.reply("❌ This command cannot be used in DMs.")
                    return
                parts = message.content.split(maxsplit=1)
                guild_id = message.guild.id
                channel_id = message.channel.id
                if len(parts) < 2:
                    cursor=get_cursor()
                    cursor.execute("""
                        INSERT INTO server_logging (guild_id, channel_id)
                        VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE channel_id = VALUES(channel_id)
                    """, (guild_id, channel_id))
                    cnx.commit()
                    await self.log_message(guild_id, f"Logging channel set to {channel_id}")
                    logger.info(f"Logging channel set to {channel_id} on server {guild_id}")
                    # send a log
                    return
                else:
                    if message.channel_mentions:
                        channel_id = message.channel_mentions[0].id
                        if channel_id:
                            cursor=get_cursor()
                            cursor.execute("""
                                INSERT INTO server_logging (guild_id, channel_id)
                                VALUES (%s, %s)
                                ON DUPLICATE KEY UPDATE channel_id = VALUES(channel_id)
                            """, (guild_id, channel_id))
                            cnx.commit()
                            cursor.close()
                            # send a log
                            await self.log_message(guild_id, f"Logging channel set to {channel_id}")
                            logger.info(f"Logging channel set to {channel_id} on server {guild_id}")
                            return
                        else:
                            await message.reply("❌ Could not find channel {parts[1]}.")
                    else:
                        await message.reply(f"❌ Could not find a channel mention in '{parts[1]}'.")
                        return
            if message.content.startswith('!setver'):
                if message.guild is None:
                    await message.reply("❌ This command cannot be used in DMs.")
                    return
                if not message.author.guild_permissions.administrator:
                    await message.reply("❌ You must be an administrator to use this command.")
                    return
                if isinstance(message.channel, discord.DMChannel):
                    await message.reply("❌ This command cannot be used in DMs.")
                    return

                parts = message.content.split(maxsplit=1)
                guild_id = message.guild.id
                if len(parts) < 2 or not message.channel_mentions:
                    await message.reply("❌ Please mention a channel. Example: `!setver #verification`")
                    return

                channel_id = message.channel_mentions[0].id
                cursor = get_cursor()
                try:
                    cursor.execute("""
                        INSERT INTO server_verification (guild_id, channel_id)
                        VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE channel_id = VALUES(channel_id)
                    """, (guild_id, channel_id))
                    cnx.commit()
                    await message.reply(f"✅ Verification channel set to <#{channel_id}>")
                except mysql.connector.Error as err:
                    logger.error(f"Database Error: {err}")
                    await message.reply("❌ Failed to save verification channel.")
                finally:
                    cursor.close()
                return

            if message.content.startswith('!setevent'):
                if message.guild is None:
                    await message.reply("❌ This command cannot be used in DMs.")
                    return
                if not message.author.guild_permissions.administrator:
                    await message.reply("❌ You must be an administrator to use this command.")
                    return
                if isinstance(message.channel, discord.DMChannel):
                    await message.reply("❌ This command cannot be used in DMs.")
                    return
                parts = message.content.split(maxsplit=2)
                if len(parts) < 3:
                    # Show current config
                    cursor = get_cursor()
                    cursor.execute(
                        "SELECT event_id, role_id FROM server_event_roles WHERE guild_id = %s",
                        (message.guild.id,)
                    )
                    config = cursor.fetchone()
                    cursor.fetchall()
                    cursor.close()
                    if config:
                        role = discord.utils.get(message.guild.roles, id=config[1])
                        role_name = role.name if role else f"<deleted role {config[1]}>"
                        await message.reply(f"Current event: ID {config[0]}, role '{role_name}'")
                    else:
                        await message.reply("No event configured. Use `!setevent <event_id> <role name or ID>`")
                    return
                _, event_id_str, role_input = parts
                try:
                    event_id = int(event_id_str)
                except ValueError:
                    await message.reply("❌ Event ID must be a number.")
                    return
                role = None
                try:
                    role_id = int(role_input)
                    role = discord.utils.get(message.guild.roles, id=role_id)
                except ValueError:
                    role = discord.utils.get(message.guild.roles, name=role_input)
                if not role:
                    await message.reply(f"❌ No matching role '{role_input}' found.")
                    return
                cursor = get_cursor()
                try:
                    cursor.execute(
                        "INSERT INTO server_event_roles (guild_id, event_id, role_id) VALUES (%s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE event_id = %s, role_id = %s",
                        (message.guild.id, event_id, role.id, event_id, role.id)
                    )
                    cnx.commit()
                    await message.reply(f"✅ Event {event_id} configured with role '{role.name}'.")
                except mysql.connector.Error as err:
                    logger.error(f"Database Error: {err}")
                    await message.reply("❌ An error occurred while saving the event configuration.")
                finally:
                    cursor.close()
                return

            if message.content.startswith('!clearevent'):
                if message.guild is None:
                    await message.reply("❌ This command cannot be used in DMs.")
                    return
                if not message.author.guild_permissions.administrator:
                    await message.reply("❌ You must be an administrator to use this command.")
                    return
                if isinstance(message.channel, discord.DMChannel):
                    await message.reply("❌ This command cannot be used in DMs.")
                    return
                cursor = get_cursor()
                try:
                    cursor.execute("DELETE FROM server_event_roles WHERE guild_id = %s", (message.guild.id,))
                    cnx.commit()
                    await message.reply("✅ Event role configuration removed.")
                except mysql.connector.Error as err:
                    logger.error(f"Database Error: {err}")
                    await message.reply("❌ An error occurred while removing the event configuration.")
                finally:
                    cursor.close()
                return

        except mysql.connector.Error as e:
            logger.error(f"DB error in on_message from {message.author.id}: {e}")
            return



    async def on_member_join(self, member):
        try:
            # check if user is authorized
            cursor=get_cursor()
            cursor.execute(
                    "SELECT last_authorized FROM user_authorizations WHERE discord_user_id = %s",
                    (member.id,)
            )
            result = cursor.fetchone()
            cursor.fetchall()
            cursor.close()
            if result:
                last_authorized = result[0]
                if last_authorized:
                    # user with authorized no need for oauth
                    cursor=get_cursor()
                    cursor.execute("SELECT role_id FROM server_roles WHERE guild_id = %s", (member.guild.id,))
                    role_data = cursor.fetchone()
                    cursor.fetchall()
                    cursor.close()
                    if role_data:
                        role = discord.utils.get(member.guild.roles, id=role_data[0])
                        if role:
                            try:
                                await member.add_roles(role)
                                # === Get user_result and auth_result ===
                                cursor = get_cursor()
                                cursor.execute("SELECT access_token FROM user_authorizations WHERE discord_user_id = %s", (member.id,))
                                auth_row = cursor.fetchone()
                                cursor.fetchall()
                                if auth_row and auth_row[0]:
                                    cursor.execute("""
                                        SELECT firstName, lastName, membershipNumber
                                        FROM `mes-portal`.User
                                        WHERE membershipNumber = %s
                                    """, (auth_row[0],))
                                    user_row = cursor.fetchone()
                                    cursor.fetchall()
                                    cursor.close()
                                    if user_row:
                                        user_result = f"{user_row[0]} {user_row[1]}"
                                        auth_result = f"({user_row[2]})"
                                    else:
                                        user_result = ""
                                        auth_result = ""
                                else:
                                    user_result = ""
                                    auth_result = ""
                                welcome_text = f"Welcome {member.mention} {user_result} {auth_result} membership verified!"
                                await member.send(f"You've been assigned the role '{role.name}' on server '{member.guild.name}' automatically via your stored token.")
                                ver_channel_id = await self.get_ver_channel(member.guild.id)
                                if ver_channel_id:
                                    guild = await self.fetch_guild(member.guild.id)
                                    ver_channel = await guild.fetch_channel(ver_channel_id) if guild else None
                                    if ver_channel:
                                        await ver_channel.send(welcome_text)
                                    else:
                                        # Verification channel deleted
                                        await self.log_message(member.guild.id, f"⚠️ Verification channel {ver_channel_id} no longer exists!")
                                await self.log_message(member.guild.id,welcome_text)
                            except Exception as e:
                                logger.error(f"Error assigning role to {member.name} on {member.guild.name}: {e}")
                                await self.log_message(member.guild.id,f"Error: Unable to assign {member.name} role {role.name}: {e}")
                        else:
                            logger.warning(f"Unable to locate role id {role_data[0]} in server {member.guild.name}")
                            await self.log_message(member.guild.id,f"Error: Unable to load role id {role_data[0]}")
                    else:
                        logger.warning(f"Unable to load role from database for server {member.guild.name}")
                        await self.log_message(member.guild.id,f"Unable to load role from database. Please check that a role is configured correctly.")
                    await self.assign_event_role(member)
                    return
            else:
                # user has no token
                # redirect to auth
                state = str(uuid.uuid4())
                cursor=get_cursor()
                cursor.execute(
                    "INSERT INTO user_states (state, user_id, guild_id, timestamp) VALUES (%s, %s, %s, %s)",
                    (state, member.id, member.guild.id, datetime.datetime.now())
                )
                cnx.commit()
                cursor.close()
                logger.debug(f"Wrote state into state table with user_id {member.id} and guild id {member.guild.id} at {datetime.datetime.now()}")
                auth_url = f"https://discord.modernenigmasociety.org/oauth/authorize?state={state}"
                try:
                    message = await member.send(f"If you are an MES member, this bot is setup to automatically validate your membership and give you full access to MES discord servers.\nPlease authenticate with our service by clicking here: {auth_url}\nThis link expires after 15 minutes, and you can use the !auth command to generate a new link.\nIf you have questions or are interested in joining one of our LARPs, you can ask questions and get advice in the welcome room without verification or membership. https://discord.gg/dEudtugYdM")
                except (discord.Forbidden, discord.HTTPException) as e:
                    logger.error(f"Could not DM auth link to {member.id}: {e}")
                    return
                # Store message_id and state
                cursor=get_cursor()
                cursor.execute("""
                    INSERT INTO auth_messages (state, message_id, timestamp)
                    VALUES (%s, %s, %s)
                    """, (state, message.id, datetime.datetime.now()))
                cnx.commit()
                cursor.close()
        except mysql.connector.Error as e:
            logger.error(f"DB error in on_member_join for {member.id}: {e}")
            return

    async def log_message(self, guild_id, message):
        # logs a message in the configured logging channel or else does nothing
        cursor=get_cursor()
        cursor.execute("SELECT channel_id FROM server_logging WHERE guild_id = %s", (guild_id,))
        result = cursor.fetchone()
        cursor.close()
        if result:
            channel_id=result[0]
            guild = await self.fetch_guild(guild_id)
            if guild:
                log_channel = await guild.fetch_channel(channel_id)
                if log_channel:
                    await log_channel.send(message)
                else:
                    logger.warning(f"⚠️ Logging channel {channel_id} not found in guild {guild_id}.")
            else:
                logger.warning(f"⚠️ Guild {guild_id} not found.")
        else:
            logger.warning(f"⚠️ No logging channel set for guild {guild_id}.")

    async def get_ver_channel(self, guild_id: int):
        """Returns verification channel ID or None"""
        cursor = get_cursor()
        try:
            cursor.execute("SELECT channel_id FROM server_verification WHERE guild_id = %s", (guild_id,))
            result = cursor.fetchone()
            return result[0] if result else None
        finally:
            cursor.close()

    async def assign_event_role(self, member: discord.Member):
        """Assign the event role to a member if they are a registered event attendee.

        Checks server_event_roles for this guild's configured event, then verifies the
        member's MES membership number appears in mes-portal.EventAttendee for that event.
        Does nothing if no event is configured for this guild, the role is already
        assigned, or the member has not completed OAuth verification.

        @param member: The guild member to check and potentially assign the event role.
        @see check_user_states
        """
        cursor = get_cursor()
        cursor.execute(
            "SELECT event_id, role_id FROM server_event_roles WHERE guild_id = %s",
            (member.guild.id,)
        )
        config = cursor.fetchone()
        cursor.fetchall()
        cursor.close()
        if not config:
            return
        event_id, role_id = config

        role = discord.utils.get(member.guild.roles, id=role_id)
        if not role or role in member.roles:
            return

        cursor = get_cursor()
        cursor.execute("""
            SELECT 1
            FROM user_authorizations ua
            JOIN `mes-portal`.EventAttendee ea ON ua.access_token = ea.membershipNumberSubmitted
            JOIN `mes-portal`.User u ON ua.access_token = u.membershipNumber
            WHERE ua.discord_user_id = %s AND ea.event_id = %s
              AND u.membershipExpiration >= CURDATE()
            LIMIT 1
        """, (member.id, event_id))
        is_attendee = cursor.fetchone()
        cursor.fetchall()
        cursor.close()

        if is_attendee:
            try:
                await member.add_roles(role)
                await self.log_message(member.guild.id, f"✅ Assigned event role {role.name} to {member.display_name}.")
                logger.info(f"✅ Assigned event role {role.name} to {member.display_name} in {member.guild.name}")
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.error(f"Failed to assign event role to {member.id}: {e}")
                await self.log_message(member.guild.id, f"❌ Failed to assign event role to {member.display_name}: {e}")


intents=discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

try:
  cnx = mysql.connector.connect(user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'), host= os.getenv('DB_SERVER'),
                                database=os.getenv('DB_DATABASE'))
except mysql.connector.Error as err:
  if err.errno == errorcode.ER_ACCESS_DENIED_ERROR:
    logger.critical("Something is wrong with your user name or password")
  elif err.errno == errorcode.ER_BAD_DB_ERROR:
    logger.critical("Database does not exist")
  else:
    logger.critical(err)

#cursor=cnx.cursor()

if __name__ == "__main__":
    client = MyClient(intents=intents)
    client.run(os.getenv('TOKEN'))
