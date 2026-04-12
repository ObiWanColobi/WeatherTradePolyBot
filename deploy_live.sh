#!/bin/bash
# Deploy script for PythonAnywhere — LIVE branch
# Run from a PythonAnywhere Bash console
# Usage: bash deploy_live.sh
#
# IMPORTANT: This deploys the 'live' branch with real money trading.
# Make sure TRADING_MODE=live is set in .env on the server.

set -e

REPO_DIR="/home/obiwancolobi/WeatherTradePolyBot-Live"
PA_USERNAME="obiwancolobi"
PA_API="https://www.pythonanywhere.com/api/v0/user/$PA_USERNAME"

echo "=== LIVE DEPLOYMENT ==="
echo "WARNING: This deploys real-money trading code."
read -p "Continue? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

echo "=== Pulling latest changes from live branch ==="
cd "$REPO_DIR"
git pull origin live

echo "=== Installing/updating dependencies ==="
pip install -r requirements.txt --quiet

echo "=== Restarting always-on task ==="
if [ -z "$PA_API_TOKEN" ]; then
    echo "WARNING: PA_API_TOKEN not set. Skipping auto-restart."
    echo "  -> Manually restart in the PythonAnywhere Tasks dashboard."
else
    TASK_ID=$(curl -s -H "Authorization: Token $PA_API_TOKEN" \
        "$PA_API/always_on/" | python3 -c "
import sys, json
tasks = json.load(sys.stdin)
for t in tasks:
    if 'weather_bot.py' in t.get('command', '') and 'Live' in t.get('description', ''):
        print(t['id'])
        break
")

    if [ -z "$TASK_ID" ]; then
        echo "ERROR: Could not find live always-on task."
        echo "  Create one first in PythonAnywhere with description containing 'Live'."
        exit 1
    fi

    curl -s -X POST -H "Authorization: Token $PA_API_TOKEN" \
        "$PA_API/always_on/$TASK_ID/restart/" > /dev/null

    echo "Always-on task $TASK_ID restarted."
fi

echo "=== Live deploy complete ==="
