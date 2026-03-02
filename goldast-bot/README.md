# GoldasT Bot v2

Automated **FVG/IFVG trading bot** for [Bitunix Futures](https://www.bitunix.com/register?vipCode=DiIs). Detects Fair Value Gaps on 15-minute candles, enters on optimal zone fill, and manages positions with ATR-adaptive TP/SL — all running as a single Docker container.

---

## Table of Contents

- [Trading Logic](#trading-logic)
  - [FVG Detection](#fvg-detection)
  - [Entry Conditions](#entry-conditions)
  - [IFVG (Inverse FVG)](#ifvg-inverse-fvg)
  - [TP/SL Calculation (ATR-based)](#tpsl-calculation-atr-based)
  - [FVG Strength Scoring](#fvg-strength-scoring)
  - [Dynamic Leverage](#dynamic-leverage)
  - [Position Sizing](#position-sizing)
  - [Session Filter](#session-filter)
- [Architecture](#architecture)
  - [Trade Flow](#trade-flow)
  - [Startup Sequence](#startup-sequence)
  - [Module Reference](#module-reference)
- [Configuration Reference](#configuration-reference)
- [Deployment](#deployment)
  - [Prerequisites](#prerequisites)
  - [Quick Start](#quick-start)
  - [Environment Variables](#environment-variables)
  - [Operations](#operations)
- [Project Structure](#project-structure)

---

## Trading Logic

### FVG Detection

A **Fair Value Gap** is a 3-candle price imbalance where the wicks of candle 1 and candle 3 don't overlap, leaving an unfilled zone around candle 2.

```
Bullish FVG (LONG signal):          Bearish FVG (SHORT signal):

     ┌──┐                               │
     │c3│ ← c3.low                  c1   │  ← c1.low
     └──┘                          ┌──┐  │
  ~~~gap~~~ zone                   └──┘
     ┌──┐                          ~~~gap~~~ zone
     │c1│ ← c1.high                    ┌──┐
     └──┘                         c3   │  │ ← c3.high
                                       └──┘
```

**Detection rules:**

| Direction | Condition | Zone |
|-----------|-----------|------|
| Bullish (LONG) | `c3.low > c1.high` | bottom = `c1.high`, top = `c3.low` |
| Bearish (SHORT) | `c1.low > c3.high` | bottom = `c3.high`, top = `c1.low` |

**Minimum gap filter:** The gap must be at least `min_gap_percent` of the c2 close price (default 0.05%). This filters out noise-level gaps that don't represent real imbalances.

Detection runs on every closed 15-minute candle across all configured symbols simultaneously.

### Entry Conditions

After an FVG is detected, the bot waits for price to retrace into the gap zone. Entry triggers when price fills **50–80%** of the zone — the institutional "sweet spot" where:

- Below 50%: Price hasn't reached the zone yet — no confirmation of retracement
- 50–80%: Optimal fill — smart money is likely defending this zone
- Above 80%: Too deep — high risk the zone will break entirely (violation)

```
Bullish FVG entry example:

  $70,706 ─── top (c3.low)
              │
              │  0%   ← price above zone, no fill
              │ 50%   ← ENTRY ZONE START
              │ 60%   ← ✅ entry triggered here
              │ 80%   ← ENTRY ZONE END
              │100%   ← violated, zone broken
  $70,582 ─── bottom (c1.high)
```

Entry check runs on every live WebSocket tick (~500ms), not just on candle close, for precise fills.

### IFVG (Inverse FVG)

When an FVG zone is **violated** (price breaks through 100% of the zone), it can become an **Inverse FVG** — the zone is now expected to act as resistance/support in the opposite direction.

- Violated bullish FVG → bearish IFVG (SHORT)
- Violated bearish FVG → bullish IFVG (LONG)

IFVG strength is derated to 80% of the original FVG's strength score.

### TP/SL Calculation (ATR-based)

TP/SL levels are calculated using **14-period ATR** (Average True Range) anchored to the FVG structure, adapting to actual market volatility per symbol.

#### Stop Loss

The SL is placed at the **structural invalidation level** — the point where the FVG thesis is provably wrong:

```
For LONG:
  SL = FVG zone bottom − (ATR × 0.3 noise buffer)
  SL distance = max(entry − SL, ATR × 0.5 minimum floor)

For SHORT:
  SL = FVG zone top + (ATR × 0.3 noise buffer)
  SL distance = max(SL − entry, ATR × 0.5 minimum floor)
```

The noise buffer (0.3 ATR) prevents stop-hunts from wicking through the zone edge. The minimum floor (0.5 ATR) prevents the SL from being unrealistically tight.

#### Take Profit

TP is risk-multiple based with strength scaling:

```
TP distance = max(risk × 2.0 R:R, ATR × 1.0 floor)

Strength scaling:
  FVG strength ≥ 0.8 → TP × 1.5 (aggressive, high-conviction)
  FVG strength < 0.5 → TP × 0.8 (conservative, lower conviction)
```

#### Real-world examples (BTC 5m, ATR ≈ $160)

| FVG Gap | SL Distance | TP Distance | R:R |
|---------|-------------|-------------|-----|
| $35 (0.05%) | $80 (0.11%) | $160 (0.23%) | 2.0:1 |
| $140 (0.20%) | $118 (0.17%) | $236 (0.33%) | 2.0:1 |

Both TP and SL are set as **exchange-side orders** (`LAST_PRICE` trigger type) — they execute even if the bot goes offline.

### FVG Strength Scoring

Each detected FVG receives a strength score from 0.0 to 1.0, used for leverage and TP scaling:

| Factor | Weight | Max Score Condition |
|--------|--------|---------------------|
| **Gap size** | 40% | Gap ≥ 1% of price |
| **Volume ratio** | 30% | Gap candle volume ≥ 2× recent average |
| **Trend alignment** | 30% | FVG direction matches 20-SMA trend |

- Bullish FVG in uptrend (price > SMA20) → trend score = 1.0
- Counter-trend FVG → trend score = 0.3 (still valid, reduced confidence)

### Dynamic Leverage

Leverage scales with FVG strength:

| FVG Strength | Leverage | Rationale |
|-------------|----------|-----------|
| ≥ 0.8 (strong) | 10× | High conviction — large gap, trend-aligned, high volume |
| ≥ 0.5 (medium) | 7× | Standard setup |
| < 0.5 (weak) | 5× | Lower conviction — reduce exposure |

### Position Sizing

Position size is calculated from risk management, not from a fixed dollar amount:

```
position_usd = (balance × risk_percent) / sl_distance_percent
```

**Constraints applied in order:**
1. Cap at `max_position_usd` (default $15)
2. Cap at `balance × max_balance_percent` (default 150%)
3. Floor at `min_position_usd` (default $5)
4. Floor at symbol-specific minimum quantity (e.g. 0.0001 BTC)
5. ±5% random jitter (anti-detection)

### Session Filter

By default, trading is restricted to the **New York session** (09:30–16:00 ET, weekdays only). This is when institutional order flow creates the strongest FVG signals. Configurable via `session.enabled: false` to trade 24/7.

---

## Architecture

### Trade Flow

```
┌─────────────┐     ┌────────────┐     ┌─────────────┐     ┌──────────┐
│  WS Kline   │────►│ FVG Detect │────►│ Entry Check │────►│  Market  │
│  (500ms)    │     │ (on close) │     │ (live tick) │     │  Order   │
└─────────────┘     └────────────┘     └─────────────┘     └────┬─────┘
                                                                │
                     ┌────────────┐     ┌─────────────┐         │
                     │  Exchange  │◄────│  TP/SL Set  │◄────────┘
                     │  Manages   │     │ (ATR-based) │
                     └────────────┘     └─────────────┘
```

1. **WebSocket kline** pushes arrive every ~500ms per symbol
2. **On candle close**: FVG detection runs on the last 3 closed candles
3. **On live ticks**: If an active FVG exists, check entry conditions at current price
4. **Entry triggered**: Place market order → poll REST for position confirmation (5 retries) → calculate TP/SL from ATR + FVG structure → set TP/SL on exchange
5. **Position management**: Exchange-side TP/SL orders execute autonomously. Bot syncs state via WebSocket private channels.

### Startup Sequence

1. Load config, expand `${ENV_VARS}`
2. Initialize all components (exchange adapter, WS handler, detectors, calculators)
3. Backfill 100 historical candles per symbol via REST (immediate FVG detection capability)
4. Sync existing positions from exchange → calculate and set TP/SL using ATR fallback
5. Connect WebSocket (public klines + private positions/orders/tpsl)
6. Enter main loop

### Module Reference

| Module | Responsibility |
|--------|---------------|
| `bot.py` | Main orchestrator — startup, event routing, trade execution |
| `fvg_detector.py` | FVG/IFVG detection, strength scoring, entry condition checks |
| `tpsl_calculator.py` | ATR calculation, TP/SL level computation with structural anchoring |
| `position_sizer.py` | Risk-based position sizing with constraints and jitter |
| `exchange_adapter.py` | Async Bitunix REST API wrapper (orders, positions, balance, TP/SL) |
| `bitunix_client.py` | Low-level HTTP client with HMAC signing, rate limiting |
| `bitunix_ws.py` | Raw WebSocket connection management (public + private) |
| `websocket_handler.py` | WS message parsing, candle buffer management, event dispatching |
| `order_state_machine.py` | Order lifecycle FSM: IDLE → PENDING → FILL → TPSL → TRACKING |
| `models.py` | Data models: Candle, FVG, TradeSignal, Position, SymbolState, BotState |
| `config.py` | Config loader + validation |
| `error_recovery.py` | Circuit breaker + retry logic |

---

## Configuration Reference

All configuration lives in `config.yaml`. Environment variables are expanded from `${VAR_NAME}` syntax.

### API Credentials

```yaml
api:
  key: "${BITUNIX_API_KEY}"       # From .env file
  secret: "${BITUNIX_SECRET}"     # From .env file
  base_url: "https://fapi.bitunix.com"
```

### Symbols

```yaml
symbols:
  - BTCUSDT
  - ETHUSDT
  - SOLUSDT
```

Up to `max_concurrent_positions` symbols can have open positions simultaneously.

### FVG Detection

```yaml
fvg:
  timeframe: "5m"              # Candle interval
  entry_zone_min: 0.5          # Enter after 50% fill
  entry_zone_max: 0.8          # Don't enter past 80% fill
  min_gap_percent: 0.0005      # Minimum gap = 0.05% of price
  max_active_fvgs: 5           # Max tracked FVGs per symbol
  lookback_candles: 50         # Candle buffer size
```

**Tuning `min_gap_percent`:** Lower values (e.g., 0.0003) catch more signals but include weaker gaps. Higher values (e.g., 0.001) are more selective. For BTC at $70K, 0.0005 requires a $35 gap minimum.

### TP/SL (ATR-based)

```yaml
tpsl:
  sl_buffer_atr_mult: 0.3     # Noise buffer beyond zone edge (× ATR)
  sl_min_atr_mult: 0.5        # Minimum SL distance floor (× ATR)
  min_rr: 2.0                 # Minimum risk:reward ratio
  tp_min_atr_mult: 1.0        # TP distance floor (× ATR)
```

| Parameter | Effect of increasing | Effect of decreasing |
|-----------|---------------------|---------------------|
| `sl_buffer_atr_mult` | Wider SL, fewer stop-hunts, more risk per trade | Tighter SL, more stops hit, less risk |
| `sl_min_atr_mult` | Higher minimum SL floor, safer but wider stops | Allows very tight SLs on small FVGs |
| `min_rr` | Needs larger price moves to hit TP | More TPs hit but smaller wins |
| `tp_min_atr_mult` | TP never below this ATR multiple | Allows very tight TPs |

### Leverage

```yaml
leverage:
  min: 5                       # Weak FVG (strength < 0.5)
  max: 10                      # Strong FVG (strength ≥ 0.8)
  default: 7                   # Medium FVG
  strength_high_threshold: 0.8
  strength_medium_threshold: 0.5
```

### Position Sizing

```yaml
position:
  risk_percent: 0.01           # 1% of balance risked per trade
  min_position_usd: 5          # Minimum position size
  max_position_usd: 15         # Maximum position size
  max_balance_percent: 1.5     # Max 150% of balance (with leverage)
  min_quantities:              # Exchange minimum order sizes
    BTCUSDT: 0.0001
    ETHUSDT: 0.003
    SOLUSDT: 0.1
```

### Session Filter

```yaml
session:
  enabled: true                # Set false for 24/7 trading
  start: "09:30"               # NY session open
  end: "16:00"                 # NY session close
  timezone: "America/New_York"
  weekdays_only: true
```

### Cooldowns

```yaml
cooldowns:
  entry_cooldown_seconds: 300  # 5 min between entries on same symbol
  signal_cooldown_seconds: 30  # 30s between signals per symbol
  klines_ready_threshold: 50   # Min candles before first trade
  tpsl_placement_delay: 2      # Seconds to wait after fill before TP/SL
```

### Risk Management

```yaml
risk:
  max_daily_loss_percent: 5.0  # Stop trading if daily loss > 5%
  max_drawdown_percent: 10.0   # Emergency stop at 10% drawdown
  margin_warning_percent: 80.0 # Alert if margin usage > 80%
```

### Circuit Breaker

```yaml
circuit_breaker:
  failure_threshold: 5         # Consecutive failures before circuit opens
  recovery_timeout: 60         # Seconds to wait before retrying
```

### Anti-Detection

```yaml
randomization:
  enabled: true
  size_jitter_percent: 0.05    # ±5% random variation on position size
  timing_jitter_ms: 500        # ±500ms random delay on order placement
```

### WebSocket

```yaml
websocket:
  public_url: "wss://fapi.bitunix.com/public/"
  private_url: "wss://fapi.bitunix.com/private/"
  ping_interval: 20            # Keep-alive interval (seconds)
  reconnect_attempts: 5
  reconnect_interval: 5        # Seconds between reconnect attempts
```

### Logging

```yaml
logging:
  level: "INFO"                # DEBUG for detailed gap/entry logs
  file: "logs/goldast_bot.log"
  max_size_mb: 10
  backup_count: 5
```

### Dry Run

```yaml
dry_run: false                 # true = log signals without placing orders
```

---

## Deployment

### Prerequisites

- Docker and Docker Compose
- Bitunix Futures API key with trading permissions
- VPS or always-on machine (512MB RAM minimum)

### Quick Start

```bash
cd goldast-bot

# 1. Create environment file
cat > .env << 'EOF'
BITUNIX_API_KEY=your_api_key_here
BITUNIX_SECRET=your_api_secret_here
EOF

# 2. Review config
nano config.yaml

# 3. Start
docker compose up -d --build

# 4. Watch logs
docker compose logs -f
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BITUNIX_API_KEY` | Yes | Bitunix API key |
| `BITUNIX_SECRET` | Yes | Bitunix API secret |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token for notifications |
| `TELEGRAM_USER_ID` | No | Telegram chat ID for notifications |

### Operations

```bash
# View live logs
docker compose logs -f

# View recent logs (last 5 minutes)
docker compose logs --since 5m

# Stop bot
docker compose down

# Rebuild after code changes
docker compose up -d --build

# Restart without rebuild (config change only)
docker compose restart

# Check container status
docker compose ps

# Shell into container
docker compose exec goldast-bot sh
```

### Log Format

```
09/02 15:02:00 [INFO] src.bot - 📈 bal=$90.86 | pos=[SOLUSDT LONG @88.10] | fvg=[BTCUSDT LONG 70582-70705] | prices={...} | trades=0
09/02 15:02:00 [INFO] src.fvg_detector - 📈 Bullish FVG detected: BTCUSDT zone=[$70,582 - $70,706] gap=0.175% strength=0.67
09/02 15:02:00 [INFO] src.tpsl_calculator - 📊 TP/SL [BTCUSDT LONG]: Entry=$70,650  SL=$70,530 (0.17%)  TP=$70,890 (0.34%)  R:R=2.00:1  ATR=$160
```

Status line prints every 30 seconds showing balance, open positions, active FVGs, and current prices.

---

## Project Structure

```
goldast-bot/
├── config.yaml          # All bot configuration
├── docker-compose.yml   # Container orchestration
├── Dockerfile           # Multi-stage build (python:3.11-slim)
├── requirements.txt     # Python dependencies
├── main.py              # Entry point
├── .env                 # API credentials (not committed)
├── src/
│   ├── bot.py               # Main orchestrator (~710 lines)
│   ├── fvg_detector.py      # FVG/IFVG detection + strength scoring
│   ├── tpsl_calculator.py   # ATR-based TP/SL calculator
│   ├── position_sizer.py    # Risk-based position sizing
│   ├── exchange_adapter.py  # Bitunix REST API wrapper
│   ├── bitunix_client.py    # Low-level HTTP + HMAC signing
│   ├── bitunix_ws.py        # WebSocket connection manager
│   ├── websocket_handler.py # WS message parsing + candle buffers
│   ├── order_state_machine.py # Order lifecycle FSM
│   ├── models.py            # Data models and enums
│   ├── config.py            # Config loader + validation
│   └── error_recovery.py    # Circuit breaker + retry logic
├── data/                # Persistent data (volume-mounted)
└── logs/                # Log files (volume-mounted)
```

### Dependencies

- `websockets` — WebSocket client
- `aiohttp` — Async HTTP for REST API
- `pyyaml` — Configuration parsing
- `python-dotenv` — Environment variable loading

### Runtime

- **Python 3.11** (slim Docker image)
- **Memory:** ~128MB typical, 512MB limit
- **Network:** WebSocket (persistent) + REST API calls
- **Container:** Runs as non-root `goldast` user
- **Restart policy:** `unless-stopped`
