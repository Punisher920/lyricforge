#!/usr/bin/env bash
# Double-click this on macOS, or run ./lyricforge.command on Linux.
set -u
cd "$(dirname "$0")"

VENV=".venv"
VPY="$VENV/bin/python"

if [ ! -x "$VPY" ]; then
    echo
    echo "  First run - setting things up. This takes a minute or two."
    echo
    BOOT=""
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then BOOT="$candidate"; break; fi
    done
    if [ -z "$BOOT" ]; then
        echo "  Python was not found."
        echo
        echo "  Install Python 3.10 or newer from https://www.python.org/downloads/"
        echo "  then run this again."
        echo
        read -r -p "Press Return to close."
        exit 1
    fi
    if ! "$BOOT" -m venv "$VENV"; then
        echo "  Could not create the Python environment."
        read -r -p "Press Return to close."
        exit 1
    fi
fi

if ! "$VPY" -c "import numpy, PIL, imageio_ffmpeg" >/dev/null 2>&1; then
    echo "  Installing what it needs (one time only)..."
    echo
    if ! "$VPY" -m pip install --disable-pip-version-check --quiet -r requirements.txt; then
        echo
        echo "  Installing the dependencies failed - see the error above."
        read -r -p "Press Return to close."
        exit 1
    fi
fi

echo
echo "  Starting lyricforge - your browser will open in a moment."
echo "  Keep this window open while you work; press Ctrl+C when finished."
echo
"$VPY" -m lyricforge ui
