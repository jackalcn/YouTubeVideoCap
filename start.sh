#!/bin/sh
# Run with: sh start.sh. Sets up its own Python environment the first time, and repairs or updates it when needed.
set -e
cd "$(dirname "$0")"

STAMP=.venv/installed-requirements.txt
IS_SUPPORTED='import sys; sys.exit(sys.version_info < (3, 11))'

# Set up again when the environment is missing or broken, or requirements.txt changed since the last install that finished.
if ! cmp -s requirements.txt "$STAMP" || ! .venv/bin/python -c "import streamlit, yt_dlp" >/dev/null 2>&1; then
    echo "Setting up, this takes a few minutes the first time..."
    rm -f "$STAMP"
    # An environment whose Python was uninstalled, is older than 3.11, or has no pip cannot be repaired, only rebuilt.
    if ! .venv/bin/python -c "import pip; $IS_SUPPORTED" >/dev/null 2>&1; then
        base=""
        for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
            if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "$IS_SUPPORTED" >/dev/null 2>&1; then
                base=$candidate
                break
            fi
        done
        if [ -z "$base" ]; then
            echo "This app needs Python 3.11 or newer. Install it (for example: brew install python, or sudo apt install python3), then run start.sh again." >&2
            exit 1
        fi
        if ! "$base" -m venv --clear .venv; then
            echo "Could not create the Python environment; the message above says why. On Debian or Ubuntu, first run: sudo apt install python3-venv" >&2
            exit 1
        fi
    fi
    if ! .venv/bin/python -m pip install -r requirements.txt; then
        echo "Setup did not finish. The messages above say why, for example no internet connection. After fixing that, run start.sh again: setup picks up where it stopped." >&2
        exit 1
    fi
    cp requirements.txt "$STAMP"
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg was not found. Downloads will be limited to about 360p. To install it: brew install ffmpeg, or apt install ffmpeg"
fi

# Only this computer can connect. Set VIDEOCAP_ADDRESS=0.0.0.0 to allow other devices on the network.
# Through the app's launcher, so subscriptions and unfinished downloads resume before anyone opens the page.
exec .venv/bin/python -m videocap --server.address "${VIDEOCAP_ADDRESS:-localhost}" --server.headless "${VIDEOCAP_HEADLESS:-false}"
