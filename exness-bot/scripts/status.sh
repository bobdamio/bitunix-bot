#!/bin/bash
# =============================================================
# Exness Bot — Status Script
# Shows bot status, recent logs, and open positions
# Run: bash scripts/status.sh
# =============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "================================================"
echo "  Exness Bot — Status"
echo "  $(date '+%Y-%m-%d %H:%M:%S UTC')"
echo "================================================"

# Check if running via systemd
if systemctl is-active --quiet exness-bot 2>/dev/null; then
    echo "  Service: ✅ RUNNING (systemd)"
    echo ""
    echo "  Recent journal logs:"
    journalctl -u exness-bot --no-pager -n 10
elif docker ps --filter name=exness-bot --format '{{.Status}}' 2>/dev/null | grep -q 'Up'; then
    echo "  Container: ✅ RUNNING (docker)"
    echo ""
    echo "  Recent docker logs:"
    docker logs exness-bot --tail 10
elif pgrep -f "python.*main.py" > /dev/null; then
    PID=$(pgrep -f "python.*main.py")
    echo "  Process: ✅ RUNNING (PID $PID)"
else
    echo "  Status: ❌ NOT RUNNING"
fi

echo ""

# Check log file
LOG_FILE="$PROJECT_DIR/logs/exness_bot.log"
if [ -f "$LOG_FILE" ]; then
    LOG_SIZE=$(du -h "$LOG_FILE" | cut -f1)
    LOG_LINES=$(wc -l < "$LOG_FILE")
    echo "  Log file: $LOG_FILE ($LOG_SIZE, $LOG_LINES lines)"
    echo ""
    echo "  Last 15 lines:"
    echo "  ─────────────────────────────────────────────"
    tail -15 "$LOG_FILE" | sed 's/^/  /'
else
    echo "  Log file: not found (bot hasn't started yet)"
fi

echo ""
echo "================================================"
