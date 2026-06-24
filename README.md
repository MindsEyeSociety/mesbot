# MESBot

An NTA technology project — a Discord verification bot for MES (Modern Enigma Society) servers.

I'm excited to introduce MESBot, an advanced verification bot designed to streamline and secure user verification from our membership portal. This bot will help with member verification and cut down on administration tasks on your Discord server. Add MESBot to your discord server.

## For Server Admins

### Suggested Setup Steps

1. Add the bot to the server
2. In Server Settings → Roles, move the MESBot role to be above the "Verified" or "Member" role so that it can assign that role
3. Add MESBot role to your welcome channel so that people can interact with it there
4. Set the server role that you want it to assign with `!role <role name>`
5. Set the verification channel with `!setver <channel name>` (This is the channel where you want people to interact with MESBot)
6. Set the logging channel with `!setlog <channel name>` (This should be a private channel for admins only)
7. To run an online event, configure event role assignment with `!setevent <event_id> <role>`
8. Use `!clearevent` when the event is over to remove the event role configuration

## For MES Members

MESBot sends a special link with a token to you via direct message when you join an MES discord server where it is installed. For security purposes the link expires after 5 minutes and because this link contains a token, it should not be shared with anyone else, and also should not be posted publicly. This means that you will have to allow MESBot to send you direct messages in order to verify your account. If you are currently a member on a server and not yet verified, it will send reminder for you to login. After 7 days, your role as a MES member will be removed if you do not authenticate.

## Features

### User Verification

MESBot authenticates discord users via the membership portal oauth sign-on, giving you 100% reliability that users who pass validation have a valid membership.

### Role Assignment

Server admins can configure a server role that MESBot will control. It will verify that the role exists and store this setting in our database. When a user joins the server, they will receive a link to authenticate via the portal, if they already have a token, they will automatically be given the role you configured. Members who verify while already on the server receive the role automatically within about a minute. As a safety net, MESBot also reconciles daily and will grant the configured role to any verified member with a current membership who is missing it. If the discord user does not have a current membership they will not get the role. Any users who have the server role but not a token will get a daily reminder to login, and after 7 days will lose the role.

### Event Role Assignment

When a server admin configures an event with `!setevent`, MESBot automatically assigns the configured event role to any verified member who has purchased a ticket for that event. The role is assigned immediately when the member joins, or within 60 seconds via the background check. The event ID corresponds to the event record in the MES portal. Use `!clearevent` to remove the configuration when the event is over.

### Membership Expiration

If a membership expires, the bot checks daily for expired memberships and removes the assigned role from those users. After the membership is renewed, they can once again get a token to restore their access.

### Membership Renewal Reminders

Users with a token will receive a discord notification 4 weeks, 2 weeks, and 1 week before your membership expires. You will also be notified the day that your membership expires.

### Authentication

You can request a new token at any time with the `!auth` command, for example if a previous link expired. You can also clear your token with `!expire`. For officer accounts: you should always remove the token associating a discord account with your membership before turning it over to a new officer.

### Global Banlist

Globally banned discord users as identified by the Board of Directors will be added to a global banlist, and the bot will automatically add ban those users on servers where the bot is installed (if the bot has permission to do so).

### Member Lookup

`!id <user id>` will return the membership number and first and last name of the member if they have a valid token. For security purposes, only users with a token have access to this command.

## Commands

Commands can be sent in a DM to the bot or in a channel where MESBot can view messages. `[param]` indicates an optional parameter; `<param>` indicates a required parameter.

### Member Commands

| Command | Description |
|---|---|
| `!help` | Provides a list of available commands |
| `!auth [server name]` | Requests a new token to authenticate your membership. If you send the command in a DM you must provide the `[server name]`. The bot will respond to this command by sending you a link. To complete your authentication you must click the link, login to the portal, and click on the authorize accept button. Once you do this you will receive a message confirming the role that the bot has assigned to you. |
| `!expire` | This command will erase the token associated with your discord account. Officers should use this before handing over a discord account to another member. The token is associated with your membership so when accounts change hands, please do this to ensure that the new owner's membership is associated with the account. |
| `!id <user id>` | This command will return the name and membership number of any discord user with a token. |

### Admin Commands

Admin commands cannot be sent via DM and require the Administrator permission.

| Command | Description |
|---|---|
| `!role [role name or role id]` | Configures the role that you want MESBot to assign to authenticated members. Please note: it will also remove this role after a grace period of 7 days from any discord members who do not authenticate. This includes bots, etc. Please ensure that your bots, etc. are granted permissions via a different role than the one you use for members. Run without arguments to see the current setting. |
| `!setlog [#channel]` | Configures logging messages to inform you of errors or actions that MESBot is taking on your server. Tag the channel where you want the messages to be sent, or run without arguments to use the current channel. Make sure that MESBot has access to the channel. |
| `!setver #channel` | Configures the verification channel where MESBot will post welcome messages for authorized members. MESBot must be able to post in this channel. |
| `!setevent <event_id> <role>` | Configures event role assignment: registers the MES portal event ID and the Discord role to assign to registered attendees. Run without arguments to see the current setting. |
| `!clearevent` | Removes the event role configuration for this server. |

## Permissions

MESBot needs permission to view channels, send messages, and (optionally) control the server ban list. Users will also need to allow MESBot to send DMs, as sign-in URLs cannot be posted publicly for security.

## Configuration

| Variable | Description |
|---|---|
| `LOG_LEVEL` | Logging verbosity. `INFO` (default) logs operational events. `DEBUG` enables detailed tracing including OAuth state tokens and membership data — for troubleshooting only. |

## How It Works

On the backend MESBot has read-only access to our membership database to provide membership expiration reminders to users, and full access to its own private database to track server settings and user tokens. Authentication is implemented via oauth v2 which is part of our membership portal. Oauth v2 is a single sign-on standard used by many major providers like Microsoft and Google.

## Getting Help

Known issues: No current known issues.

You can contact the NTA for assistance with MESBot via email or DM on discord.

New features: I will implement the most requested user features. If you come up with a useful feature that is not available in any other bot, submit your feature via this link, and I will (time permitting) work on adding new features. Planned improvements include: accessing and sharing character sheets from the new database. Currently pending completion of the database.
