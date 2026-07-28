#!/usr/bin/env bash
# mesbot-supervisor-alert.sh — alert when a mesbot supervisor program leaves RUNNING.
#
# Companion to the fleet-wide /usr/local/sbin/host-health-alert.sh, which watches disk and
# memory on every host. This one is specific to the discord host and watches supervisor
# program state instead. It deliberately reuses that script's proven shape: the same SNS
# topic, the same 5-minute root cron cadence, and the same state-file dedup so a sustained
# outage produces one alert rather than one every five minutes.
#
# Installed 2026-07-28, after a planned RDS maintenance window left discord_app in FATAL for
# ~25 minutes with nothing to report it. Supervisor had exhausted its retries and given up;
# the bot was simply gone until someone happened to look.
#
# State handling:
#   RUNNING                     healthy
#   FATAL                       critical — supervisor has given up, a human must intervene.
#                               Alerts on the next run, no grace period.
#   BACKOFF/STARTING/EXITED/…   degraded — alerts only once it persists past GRACE_SECS, so an
#                               ordinary deploy restart, or the startretries=60 backoff ladder
#                               riding out DB maintenance, stays quiet while it is still
#                               plausibly recovering on its own.
#   supervisord unreachable     critical — the supervisor daemon itself is down.
#
# Not covered: a process that is RUNNING but internally wedged. flask_app in particular stays
# RUNNING and returns 500s when the database is unreachable, so this script would call it
# healthy. Catching that needs an application-level probe against its HTTP endpoint.
#
# Testing: run with --dry-run to print the SNS call instead of publishing it. MSA_STATE_DIR and
# MSA_SUPERVISORCTL override the state directory and the supervisorctl invocation, so the whole
# thing can be exercised as an unprivileged user without touching /var/lib or paging anyone.

set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

TOPIC="arn:aws:sns:us-east-1:275903357602:disk-space-alerts"
REGION="us-east-1"
PROGRAMS="discord_app flask_app"
GRACE_SECS=1200             # tolerate transient non-RUNNING states this long (20 min)
REALERT_SECS=21600          # re-alert at most every 6h while still breaching
STATE_DIR="${MSA_STATE_DIR:-/var/lib/mesbot-supervisor-alert}"
STATE="$STATE_DIR/state"
SUPERVISORCTL="${MSA_SUPERVISORCTL:-supervisorctl}"
HOST="$(hostname)"

AWS="$(command -v aws 2>/dev/null || true)"
[ -z "$AWS" ] && for p in /snap/bin/aws /usr/local/bin/aws /usr/bin/aws; do [ -x "$p" ] && AWS="$p" && break; done
if [ -z "$AWS" ] && [ "$DRY_RUN" -eq 0 ]; then echo "aws cli not found" >&2; exit 1; fi

mkdir -p "$STATE_DIR"
now=$(date +%s)

critical=""
degraded=""

# supervisorctl exits 3 when a program is not RUNNING, which is exactly the case we are here
# for, so its exit status is not a useful signal. Detect a dead daemon by the absence of any
# parseable program line rather than by exit code or error-string matching.
status_out="$($SUPERVISORCTL status 2>&1)"
parsed="$(printf '%s\n' "$status_out" | awk '$2 ~ /^(RUNNING|STARTING|BACKOFF|STOPPING|STOPPED|EXITED|FATAL|UNKNOWN)$/ {print $1, $2}')"

if [ -z "$parsed" ]; then
    first_line="$(printf '%s\n' "$status_out" | head -n 1)"
    critical="SUPERVISORD unreachable — ${first_line:-no output from supervisorctl}"$'\n'
else
    for prog in $PROGRAMS; do
        state="$(printf '%s\n' "$parsed" | awk -v p="$prog" '$1==p {print $2; exit}')"
        case "${state:-MISSING}" in
            RUNNING) ;;
            FATAL)   critical="${critical}${prog} FATAL — supervisor gave up restarting it"$'\n' ;;
            MISSING) critical="${critical}${prog} MISSING — no such program in supervisor"$'\n' ;;
            *)       degraded="${degraded}${prog} ${state}"$'\n' ;;
        esac
    done
fi

breaches="${critical}${degraded}"

# State file layout: line 1 breach signature, line 2 first-seen epoch, line 3 last-alert epoch
# (0 means "seen but never alerted", which is how the grace period is tracked across runs).
prev_sig=""; first_seen=0; last_alert=0
if [ -f "$STATE" ]; then
    prev_sig="$(sed -n '1p' "$STATE")"
    first_seen="$(sed -n '2p' "$STATE")"
    last_alert="$(sed -n '3p' "$STATE")"
fi
: "${first_seen:=0}"; : "${last_alert:=0}"

publish() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf -- '--- DRY RUN: would publish to %s ---\nSubject: %s\n\n%s\n--- end ---\n' \
            "$TOPIC" "$1" "$2"
        return 0
    fi
    "$AWS" sns publish --region "$REGION" --topic-arn "$TOPIC" \
        --subject "$1" --message "$2" >/dev/null 2>&1
}

if [ -z "$breaches" ]; then
    # Only announce recovery if we actually alerted; a blip inside the grace window is not news.
    if [ "$last_alert" -gt 0 ]; then
        publish "[mesbot] ${HOST} supervisor recovered" \
                "All mesbot supervisor programs are RUNNING again on ${HOST}: ${PROGRAMS}."
    fi
    rm -f "$STATE"
    exit 0
fi

cur_sig="$(printf '%s' "$breaches" | sort | tr '\n' ';')"
if [ "$cur_sig" != "$prev_sig" ]; then
    first_seen=$now
    last_alert=0
fi

# Critical states skip the grace period; degraded ones must outlast it.
if [ -n "$critical" ] || [ $(( now - first_seen )) -ge "$GRACE_SECS" ]; then
    if [ "$last_alert" -eq 0 ] || [ $(( now - last_alert )) -ge "$REALERT_SECS" ]; then
        down_for=$(( (now - first_seen) / 60 ))
        tail_out=""
        if printf '%s' "$breaches" | grep -q '^discord_app'; then
            tail_out="$(printf '\nLast lines of /var/log/discord_app.log:\n%s\n' \
                        "$(tail -n 8 /var/log/discord_app.log 2>/dev/null)")"
        fi
        msg="$(printf 'mesbot supervisor alert on %s:\n\n%s\nIn this state for ~%s min.\n%s\nFull status:\n%s\n' \
               "$HOST" "$breaches" "$down_for" "$tail_out" "$status_out")"
        publish "[mesbot] ${HOST} supervisor program not running" "$msg"
        last_alert=$now
    fi
fi

printf '%s\n%s\n%s\n' "$cur_sig" "$first_seen" "$last_alert" > "$STATE"
exit 0
