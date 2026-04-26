# GoldasT Bot v2.1

Automated **FVG/IFVG trading bot** for [Bitunix Futures](https://www.bitunix.com/register?vipCode=DiIs). Detects Fair Value Gaps on 15-minute candles, enters on optimal zone fill, and manages positions with ATR-adaptive trailing TP/SL — all running as a single Docker container.

---

## Table of Contents

- [Trading Strategy](#trading-strategy)
  - [Entry Signals](#entry-signals)
  - [TP/SL & Position Management](#tpsl--position-management)
  - [Risk Management](#risk-management)
- [Architecture](#architecture)
  - [Module Reference](#module-reference)
  - [Trade Flow](#trade-flow)
- [Symbol Rotation](#symbol-rotation)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Project Structure](#project-structure)

---

## Trading Strategy

### Entry Signals

The bot runs three independent entry pipelines, all gated by **killzone** (default 05:00–21:00 UTC):

#### 1. FVG Entry (Primary)

Detects **Fair Value Gaps** — 3-candle price imbalances on 15m timeframe. Entry triggers when price retraces 40–80% into the gap zone.

**Filters applied before entry:**
- FVG strength ≥ 0.85 (gap size + volume ratio + trend alignment)
- BOS (Break of Structure) confirmation via `market_structure.py`
- HTF (1h) FVG confluence — zone must align with higher timeframe
- Combined trend score: SHORT ≥ 0.75, LONG ≥ 0.45
- Killzone active
- Cooldowns (per-symbol, global, loss-based)
- Max concurrent positions / same-direction limits
- Correlation guard (blocks correlated pairs in same direction)

#### 2. EMA Cross Entry

9/21 EMA crossover on 15m, confirmed by:
- Trend alignment (combined score threshold)
- Killzone filter
- 2.0 ATR stop-loss, R:R from config

#### 3. Mean Reversion Entry

Bollinger Band touch + RSI extreme in ranging markets (ADX < threshold):
- LONG: price ≤ BB lower + RSI ≤ 30
- SHORT: price ≥ BB upper + RSI ≥ 70
- ADX < 25 (ranging market only)
- Killzone filter (added v2.1 — was missing, caused API3 bug)
- 1.0% SL, 1.5% TP

### TP/SL & Position Management

- **ATR-based TP/SL**: SL at structural invalidation (zone edge + buffer), TP at configurable R:R
- **Breakeven**: Moves SL to entry + lock after price reaches 1.0R
- **Dynamic ATR trailing**: After BE, SL trails using ATR-scaled distance (clamp 0.5)
- **Min TP distance**: 0.60% — prevents micro-TPs
- **Min TP value**: $1.00 — skips entries with TP < $1
- **Exchange-side TP/SL**: Orders persist even if bot goes offline
- **Position sync**: On startup, syncs all exchange positions, restores TP/SL from exchange or recalculates via ATR

### Risk Management

- **Position sizing**: Risk-based (`balance × risk% / SL_distance`)
- **Leverage**: Fixed 5× (configurable)
- **Max concurrent positions**: 7 (configurable)
- **Max same direction**: 3
- **Daily loss limit**: 5% of balance → stops trading
- **Per-symbol loss ban**: 3 consecutive losses → 24h ban (core: 1h)
- **Global loss pause**: 4 consecutive losses → 30min pause
- **Direction nerfing**: Blocks a direction if last 5 trades net PnL < -$2
- **Trial symbols**: 50% position size, 1 loss = immediate removal
- **Correlation guard**: Blocks correlated pairs (e.g., BTC+ETH) in same direction

---

## Architecture

### Module Reference

| Module | Responsibility |
|--------|---------------|
| `bot.py` | Main orchestrator — startup, event routing, scheduling |
| `strategy_engine.py` | All entry logic (FVG, EMA cross, Mean Reversion), trend scoring |
| `fvg_detector.py` | FVG/IFVG detection, strength scoring, sliding window |
| `market_structure.py` | BOS (Break of Structure) detection, swing highs/lows |
| `tpsl_calculator.py` | ATR calculation, TP/SL computation, trailing logic |
| `position_manager.py` | Position sync, trailing updates, breakeven, orphan detection |
| `position_sizer.py` | Risk-based sizing with constraints and jitter |
| `symbol_rotation.py` | FVG scanner, scoring, rotation logic, proven/trial management |
| `signal_tracker.py` | Zone hit tracking for rotation ban decisions |
| `trade_history.py` | PnL tracking, WR, streak analysis, archiving |
| `exchange_adapter.py` | Async Bitunix REST API wrapper |
| `bitunix_client.py` | Low-level HTTP + HMAC signing, rate limiting |
| `bitunix_ws.py` | WebSocket connection (public + private) |
| `websocket_handler.py` | WS message parsing, candle buffers, event dispatch |
| `order_state_machine.py` | Order lifecycle FSM |
| `telegram_bot.py` | Telegram notifications (entries, exits, errors) |
| `config.py` | Config loader, dataclasses, validation |
| `models.py` | Data models: Candle, FVG, SymbolState, BotState |
| `error_recovery.py` | Circuit breaker + retry logic |

### Trade Flow

```
WebSocket 15m Klines
       │
       ▼
  Candle Close ──► FVG Detector ──► Strategy Engine
       │                                │
       ├── EMA Cross Check              ├── Trend Score (15m+1h)
       ├── Mean Reversion Check         ├── BOS Confirmation
       │                                ├── HTF Confluence
       ▼                                ├── Killzone Gate
  Live Ticks ──► FVG Entry Check        ▼
                                   Position Sizer
                                        │
                                        ▼
                                   Exchange Order
                                        │
                                        ▼
                                   Position Manager
                                   ├── TP/SL Set
                                   ├── Breakeven
                                   ├── ATR Trailing
                                   └── Sync & Orphan Detection
```

---

## Symbol Rotation

Automated symbol management with quality scoring (0–100):

- **Core symbols**: Always active (BTCUSDT, ETHUSDT, XRPUSDT, LINKUSDT, NEARUSDT)
- **Trial symbols**: Added by scanner, 50% size, removed after 1 loss
- **Proven symbols**: Promoted after +$2 PnL, 3+ trades, 50%+ WR
- **Blacklist**: Permanently banned symbols (bad history, not tradeable)

**Scoring weights:**
- Proximity (40pts) — is price near an actionable FVG zone?
- Quality (30pts) — signal rate, bounce rate, fill rate, avg R
- Filter (20pts) — BOS confirmed, HTF confluence, zone approach rate
- Liquidity (10pts) — ATR, volume, volatility structure

**Safety:**
- Exchange position check before removing symbols (v2.1)
- PnL-based bans ($0.50 threshold, 72h duration)
- Cooldown after removal (6h)
- Min 24h volume: $10M

---

## Configuration

All configuration in `config.yaml`. Environment variables expanded via `${VAR_NAME}`.

### Key Settings

```yaml
core_symbols:           # Always-active symbols
  - BTCUSDT
  - ETHUSDT
  - XRPUSDT
  - LINKUSDT
  - NEARUSDT

fvg:
  timeframe: "15m"
  entry_zone_min: 0.40        # Enter after 40% fill
  entry_zone_max: 0.80        # Don't enter past 80%
  min_strength: 0.85          # Only high-quality FVGs

tpsl:
  force_close_at_r: 3.0       # 3:1 R:R target
  sl_buffer_atr_mult: 0.50    # SL buffer beyond zone edge
  sl_min_atr_mult: 1.2        # Min SL distance floor
  be_trigger_r: 1.0           # Move to BE at 1R
  be_lock_r: 0.15             # Lock 0.15R profit at BE
  dynamic_atr_trailing: true  # ATR-based trailing after BE

session:
  enabled: true
  start_hour: 5               # Killzone start (UTC)
  end_hour: 21                # Killzone end (UTC)

rotation:
  enabled: true
  interval_hours: 4.0
  max_symbols: 6
  min_score: 55.0
  min_24h_volume: 10000000    # $10M minimum
  trial_max_losing_trades: 1  # 1 loss = removed
  trial_size_multiplier: 0.5  # 50% size for trials
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BITUNIX_API_KEY` | Yes | Bitunix API key |
| `BITUNIX_SECRET` | Yes | Bitunix API secret |
| `TELEGRAM_BOT_TOKEN` | No | Telegram notifications |
| `TELEGRAM_USER_ID` | No | Telegram chat ID |

---

## Deployment

### Quick Start

```bash
cd goldast-bot

# 1. Create .env
cat > .env << 'EOF'
BITUNIX_API_KEY=your_key
BITUNIX_SECRET=your_secret
EOF

# 2. Build and start
docker compose up -d --build

# 3. Watch logs
docker compose logs -f
```

### Operations

```bash
docker compose logs -f              # Live logs
docker compose logs --since 5m      # Recent logs
docker compose down                  # Stop
docker compose up -d --build         # Rebuild + restart
docker compose ps                    # Status
```

### Runtime

- **Python 3.11** (slim Docker image)
- **Memory:** ~128MB typical
- **Restart policy:** `unless-stopped`
- **Volumes:** `config.yaml`, `data/`, `logs/`

---

## Project Structure

```
goldast-bot/
├── config.yaml            # All bot configuration
├── config.yaml.example    # Template config
├── docker-compose.yml     # Container orchestration
├── Dockerfile             # Multi-stage build
├── requirements.txt       # Python dependencies
├── main.py                # Entry point
├── .env                   # API credentials (not committed)
├── src/
│   ├── bot.py                 # Main orchestrator
│   ├── strategy_engine.py     # Entry logic (FVG/EMA/MR), trend scoring
│   ├── fvg_detector.py        # FVG/IFVG detection + scoring
│   ├── market_structure.py    # BOS detection
│   ├── tpsl_calculator.py     # ATR-based TP/SL + trailing
│   ├── position_manager.py    # Position sync, trailing, orphans
│   ├── position_sizer.py      # Risk-based position sizing
│   ├── symbol_rotation.py     # Scanner, scoring, rotation
│   ├── signal_tracker.py      # Zone hit tracking
│   ├── trade_history.py       # PnL / WR tracking
│   ├── exchange_adapter.py    # Bitunix REST API
│   ├── bitunix_client.py      # HTTP + HMAC signing
│   ├── bitunix_ws.py          # WebSocket connections
│   ├── websocket_handler.py   # WS message parsing
│   ├── order_state_machine.py # Order lifecycle FSM
│   ├── telegram_bot.py        # Telegram notifications
│   ├── config.py              # Config loader
│   ├── models.py              # Data models
│   └── error_recovery.py      # Circuit breaker
├── data/                  # Persistent data (trade history, rotation state)
├── logs/                  # Log files
├── scripts/               # Utility scripts
└── tests/                 # Test files
```
