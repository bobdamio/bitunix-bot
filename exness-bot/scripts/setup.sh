#!/bin/bash
# =============================================================
# Exness Bot — Server Setup Script
# Installs all dependencies and prepares the bot for deployment
# Run: bash scripts/setup.sh
# =============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "================================================"
echo "  Exness Bot v1.0 — Server Setup"
echo "================================================"
echo ""
echo "Project dir: $PROJECT_DIR"
echo ""

cd "$PROJECT_DIR"

# --- 1. System packages ---
echo "[1/6] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv \
    wine64 wine32 \
    xvfb \
    wget cabextract \
    > /dev/null 2>&1 || {
        echo "⚠️  Some packages failed. Wine on Linux requires:"
        echo "    sudo dpkg --add-architecture i386"
        echo "    sudo apt-get update && sudo apt-get install wine64 wine32"
    }
echo "  ✅ System packages installed"

# --- 2. Python venv ---
echo "[2/6] Setting up Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "  Created venv/"
fi
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "  ✅ Python dependencies installed"

# --- 3. Create directories ---
echo "[3/6] Creating directories..."
mkdir -p logs data
echo "  ✅ logs/ data/ created"

# --- 4. Environment file ---
echo "[4/6] Checking .env file..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "  ⚠️  Created .env from template — EDIT IT with your MT5 password:"
    echo "     nano $PROJECT_DIR/.env"
else
    echo "  ✅ .env exists"
fi

# --- 5. Validate config ---
echo "[5/6] Validating config..."
python3 -c "
import sys
sys.path.insert(0, '.')
from src.config import load_config
config = load_config('config.yaml')
print(f'  Symbols: {config.symbols}')
print(f'  Server: {config.mt5.server}')
print(f'  Login: {config.mt5.login}')
print(f'  Risk: {config.position.risk_percent*100:.1f}%')
print(f'  MTF: {config.mtf.htf_timeframe} → {config.mtf.mtf_timeframe} → {config.mtf.ltf_timeframe}')
print('  ✅ Config valid')
"

# --- 6. Systemd service ---
echo "[6/6] Creating systemd service..."
SERVICE_FILE="/etc/systemd/system/exness-bot.service"
if [ ! -f "$SERVICE_FILE" ]; then
    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Exness Trading Bot v1.0
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$PROJECT_DIR
Environment=DISPLAY=:99
Environment=PATH=$PROJECT_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=$PROJECT_DIR/.env
ExecStartPre=/usr/bin/Xvfb :99 -screen 0 1024x768x16 &
ExecStart=$PROJECT_DIR/venv/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    echo "  ✅ Systemd service created: exness-bot.service"
    echo "     Start: sudo systemctl start exness-bot"
    echo "     Enable: sudo systemctl enable exness-bot"
else
    echo "  ✅ Systemd service already exists"
fi

echo ""
echo "================================================"
echo "  Setup complete!"
echo "================================================"
echo ""
echo "Next steps:"
echo "  1. Edit .env with your MT5 password:"
echo "     nano $PROJECT_DIR/.env"
echo ""
echo "  2. Start the bot (choose one):"
echo "     a) Direct:   cd $PROJECT_DIR && source venv/bin/activate && python main.py"
echo "     b) Systemd:  sudo systemctl start exness-bot"
echo "     c) Docker:   docker compose up -d"
echo ""
echo "  3. View logs:"
echo "     tail -f $PROJECT_DIR/logs/exness_bot.log"
echo ""
