# MESBot — Architecture & Operations

Maintainer-facing reference for how MESBot is built, deployed, and how membership/event data
flows through it. (User/admin command docs live in the [README](../README.md).)

## Components

MESBot is two Python processes that share a database:

| Process | File | Role |
|---|---|---|
| `discord_app` | `main.py` | The Discord bot (discord.py). Handles `!` commands, member joins, and two background loops. |
| `flask_app` | `server.py` | A small Flask app serving the OAuth callback (`/oauth/callback`, `/oauth/authorize`) on port 5000. |

### Background loops in `main.py`
- **`check_user_states`** — `@tasks.loop(seconds=60)`. Assigns roles to members who have just verified,
  and runs the periodic event-role scan. Also expires stale OAuth states.
- **`daily_task`** — `@tasks.loop(hours=1)`, gated to run its maintenance once per calendar day. Processes
  the global ban/unban lists, sends membership-expiration reminders, removes the role from unverified
  members after a grace period, and self-heals the member role (grants it to any verified, valid member
  who is missing it).
- **`loop_watchdog`** — `@tasks.loop(seconds=60)`. Resilience backstop. Each of the two loops above wraps
  its body in `asyncio.wait_for` (cancels a cancellable async hang and retries next tick) and stamps a
  `time.monotonic()` heartbeat after every pass. The watchdog `os._exit(1)`s (→ supervisor restart) if a
  heartbeat goes stale past its limit (`CHECK_USER_STATES_STALL_LIMIT` / `DAILY_TASK_STALL_LIMIT`) —
  catching the cases `wait_for` can't (a non-cancellable block or a stopped loop) so role assignment can
  never silently freeze for hours.

## Deployment

- Runs on the SSH host **`discord`** (user `mesbot`). The repo is checked out at **`/home/mesbot`**
  (which is also `$HOME`, so untracked dotfiles in `git status` are normal).
- Managed by **supervisor**: `discord_app` and `flask_app` (`supervisor/*.conf`, logs in
  `/var/log/{discord_app,flask_app}.log`), both with `autorestart=true`.
- **`deploy.sh`** is the canonical deploy: `git pull origin main` then
  `sudo supervisorctl restart discord_app flask_app`. Run it on the server after merging a PR to `main`.
  - The `mesbot` user is a sudoer but **not passwordless** by default, so `deploy.sh` prompts for the
    sudo password at the restart step. To make it non-interactive, add a sudoers drop-in
    **`/etc/sudoers.d/mesbot-deploy`** (filename must have no `.`/extension or it is ignored), edited via
    `sudo visudo -f /etc/sudoers.d/mesbot-deploy`:

    ```sudoers
    mesbot ALL=(root) NOPASSWD: /usr/bin/supervisorctl restart discord_app, /usr/bin/supervisorctl restart flask_app, /usr/bin/supervisorctl status
    ```

    Scoped to exactly the commands `deploy.sh` runs; update it if those commands change. Verify with
    `sudo -n supervisorctl status`.
  - Config (`.env`, `TOKEN`, `DB_*`) lives at `/home/mesbot/.env`.

## Data model

One MySQL server hosts **two databases**, queried together via cross-DB references
(`` `mes-portal`.Table ``).

### Bot database (owned by MESBot)
- **`user_authorizations`** — the permanent map: `discord_user_id` (unique) ↔ `access_token`.
  Note: **`access_token` stores the MES `membershipNumber`**, not an OAuth token.
- **`user_states`** — transient per-OAuth row `(state, user_id, guild_id, timestamp)`; also the work
  queue telling the bot "this user just verified — assign the role and welcome them in this guild."
- `server_roles` (member role per guild), `server_event_roles` (`guild_id, event_id, role_id`),
  `server_verification`, `server_logging`, `banned_users`, `unbanned_users`, `unauthorized_users`
  (`user_id, guild_id, notified_at` — per-guild grace flag for members holding the role without a
  valid membership), `daily_tasks` (`task_name, last_run_date` — once-per-day gate per section),
  `auth_messages`.
- **`expired_members`** — audit log of lapsed memberships: `discord_user_id` (PK),
  `membership_number`, `membership_expiration`, `recorded_at`. One row per user, refreshed each time
  their membership is found expired (written by the daily task before it drops the authorization).
  Auto-created on startup via `_ensure_schema` (the project has no migration system).

### Portal database `mes-portal` (read-mostly; owned by the Symfony membership portal)
- `User` — `membershipNumber`, `emailAddress`, `firstName`, `lastName`, `membershipExpiration`,
  `membershipType`, `id`.
- `EventAttendee` — `event_id` (→ `PortalEvent.id`), `user_id` (→ `User.id`; the portal-resolved
  attendee), `membershipNumberSubmitted` (raw buyer input), `zeffy_donation_id`, `registeredAt`.
- `PortalEvent` — `id`, `name`, `startDate`, `zeffyTicketingId`.
- `ZeffyDonation` — raw Zeffy import: `email`, `first_name`, `last_name`, `status`
  (`new`/`error`/`user-choice`/`completed`/`NA`), `ticketing_id`, `amount`, `tickets_quantity`.

## Verification & role assignment

**Member role** (set per server with `!role`):
1. On join, `on_member_join` assigns it immediately if the user already has a token in
   `user_authorizations`; otherwise it DMs an OAuth link and writes a `user_states` row.
2. After the user completes OAuth (`server.py:oauth_callback` writes `user_authorizations`), the
   `check_user_states` loop matches `user_authorizations → user_states → server_roles`, assigns the role,
   posts the welcome, and deletes the `user_states` row.

> **Important:** `oauth_callback` must **not** delete the `user_states` row — that's the loop's job. A
> previous change deleted it in the same transaction that created the authorization, so the loop's join
> never matched and already-present members never received the role on first verification. See the
> "Member-role regression" history below.

**Event role** (set per server with `!setevent <event_id> <role>`): granted to verified members who
registered for the event, matching `EventAttendee.user_id` (preferred) or `membershipNumberSubmitted`,
with a current membership. Applied on join (`assign_event_role`) and in the 60s event scan.

> **Important — member resolution in the scan:** the scan reads members from the gateway cache
> (`get_member`, zero HTTP). On a cache miss it falls back to a single `fetch_member` (REST),
> throttled by a per-`(guild, user)` negative cache (`_fetch_attendee` / `EVENT_ABSENT_TTL`) so
> not-in-guild attendees aren't re-fetched every pass — that repeated REST was a 429 storm.
> **Never use `guild.query_members` here:** it shares discord.py's chunk-request machinery and
> deadlocks against the gateway's automatic guild re-chunking (startup + every reconnect), which
> froze the whole loop for hours. Reproduced: concurrent `query_members` + `chunk()` on one guild
> reliably hangs.

## Zeffy → event-role pipeline

Ticket data is **pushed**, not pulled:

1. **Zeffy → Zapier → `POST /membership/zeffy-zapier`** on the portal (authed by `ZEFFY_OUR_API_KEY`)
   inserts a `ZeffyDonation` row (`status='new'`).
2. The Symfony command **`membership:zeffy-payments-check`** runs **every minute**
   (`/etc/cron.minutly/...`), matches the donor email to `User.emailAddress`, processes the membership,
   creates the `EventAttendee` row, and sets the donation status to `completed` / `error` / `user-choice`.
3. MESBot then grants the Discord event role as above.

A `status='error'` donation never produced an `EventAttendee` row, so that member won't get the event
role automatically. (Known cause: a Zeffy form edit that asked for "membership number **or** email" broke
the matcher.) Such registrants are recovered with the backfill tool below.

## Maintenance tooling

**`scripts/backfill_event_roles.py`** — one-off, idempotent, dry-run-by-default. Grants the configured
event role to verified members who registered, keying off the permanent `user_authorizations` map (no new
links created) and keeping the membership-validity filter. Run from `/home/mesbot`.

```sh
python3 scripts/backfill_event_roles.py                         # dry run (prints the plan)
python3 scripts/backfill_event_roles.py --apply                 # grant + notify
python3 scripts/backfill_event_roles.py --apply \
        --include-error-registrants                             # also recover error-status registrants
```

`--include-error-registrants` additionally matches raw `ZeffyDonation` rows by email (including
`error` status). Review the dry-run audit before applying — `error` rows can include failed/voided
payments. The live bot does **not** include this recovery path, so error-status members won't auto-grant
later unless their portal registration is corrected.
