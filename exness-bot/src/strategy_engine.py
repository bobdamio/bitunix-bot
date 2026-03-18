"""
Exness Bot - Strategy Engine
Orchestrates multi-timeframe analysis, entry/exit logic, and trade execution.
Core strategy: FVG/IFVG within Supply/Demand zones on 15m→5m→1m.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .models import (
    Candle, FVG, SupplyDemandZone, TradeDirection, TradeSignal,
    Position, SymbolState, BotState, OrderState, ZoneType,
)
from .config import Config
from .mt5_client import MT5Client
from .fvg_detector import FVGDetector
from .supply_demand import SupplyDemandDetector
from .mtf_analyzer import MTFAnalyzer
from .tpsl_calculator import TPSLCalculator, TPSLLevels
from .position_sizer import PositionSizer
from .market_structure import MarketStructure
from . import fmt_price

logger = logging.getLogger(__name__)


class StrategyEngine:
    """
    Main strategy engine for Exness MT5 bot.

    Cycle:
    1. Fetch candles for all timeframes (M15, M5, M1)
    2. Run MTF analysis (supply/demand zones + FVG detection)
    3. If valid setup found → calculate TP/SL → size position → execute
    4. Manage open positions (trailing SL, BE moves)
    5. Handle pending orders (Buy Stop / Sell Stop)
    """

    def __init__(self, config: Config, mt5_client: MT5Client):
        self.config = config
        self.mt5 = mt5_client

        # Core components
        self.fvg_detector = FVGDetector(config.fvg)
        self.sd_detector = SupplyDemandDetector(config.supply_demand)
        self.mtf_analyzer = MTFAnalyzer(config.mtf, self.fvg_detector, self.sd_detector)
        self.tpsl_calculator = TPSLCalculator(config.tpsl)
        self.position_sizer = PositionSizer(config.position)

        # State
        self.state = BotState()
        self._last_entry_time: Dict[str, datetime] = {}
        self._last_loss_time: Dict[str, datetime] = {}
        self._spent_zones: Dict[str, list] = {}

    def initialize(self) -> bool:
        """Initialize strategy: connect to MT5, setup symbols."""
        if not self.mt5.connect():
            return False

        account = self.mt5.get_account_info()
        if account is None:
            return False

        self.state.balance = account["balance"]
        self.state.equity = account["equity"]
        self.state.margin_free = account["margin_free"]
        self.state.is_running = True
        self.state.start_time = datetime.now()

        # Initialize symbol states (try suffixes if exact name not found)
        resolved_symbols = []
        for symbol in self.config.symbols:
            resolved = self._resolve_symbol(symbol)
            if resolved is None:
                logger.error(f"Cannot access symbol: {symbol} (tried suffixes: m, c, e, .raw)")
                continue
            sym_info = self.mt5.get_symbol_info(resolved)
            self.state.symbols[resolved] = SymbolState(symbol=resolved)
            resolved_symbols.append(resolved)
            if resolved != symbol:
                logger.info(f"Symbol mapped: {symbol} -> {resolved} | digits={sym_info['digits']} | spread={sym_info['spread']}")
            else:
                logger.info(f"Symbol ready: {symbol} | digits={sym_info['digits']} | spread={sym_info['spread']}")
        self.config.symbols = resolved_symbols

        # Sync existing positions
        self._sync_positions()

        logger.info(
            f"Strategy initialized: {len(self.state.symbols)} symbols | "
            f"Balance: ${self.state.balance:.2f} | Equity: ${self.state.equity:.2f}"
        )
        return True

    def _resolve_symbol(self, symbol: str) -> Optional[str]:
        """Try to find symbol with broker-specific suffix (e.g. XAUUSDm, XAUUSDc)."""
        # Try exact name first
        if self.mt5.get_symbol_info(symbol) is not None:
            return symbol
        # Try common Exness suffixes
        for suffix in ["m", "c", "e", ".raw", "pro", "#"]:
            candidate = symbol + suffix
            if self.mt5.get_symbol_info(candidate) is not None:
                return candidate
        return None

    def run_cycle(self) -> None:
        """Run one analysis + trading cycle for all symbols."""
        # Update account info
        account = self.mt5.get_account_info()
        if account:
            self.state.balance = account["balance"]
            self.state.equity = account["equity"]
            self.state.margin_free = account["margin_free"]

        # Check session
        if self.config.session.enabled:
            in_session, session_name = self.config.session.is_killzone_now()
            self.state.in_session = in_session
            if not in_session:
                logger.debug(f"Outside killzone ({session_name}), skipping cycle")
                return

        # Process each symbol
        for symbol, sym_state in self.state.symbols.items():
            try:
                self._process_symbol(symbol, sym_state)
            except Exception as e:
                logger.error(f"Error processing {symbol}: {e}", exc_info=True)

        # Manage open positions (trailing, etc.)
        self._manage_positions()

    def _process_symbol(self, symbol: str, sym_state: SymbolState) -> None:
        """Process a single symbol: fetch data, analyze, trade."""
        # Fetch candles for all timeframes
        candles_m15 = self.mt5.get_candles(symbol, "M15", self.config.fvg.lookback_candles)
        candles_m5 = self.mt5.get_candles(symbol, "M5", self.config.fvg.lookback_candles)
        candles_m1 = self.mt5.get_candles(symbol, "M1", self.config.fvg.lookback_candles)

        if not candles_m1:
            logger.debug(f"No M1 candles for {symbol}")
            return

        # Update state candles
        sym_state.candles_m1 = candles_m1
        sym_state.candles_m5 = candles_m5
        sym_state.candles_m15 = candles_m15

        # Get current price
        price_info = self.mt5.get_current_price(symbol)
        if price_info is None:
            return
        bid, ask = price_info
        current_price = (bid + ask) / 2
        sym_state.last_price = current_price
        sym_state.last_update = datetime.now()

        # Check cooldowns
        if not self._check_cooldowns(symbol):
            return

        # Check max positions
        open_count = self.state.get_open_positions_count()
        if open_count >= self.config.position.max_positions:
            return

        symbol_positions = len(sym_state.positions)
        if symbol_positions >= self.config.position.max_positions_per_symbol:
            return

        # Run multi-timeframe analysis
        setup = self.mtf_analyzer.analyze(
            symbol, candles_m15, candles_m5, candles_m1, current_price
        )

        if setup is None:
            return

        # Check FVG entry conditions (if FVG-based entry)
        entry_fvg = setup.get("entry_fvg")
        if entry_fvg:
            can_enter, reason = self.fvg_detector.check_entry_conditions(entry_fvg, current_price)
            if not can_enter:
                logger.info(
                    f"{symbol}: Setup found but entry blocked - {reason} | "
                    f"price={fmt_price(current_price)} FVG={fmt_price(entry_fvg.bottom)}-{fmt_price(entry_fvg.top)} "
                    f"fill={entry_fvg.fill_percent*100:.1f}%"
                )
                return

        # Check zone cooldown
        zone = setup.get("zone")
        if zone and self._is_zone_spent(symbol, zone):
            logger.info(f"{symbol}: Setup found but zone is on cooldown")
            return

        # Execute trade
        self._execute_setup(symbol, sym_state, setup, current_price, candles_m1)

    def _execute_setup(
        self,
        symbol: str,
        sym_state: SymbolState,
        setup: Dict,
        current_price: float,
        candles: List[Candle],
    ) -> None:
        """Execute a validated trading setup."""
        direction = setup["direction"]
        entry_fvg = setup.get("entry_fvg")
        zone = setup.get("zone")
        order_type = setup.get("order_type", "MARKET")
        confluence_score = setup.get("confluence_score", 0.5)

        # Determine R:R based on confluence
        if confluence_score >= 0.80:
            target_rr = self.config.tpsl.max_rr  # Strong setup → 1:3
        elif confluence_score >= 0.65:
            target_rr = self.config.tpsl.default_rr  # Medium → 1:2
        else:
            target_rr = self.config.tpsl.min_rr  # Weak → 1:1

        # Calculate TP/SL
        tpsl = self.tpsl_calculator.calculate(
            entry_price=current_price,
            direction=direction,
            candles=candles,
            fvg=entry_fvg,
            zone=zone,
            target_rr=target_rr,
        )

        # Validate R:R
        if tpsl.risk_reward_ratio < self.config.tpsl.min_rr:
            logger.info(
                f"{symbol}: R:R too low ({tpsl.risk_reward_ratio:.2f} < {self.config.tpsl.min_rr})"
            )
            return

        # Calculate lot size
        sym_info = self.mt5.get_symbol_info(symbol)
        if sym_info is None:
            return

        lot_size = self.position_sizer.calculate_lot_size(
            balance=self.state.balance,
            entry_price=current_price,
            sl_price=tpsl.sl_price,
            symbol_info=sym_info,
            direction=direction,
        )

        logger.info(
            f">> Signal: {symbol} {direction.value} | "
            f"lot={lot_size} | SL={fmt_price(tpsl.sl_price)} | "
            f"TP={fmt_price(tpsl.tp_price)} | R:R={tpsl.risk_reward_ratio:.1f} | "
            f"confluence={confluence_score:.2f} | type={order_type}"
        )

        # Execute order
        if order_type == "MARKET":
            ticket = self.mt5.place_market_order(
                symbol=symbol,
                direction=direction,
                lot_size=lot_size,
                sl_price=tpsl.sl_price,
                tp_price=tpsl.tp_price,
                comment=f"ExBot_{direction.value}_{confluence_score:.0%}",
            )
        elif order_type in ("BUY_STOP", "SELL_STOP"):
            pending_price = setup.get("pending_price", current_price)
            ticket = self.mt5.place_pending_order(
                symbol=symbol,
                direction=direction,
                order_type=order_type,
                price=pending_price,
                lot_size=lot_size,
                sl_price=tpsl.sl_price,
                tp_price=tpsl.tp_price,
                comment=f"ExBot_{order_type}_{confluence_score:.0%}",
            )
        else:
            logger.error(f"Unknown order type: {order_type}")
            return

        if ticket is None:
            logger.error(f"{symbol}: Order execution failed")
            return

        # Track position
        position = Position(
            ticket=ticket,
            symbol=symbol,
            direction=direction,
            entry_price=current_price,
            lot_size=lot_size,
            magic=self.config.account.magic_number,
            tp_price=tpsl.tp_price,
            sl_price=tpsl.sl_price,
            tpsl_placed=True,
            fvg_bottom=entry_fvg.bottom if entry_fvg else 0,
            fvg_top=entry_fvg.top if entry_fvg else 0,
            zone_bottom=zone.bottom if zone else 0,
            zone_top=zone.top if zone else 0,
            best_price=current_price,
        )
        sym_state.positions.append(position)
        sym_state.trades_executed += 1
        self.state.total_trades += 1
        self._last_entry_time[symbol] = datetime.now()

        logger.info(
            f"Trade opened: {symbol} {direction.value} | "
            f"ticket={ticket} lot={lot_size} @ {fmt_price(current_price)}"
        )

    def _manage_positions(self) -> None:
        """Manage open positions: trailing SL, BE moves."""
        if not self.config.tpsl.trailing_enabled:
            return

        for symbol, sym_state in self.state.symbols.items():
            for pos in list(sym_state.positions):
                try:
                    self._update_trailing(symbol, pos)
                except Exception as e:
                    logger.error(f"Trailing error {symbol} ticket={pos.ticket}: {e}")

    def _update_trailing(self, symbol: str, pos: Position) -> None:
        """Update trailing SL for a position."""
        price_info = self.mt5.get_current_price(symbol)
        if price_info is None:
            return
        bid, ask = price_info
        current_price = bid if pos.direction == TradeDirection.SHORT else ask

        # Calculate current R-multiple
        if pos.direction == TradeDirection.LONG:
            sl_distance = pos.entry_price - pos.sl_price
            current_profit = current_price - pos.entry_price
        else:
            sl_distance = pos.sl_price - pos.entry_price
            current_profit = pos.entry_price - current_price

        if sl_distance <= 0:
            return

        current_r = current_profit / sl_distance

        # Update best price
        if pos.direction == TradeDirection.LONG:
            pos.best_price = max(pos.best_price, current_price)
        else:
            pos.best_price = min(pos.best_price, current_price) if pos.best_price > 0 else current_price

        # Phase 2: Breakeven at 1R
        be_at = self.config.tpsl.trailing_breakeven_at_r
        be_lock = self.config.tpsl.trailing_be_lock_r

        if current_r >= be_at and pos.trailing_state == "initial":
            if pos.direction == TradeDirection.LONG:
                new_sl = pos.entry_price + sl_distance * be_lock
            else:
                new_sl = pos.entry_price - sl_distance * be_lock

            # Only move SL in our favor
            if pos.direction == TradeDirection.LONG and new_sl > pos.sl_price:
                if self.mt5.modify_position(pos.ticket, symbol, new_sl, pos.tp_price):
                    pos.sl_price = new_sl
                    pos.trailing_sl_price = new_sl
                    pos.trailing_state = "breakeven"
                    logger.info(f"BE activated: {symbol} ticket={pos.ticket} SL->{fmt_price(new_sl)}")

            elif pos.direction == TradeDirection.SHORT and new_sl < pos.sl_price:
                if self.mt5.modify_position(pos.ticket, symbol, new_sl, pos.tp_price):
                    pos.sl_price = new_sl
                    pos.trailing_sl_price = new_sl
                    pos.trailing_state = "breakeven"
                    logger.info(f"BE activated: {symbol} ticket={pos.ticket} SL->{fmt_price(new_sl)}")

        # Phase 3: Runner at 2R
        runner_at = self.config.tpsl.trailing_runner_at_r
        runner_trail = self.config.tpsl.trailing_runner_sl_distance_r
        step_r = self.config.tpsl.trailing_step_r

        if current_r >= runner_at and pos.trailing_state in ("breakeven", "trailing"):
            pos.trailing_state = "trailing"

            if pos.direction == TradeDirection.LONG:
                new_sl = current_price - sl_distance * runner_trail
                if new_sl > pos.sl_price + sl_distance * step_r:
                    if self.mt5.modify_position(pos.ticket, symbol, new_sl, pos.tp_price):
                        pos.sl_price = new_sl
                        pos.trailing_sl_price = new_sl
                        logger.info(f"Trail UP: {symbol} ticket={pos.ticket} SL->{fmt_price(new_sl)} ({current_r:.1f}R)")

            else:
                new_sl = current_price + sl_distance * runner_trail
                if new_sl < pos.sl_price - sl_distance * step_r:
                    if self.mt5.modify_position(pos.ticket, symbol, new_sl, pos.tp_price):
                        pos.sl_price = new_sl
                        pos.trailing_sl_price = new_sl
                        logger.info(f"Trail DN: {symbol} ticket={pos.ticket} SL->{fmt_price(new_sl)} ({current_r:.1f}R)")

    def _sync_positions(self) -> None:
        """Sync open positions from MT5 on startup."""
        for symbol in self.config.symbols:
            mt5_positions = self.mt5.get_open_positions(symbol)
            sym_state = self.state.symbols.get(symbol)
            if sym_state is None:
                continue

            for mp in mt5_positions:
                direction = TradeDirection.LONG if mp["type"] == "LONG" else TradeDirection.SHORT
                pos = Position(
                    ticket=mp["ticket"],
                    symbol=symbol,
                    direction=direction,
                    entry_price=mp["price_open"],
                    lot_size=mp["volume"],
                    magic=mp["magic"],
                    tp_price=mp["tp"],
                    sl_price=mp["sl"],
                    tpsl_placed=True,
                    unrealized_pnl=mp["profit"],
                    best_price=mp["price_open"],
                )
                sym_state.positions.append(pos)

            if mt5_positions:
                logger.info(f"Synced {len(mt5_positions)} positions for {symbol}")

    def _check_cooldowns(self, symbol: str) -> bool:
        """Check if symbol is in cooldown."""
        now = datetime.now()

        # Entry cooldown
        last_entry = self._last_entry_time.get(symbol)
        if last_entry:
            elapsed = (now - last_entry).total_seconds()
            if elapsed < self.config.cooldowns.entry_cooldown_seconds:
                return False

        # Loss cooldown
        last_loss = self._last_loss_time.get(symbol)
        if last_loss:
            elapsed = (now - last_loss).total_seconds()
            if elapsed < self.config.cooldowns.loss_cooldown_seconds:
                return False

        return True

    def _is_zone_spent(self, symbol: str, zone: SupplyDemandZone) -> bool:
        """Check if zone overlaps with a recently-spent zone."""
        spent = self._spent_zones.get(symbol, [])
        if not spent:
            return False

        cooldown = self.config.cooldowns.zone_cooldown_seconds
        now = datetime.now()

        for (bottom, top, zone_type, ts) in spent:
            if (now - ts).total_seconds() >= cooldown:
                continue
            if zone_type != zone.zone_type.value:
                continue
            overlap_top = min(zone.top, top)
            overlap_bottom = max(zone.bottom, bottom)
            if overlap_top > overlap_bottom:
                overlap_ratio = (overlap_top - overlap_bottom) / zone.range if zone.range > 0 else 0
                if overlap_ratio >= 0.50:
                    return True
        return False

    def record_spent_zone(self, symbol: str, zone: SupplyDemandZone) -> None:
        """Record a zone that was used and hit SL."""
        if symbol not in self._spent_zones:
            self._spent_zones[symbol] = []
        self._spent_zones[symbol].append(
            (zone.bottom, zone.top, zone.zone_type.value, datetime.now())
        )

    def cleanup_closed_positions(self) -> None:
        """Remove positions that are no longer open in MT5."""
        for symbol, sym_state in self.state.symbols.items():
            mt5_positions = self.mt5.get_open_positions(symbol)
            mt5_tickets = {p["ticket"] for p in mt5_positions}

            closed = [p for p in sym_state.positions if p.ticket not in mt5_tickets]
            for pos in closed:
                logger.info(
                    f"Position closed: {symbol} {pos.direction.value} "
                    f"ticket={pos.ticket}"
                )
                # If it was a loss, record cooldown
                # (We can check PnL from deal history, simplified here)
                self._last_loss_time[symbol] = datetime.now()

            sym_state.positions = [p for p in sym_state.positions if p.ticket in mt5_tickets]
