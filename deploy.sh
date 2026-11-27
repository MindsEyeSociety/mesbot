#!/bin/bash
# Manual deploy: pull latest main, install the hash-locked dependencies, restart both
# supervisord programs. Run it on the server as `mesbot` (the venv belongs to mesbot).
# Normal deploys are done by the daily job, scripts/mesbot_deploy.py (F-004).
#
# It takes the same deploy lock as the daily job, so the two never run at once. If the install
# fails it resets the checkout to the commit it started at and does not restart, so the running
# programs keep the old code (packages already changed in the venv are not restored).
# It neither reads nor writes the daily job's marker and has no health check: after a manual
# fix, delete /var/lib/mesbot-deploy/marker by hand.
#
# Overrides, for tests that run it unprivileged against a local remote and stubs (the same
# names as the daily job):
#   MESBOT_DEPLOY_REPO_DIR       checkout                       (default /home/mesbot)
#   MESBOT_DEPLOY_VENV_PYTHON    venv interpreter               (default /home/mesbot/.venv/bin/python)
#   MESBOT_DEPLOY_STATE_DIR      directory holding deploy.lock  (default /var/lib/mesbot-deploy)
#   MESBOT_DEPLOY_SUPERVISORCTL  supervisorctl command          (default "sudo supervisorctl")
set -euo pipefail

REPO_DIR="${MESBOT_DEPLOY_REPO_DIR:-/home/mesbot}"
VENV_PYTHON="${MESBOT_DEPLOY_VENV_PYTHON:-/home/mesbot/.venv/bin/python}"
STATE_DIR="${MESBOT_DEPLOY_STATE_DIR:-/var/lib/mesbot-deploy}"
LOCK_FILE="$STATE_DIR/deploy.lock"
# Word-split on purpose, so the override can carry a command plus arguments.
read -r -a SUPERVISORCTL <<< "${MESBOT_DEPLOY_SUPERVISORCTL:-sudo supervisorctl}"

if [ "$(id -u)" -eq 0 ]; then
    # git would run repo hooks and config with root rights, and pip would write a root-owned venv.
    echo "deploy.sh must be run as mesbot, not root" >&2
    exit 1
fi

# The lock is held on fd 9 until this script exits.
if ! exec 9>>"$LOCK_FILE"; then
    echo "Cannot open the deploy lock $LOCK_FILE (does $STATE_DIR exist and belong to mesbot?)" >&2
    exit 1
fi
if ! flock -n 9; then
    echo "Another deploy is running (lock $LOCK_FILE is held); nothing changed" >&2
    exit 1
fi

cd "$REPO_DIR"
PREVIOUS="$(git rev-parse --verify 'HEAD^{commit}')"

echo "=== Pulling latest main ==="
git pull origin main

echo "=== Installing requirements.lock ==="
if ! "$VENV_PYTHON" -m pip install --require-hashes -r requirements.lock; then
    echo "=== Install failed: resetting the checkout to $PREVIOUS, not restarting ===" >&2
    git reset --hard --quiet "$PREVIOUS"
    exit 1
fi

echo "=== Restarting services ==="
"${SUPERVISORCTL[@]}" restart discord_app
"${SUPERVISORCTL[@]}" restart flask_app

echo "=== Status ==="
"${SUPERVISORCTL[@]}" status

echo "=== Deploy complete ==="
