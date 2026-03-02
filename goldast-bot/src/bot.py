"""
GoldasT Bot v2 - Main Bot Orchestrator
Coordinates all components for the FVG/IFVG trading strategy.

Delegates to:
    - StrategyEngine: FVG detection, entry logic, trade execution
    - PositionManager: Position state, WS callbacks, periodic sync
    - TelegramBotController: Telegram UI for monitoring and control
"""

import asyncio
import logging
import signal
from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from .config import Config, load_config
from .models import SymbolState, BotState
from . import fmt_price
from .fvg_detector import FVGDetector
from .tpsl_calculator import TPSLCalculator
from .position_sizer import PositionSizer
from .order_state_machine import OrderManager
from .exchange_adapter import ExchangeAdapter
from .websocket_handler import WebSocketHandler
from .error_recovery import ResilientExecutor
from .strategy_engine import StrategyEngine
from .position_manager import PositionManager
from .trade_history import TradeHistory
from .telegram_bot import TelegramBotController, TelegramConfig as TgConfig
from .symbol_rotation import SymbolRotation
from .signal_tracker import SignalTracker


logger = logging.getLogger(__name__)


class GoldastBot:
    """
    Main bot orchestrator — thin coordination layer.

    Startup:
        1. Load config → init components → backfill candles
        2. Sync existing positions (PositionManager)
        3. Connect WS → register callbacks
        4. Enter main loop (periodic tasks + shutdown wait)

    Runtime:
        - StrategyEngine handles kline callbacks (FVG detection + entry)
        - PositionManager handles position/tpsl/order callbacks
        - Main loop runs periodic balance + position sync
        - Telegram bot sends notifications and accepts commands
    """

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.config: Optional[Config] = None

        # Components (initialized in start())
        self.exchange: Optional[ExchangeAdapter] = None
        self.ws_handler: Optional[WebSocketHandler] = None
        self.order_manager: Optional[OrderManager] = None
        self.executor: Optional[ResilientExecutor] = None

        # Delegated modules
        self.strategy: Optional[StrategyEngine] = None
        self.positions: Optional[PositionManager] = None

        # Telegram bot
        self.telegram: Optional[TelegramBotController] = None

        # Symbol rotation (daily auto-update)
        self.rotation: Optional[SymbolRotation] = None

        # Signal activity tracker (pipeline ban logic)
        self.signal_tracker: Optional[SignalTracker] = None

        # Shared state
        self.state = BotState()
        self.symbol_states: Dict[str, SymbolState] = {}

        # Control
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._periodic_tick = 0

    # ==================== Lifecycle ====================

    async def start(self) -> None:
        """Start the bot."""
        logger.info("🚀 Starting GoldasT Bot v2...")

        self.config = load_config(self.config_path)
        self._setup_logging()
        core = getattr(self.config, 'core_symbols', [])
        blacklist = getattr(self.config, 'blacklist', [])
        rotation = [s for s in self.config.symbols if s not in core]
        logger.info(f"Trading {len(self.config.symbols)} symbols: core={core}, rotation={rotation}")
        if blacklist:
            logger.info(f"🚫 Blacklisted symbols: {blacklist}")

        await self._init_components()

        # Initialize symbol rotation & fetch precision from exchange
        self.rotation = SymbolRotation(self.exchange, self.config)
        self.rotation._config_path = self.config_path
        await self.rotation.fetch_all_symbol_info()
        # Update precision dicts from exchange data
        from . import exchange_adapter
        for sym in self.config.symbols:
            price_prec, qty_prec = self.rotation.get_precision(sym)
            exchange_adapter.PRICE_PRECISION.setdefault(sym, price_prec)
            exchange_adapter.QTY_PRECISION.setdefault(sym, qty_prec)

        await self._backfill_historical_candles()
        await self.positions.sync_positions_from_exchange()
        await self.strategy.refresh_htf_trends()
        await self._connect()

        self.state.is_running = True
        self.state.start_time = datetime.now()
        self._running = True
        self._setup_signals()

        logger.info("✅ Bot started successfully")
        await self._run_loop()

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        logger.info("🛑 Stopping GoldasT Bot...")
        self._running = False
        self._shutdown_event.set()
        self.state.is_running = False

        if self.ws_handler:
            await self.ws_handler.disconnect()
        if self.exchange:
            await self.exchange.close()
        if self.telegram:
            await self.telegram.stop()

        logger.info("✅ Bot stopped")

    # ==================== Init ====================

    def _setup_logging(self) -> None:
        """Configure logging with rotating file handler + console output."""
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        log_cfg = self.config.logging
        log_level = getattr(logging, log_cfg.level.upper(), logging.INFO)
        log_fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
            datefmt="%d/%m %H:%M:%S",
        )

        # Root logger
        root = logging.getLogger()
        root.setLevel(log_level)

        # Remove default handlers (basicConfig may have added one)
        for h in root.handlers[:]:
            root.removeHandler(h)

        # Console handler (stdout — captured by Docker json-file driver)
        console = logging.StreamHandler()
        console.setLevel(log_level)
        console.setFormatter(log_fmt)
        root.addHandler(console)

        # Rotating file handler — auto-rotates when file exceeds max_size_mb
        log_file = Path(log_cfg.file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = log_cfg.max_size_mb * 1024 * 1024
        file_handler = RotatingFileHandler(
            str(log_file),
            maxBytes=max_bytes,
            backupCount=log_cfg.backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(log_fmt)
        root.addHandler(file_handler)

        logger.info(
            f"📝 Logging: {log_cfg.level} → {log_file} "
            f"(max {log_cfg.max_size_mb}MB × {log_cfg.backup_count} backups)"
        )

        # Suppress httpx INFO logs (they leak the Telegram bot token in URLs)
        logging.getLogger("httpx").setLevel(logging.WARNING)

    async def _init_components(self) -> None:
        """Initialize all trading components."""
        # Exchange adapter
        self.exchange = ExchangeAdapter(
            self.config.api,
            sl_correction_pct=self.config.tpsl.sl_correction_pct,
        )

        # WebSocket handler
        self.ws_handler = WebSocketHandler(
            config=self.config.api,
            symbols=self.config.symbols,
            kline_interval=self.config.fvg.timeframe.replace("m", "min"),
        )

        # Strategy components
        fvg_detector = FVGDetector(self.config.fvg, self.config.leverage)
        tpsl_calculator = TPSLCalculator(self.config.tpsl)
        position_sizer = PositionSizer(
            config=self.config.position,
            randomization_config=self.config.randomization,
        )

        # Order manager
        self.order_manager = OrderManager(
            place_order_fn=self._place_order,
            set_tpsl_fn=self._set_tpsl,
            cancel_order_fn=self._cancel_order,
            max_concurrent_orders=self.config.multi_symbol.max_concurrent_positions,
        )

        # Resilient executor
        self.executor = ResilientExecutor("order_executor")

        # Initialize symbol states
        for symbol in self.config.symbols:
            self.symbol_states[symbol] = SymbolState(symbol=symbol)

        # Strategy engine (FVG detection + trade execution)
        self.strategy = StrategyEngine(
            config=self.config,
            exchange=self.exchange,
            ws_handler=self.ws_handler,
            fvg_detector=fvg_detector,
            tpsl_calculator=tpsl_calculator,
            position_sizer=position_sizer,
            order_manager=self.order_manager,
            executor=self.executor,
            state=self.state,
            symbol_states=self.symbol_states,
            signal_tracker=self.signal_tracker,
        )

        # Trade history (persistent storage)
        self.trade_history = TradeHistory()

        # Signal tracker — pipeline rotation
        self.signal_tracker = SignalTracker(data_dir="data")
        # Activate initial symbols so timers start from boot
        for sym in self.config.symbols:
            self.signal_tracker.activate(sym)

        # Position manager (state sync + WS callbacks)
        self.positions = PositionManager(
            exchange=self.exchange,
            ws_handler=self.ws_handler,
            tpsl_calculator=tpsl_calculator,
            order_manager=self.order_manager,
            state=self.state,
            symbol_states=self.symbol_states,
            trade_history=self.trade_history,
        )

        # Wire back-reference for loss cooldown tracking
        self.positions.set_strategy(self.strategy)

        # Initialize Telegram bot if enabled
        await self._init_telegram()

        # Wire Telegram to strategy engine and position manager
        if self.telegram:
            self.strategy.set_telegram(self.telegram)
            self.positions.set_telegram(self.telegram)

        # Track current trading day for daily PnL reset (NY timezone)
        self._ny_tz = ZoneInfo("America/New_York")
        self._current_trading_day = datetime.now(self._ny_tz).date()

        logger.debug("Components initialized")

    async def _init_telegram(self) -> None:
        """Initialize Telegram bot if enabled in config."""
        tg_config = self.config.telegram
        
        if not tg_config.enabled:
            logger.info("ℹ️ Telegram bot disabled")
            return
        
        if not tg_config.bot_token:
            logger.warning("⚠️ Telegram enabled but no bot token found")
            return
        
        try:
            # Convert allowed_users to list of ints
            allowed_users = []
            for user_id in tg_config.allowed_users:
                try:
                    allowed_users.append(int(user_id))
                except (ValueError, TypeError):
                    logger.warning(f"Invalid user ID in config: {user_id}")
            
            if not allowed_users:
                logger.warning("⚠️ Telegram enabled but no allowed users configured")
                return
            
            # Create Telegram config
            telegram_config = TgConfig(
                token=tg_config.bot_token,
                allowed_users=allowed_users,
                notifications_enabled=tg_config.notifications_enabled,
            )
            
            # Initialize Telegram bot controller
            self.telegram = TelegramBotController(telegram_config, bot=self)
            await self.telegram.initialize()
            
            # Start Telegram bot in background
            asyncio.create_task(self.telegram.run())
            
            logger.info("✅ Telegram bot initialized and running")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Telegram bot: {e}")

    async def _backfill_historical_candles(self) -> None:
        """Backfill candle data via REST before WS starts."""
        logger.info("🔄 Backfilling historical candles for all symbols...")
        for symbol in self.config.symbols:
            try:
                candles = await self.exchange.get_historical_candles(
                    symbol, limit=100, interval=self.config.fvg.timeframe,
                )
                for candle in candles:
                    self.ws_handler.add_candle(symbol, candle)
                logger.info(f"✅ Backfilled {len(candles)} candles for {symbol}")
            except Exception as e:
                logger.error(f"Failed to backfill candles for {symbol}: {e}")

        # Scan backfilled candles for FVGs so we don't wait 15min after restart
        logger.info("🔍 Scanning backfilled candles for FVGs...")
        for symbol in self.config.symbols:
            try:
                # Compute RSI from backfilled data
                buf = self.ws_handler.get_candle_buffer(symbol)
                if buf and len(buf) >= 15:
                    closes = [c.close for c in buf[-30:]]
                    self.strategy._rsi_cache[symbol] = self.strategy._rsi(closes, 14)

                # Detect FVG from last 3 candles
                await self.strategy._detect_fvg_on_close(symbol)
            except Exception as e:
                logger.error(f"Failed startup FVG scan for {symbol}: {e}")

    async def _backfill_symbol(self, symbol: str) -> None:
        """Backfill candle data for a single symbol (used after rotation adds new symbols)."""
        try:
            candles = await self.exchange.get_historical_candles(
                symbol, limit=100, interval=self.config.fvg.timeframe,
            )
            for candle in candles:
                self.ws_handler.add_candle(symbol, candle)
            logger.info(f"✅ Backfilled {len(candles)} candles for {symbol} (rotation)")
            # Detect FVG from backfilled data
            await self.strategy._detect_fvg_on_close(symbol)
        except Exception as e:
            logger.error(f"Failed to backfill {symbol}: {e}")

    async def _connect(self) -> None:
        """Connect to exchange and WebSocket."""
        balance = await self.exchange.get_balance()
        if balance is not None:
            self.state.balance = balance.available
            logger.info(f"💰 Account balance: ${balance.available:.2f}")
        else:
            logger.warning("⚠️ Could not fetch initial balance")

        connected = await self.ws_handler.connect()
        if not connected:
            raise RuntimeError("Failed to connect WebSocket")

        # Route callbacks to strategy engine and position manager
        self.ws_handler.on_kline(self.strategy.on_kline)
        self.ws_handler.on_position(self.positions.on_position)
        self.ws_handler.on_tpsl(self.positions.on_tpsl)
        self.ws_handler.on_order(self.positions.on_order)

        logger.debug("WebSocket connected and callbacks registered")

    def _setup_signals(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

    # ==================== Main Loop ====================

    async def _run_loop(self) -> None:
        """Main event loop — periodic tasks + shutdown wait."""
        logger.info("📊 Entering main loop (WS kline streaming)...")
        periodic_interval = 30

        while self._running:
            try:
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=float(periodic_interval),
                    )
                    break
                except asyncio.TimeoutError:
                    pass

                await self._periodic_tasks()
            except Exception as e:
                logger.error(f"Main loop error: {e}")
                await asyncio.sleep(5)

    async def _periodic_tasks(self) -> None:
        """Run periodic maintenance tasks."""
        self._periodic_tick += 1

        # Daily PnL reset at midnight NY time
        today = datetime.now(self._ny_tz).date()
        if today != self._current_trading_day:
            old_pnl = self.state.daily_pnl
            self.state.daily_pnl = 0.0
            self._current_trading_day = today
            logger.info(f"🔄 New trading day — daily PnL reset (yesterday: ${old_pnl:.2f})")

        balance = await self.exchange.get_balance()
        if balance is not None:
            self.state.balance = balance.available

        # Every 30s: manage trailing SL + partial TP
        await self.strategy.manage_open_positions()

        # Every ~60s: reconcile positions with exchange
        if self._periodic_tick % 2 == 0:
            await self.positions.periodic_position_sync()

        # Every ~60s: check WS health (detect dead symbols, re-subscribe)
        if self._periodic_tick % 2 == 0:
            await self.ws_handler.check_ws_health()

        # Refresh 1h + 15m trend data
        # On 15m TF: refresh every ~15min aligns with candle closes
        if self._periodic_tick % 30 == 0:  # Every ~15min (30 ticks × 30s)
            await self.strategy.refresh_htf_trends()

        # Every ~1h (120 ticks × 30s): check if daily symbol rotation is due
        if self._periodic_tick % 120 == 0 and self.rotation:
            try:
                new_symbols = await self.rotation.maybe_rotate(
                    symbol_states=self.symbol_states,
                    ws_handler=self.ws_handler,
                    bot_state=self.state,
                    trade_history=self.trade_history,
                    signal_tracker=self.signal_tracker,
                )
                if new_symbols:
                    # Refresh trends for any newly added symbols
                    await self.strategy.refresh_htf_trends()
                    # Backfill candles for new symbols
                    for sym in new_symbols:
                        if sym not in [s for s in self.symbol_states if self.ws_handler.get_candle_buffer(s)]:
                            await self._backfill_symbol(sym)
                # Purge stale signal stats for symbols no longer active (memory cleanup)
                if self.signal_tracker:
                    self.signal_tracker.purge_stale(
                        active_symbols=list(self.symbol_states.keys()),
                        max_age_hours=72.0,
                    )
            except Exception as e:
                logger.error(f"Symbol rotation error: {e}")

        # Every ~2h (240 ticks × 30s): fast pipeline ban check
        # Replaces silent symbols between full rotations
        if self._periodic_tick % 240 == 120 and self.rotation and self.signal_tracker:
            try:
                await self._pipeline_ban_check()
            except Exception as e:
                logger.error(f"Pipeline ban check error: {e}")

        # Status log
        pos_info = []
        fvg_info = []
        for sym in self.config.symbols:
            st = self.symbol_states.get(sym)
            if not st:
                continue
            if st.has_position and st.current_order:
                o = st.current_order
                d = o.get("direction", "")
                d = d.value if hasattr(d, "value") else str(d)
                pos_info.append(f"{sym} {d} @{fmt_price(o.get('entry_price', 0))}")
            if st.active_fvg:
                f = st.active_fvg
                fvg_info.append(
                    f"{sym} {f.direction.value} {fmt_price(f.bottom)}-{fmt_price(f.top)}"
                )

        pos_str = ", ".join(pos_info) if pos_info else "none"
        fvg_str = ", ".join(fvg_info) if fvg_info else "none"
        prices = {
            s: fmt_price(self.symbol_states[s].last_price)
            for s in self.config.symbols
            if self.symbol_states.get(s)
        }

        logger.info(
            f"📈 bal=${self.state.balance:.2f} | pos=[{pos_str}] | "
            f"fvg=[{fvg_str}] | prices={prices} | "
            f"trades={self.state.total_trades}"
        )

    # ==================== Pipeline Ban Check ====================

    async def _pipeline_ban_check(self) -> None:
        """
        Fast pipeline: replace silent symbols between full rotations.

        Runs every 2h. Detects symbols that have been active but never
        generated a zone hit → dead zones → swap with scanner candidates.
        Much faster than full rotation: lightweight scan (top 50, 100 candles).
        """
        rotation_cfg = self.config.rotation
        ban_active_h = getattr(rotation_cfg, 'signal_ban_min_active_hours', 8.0)
        ban_no_hit_h = getattr(rotation_cfg, 'signal_ban_no_hit_hours', 8.0)

        silent = self.signal_tracker.get_silent_symbols(
            active_symbols=list(self.symbol_states.keys()),
            min_active_hours=ban_active_h,
            no_signal_hours=ban_no_hit_h,
        )

        # Log signal pipeline stats
        logger.info("📡 Pipeline ban check:")
        for line in self.signal_tracker.get_summary_lines(list(self.symbol_states.keys())):
            logger.info(line)

        if not silent:
            logger.info("📡 Pipeline: all symbols generating signals ✅")
            return

        # Check which have open positions (protect those)
        to_remove = [
            sym for sym in silent
            if not self.symbol_states.get(sym, object()).has_position
        ]
        if not to_remove:
            logger.info(f"📡 Pipeline: {len(silent)} silent but all have open positions — skipping")
            return

        logger.info(
            f"📡 Pipeline: swapping {len(to_remove)} silent symbols → "
            f"{', '.join(sorted(to_remove))}"
        )

        # Fast scan: top 50 symbols, 100 candles (quick)
        scan_results = await self.rotation.scan_all_symbols(
            top_n=50,
            candle_count=100,
            min_gap=self.config.fvg.min_gap_percent,
        )
        if not scan_results:
            logger.warning("📡 Pipeline: scan returned no results")
            return

        current_symbols = set(self.symbol_states.keys())
        candidates = [
            r for r in scan_results
            if r.symbol not in current_symbols
            and r.score >= rotation_cfg.min_score
        ]

        replacements = candidates[:len(to_remove)]
        if not replacements:
            logger.warning("📡 Pipeline: no eligible replacements found")
            return

        added = {r.symbol for r in replacements}
        removed = set(to_remove[:len(replacements)])
        score_map = {r.symbol: r.score for r in scan_results}

        logger.info(
            f"📡 Pipeline swap: "
            f"out={sorted(removed)} "
            f"in={[(r.symbol, r.score) for r in replacements]}"
        )

        await self.rotation._apply_rotation(
            new_list=sorted((current_symbols - removed) | added),
            removed=removed,
            added=added,
            symbol_states=self.symbol_states,
            ws_handler=self.ws_handler,
            score_map=score_map,
            signal_tracker=self.signal_tracker,
        )

        # Backfill candles for new symbols
        for sym in added:
            await self._backfill_symbol(sym)

        # Update config symbol list
        self.config.symbols = list(self.symbol_states.keys())

        await self.strategy.refresh_htf_trends()

    # ==================== Order Callbacks ====================

    async def _place_order(self, ctx) -> str:
        """Place market order (callback for OrderManager)."""
        return await self.executor.execute(
            self.exchange.place_order_from_context, ctx
        )

    async def _set_tpsl(self, ctx, levels) -> bool:
        """Set TP/SL (callback for OrderManager)."""
        return await self.executor.execute(
            self.exchange.set_tpsl_from_context, ctx, levels
        )

    async def _cancel_order(self, order_id: str) -> bool:
        """Cancel order (callback for OrderManager)."""
        return True


async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="GoldasT Bot v2")
    parser.add_argument(
        "-c", "--config", default="config.yaml", help="Path to configuration file"
    )
    args = parser.parse_args()

    bot = GoldastBot(config_path=args.config)
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.exception(f"Bot error: {e}")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
