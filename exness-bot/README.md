# Exness Bot v1.0

**Automated forex/commodities trading bot for Exness MT5**

Strategy: **FVG/IFVG + Supply/Demand Zones + Multi-Timeframe Analysis (15m→5m→1m)**
Instruments: **XAUUSD** (Gold), **USOIL** (WTI Crude Oil)
Account type: **Hedging**
Platform: **Windows** (VPS or local)

---

## Table of Contents

- [Strategy Overview](#strategy-overview)
- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick Start (Windows VPS)](#quick-start-windows-vps)
- [Configuration Reference](#configuration-reference)
- [Auto-Start on Reboot](#auto-start-on-reboot)
- [Monitoring & Logs](#monitoring--logs)
- [FAQ & Troubleshooting](#faq--troubleshooting)

---

## Strategy Overview

### Multi-Timeframe Analysis (15m → 5m → 1m)

| Timeframe | Role | What it does |
|-----------|------|-------------|
| **M15** | Directional bias | Identifies fresh Supply/Demand zones. Sets the macro direction. |
| **M5** | Confirmation | Confirms zone alignment. Detects FVGs within the 15m zone. |
| **M1** | Entry timing | Finds precise FVG/IFVG entry point inside the confirmed zone. |

### Supply & Demand Zones

Zones are detected by finding consolidation bases followed by strong impulse moves:

```
Supply Zone (SHORT signal):
  Price consolidates → sharp drop (impulse)
  Zone = consolidation area BEFORE the drop
  When price returns → expect selling reaction → SELL

Demand Zone (LONG signal):
  Price consolidates → sharp rally (impulse)
  Zone = consolidation area BEFORE the rally
  When price returns → expect buying reaction → BUY
```

- **Fresh zones** (untouched) get a +20% strength bonus
- Zones invalidate after 3 touches
- Zones broken by close above/below are removed

### FVG/IFVG Detection (from GoldasT Bot)

Fair Value Gaps represent imbalances in price action:

```
Bullish FVG (LONG):    Candle1.high < Candle3.low    → gap UP
Bearish FVG (SHORT):   Candle1.low  > Candle3.high   → gap DOWN
IFVG (Inverse):        Price partially violates FVG   → reversal entry
```

- Sliding window scans ALL candle triplets (not just the last 3)
- Non-violated zones are ranked by proximity (60%) + strength (40%)
- Strength score = gap size + volume + trend alignment + impulse quality

### Entry Logic

```
1.  15m Supply zone detected → bearish bias
2.  5m or 1m FVG (bearish) overlaps with that supply zone
3.  Price fills 25-85% of the FVG → optimal entry zone
4.  Confluence score ≥ 0.60 → SELL with SL above supply zone
```

Same logic reversed for demand + bullish FVG → BUY.

### Entry Types

| Type | When | How |
|------|------|-----|
| **Market** | FVG + Zone confluence confirmed, price in entry zone | Direct market execution |
| **Buy Stop** | Price consolidating just below resistance | Placed ATR×0.3 above resistance |
| **Sell Stop** | Price consolidating just above support | Placed ATR×0.3 below support |

### Risk Management

| Component | Setting | Description |
|-----------|---------|-------------|
| Risk per trade | 2% | Position sized by SL distance and contract size |
| SL placement | Zone edge + ATR buffer | Below demand zone (LONG) / Above supply zone (SHORT) |
| Dynamic R:R | 1:1 → 1:2 → 1:3 | Based on confluence score: weak → medium → strong |
| Trailing SL | 3-phase | Initial → BE at 1R → Runner trail at 2R |
| Max positions | 3 total | 2 per symbol (hedging allowed) |
| Killzones | London/NY/Late NY | Only trades during active sessions |

### Trailing SL Phases

```
Phase 1 (Initial):    SL = zone edge + ATR buffer
Phase 2 (Breakeven):  At 1R profit → SL moves to entry + 0.3R (lock profit)
Phase 3 (Runner):     At 2R profit → SL trails 1.5R behind price
```

---

## How It Works

```
Every 5 seconds:
  ┌─────────────────────────────────────────────┐
  │  Check MT5 connection                        │
  │  Update account balance/equity               │
  │  Check if inside killzone (London/NY)        │
  │                                              │
  │  For each symbol (XAUUSD, USOIL):            │
  │    ├─ Fetch M15, M5, M1 candles              │
  │    ├─ Detect Supply/Demand zones (M15)       │
  │    ├─ Detect FVGs across all timeframes      │
  │    ├─ Find FVG inside zone (confluence)      │
  │    ├─ Calculate confluence score              │
  │    ├─ If score ≥ 0.60:                       │
  │    │   ├─ Calculate TP/SL (ATR + zone)       │
  │    │   ├─ Calculate lot size (2% risk)        │
  │    │   └─ Execute trade (market/pending)      │
  │    │                                          │
  │    └─ Manage open positions:                  │
  │        ├─ Check BE activation (1R)            │
  │        └─ Update trailing SL (2R+)            │
  └─────────────────────────────────────────────┘
```

---

## Architecture

```
exness-bot/
├── main.py                     # Entry point
├── config.yaml                 # All settings
├── .env                        # MT5 credentials (not in git)
├── requirements.txt            # Python dependencies
│
├── src/
│   ├── bot.py                  # Bot lifecycle: start/stop/loop
│   ├── config.py               # YAML config loader
│   ├── models.py               # Data models: Candle, FVG, Zone, etc.
│   ├── mt5_client.py           # MetaTrader5 API client
│   ├── strategy_engine.py      # Main orchestrator
│   ├── fvg_detector.py         # FVG/IFVG detection
│   ├── supply_demand.py        # Supply/Demand zone detector
│   ├── mtf_analyzer.py         # Multi-Timeframe analyzer
│   ├── market_structure.py     # BOS / Swing detection
│   ├── tpsl_calculator.py      # TP/SL calculator
│   └── position_sizer.py       # Lot size calculator
│
├── scripts/
│   ├── install_windows.bat     # Setup script
│   └── run_bot.bat             # Run script
│
├── data/                       # Persistent data
└── logs/                       # Log files
```

---

## Requirements

- **Windows** 10/11 or Windows Server 2019+
- **Python** 3.10 – 3.12 (3.13 is not supported by the MetaTrader5 package)
- **MetaTrader 5** — auto-installed by `INSTALL.bat`, or download from [metatrader5.com](https://www.metatrader5.com/en/download)
  > **Note:** Any MT5 installer works (generic or Exness-branded). After installing, open MT5 → File → Open an Account → search "Exness" → select your server and log in.
- **RAM:** 1GB+
- **Internet:** stable connection

---

## Quick Start (Windows VPS)

### One-Click Install

1. Connect to your Windows VPS via RDP
2. Download or clone this repo:
   ```cmd
   git clone https://github.com/bobdamio/bitunix-bot.git
   ```
3. Open `bitunix-bot\exness-bot` folder
4. **Double-click `INSTALL.bat`**

That's it. The installer will:
- Download and install **MetaTrader 5** (if missing)
- Download and install **Python 3.12** (if missing)
- Install all dependencies
- Ask for your MT5 login/password
- Set up auto-start on reboot (both MT5 and the bot)
- Offer to start the bot immediately

> **Note:** After MT5 installation, you need to add your Exness server:
> 1. Open MT5 → File → Open an Account
> 2. Search "Exness" → select your server (e.g. Exness-MT5Trial15)
> 3. Log in with credentials from your [Exness Personal Area](https://my.exness.com)
> 4. Keep MT5 running — the bot connects to it.

### Manual Setup (alternative)

If you prefer to install manually instead of using `install.ps1`:

1. Install **Python 3.12** from https://python.org/downloads/ (check ✅ "Add to PATH")
2. Install **Git** from https://git-scm.com/download/win
3. Install **MetaTrader 5** from [metatrader5.com](https://www.metatrader5.com/en/download). After installing, open MT5 → File → Open an Account → search "Exness" → select your server → log in with your Exness credentials.
4. Clone and setup:
   ```cmd
   git clone https://github.com/bobdamio/bitunix-bot.git
   cd bitunix-bot\exness-bot
   python -m venv venv
   venv\Scripts\activate.bat
   pip install -r requirements.txt
   copy .env.example .env
   notepad .env
   ```
5. Fill in `.env` with your MT5 credentials
6. Run: `scripts\run_bot.bat`

### Expected output

```
12:00:00 [INFO] ============================================================
12:00:00 [INFO] 🤖 Exness Bot v1.0 Starting...
12:00:00 [INFO]    Symbols: XAUUSD, USOIL
12:00:00 [INFO]    Server: Exness-MT5Trial15
12:00:00 [INFO]    MTF: M15 → M5 → M1
12:00:00 [INFO]    Risk: 2.0%
12:00:00 [INFO] ============================================================
12:00:01 [INFO] ✅ MT5 connected: Exness-MT5Trial15 | Account #260474980 | Balance: $10000.00
```

---

## Auto-Start on Reboot

To automatically restart the bot when the Windows VPS reboots:

### Option 1: Task Scheduler (simple)

1. Open **Task Scheduler** (`taskschd.msc`)
2. Click **Create Task** (not "Create Basic Task")
3. Tab **General**:
   - Name: `ExnessBot`
   - ✅ "Run whether user is logged on or not"
   - ✅ "Run with highest privileges"
4. Tab **Triggers**:
   - New → "At startup" → Delay: 30 seconds
5. Tab **Actions**:
   - New → Action: "Start a program"
   - Program: `C:\Users\Administrator\bitunix-bot\exness-bot\scripts\run_bot.bat`
   - Start in: `C:\Users\Administrator\bitunix-bot\exness-bot`
6. Tab **Settings**:
   - ✅ "If the task fails, restart every 1 minute"
   - "Attempt to restart up to 999 times"

### Option 2: NSSM (as a Windows Service)

```cmd
REM Download NSSM from: https://nssm.cc/download
nssm install ExnessBot C:\Users\Administrator\bitunix-bot\exness-bot\venv\Scripts\python.exe main.py
nssm set ExnessBot AppDirectory C:\Users\Administrator\bitunix-bot\exness-bot
nssm set ExnessBot AppStdout C:\Users\Administrator\bitunix-bot\exness-bot\logs\service.log
nssm set ExnessBot AppStderr C:\Users\Administrator\bitunix-bot\exness-bot\logs\service.log
nssm start ExnessBot
```

> **Important:** MT5 terminal must also start on boot.
> Place a shortcut to `terminal64.exe` in the Startup folder:
> `C:\Users\Administrator\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\`

---

## Configuration Reference

The `config.yaml` file contains all bot settings. Key sections:

| Section | What it configures |
|---------|-------------------|
| `mt5` | Server, login, password (from .env), terminal path |
| `symbols` | Traded instruments (XAUUSD, USOIL, ...) |
| `account` | Hedge mode, magic number, deviation |
| `fvg` | FVG/IFVG detection — entry zones, strength, volume |
| `supply_demand` | S/D zones — lookback, strength, touches |
| `mtf` | Multi-Timeframe — timeframes, weights, threshold |
| `pending_orders` | Buy Stop / Sell Stop — offset, expiration |
| `tpsl` | TP/SL — ATR buffers, R:R, trailing SL |
| `position` | Risk %, min/max lots, max positions |
| `cooldowns` | Cooldowns between entries |
| `session` | Killzones (London, NY, Late NY) |
| `telegram` | Telegram notifications (disabled by default) |

---

## Monitoring & Logs

### Log files
```cmd
type logs\exness_bot.log
```

Real-time log monitoring (PowerShell):
```powershell
Get-Content logs\exness_bot.log -Wait -Tail 50
```

### Health check
- MT5 terminal should show "Connected" in the status bar
- Bot logs should contain cycle entries every 5 seconds

---

## FAQ & Troubleshooting

### MT5 initialize failed
- Make sure the MT5 terminal is **running and logged in**
- Verify you are using Python 3.12 (not 3.13)
- Leave `terminal_path` empty in config.yaml for auto-detection

### Login failed
- Check `.env` — server, login, password
- Make sure the account is active in Exness Personal Area
- Verify account type — must be MT5 (not MT4)

### "MetaTrader5 package not installed"
```cmd
venv\Scripts\activate.bat
pip install MetaTrader5
```

### Bot is not trading
- Check that the current time is within an active killzone: London 07-10, NY 12-16, Late NY 20-23 UTC
- Verify that `XAUUSD` and `USOIL` are available on your account
- Check the log for `confluence score` and `entry conditions` entries

### How to stop the bot
- Press `Ctrl+C` in the CMD window
- Or stop the scheduled Task / Windows Service
