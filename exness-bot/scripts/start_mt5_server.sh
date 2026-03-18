#!/bin/bash
# Start Xvfb + MT5 Terminal + RPyC bridge server
# This script must run BEFORE the bot starts

set -e

DISPLAY_NUM=99
WINE_PYTHON="$HOME/.wine/drive_c/users/goldast/AppData/Local/Programs/Python/Python311/python.exe"
MT5_TERMINAL="$HOME/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe"
RPYC_PORT=${RPYC_PORT:-18812}
LOG_DIR="$(dirname "$0")/../logs"
mkdir -p "$LOG_DIR"

echo "=== Starting MT5 Bridge Server ==="

# 1. Start Xvfb (virtual display)
if pgrep -x Xvfb > /dev/null; then
    echo "[OK] Xvfb already running"
else
    echo "[..] Starting Xvfb on :${DISPLAY_NUM}..."
    Xvfb :${DISPLAY_NUM} -screen 0 1024x768x16 &>/dev/null &
    sleep 1
    echo "[OK] Xvfb started"
fi
export DISPLAY=:${DISPLAY_NUM}

# 2. Start MT5 Terminal
if pgrep -f "terminal64.exe" > /dev/null; then
    echo "[OK] MT5 terminal already running"
else
    echo "[..] Starting MT5 Terminal..."
    wine64 "$MT5_TERMINAL" /portable &> "$LOG_DIR/mt5_terminal.log" &
    sleep 5
    echo "[OK] MT5 Terminal started"
fi

# 3. Start RPyC server (under Wine's Python)
if pgrep -f "rpyc_server.py" > /dev/null || ss -tlnp 2>/dev/null | grep -q ":${RPYC_PORT}"; then
    echo "[OK] RPyC server already running on port ${RPYC_PORT}"
else
    echo "[..] Starting RPyC server on port ${RPYC_PORT}..."
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    wine64 "$WINE_PYTHON" "$SCRIPT_DIR/rpyc_server.py" ${RPYC_PORT} 0.0.0.0 &
    sleep 5

    # Verify server is listening
    if ss -tlnp 2>/dev/null | grep -q ":${RPYC_PORT}"; then
        echo "[OK] RPyC server listening on port ${RPYC_PORT}"
    else
        echo "[WARN] RPyC server may not be ready yet. Check logs/rpyc_server.log"
    fi
fi

echo ""
echo "=== MT5 Bridge Ready ==="
echo "RPyC port: ${RPYC_PORT}"
echo "Bot can now connect to localhost:${RPYC_PORT}"
