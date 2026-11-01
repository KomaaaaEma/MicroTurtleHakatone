#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

STUDIO_PYTHON="${STUDIO_PYTHON:-python3}"
STUDIO_PORT="${STUDIO_PORT:-8765}"
if [ ! -x .venv/bin/python ]; then
    "$STUDIO_PYTHON" -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"
    "$STUDIO_PYTHON" -m venv .venv
fi
if [ ! -f .venv/installed-requirements.txt ] || ! cmp -s requirements.txt .venv/installed-requirements.txt; then
    .venv/bin/python -m pip install -r requirements.txt
    cp requirements.txt .venv/installed-requirements.txt
fi
printf '\nMicroTurtles: http://127.0.0.1:%s\nPress Ctrl+C to stop. Data is saved in data/.\n\n' "$STUDIO_PORT"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$STUDIO_PORT"
