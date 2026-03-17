# Exness Bot v1.0

**Automated forex/commodities trading bot for Exness MT5**

Strategy: **FVG/IFVG + Supply/Demand Zones + Multi-Timeframe Analysis (15m→5m→1m)**
Instruments: **XAUUSD** (Gold), **USOIL** (WTI Crude Oil)
Account type: **Hedging**

---

## Table of Contents

- [Strategy Overview](#strategy-overview)
- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Server Requirements](#server-requirements)
- [Quick Start](#quick-start)
- [Deployment Options](#deployment-options)
- [Configuration Reference](#configuration-reference)
- [Monitoring & Logs](#monitoring--logs)
- [FAQ & Troubleshooting](#faq--troubleshooting)

---

## Strategy Overview

### Multi-Timeframe Analysis (15m → 5m → 1m)

The bot analyzes three timeframes simultaneously to find high-probability trade setups:

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

The bot enters when **FVG/IFVG appears inside a fresh Supply/Demand zone**:

```
1.  15m Supply zone detected → bearish bias
2.  5m or 1m FVG (bearish) overlaps with that supply zone
3.  Price fills 25-85% of the FVG → optimal entry zone
4.  Confluence score ≥ 0.60 → SELL with SL above supply zone

Same logic reversed for demand + bullish FVG → BUY
```

### Entry Types

| Type | When | How |
|------|------|-----|
| **Market** | FVG + Zone confluence confirmed, price in entry zone | Direct market execution |
| **Buy Stop** | Price consolidating just below resistance | Placed ATR×0.3 above resistance breakout level |
| **Sell Stop** | Price consolidating just above support | Placed ATR×0.3 below support breakdown level |

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
├── main.py                    # Entry point (argparse → bot.main())
├── config.yaml                # All settings (MT5, strategy, risk)
├── config.yaml.example        # Template config
├── .env.example               # Environment vars template
├── requirements.txt           # Python dependencies
├── Dockerfile                 # Docker image (Wine + Xvfb + MT5)
├── docker-compose.yml         # Docker orchestration
│
├── src/
│   ├── __init__.py            # Package init, fmt_price()
│   ├── models.py              # Data models: Candle, FVG, Zone, Position, etc.
│   ├── config.py              # YAML config loader with env var resolution
│   │
│   ├── mt5_client.py          # MT5 API: connect, orders, positions, candles
│   │   └── connect/login/reconnect
│   │   └── place_market_order / place_pending_order
│   │   └── modify_position / close_position
│   │   └── get_candles / get_current_price
│   │
│   ├── fvg_detector.py        # FVG/IFVG detection (from GoldasT Bot)
│   │   └── detect_fvg() — last 3 candles
│   │   └── detect_fvg_sliding_window() — full buffer scan
│   │   └── check_entry_conditions() — fill zone / edge anticipation
│   │   └── _calculate_strength() — gap + volume + trend + impulse
│   │
│   ├── supply_demand.py       # Supply/Demand zone detector
│   │   └── detect_zones() → (supply_zones, demand_zones)
│   │   └── find_fvg_in_zone() — FVG↔Zone overlap check
│   │   └── _find_base() — consolidation detection
│   │   └── _calculate_zone_strength() — impulse + base + freshness
│   │
│   ├── market_structure.py    # BOS / Swing High/Low / Premium-Discount
│   │   └── warmup() / update()
│   │   └── is_bos_aligned() / is_bos_stable()
│   │   └── get_support_resistance()
│   │
│   ├── mtf_analyzer.py        # Multi-Timeframe Analyzer (15m→5m→1m)
│   │   └── analyze() — full MTF pipeline
│   │   └── _calculate_confluence_score()
│   │   └── _check_pending_order_setup() — Buy/Sell Stop
│   │
│   ├── tpsl_calculator.py     # TP/SL with ATR + zone anchoring
│   │   └── calculate() → TPSLLevels
│   │   └── calculate_atr()
│   │
│   ├── position_sizer.py      # Lot size calculator (risk-based)
│   │   └── calculate_lot_size()
│   │
│   ├── strategy_engine.py     # Main orchestrator
│   │   └── initialize() — connect, sync positions
│   │   └── run_cycle() — main loop iteration
│   │   └── _process_symbol() — analyze + trade
│   │   └── _manage_positions() — trailing SL
│   │   └── _update_trailing() — 3-phase trailing
│   │
│   └── bot.py                 # Bot lifecycle: logging, main loop, shutdown
│       └── ExnessBot.start() / stop()
│       └── setup_logging()
│       └── main()
│
├── scripts/
│   ├── setup.sh               # Full server setup (deps + venv + systemd)
│   ├── start.sh               # Start with Xvfb
│   └── status.sh              # Check bot status + recent logs
│
├── data/                      # Persistent data (trade history, etc.)
├── logs/                      # Log files (rotated, 10MB × 5 backups)
└── tests/                     # Unit tests
```

---

## Server Requirements

| Requirement | Minimum | This VPS |
|-------------|---------|----------|
| OS | Ubuntu 22.04+ | Ubuntu 24.04 ✅ |
| Python | 3.10+ | 3.12.3 ✅ |
| RAM | 512MB | 8GB ✅ |
| Disk | 1GB free | 93GB free ✅ |
| Docker | 20.0+ (if Docker deploy) | 29.2.1 ✅ |
| Network | Stable internet | VPS ✅ |

**Note:** MetaTrader5 Python package requires Wine on Linux (it wraps a Windows DLL). The `Xvfb` virtual display provides the headless X11 session MT5 needs.

---

## Quick Start

### Option A: Direct (venv)

```bash
cd /home/goldast/projects/bitunix-bot/exness-bot

# Run automated setup
bash scripts/setup.sh

# Set your MT5 password
echo 'MT5_PASSWORD=YOUR_PASSWORD_HERE' > .env

# Start
bash scripts/start.sh
```

### Option B: Docker

```bash
cd /home/goldast/projects/bitunix-bot/exness-bot

# Set credentials
echo 'MT5_PASSWORD=YOUR_PASSWORD_HERE' > .env

# Build and start
docker compose up -d

# View logs
docker compose logs -f
```

### Option C: Systemd (auto-start on reboot)

```bash
# First run setup.sh (creates the service file)
bash scripts/setup.sh

# Set password
echo 'MT5_PASSWORD=YOUR_PASSWORD_HERE' > .env

# Enable and start
sudo systemctl enable exness-bot
sudo systemctl start exness-bot

# Check status
sudo systemctl status exness-bot

# View logs
journalctl -u exness-bot -f
```

---

## Deployment Options

### 1. Docker Compose (recommended)

```bash
# Build image
docker compose build

# Start in background
docker compose up -d

# View live logs
docker compose logs -f

# Stop
docker compose down

# Restart
docker compose restart

# Rebuild after code changes
docker compose up -d --build
```

### 2. Systemd Service

The `scripts/setup.sh` creates a systemd service at `/etc/systemd/system/exness-bot.service`.

```bash
sudo systemctl start exness-bot      # Start
sudo systemctl stop exness-bot       # Stop
sudo systemctl restart exness-bot    # Restart
sudo systemctl status exness-bot     # Status
sudo systemctl enable exness-bot     # Auto-start on boot
journalctl -u exness-bot -f          # Live logs
```

### 3. Direct (development)

```bash
source venv/bin/activate
export MT5_PASSWORD="YOUR_PASSWORD_HERE"
python main.py
```

---

## Configuration Reference

### MT5 Connection (`mt5:`)

| Param | Value | Description |
|-------|-------|-------------|
| `server` | `Exness-MT5Trial15` | Exness MT5 server name |
| `login` | `260474980` | MT5 account number |
| `password` | `${MT5_PASSWORD}` | From .env file |
| `timeout` | `30000` | Connection timeout (ms) |

### Symbols (`symbols:`)

```yaml
symbols:
  - XAUUSD    # Gold vs USD
  - USOIL     # WTI Crude Oil
```

### FVG Settings (`fvg:`)

| Param | Default | Description |
|-------|---------|-------------|
| `entry_zone_min` | 0.25 | Min fill % to enter (25%) |
| `entry_zone_max` | 0.85 | Max fill % to enter (85%) |
| `min_strength` | 0.55 | Min FVG strength score |
| `min_gap_percent` | 0.0005 | Min gap size (0.05%) |
| `min_gap_atr_mult` | 0.3 | Min gap = 0.3×ATR |
| `max_active_fvgs` | 3 | Max FVGs tracked per symbol |
| `lookback_candles` | 50 | Candles to scan |
| `ifvg_threshold_pct` | 0.5 | IFVG violation threshold |

### Supply/Demand (`supply_demand:`)

| Param | Default | Description |
|-------|---------|-------------|
| `enabled` | true | Enable S/D zone detection |
| `lookback_candles` | 100 | Candles to scan for zones |
| `min_impulse_atr_mult` | 1.5 | Impulse must be ≥ 1.5×ATR |
| `max_base_candles` | 5 | Max consolidation candles |
| `zone_touch_invalidation` | 3 | Zone removed after 3 touches |
| `fresh_zone_bonus` | 0.20 | +20% strength for fresh zones |

### Multi-Timeframe (`mtf:`)

| Param | Default | Description |
|-------|---------|-------------|
| `enabled` | true | Enable MTF analysis |
| `htf_timeframe` | M15 | Higher timeframe |
| `mtf_timeframe` | M5 | Medium timeframe |
| `ltf_timeframe` | M1 | Entry timeframe |
| `htf_weight` | 0.50 | 15m weight in confluence |
| `mtf_weight` | 0.30 | 5m weight |
| `ltf_weight` | 0.20 | 1m weight |
| `min_confluence_score` | 0.60 | Min score to enter |

### TP/SL (`tpsl:`)

| Param | Default | Description |
|-------|---------|-------------|
| `sl_buffer_atr_mult` | 0.15 | SL noise buffer |
| `sl_min_atr_mult` | 0.5 | Min SL = 0.5×ATR |
| `sl_max_atr_mult` | 2.0 | Max SL = 2.0×ATR |
| `default_rr` | 2.0 | Default R:R ratio |
| `min_rr` | 1.0 | Min R:R (1:1) |
| `max_rr` | 3.0 | Max R:R (1:3) |
| `trailing_breakeven_at_r` | 1.0 | BE at 1R |
| `trailing_runner_at_r` | 2.0 | Runner at 2R |

### Position Sizing (`position:`)

| Param | Default | Description |
|-------|---------|-------------|
| `risk_percent` | 0.02 | 2% risk per trade |
| `min_lot` | 0.01 | Minimum lot size |
| `max_lot` | 1.0 | Maximum lot size |
| `max_positions` | 3 | Max simultaneous positions |
| `max_positions_per_symbol` | 2 | Max per symbol (hedging) |

### Sessions (`session:`)

| Killzone | UTC Hours | Description |
|----------|-----------|-------------|
| London | 07:00—10:00 | London session open |
| NY | 12:00—16:00 | New York session |
| Late NY | 20:00—23:00 | Late NY / Asian pre-open |

### Pending Orders (`pending_orders:`)

| Param | Default | Description |
|-------|---------|-------------|
| `enabled` | true | Enable Buy/Sell Stop orders |
| `buy_stop_offset_atr` | 0.3 | Buy Stop = resistance + 0.3×ATR |
| `sell_stop_offset_atr` | 0.3 | Sell Stop = support - 0.3×ATR |
| `expiration_candles` | 10 | Cancel after 10 candles |

---

## Monitoring & Logs

### Log File

```bash
# Live tail
tail -f logs/exness_bot.log

# Search for trades
grep "✅ Trade opened" logs/exness_bot.log
grep "🔒 Position closed" logs/exness_bot.log

# Search for signals
grep "🎯 Signal" logs/exness_bot.log
grep "🎯 MTF Setup" logs/exness_bot.log

# Search for zones
grep "🔴 Supply zones" logs/exness_bot.log
grep "🟢 Demand zones" logs/exness_bot.log

# Search for FVGs
grep "📈 Bullish FVG" logs/exness_bot.log
grep "📉 Bearish FVG" logs/exness_bot.log
```

### Status Check

```bash
bash scripts/status.sh
```

### Docker Monitoring

```bash
docker compose logs -f exness-bot          # Live logs
docker compose logs --tail 50 exness-bot   # Last 50 lines
docker stats exness-bot                    # CPU/RAM usage
docker compose ps                          # Container status
```

---

## FAQ & Troubleshooting

### MT5 won't connect on Linux

MT5 Python package requires Wine and a virtual X display:

```bash
# Install Wine
sudo dpkg --add-architecture i386
sudo apt-get update
sudo apt-get install wine64 wine32 xvfb

# Start virtual display
Xvfb :99 -screen 0 1024x768x16 &
export DISPLAY=:99
```

### "MetaTrader5 package not installed"

```bash
pip install MetaTrader5
```

If on Linux, MT5 must be installed via Wine first:
```bash
# Download MT5
wget https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe
# Install via Wine
wine mt5setup.exe
```

### Bot starts but no trades

1. Check killzone — bot only trades during London (07-10), NY (12-16), Late NY (20-23) UTC
2. Check `min_confluence_score` — default 0.60, lower to 0.50 for more signals
3. Check logs for zone/FVG detection: `grep "Supply\|Demand\|FVG" logs/exness_bot.log`
4. Verify MT5 symbol names match exactly (case-sensitive): `XAUUSD`, `USOIL`

### Symbol not found

Exness symbol names may differ. Check in MT5 Market Watch:
```python
import MetaTrader5 as mt5
mt5.initialize()
mt5.login(260474980, password="...", server="Exness-MT5Trial15")
symbols = mt5.symbols_get()
for s in symbols:
    if "XAU" in s.name or "OIL" in s.name:
        print(s.name)
```

### How to add more symbols

Edit `config.yaml`:
```yaml
symbols:
  - XAUUSD
  - USOIL
  - EURUSD     # add forex pairs
  - GBPUSD
```

### Bot keeps reconnecting

Check Exness server status and your internet connection. The bot auto-reconnects with a 30s wait on failure.

---

## Key Differences from GoldasT Bot

| Feature | GoldasT Bot (Bitunix) | Exness Bot |
|---------|----------------------|------------|
| Platform | Bitunix Futures (REST/WS API) | Exness MT5 (MetaTrader API) |
| Instruments | Crypto (ETHUSDT, XAUTUSDT) | Forex/Commodities (XAUUSD, USOIL) |
| Symbol rotation | ✅ Rotator + ban system | ❌ Removed — fixed symbol list |
| Coin banning | ✅ PnL-based auto-ban | ❌ Removed — not applicable |
| Supply/Demand | ❌ | ✅ Full zone detection |
| Multi-TF | 15m + 1h HTF | 15m → 5m → 1m cascade |
| Entry types | Market only | Market + Buy/Sell Stop |
| Account mode | One-way | Hedging (multiple positions) |
| Order types | REST API calls | MT5 `order_send()` |
