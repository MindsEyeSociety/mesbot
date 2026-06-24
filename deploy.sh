#!/bin/bash
# Deploy script: pull latest main and restart both supervisord processes.
# Run this on the server after merging a PR to main.
set -euo pipefail

REPO_DIR="/home/mesbot"

echo "=== Pulling latest main ==="
cd "$REPO_DIR"
git pull origin main

echo "=== Restarting services ==="
sudo supervisorctl restart discord_app
sudo supervisorctl restart flask_app

echo "=== Status ==="
sudo supervisorctl status

echo "=== Deploy complete ==="
