# GoldasT Bot v2

Automated **FVG/IFVG futures trading bot** for [Bitunix](https://www.bitunix.com/register?vipCode=DiIs) exchange.

Detects Fair Value Gaps on 15-minute candles, enters on optimal zone fill with ICT killzone session filtering, and manages positions via a 3-phase trailing TP/SL system — all running as a single Docker container.

## Status

**Production-ready.** The bot is actively running and being optimized.

## Features

- **FVG & IFVG detection** — sliding window scanner across configurable lookback period
- **ICT Killzone session filter** — trades only during high-probability windows (Asian, London, NY, Late NY)
- **EMA trend filter** — weighted 15m/1h trend scoring with BTC market leader alignment
- **3-phase trailing system** — initial SL → breakeven lock → runner mode with sliding TP
- **ATR-adaptive TP/SL** — dynamic stop placement based on volatility
- **Smart symbol rotation** — automatic symbol selection based on FVG quality, trend alignment, and volume
- **Telegram integration** — real-time trade notifications and bot control
- **Docker deployment** — single container, multi-stage build, minimal footprint

## Quick Start

```bash
cd goldast-bot
cp .env.example .env        # Add your API keys
cp config.yaml.example config.yaml  # Configure strategy (or request config — see below)
docker compose up -d --build
docker logs -f goldast-bot
```

## Getting the Config

The production `config.yaml` is not included in this repository.

**To get a working configuration, open a thread in [Discussions](../../discussions).**

## Tech Stack

- Python 3.11, asyncio
- Bitunix REST API + WebSocket (public + private)
- Docker multi-stage build
- Telegram Bot API

## Project Structure

```
goldast-bot/
├── main.py                  # Entry point
├── config.yaml.example      # Example configuration
├── docker-compose.yml       # Docker deployment
├── Dockerfile               # Multi-stage production build
├── requirements.txt         # Python dependencies
├── src/
│   ├── bot.py               # Main bot orchestrator
│   ├── strategy_engine.py   # FVG strategy + entry filters
│   ├── fvg_detector.py      # FVG/IFVG detection engine
│   ├── tpsl_calculator.py   # ATR-based TP/SL + trailing
│   ├── symbol_rotation.py   # Auto symbol rotation
│   ├── config.py            # Configuration loader
│   ├── bitunix_client.py    # REST API client
│   ├── bitunix_ws.py        # WebSocket client
│   ├── exchange_adapter.py  # Exchange abstraction
│   ├── position_manager.py  # Position tracking
│   ├── position_sizer.py    # Risk-based position sizing
│   ├── signal_tracker.py    # Zone hit / signal tracking
│   ├── telegram_bot.py      # Telegram notifications
│   └── trade_history.py     # Trade logging
├── scripts/
│   ├── symbol_scanner.py    # CLI symbol analysis tool
│   └── filter_simulation.py # Filter backtesting
└── tests/
```

## License

Private. All rights reserved.
