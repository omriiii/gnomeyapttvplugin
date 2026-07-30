#!/usr/bin/env bash
# Mac/Linux launcher. HL:A itself only runs on Windows, so this is mainly
# useful for testing the bot's chat/TTS side (voices, commands, the status
# page) without the game -- HLAlyxQueries just retries the connection
# every second in the background regardless, so it's safe to run.
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "  Gnome Bot Launcher"
echo "============================================"
echo

if command -v python3 >/dev/null 2>&1; then
    PYLAUNCH=python3
elif command -v python >/dev/null 2>&1; then
    PYLAUNCH=python
else
    echo "[ERROR] Python 3 wasn't found on this machine."
    echo "Install it from https://www.python.org/downloads/ and run this again."
    exit 1
fi

if [ ! -x "venv/bin/python" ]; then
    echo "Setting up a virtual environment - this only happens once..."
    "$PYLAUNCH" -m venv venv
fi

VENV_PY="venv/bin/python"

echo "Checking dependencies..."
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install -r requirements.txt

echo
echo "Starting the bot..."
echo "Status page: http://127.0.0.1:8420/"
echo "Press Ctrl+C to stop."
echo

# Best-effort browser open; harmless if neither command exists (e.g. a
# headless/remote machine) -- suppressed with an "|| true" ignore-failure.
( sleep 3 && (open http://127.0.0.1:8420/ 2>/dev/null || xdg-open http://127.0.0.1:8420/ 2>/dev/null || true) ) &

"$VENV_PY" twitch_gnome_bot.py
