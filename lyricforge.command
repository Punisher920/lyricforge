#!/usr/bin/env bash
# Double-click this on macOS, or run ./lyricforge.command on Linux.
set -u
cd "$(dirname "$0")"

if [ ! -f requirements.txt ] || [ ! -f lyricforge/__init__.py ]; then
    echo
    echo "  This launcher is not inside the lyricforge project folder."
    echo "  It is sitting in: $(pwd)"
    echo
    echo "  It has to live next to requirements.txt and the lyricforge folder."
    echo "  Download the project from https://github.com/Punisher920/lyricforge"
    echo "  then run this file from inside it."
    echo
    read -r -p "Press Return to close."
    exit 1
fi

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
        echo "  Usually no internet, or a network that blocks pip."
        read -r -p "Press Return to close."
        exit 1
    fi
fi

echo
echo "  Starting lyricforge - your browser will open in a moment."
echo "  Keep this window open while you work; press Ctrl+C when finished."
echo
"$VPY" -m lyricforge ui
