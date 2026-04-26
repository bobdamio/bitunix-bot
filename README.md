# GoldasT Bot v2.1

Automated **FVG/IFVG futures trading bot** for [Bitunix](https://www.bitunix.com/register?vipCode=DiIs) exchange.

Three entry strategies (FVG retracement, EMA cross, Mean Reversion) on 15m candles, gated by ICT killzone session filter. Manages positions with ATR-adaptive trailing TP/SL, breakeven lock, and exchange-side orders. Includes automated symbol rotation with FVG quality scoring.

## Status

**Production.** Running on live account with 5 core symbols + rotator.

## Features

- **FVG/IFVG detection** — sliding window scanner with strength scoring
- **3 entry strategies** — FVG zone fill, 9/21 EMA cross, Bollinger Band mean reversion
- **Killzone filter** — all entries gated by configurable UTC session window
- **BOS + HTF confluence** — Break of Structure confirmation + 1h FVG alignment
- **EMA trend scoring** — weighted 15m/1h combined score with directional thresholds
- **ATR trailing TP/SL** — initial stop → breakeven lock → dynamic ATR runner
- **Symbol rotation** — automated scanner adds/removes symbols by FVG quality score (0–100)
- **Risk management** — per-symbol loss bans, direction nerfing, daily loss cap, correlation guard
- **Position sync** — orphan detection, exchange position reconciliation on restart
- **Telegram** — real-time trade notifications
- **Docker** — single container, config-as-volume

## Quick Start

```bash
cd goldast-bot
cp .env.example .env                  # Add BITUNIX_API_KEY + BITUNIX_SECRET
cp config.yaml.example config.yaml    # Or request config via Discussions
docker compose up -d --build
docker logs -f goldast-bot
```

## Getting the Config

The production `config.yaml` is not included in this repository.

**To get a working configuration, open a thread in [Discussions](../../discussions).**

## Tech Stack

- Python 3.11, asyncio
- Bitunix REST + WebSocket (public klines, private orders/positions)
- Docker multi-stage build
- Telegram Bot API

## Project Structure

```
goldast-bot/
├── main.py                    # Entry point
├── config.yaml                # All bot configuration
├── docker-compose.yml         # Container orchestration
├── Dockerfile                 # Multi-stage production build
├── src/
│   ├── bot.py                 # Main orchestrator
│   ├── strategy_engine.py     # Entry logic (FVG/EMA/MR), trend scoring
│   ├── fvg_detector.py        # FVG/IFVG detection + strength scoring
│   ├── market_structure.py    # BOS detection, swing highs/lows
│   ├── tpsl_calculator.py     # ATR-based TP/SL + trailing
│   ├── position_manager.py    # Position sync, trailing, orphan detection
│   ├── position_sizer.py      # Risk-based position sizing
│   ├── symbol_rotation.py     # Scanner, scoring, rotation logic
│   ├── signal_tracker.py      # Zone hit tracking
│   ├── trade_history.py       # PnL/WR tracking + archiving
│   ├── exchange_adapter.py    # Bitunix REST API wrapper
│   ├── bitunix_client.py      # HTTP client + HMAC signing
│   ├── bitunix_ws.py          # WebSocket connections
│   ├── websocket_handler.py   # WS message parsing, candle buffers
│   ├── order_state_machine.py # Order lifecycle FSM
│   ├── telegram_bot.py        # Telegram notifications
│   ├── config.py              # Config loader + dataclasses
│   ├── models.py              # Data models
│   └── error_recovery.py      # Circuit breaker + retries
├── scripts/                   # Analysis & utility scripts
├── data/                      # Persistent state (trade history, rotation)
├── logs/                      # Runtime logs
└── tests/                     # Test files
```

See [`goldast-bot/README.md`](goldast-bot/README.md) for full strategy documentation.

## License

Private. All rights reserved.
