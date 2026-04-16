#!/bin/bash
# Deploy script for polymarket-5m-bot
# Usage: bash deploy.sh
set -e

BRANCH="claude/poly-marketbot-investigation-MDuYT"
BOT_DIR="/root/polymarket-5m-bot"
PM2_NAME="polymarket-bot"

echo "=== Stopping bot ==="
pm2 stop "$PM2_NAME" 2>/dev/null || true
sleep 2

echo "=== Killing stale port processes ==="
fuser -k 8080/tcp 2>/dev/null || true
fuser -k 8081/tcp 2>/dev/null || true
sleep 1

echo "=== Deploying code ==="
cd "$BOT_DIR"

# Save .env (contains secrets — never overwrite)
cp .env .env.bak

# Reset to clean state and pull latest fixes
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

# Restore .env
cp .env.bak .env

echo "=== Verifying code ==="
python3 -c "import py_compile; py_compile.compile('bot.py', doraise=True); print('bot.py OK')"
python3 -c "import py_compile; py_compile.compile('core/execution.py', doraise=True); print('execution.py OK')"
python3 -c "import py_compile; py_compile.compile('dashboard/server.py', doraise=True); print('dashboard/server.py OK')"

echo "=== Starting bot ==="
pm2 restart "$PM2_NAME"
sleep 3
pm2 logs "$PM2_NAME" --lines 20 --nostream

echo "=== Done ==="
pm2 status
