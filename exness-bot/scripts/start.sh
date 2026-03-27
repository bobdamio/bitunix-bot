#!/bin/bash
# =============================================================
# Exness Bot — Start Script
# Starts the bot with Xvfb virtual display (needed for MT5)
# Run: bash scripts/start.sh
# =============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Load environment
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

# Activate venv if exists
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

# Start virtual display if not already running
if ! pgrep -x Xvfb > /dev/null; then
    echo "Starting Xvfb virtual display..."
    Xvfb :99 -screen 0 1024x768x16 &
    export DISPLAY=:99
    sleep 1
fi

echo "================================================"
echo "  Starting Exness Bot v1.0"
echo "  $(date '+%Y-%m-%d %H:%M:%S UTC')"
echo "================================================"

exec python main.py "$@"
