#!/bin/bash
# Deploy script for PythonAnywhere
# Run from a PythonAnywhere Bash console
# Usage: bash deploy.sh
#
# SETUP (one time):
#   1. Get your API token from: https://www.pythonanywhere.com/user/obiwancolobi/account/#api_token
#   2. Run: export PA_API_TOKEN="your_token_here"
#      (or add it to ~/.bashrc to persist it)

set -e  # Stop on any error

REPO_DIR="/home/obiwancolobi/WeatherTradePolyBot"
PA_USERNAME="obiwancolobi"
PA_API="https://www.pythonanywhere.com/api/v0/user/$PA_USERNAME"

echo "=== Pulling latest changes from GitHub ==="
cd "$REPO_DIR"
git pull origin main

echo "=== Installing/updating dependencies ==="
pip install -r requirements.txt --quiet

echo "=== Restarting always-on task ==="
if [ -z "$PA_API_TOKEN" ]; then
    echo "WARNING: PA_API_TOKEN not set. Skipping auto-restart."
    echo "  -> Manually click the restart button in the PythonAnywhere Tasks dashboard."
else
    # Get the always-on task ID
    TASK_ID=$(curl -s -H "Authorization: Token $PA_API_TOKEN" \
        "$PA_API/always_on/" | python3 -c "
import sys, json
tasks = json.load(sys.stdin)
for t in tasks:
    if 'weather_bot.py' in t.get('command', ''):
        print(t['id'])
        break
")

    if [ -z "$TASK_ID" ]; then
        echo "ERROR: Could not find always-on task for weather_bot.py"
        exit 1
    fi

    curl -s -X POST -H "Authorization: Token $PA_API_TOKEN" \
        "$PA_API/always_on/$TASK_ID/restart/" > /dev/null

    echo "Always-on task $TASK_ID restarted successfully."
fi

echo "=== Deploy complete ==="
