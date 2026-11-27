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
# MSA_DEPLOY_MARKER overrides the deploy marker path (default /var/lib/mesbot-deploy/marker);
# --dry-run covers the deploy alerts too.
#
# Deploy alerts (F-004): the script also reads the daily deploy job's marker and publishes one
# "deploy failed" alert per failed commit, one critical alert when the rollback failed, one
# "deploy marker invalid" alert per invalid marker, and one "deploy recovered" once an alerted
# marker is deleted. See the deploy marker section below.

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
DEPLOY_MARKER="${MSA_DEPLOY_MARKER:-/var/lib/mesbot-deploy/marker}"
DEPLOY_STATE="$STATE_DIR/deploy-state"
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

# --- Deploy marker (F-004) -------------------------------------------------------------------
# The daily deploy job (scripts/mesbot_deploy.py, run as mesbot) leaves a marker after a failed
# deploy. This script runs as root and mesbot owns the marker's directory, so the marker is
# untrusted input: it is never sourced, evaluated or executed, a symlink is never followed, and
# only values that pass the fixed formats below ever reach a message. Anything else is reported
# as "deploy marker invalid" with none of the file's content.
#
# Valid marker: a regular file of exactly these four key=value lines, in any order:
#   commit=<40 lowercase hex>  step=precheck|install|restart|healthcheck
#   rollback=succeeded|failed|none  timestamp=YYYY-MM-DDTHH:MM:SSZ (UTC)
#
# De-duplication lives in its own file, so the supervisor state above is untouched. It holds
# the signature of the last marker alerted on; a recovery is announced only if one was sent.

deploy_marker_check() {
    local tmp size nul_free line key value n=0
    # Byte-wise matching, so a locale cannot widen the character ranges below.
    local LC_ALL=C
    m_commit=""; m_step=""; m_rollback=""; m_timestamp=""; m_digest=""

    if [ -L "$DEPLOY_MARKER" ]; then return 2; fi
    if [ ! -e "$DEPLOY_MARKER" ]; then return 1; fi
    if [ ! -f "$DEPLOY_MARKER" ]; then return 2; fi

    # Copy once, then validate the private copy: nofollow refuses a symlink swapped in after the
    # checks above, nonblock stops a swapped-in FIFO from hanging the cron run, and the size cap
    # bounds what is read. A dd without those flags (not GNU, e.g. a dev box) falls back to a
    # time-limited read plus a symlink re-check afterwards.
    tmp="$(mktemp "$STATE_DIR/.deploy-marker.XXXXXX")" || return 2
    if dd if=/dev/null of=/dev/null iflag=nofollow,nonblock count=0 2>/dev/null; then
        dd if="$DEPLOY_MARKER" of="$tmp" iflag=nofollow,nonblock bs=513 count=1 2>/dev/null
    else
        timeout 5 head -c 513 < "$DEPLOY_MARKER" > "$tmp" 2>/dev/null
    fi
    if [ "$?" -ne 0 ] || [ -L "$DEPLOY_MARKER" ] || [ ! -f "$DEPLOY_MARKER" ]; then
        rm -f "$tmp"
        if [ ! -e "$DEPLOY_MARKER" ] && [ ! -L "$DEPLOY_MARKER" ]; then return 1; fi
        return 2
    fi
    # A hash of the private copy tells one invalid marker from the next for de-duplication.
    # It goes only into the root-owned state file, never into a message.
    m_digest="$(sha256sum < "$tmp" 2>/dev/null | cut -c1-64)"
    size="$(wc -c < "$tmp")"
    # Command substitution would silently drop NUL bytes, so reject them up front.
    nul_free="$(LC_ALL=C tr -d '\000' < "$tmp" | wc -c)"
    if [ "$size" -gt 512 ] || [ "$nul_free" -ne "$size" ]; then rm -f "$tmp"; return 2; fi

    while IFS= read -r line || [ -n "$line" ]; do
        n=$((n + 1))
        case "$line" in *=*) ;; *) rm -f "$tmp"; return 2 ;; esac
        key="${line%%=*}"
        value="${line#*=}"
        case "$key" in
            commit)    [ -z "$m_commit" ]    && [[ "$value" =~ ^[0-9a-f]{40}$ ]] && m_commit="$value" ;;
            step)      [ -z "$m_step" ]      && case "$value" in precheck|install|restart|healthcheck) m_step="$value" ;; *) false ;; esac ;;
            rollback)  [ -z "$m_rollback" ]  && case "$value" in succeeded|failed|none) m_rollback="$value" ;; *) false ;; esac ;;
            timestamp) [ -z "$m_timestamp" ] && [[ "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
                       && [ "$(date -u -d "$value" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" = "$value" ] \
                       && m_timestamp="$value" ;;
            *) false ;;
        esac || { rm -f "$tmp"; return 2; }
    done < "$tmp"
    rm -f "$tmp"

    if [ "$n" -ne 4 ] || [ -z "$m_commit" ] || [ -z "$m_step" ] || [ -z "$m_rollback" ] \
       || [ -z "$m_timestamp" ]; then
        return 2
    fi
    return 0
}

deploy_alerts() {
    local rc prev_deploy_sig="" sig subject msg
    [ -f "$DEPLOY_STATE" ] && prev_deploy_sig="$(sed -n '1p' "$DEPLOY_STATE")"

    deploy_marker_check
    rc=$?

    if [ "$rc" -eq 1 ]; then
        if [ -n "$prev_deploy_sig" ]; then
            publish "[mesbot] ${HOST} deploy recovered" \
                    "The mesbot deploy marker on ${HOST} has been cleared." \
                && rm -f "$DEPLOY_STATE"
        else
            rm -f "$DEPLOY_STATE"
        fi
        return 0
    fi

    if [ "$rc" -eq 2 ]; then
        # One alert per invalid marker (FR-26): the signature names this particular file, so a
        # different invalid marker put in its place alerts again. lstat data (stat without -L)
        # covers symlinks and non-regular files without following or reading them, and the
        # content hash covers a regular file rewritten in place within the same second.
        sig="invalid $(stat -c '%d:%i:%f:%s:%Y' -- "$DEPLOY_MARKER" 2>/dev/null || echo gone) ${m_digest:-none}"
        subject="[mesbot] ${HOST} deploy marker invalid"
        msg="$(printf 'The mesbot deploy marker %s on %s is invalid: a symlink, not a regular file, or not in the expected format. Its content is not included.\n\nThe daily deploy job deploys nothing until the owner inspects and deletes it by hand as mesbot.\n' \
               "$DEPLOY_MARKER" "$HOST")"
    elif [ "$m_rollback" = "failed" ]; then
        sig="rollback-failed ${m_commit}"
        subject="[mesbot] ${HOST} deploy and rollback failed, manual action needed"
        msg="$(printf 'mesbot deploy of commit %s on %s failed at step %s, and the rollback to the previous commit also failed.\n\nTimestamp: %s\n\nBoth programs may be down or on a half-restored commit. Get them RUNNING by hand, then delete %s. The daily deploy job deploys nothing until then.\n' \
               "$m_commit" "$HOST" "$m_step" "$m_timestamp" "$DEPLOY_MARKER")"
    else
        sig="deploy-failed ${m_commit}"
        subject="[mesbot] ${HOST} deploy failed"
        msg="$(printf 'mesbot deploy failed on %s.\n\nCommit: %s\nStep: %s\nRollback: %s\nTimestamp: %s\n\nThis commit is not retried; the next new commit on main deploys as usual. Delete %s to retry it.\n' \
               "$HOST" "$m_commit" "$m_step" "$m_rollback" "$m_timestamp" "$DEPLOY_MARKER")"
    fi

    if [ "$sig" != "$prev_deploy_sig" ]; then
        # Record only a delivered alert, so a failed publish is retried on the next run.
        publish "$subject" "$msg" && printf '%s\n' "$sig" > "$DEPLOY_STATE"
    fi
    return 0
}

deploy_alerts

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
