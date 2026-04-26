"""
GoldasT Bot v2 - Position Manager
Manages position state: WS callbacks, periodic sync, balance tracking.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, Optional

from .models import SymbolState, BotState, TradeDirection
from .tpsl_calculator import TPSLCalculator
from .order_state_machine import OrderManager
from .exchange_adapter import ExchangeAdapter
from .trade_history import TradeHistory
from .websocket_handler import PositionUpdate, TPSLUpdate, OrderUpdate, WebSocketHandler


logger = logging.getLogger(__name__)


class PositionManager:
    """
    Owns all position-related state management:
    - Startup sync (load existing positions from exchange)
    - WS callbacks for position/tpsl/order updates
    - Periodic position reconciliation (catches missed WS events)
    - Balance tracking
    """

    def __init__(
        self,
        exchange: ExchangeAdapter,
        ws_handler: WebSocketHandler,
        tpsl_calculator: TPSLCalculator,
        order_manager: OrderManager,
        state: BotState,
        symbol_states: Dict[str, SymbolState],
        trade_history: Optional['TradeHistory'] = None,
        strategy_engine=None,  # back-reference for loss cooldown tracking
        blacklist: Optional[list] = None,
    ):
        self.exchange = exchange
        self.ws_handler = ws_handler
        self.tpsl_calculator = tpsl_calculator
        self.order_manager = order_manager
        self.state = state
        self.symbol_states = symbol_states
        self.trade_history = trade_history or TradeHistory()
        self._strategy = strategy_engine  # set after construction via set_strategy()
        self._telegram = None  # set after construction via set_telegram()
        self._blacklist: set = set(blacklist or [])
        self._open_positions_path = Path("data/open_positions.json")

    def set_telegram(self, telegram_bot) -> None:
        """Set Telegram bot reference for close notifications."""
        self._telegram = telegram_bot

    # ==================== Wiring ====================

    def set_strategy(self, strategy_engine) -> None:
        """Set back-reference to strategy engine (for loss cooldown)."""
        self._strategy = strategy_engine

    # ==================== Persistent Open Positions ====================

    def _load_open_positions(self) -> dict:
        """Load persisted open position data (original_risk etc.)."""
        try:
            if self._open_positions_path.exists():
                return json.loads(self._open_positions_path.read_text())
        except Exception as e:
            logger.warning(f"Could not load open_positions.json: {e}")
        return {}

    def save_open_position(self, position_id: str, data: dict) -> None:
        """Persist position data for restart recovery."""
        try:
            positions = self._load_open_positions()
            positions[str(position_id)] = data
            self._open_positions_path.parent.mkdir(parents=True, exist_ok=True)
            self._open_positions_path.write_text(json.dumps(positions, indent=2))
        except Exception as e:
            logger.warning(f"Could not save open_positions.json: {e}")

    def remove_open_position(self, position_id: str) -> None:
        """Remove closed position from persistent storage."""
        try:
            positions = self._load_open_positions()
            if str(position_id) in positions:
                del positions[str(position_id)]
                self._open_positions_path.write_text(json.dumps(positions, indent=2))
        except Exception as e:
            logger.warning(f"Could not update open_positions.json: {e}")

    # ==================== Startup Sync ====================

    async def sync_positions_from_exchange(self) -> None:
        """
        Sync existing positions from exchange on startup.
        Loads each open position into bot state.
        
        Smart TP/SL handling:
        - If exchange already has SL but NO TP → partial TP was done,
          preserve existing SL and set trailing state.
        - If exchange has both TP + SL → keep them as-is.
        - If exchange has neither → compute fresh from ATR.
        """
        logger.info("🔄 Syncing positions from exchange...")
        try:
            positions = await self.exchange.get_positions()
            if not positions:
                logger.info("ℹ️ No existing positions found on exchange")
                return

            # Fetch existing TP/SL orders from exchange
            existing_tpsl = {}
            try:
                tpsl_orders = await self.exchange._api.get_pending_tpsl_orders()
                logger.info(f"🔍 [SYNC] Raw TP/SL orders from exchange: {tpsl_orders}")
                # Handle both list and dict-with-list response formats
                order_list = tpsl_orders
                if isinstance(tpsl_orders, dict):
                    order_list = (
                        tpsl_orders.get("dataList")
                        or tpsl_orders.get("orderList")
                        or tpsl_orders.get("list")
                        or []
                    )
                if isinstance(order_list, list):
                    for order in order_list:
                        pos_id = str(order.get("positionId", ""))
                        if pos_id:
                            entry = existing_tpsl.setdefault(pos_id, {})
                            tp = order.get("tpPrice") or order.get("tp_price")
                            sl = order.get("slPrice") or order.get("sl_price")
                            if tp and float(tp) > 0:
                                entry["tp"] = float(tp)
                            if sl and float(sl) > 0:
                                entry["sl"] = float(sl)
                logger.info(f"🔍 [SYNC] Parsed TP/SL map: {existing_tpsl}")
            except Exception as e:
                logger.warning(f"Could not fetch existing TP/SL orders: {e}")

            persisted = self._load_open_positions()
            if persisted:
                logger.info(f"🔍 [SYNC] Loaded persisted positions: {list(persisted.keys())}")

            for pos in positions:
                symbol = pos.get("symbol", "")
                is_orphan = symbol not in self.symbol_states
                if is_orphan:
                    if symbol in self._blacklist:
                        logger.warning(
                            f"🚫 [SYNC] Orphan position for BLACKLISTED {symbol} — "
                            f"will manage trailing/close but NOT open new trades"
                        )
                    else:
                        logger.warning(
                            f"🔀 [SYNC] Orphan position found: {symbol} not in active symbols "
                            f"— creating state for trailing management"
                        )
                    # Create state for trailing management regardless (to close gracefully)
                    self.symbol_states[symbol] = SymbolState(symbol=symbol)
                    if symbol in self._blacklist:
                        self.symbol_states[symbol]._blacklisted = True

                state = self.symbol_states[symbol]
                position_id = str(pos.get("positionId", ""))
                side = pos.get("side", "")
                size = float(pos.get("qty", "0"))
                entry_price = float(pos.get("avgOpenPrice", "0"))
                direction = (
                    TradeDirection.LONG
                    if side.upper() == "BUY"
                    else TradeDirection.SHORT
                )
                current_price = float(pos.get("markPrice") or pos.get("lastPrice") or entry_price)

                # Check existing exchange TP/SL
                exch = existing_tpsl.get(position_id, {})
                has_tp = "tp" in exch
                has_sl = "sl" in exch

                if has_sl and not has_tp:
                    # SL exists but no TP → advanced trailing state
                    sl_price = exch["sl"]
                    tp_price = 0  # No TP on exchange
                    risk = abs(entry_price - sl_price)
                    # Don't assume partial TP was done — let trailing logic decide
                    state.partial_tp_done = False
                    state.trailing_state = "breakeven"
                    state.trailing_sl_price = sl_price
                    logger.info(
                        f"📊 [SYNC] {symbol}: SL in trailing state "
                        f"(SL={sl_price:.4f}, no TP) — partial TP will fire if needed"
                    )
                elif has_tp and has_sl:
                    # Both exist — check if SL is in profit territory (Phase 2 BE or beyond)
                    tp_price = exch["tp"]
                    sl_price = exch["sl"]
                    risk = abs(entry_price - sl_price)
                    # SL in profit means Phase 2 BE already triggered
                    sl_in_profit = (
                        (direction == TradeDirection.LONG and sl_price > entry_price)
                        or (direction == TradeDirection.SHORT and sl_price < entry_price)
                    )
                    if sl_in_profit:
                        # SL is in profit — Phase 2 BE happened, but partial TP may NOT have
                        # Don't set partial_tp_done=True — let trailing logic execute it
                        state.partial_tp_done = False
                        state.trailing_state = "breakeven"
                        state.trailing_sl_price = sl_price
                        # Restore original_risk: prefer persisted value, fall back to TP-based
                        saved = persisted.get(position_id, {})
                        if saved.get("original_risk"):
                            risk = saved["original_risk"]
                            logger.info(
                                f"📊 [SYNC] {symbol}: restored original_risk={risk:.6f} from persistence"
                            )
                        else:
                            risk = abs(tp_price - entry_price) / self.tpsl_calculator.config.force_close_at_r
                            logger.info(
                                f"📊 [SYNC] {symbol}: estimated original_risk={risk:.6f} from TP distance "
                                f"(TP={tp_price:.4f}, force_close_at_r={self.tpsl_calculator.config.force_close_at_r})"
                            )
                        logger.info(
                            f"📊 [SYNC] {symbol}: SL in profit (Phase 2+) "
                            f"(SL={sl_price:.4f}, TP={tp_price:.4f}) — "
                            f"partial TP will fire on next tick if not yet done"
                        )
                    else:
                        # SL not in profit — use persisted risk if available
                        saved = persisted.get(position_id, {})
                        if saved.get("original_risk"):
                            risk = saved["original_risk"]
                            logger.info(
                                f"📊 [SYNC] {symbol}: restored original_risk={risk:.6f} from persistence"
                            )
                        logger.info(
                            f"📊 [SYNC] {symbol}: using exchange TP/SL "
                            f"(TP={tp_price:.4f}, SL={sl_price:.4f})"
                        )
                else:
                    # No TP/SL on exchange — compute fresh and set
                    sync_candles = (
                        self.ws_handler.get_candle_buffer(symbol)
                        if self.ws_handler
                        else None
                    )
                    tp_price, sl_price = self.tpsl_calculator.calculate_from_atr(
                        entry_price=entry_price,
                        direction=direction,
                        candles=sync_candles,
                    )
                    risk = abs(entry_price - sl_price)
                    await self.exchange.set_position_tpsl(
                        symbol,
                        position_id,
                        tp_price,
                        sl_price,
                        direction=direction.value,
                        current_price=current_price,
                    )
                    logger.info(
                        f"📊 [SYNC] {symbol}: no TP/SL found — set fresh "
                        f"(TP={tp_price:.4f}, SL={sl_price:.4f})"
                    )

                state.has_position = True
                state.last_price = current_price  # Set mark price for trailing calc
                state.original_qty = size  # Set original qty for partial TP calculation
                state.current_order = {
                    "entry_price": entry_price,
                    "sl_price": sl_price,
                    "tp_price": tp_price,
                    "original_risk": risk,
                    "quantity": size,
                    "direction": direction,
                    "position_id": position_id,
                    "synced_from_exchange": True,
                }
                logger.info(
                    f"📊 [POSITION SYNCED] {symbol} | ID: {position_id} | "
                    f"Size: {size} | Entry: {entry_price} | "
                    f"SL: {sl_price} | TP: {tp_price}"
                )

                # Subscribe orphan symbols to WS for price updates
                if is_orphan and self.ws_handler:
                    try:
                        await self.ws_handler.subscribe_new_symbols([symbol])
                        logger.info(f"📡 [SYNC] Subscribed orphan {symbol} to WS kline")
                    except Exception as ws_err:
                        logger.warning(f"⚠️ [SYNC] Could not subscribe orphan {symbol} to WS: {ws_err}")

        except Exception as e:
            logger.error(f"Error syncing positions from exchange: {e}")

    # ==================== Periodic Sync ====================

    async def periodic_position_sync(self) -> None:
        """Reconcile bot state with actual exchange positions.

        Detects positions the bot thinks are open but have been closed
        on the exchange (e.g. TP/SL triggered while private WS was
        reconnecting).
        """
        try:
            exchange_positions = await self.exchange.get_positions()
            if exchange_positions is None:
                # API unreachable — do NOT clear any positions
                logger.warning("⚠️ [SYNC] Skipped: API unreachable, keeping position state")
                return

            # Safety: if we track open positions but exchange returns empty,
            # verify API health. During DNS outages, get_positions() can
            # return [] (empty) instead of None if connectivity is flaky.
            tracked_count = sum(1 for s in self.symbol_states.values() if s.has_position)
            if tracked_count > 0 and len(exchange_positions) == 0:
                await asyncio.sleep(2.0)
                retry_positions = await self.exchange.get_positions()
                if retry_positions is None:
                    logger.warning(
                        f"⚠️ [SYNC] Exchange returned 0 positions but we track {tracked_count}. "
                        f"Retry failed (API unreachable) — keeping position state"
                    )
                    return
                if len(retry_positions) == 0:
                    # Also verify balance is sane (not $0 from DNS issues)
                    balance = await self.exchange.get_balance()
                    if balance is None or balance.available <= 0:
                        logger.warning(
                            f"⚠️ [SYNC] Confirmed 0 positions but balance check failed/zero — "
                            f"likely API issue, keeping {tracked_count} position(s)"
                        )
                        return
                    logger.info(
                        f"🔄 [SYNC] Confirmed: {tracked_count} tracked position(s) "
                        f"closed on exchange (balance=${balance.available:.2f})"
                    )
                    exchange_positions = retry_positions
                else:
                    exchange_positions = retry_positions

            exchange_symbols = set()
            pre_sync_symbols = set(self.symbol_states.keys())  # Track which were there before
            for pos in exchange_positions:
                sym = pos.get("symbol", "")
                exchange_symbols.add(sym)

                # Orphan position: symbol not in symbol_states but has open position
                if sym not in self.symbol_states:
                    if sym in self._blacklist:
                        logger.warning(
                            f"🚫 [SYNC] Orphan position for BLACKLISTED {sym} — "
                            f"will manage trailing/close but NOT open new trades"
                        )
                    else:
                        logger.warning(
                            f"🔀 [SYNC] Orphan position detected: {sym} — "
                            f"creating state for trailing management"
                        )
                    self.symbol_states[sym] = SymbolState(symbol=sym)
                    if sym in self._blacklist:
                        self.symbol_states[sym]._blacklisted = True
                    mark = float(pos.get("markPrice") or pos.get("lastPrice") or 0)
                    state = self.symbol_states[sym]
                    state.last_price = mark

                    # Reconstruct minimal order info for trailing
                    entry_price = float(pos.get("avgOpenPrice", "0"))
                    size = float(pos.get("qty", "0"))
                    side = pos.get("side", "")
                    direction = (
                        TradeDirection.LONG if side.upper() == "BUY"
                        else TradeDirection.SHORT
                    )
                    position_id = str(pos.get("positionId", ""))

                    # Try to find TP/SL from pending orders
                    tp_price = 0.0
                    sl_price = 0.0
                    try:
                        tpsl_orders = await self.exchange._api.get_pending_tpsl_orders()
                        order_list = tpsl_orders
                        if isinstance(tpsl_orders, dict):
                            order_list = (
                                tpsl_orders.get("dataList")
                                or tpsl_orders.get("orderList")
                                or tpsl_orders.get("list")
                                or []
                            )
                        if isinstance(order_list, list):
                            for order in order_list:
                                if str(order.get("positionId", "")) == position_id:
                                    tp = order.get("tpPrice") or order.get("tp_price")
                                    sl = order.get("slPrice") or order.get("sl_price")
                                    if tp and float(tp) > 0:
                                        tp_price = float(tp)
                                    if sl and float(sl) > 0:
                                        sl_price = float(sl)
                                    break
                    except Exception:
                        pass

                    risk = abs(entry_price - sl_price) if sl_price else 0
                    state.has_position = True
                    state.current_order = {
                        "entry_price": entry_price,
                        "sl_price": sl_price,
                        "tp_price": tp_price,
                        "original_risk": risk,
                        "quantity": size,
                        "direction": direction,
                        "position_id": position_id,
                        "synced_from_exchange": True,
                    }

                    # Subscribe to WS for price updates
                    if self.ws_handler:
                        try:
                            await self.ws_handler.subscribe_new_symbols([sym])
                        except Exception:
                            pass

                    logger.info(
                        f"📊 [SYNC] Orphan {sym} restored: entry={entry_price}, "
                        f"SL={sl_price}, TP={tp_price}, mark={mark}"
                    )
                else:
                    # Update last_price for orphan symbols (no WS kline updates)
                    state = self.symbol_states[sym]
                    if state.has_position and state.current_order:
                        order = state.current_order
                        if order.get("synced_from_exchange"):
                            mark = float(pos.get("markPrice") or pos.get("lastPrice") or 0)
                            if mark > 0:
                                state.last_price = mark

            for symbol, state in list(self.symbol_states.items()):
                if state.has_position and symbol not in exchange_symbols:
                    old_order = state.current_order or {}
                    entry = old_order.get("entry_price", 0)
                    direction = old_order.get("direction", "")
                    d_str = (
                        direction.value
                        if hasattr(direction, "value")
                        else str(direction)
                    )

                    # Look up close details from history
                    close_price = None
                    pnl = None
                    history_record = None
                    try:
                        history = (
                            await self.exchange.get_history_positions(symbol)
                        )
                        if history:
                            pos_id = old_order.get("position_id")
                            for h in history:  # search ALL, not [:5]
                                if str(h.get("positionId")) == str(pos_id):
                                    close_price = float(h.get("closePrice", 0))
                                    pnl = float(h.get("realizedPNL", 0))
                                    history_record = h
                                    break
                    except Exception:
                        pass

                    pnl_str = f" PnL=${pnl:.4f}" if pnl is not None else ""
                    close_str = (
                        f" close={close_price:.4f}" if close_price else ""
                    )
                    logger.info(
                        f"🔄 [SYNC] Position gone: {symbol} {d_str} "
                        f"entry={entry:.4f}{close_str}{pnl_str} "
                        f"— clearing bot state"
                    )

                    # Record trade in history
                    if history_record is not None:
                        self.trade_history.record_trade(history_record)

                    # Set loss cooldown for sync-detected closes
                    if self._strategy and pnl is not None:
                        from datetime import datetime as dt
                        # Record direction for adaptive nerfing
                        direction = (old_order or {}).get('direction')
                        if direction:
                            dir_str = direction.value if hasattr(direction, 'value') else str(direction)
                            self._strategy.record_direction_trade(dir_str, pnl)
                        if pnl < 0:
                            self._strategy._last_loss_time[symbol] = dt.now()
                            # Track per-symbol consecutive losses
                            self._strategy._update_symbol_loss_streak(symbol, is_loss=True)
                            logger.info(f"⏳ SL cooldown set for {symbol} (sync close, PnL=${pnl:.2f})")
                        else:
                            # pnl >= 0 (win or breakeven) — reset loss streak
                            self._strategy._last_win_time[symbol] = dt.now()
                            self._strategy._update_symbol_loss_streak(symbol, is_loss=False)

                        # Record spent zone for SL/BE closes (zone cooldown)
                        fvg_bottom = (old_order or {}).get('fvg_bottom')
                        fvg_top = (old_order or {}).get('fvg_top')
                        if fvg_bottom and fvg_top and direction:
                            dir_val = direction.value if hasattr(direction, 'value') else str(direction)
                            trailing_st = state.trailing_state if state else "initial"
                            is_be = (trailing_st == "breakeven" and abs(pnl) < self.tpsl_calculator.config.breakeven_pnl_threshold)
                            zone_ct = "sl" if pnl < 0 else ("be" if is_be else "tp")
                            self._strategy.record_spent_zone(
                                symbol, fvg_bottom, fvg_top, dir_val, zone_ct
                            )

                    state.has_position = False
                    state.active_fvg = None
                    state.current_order = None
                    state.trailing_state = "initial"
                    state.partial_tp_done = False
                    state.original_qty = 0.0
                    state.trailing_sl_price = 0.0
                    state._order_pending = False

                    # Clean up orphan SymbolState (rotated-out symbol, position now closed)
                    if symbol not in pre_sync_symbols and symbol in self.symbol_states:
                        del self.symbol_states[symbol]
                        if self.ws_handler:
                            self.ws_handler.unsubscribe_symbols([symbol])
                        logger.info(
                            f"🗑️ [SYNC] Orphan {symbol} position closed — "
                            f"removed temporary state"
                        )

                    await self.sync_balance()

        except Exception as e:
            logger.error(f"Periodic position sync error: {e}")

    # ==================== WS Callbacks ====================

    def on_position(self, update: PositionUpdate) -> None:
        """Handle position update from WS."""
        symbol = update.symbol

        if update.quantity > 0:
            logger.info(
                f"📥 [WS] Position update: {symbol} {update.side} "
                f"qty={update.quantity} @ {update.entry_price:.4f} "
                f"uPnL={update.unrealized_pnl:.2f}"
            )
        elif update.quantity == 0:
            state = self.symbol_states.get(symbol)
            # Ignore false close events during order placement or TP/SL modification
            if state and state._order_pending:
                logger.info(
                    f"⏳ [WS] Ignoring position close for {symbol} "
                    f"(order/modify in-flight)"
                )
                return
            if state and state.has_position:
                # Verify via REST before clearing state — WS can send false qty=0
                # during TP/SL modifications (caused duplicate position bug)
                asyncio.create_task(
                    self._verify_and_close_position(symbol, state)
                )
            else:
                logger.debug(
                    f"📭 [WS] Position close event for {symbol} "
                    f"(no tracked position — ignoring)"
                )

    def on_tpsl(self, update: TPSLUpdate) -> None:
        """Handle TP/SL trigger from WS."""
        symbol = update.symbol

        if update.tp_triggered or update.sl_triggered:
            close_type = update.close_type or (
                "tp" if update.tp_triggered else "sl"
            )

            self.order_manager.handle_close(
                symbol=symbol,
                close_type=close_type,
                pnl=update.pnl,
            )

            state = self.symbol_states.get(symbol)
            # Capture order info BEFORE clearing state
            order_info = state.current_order if state else None
            trailing_st = state.trailing_state if state else "initial"

            # Record spent zone for SL or near-BE closes (zone cooldown)
            if self._strategy and order_info:
                fvg_bottom = (order_info or {}).get('fvg_bottom')
                fvg_top = (order_info or {}).get('fvg_top')
                direction = (order_info or {}).get('direction')
                if fvg_bottom and fvg_top and direction:
                    dir_val = direction.value if hasattr(direction, 'value') else str(direction)
                    # SL hit → always record spent zone
                    # BE / near-zero PnL → also record (zone didn't produce R)
                    is_sl = update.sl_triggered
                    is_be = (trailing_st == "breakeven" and update.pnl is not None
                             and abs(update.pnl) < self.tpsl_calculator.config.breakeven_pnl_threshold)
                    zone_close_type = "sl" if is_sl else ("be" if is_be else "tp")
                    self._strategy.record_spent_zone(
                        symbol, fvg_bottom, fvg_top, dir_val, zone_close_type
                    )

            if state:
                self._clear_position_state(state)

            if update.tp_triggered:
                logger.info(f"🎯 TP hit on {symbol}: PnL=${update.pnl:.2f}")
            else:
                logger.info(f"🛑 SL hit on {symbol}: PnL=${update.pnl:.2f}")

            # Send Telegram notification
            if self._telegram:
                from datetime import datetime as dt
                entry_price = (order_info or {}).get('entry_price', 0)
                sl_price = (order_info or {}).get('sl_price', 0)
                original_risk = (order_info or {}).get('original_risk', 0)
                leverage_val = (order_info or {}).get('leverage', 0)
                direction = (order_info or {}).get('direction')
                entry_time = (order_info or {}).get('entry_time')
                side_str = ""
                if direction:
                    side_str = direction.value if hasattr(direction, 'value') else str(direction)

                # Approximate close price from PnL + entry
                close_price = entry_price  # fallback
                if entry_price > 0 and update.pnl is not None:
                    qty = (order_info or {}).get('quantity', 0)
                    if qty > 0:
                        close_price = entry_price + (update.pnl / qty) if side_str.upper() in ("BUY", "LONG") \
                            else entry_price - (update.pnl / qty)

                # R achieved
                r_achieved = 0.0
                if original_risk > 0 and update.pnl is not None and qty > 0:
                    price_move = abs(close_price - entry_price)
                    r_achieved = price_move / original_risk
                    if update.pnl < 0:
                        r_achieved = -r_achieved

                # Hold time
                hold_time_str = ""
                if entry_time:
                    delta = dt.now() - entry_time
                    total_sec = int(delta.total_seconds())
                    if total_sec < 60:
                        hold_time_str = f"{total_sec}s"
                    elif total_sec < 3600:
                        hold_time_str = f"{total_sec // 60}m {total_sec % 60}s"
                    else:
                        h = total_sec // 3600
                        m = (total_sec % 3600) // 60
                        hold_time_str = f"{h}h {m}m"

                # Daily PnL (will be updated AFTER this block)
                daily_pnl_after = self.state.daily_pnl + update.pnl

                asyncio.create_task(
                    self._notify_close_safe(
                        symbol=symbol, pnl=update.pnl, close_price=close_price,
                        entry_price=entry_price, side=side_str, leverage=leverage_val,
                        close_type=close_type, hold_time_str=hold_time_str,
                        daily_pnl=daily_pnl_after, r_achieved=r_achieved,
                    )
                )

            # Accumulate daily PnL
            self.state.daily_pnl += update.pnl
            logger.info(f"📊 Daily PnL: ${self.state.daily_pnl:.2f}")

            # Track win/loss timestamp for cooldown logic
            if self._strategy:
                from datetime import datetime as dt
                # Record direction for adaptive nerfing
                direction = (order_info or {}).get('direction')
                if direction and update.pnl is not None:
                    dir_str = direction.value if hasattr(direction, 'value') else str(direction)
                    self._strategy.record_direction_trade(dir_str, update.pnl)
                if update.tp_triggered:
                    self._strategy._last_win_time[symbol] = dt.now()
                    self._strategy.adjust_risk_after_trade(is_win=True)
                    self._strategy._update_symbol_loss_streak(symbol, is_loss=False)
                elif update.sl_triggered:
                    self._strategy._last_loss_time[symbol] = dt.now()
                    self._strategy.adjust_risk_after_trade(is_win=False)
                    self._strategy._update_symbol_loss_streak(symbol, is_loss=True)

            # Record trade from exchange history (uses saved order_info)
            asyncio.create_task(
                self._record_closed_trade(symbol, order_info)
            )

            asyncio.create_task(self.sync_balance())

    def on_order(self, update: OrderUpdate) -> None:
        """Handle order update from WS."""
        if update.status == "FILLED":
            logger.debug(
                f"Order filled: {update.symbol} {update.side} "
                f"qty={update.filled_quantity} @ {update.avg_fill_price}"
            )

    async def _notify_close_safe(self, symbol: str, pnl: float, close_price: float,
                                 entry_price: float = 0, side: str = "",
                                 leverage: int = 0, close_type: str = "",
                                 hold_time_str: str = "", daily_pnl: float = 0,
                                 r_achieved: float = 0) -> None:
        """Send Telegram close notification (fire-and-forget)."""
        try:
            await self._telegram.notify_position_close(
                symbol=symbol, pnl=pnl, close_price=close_price,
                entry_price=entry_price, side=side, leverage=leverage,
                close_type=close_type, hold_time_str=hold_time_str,
                daily_pnl=daily_pnl, r_achieved=r_achieved,
            )
        except Exception as e:
            logger.debug(f"Telegram close notify failed: {e}")

    def _clear_position_state(self, state) -> None:
        """Clear all position-related state fields for a symbol."""
        # Remove from persistent storage
        pos_id = (state.current_order or {}).get("position_id")
        if pos_id:
            self.remove_open_position(pos_id)
        state.has_position = False
        state.active_fvg = None
        state.current_order = None
        state.trailing_state = "initial"
        state.partial_tp_done = False
        state.original_qty = 0.0
        state.trailing_sl_price = 0.0
        state._order_pending = False

    async def _determine_close_pnl(self, symbol: str, order_info: Optional[dict]) -> Optional[float]:
        """Try to fetch PnL from exchange history for a closed position."""
        try:
            pos_id = (order_info or {}).get("position_id")
            if not pos_id:
                return None
            history = await self.exchange.get_history_positions(symbol)
            for h in (history or []):  # search ALL, not [:5]
                if str(h.get("positionId")) == str(pos_id):
                    pnl = float(h.get("realizedPNL", h.get("realizedPnl", 0)))
                    return pnl
        except Exception as e:
            logger.debug(f"Failed to fetch PnL for {symbol}: {e}")
        return None

    def _set_cooldown_by_pnl(self, symbol: str, pnl: Optional[float], order_info: Optional[dict] = None) -> None:
        """Set win or loss cooldown based on actual PnL. Falls back to loss cooldown."""
        if not self._strategy:
            return
        from datetime import datetime as dt
        # Record direction for adaptive nerfing
        direction = (order_info or {}).get('direction')
        if direction and pnl is not None:
            dir_str = direction.value if hasattr(direction, 'value') else str(direction)
            self._strategy.record_direction_trade(dir_str, pnl)
        if pnl is not None and pnl >= 0:
            self._strategy._last_win_time[symbol] = dt.now()
            self._strategy.adjust_risk_after_trade(is_win=True)
            self._strategy._update_symbol_loss_streak(symbol, is_loss=False)
            logger.info(f"⏳ Win cooldown set for {symbol} (PnL=${pnl:.2f})")
        elif pnl is not None and pnl < 0:
            self._strategy._last_loss_time[symbol] = dt.now()
            self._strategy.adjust_risk_after_trade(is_win=False)
            self._strategy._update_symbol_loss_streak(symbol, is_loss=True)
            logger.info(f"⏳ Loss cooldown set for {symbol} (PnL=${pnl:.2f})")
        else:
            # pnl is None — unknown outcome, set loss cooldown but DON'T touch streak counter
            self._strategy._last_loss_time[symbol] = dt.now()
            self._strategy.adjust_risk_after_trade(is_win=False)
            logger.info(f"⏳ Loss cooldown set for {symbol} (PnL=unknown, streak unchanged)")

    async def _verify_and_close_position(self, symbol: str, state) -> None:
        """REST-verify that position is actually closed before clearing state.

        WS can send false qty=0 during TP/SL modifications. We confirm via
        REST API that the position no longer exists before acting on it.
        """
        try:
            await asyncio.sleep(1.0)  # Brief delay for exchange to settle
            
            # Guard: on_tpsl may have already handled this close during our sleep
            if not state.has_position:
                logger.debug(f"📭 [WS] {symbol} already processed by TP/SL path — skipping verify")
                return
            
            positions = await self.exchange.get_positions(symbol=symbol)
            if positions is None:
                logger.warning(f"⚠️ [WS] API unreachable — cannot verify {symbol} close, keeping state")
                return
            still_open = any(
                float(p.get("qty", 0)) > 0 for p in positions
            )
            if still_open:
                logger.info(
                    f"⚠️ [WS] False close for {symbol} — REST confirms "
                    f"position still open. Ignoring WS event."
                )
                return

            # Position is truly closed — clear state
            logger.info(f"📭 [WS+REST] Position confirmed closed: {symbol}")
            order_info = state.current_order
            self._clear_position_state(state)

            # Determine actual PnL and set appropriate cooldown
            pnl = await self._determine_close_pnl(symbol, order_info)
            self._set_cooldown_by_pnl(symbol, pnl, order_info)

            # Accumulate daily PnL if known
            if pnl is not None:
                self.state.daily_pnl += pnl

            # Record trade
            asyncio.create_task(
                self._record_closed_trade(symbol, order_info)
            )
            await self.sync_balance()

        except Exception as e:
            logger.error(f"REST verify failed for {symbol}: {e}")
            # Fallback: trust WS and clear state
            logger.info(f"📭 [WS] Position closed (fallback): {symbol}")
            order_info = state.current_order
            self._clear_position_state(state)

            # Fallback: assume loss (conservative)
            self._set_cooldown_by_pnl(symbol, None, order_info)

            asyncio.create_task(
                self._record_closed_trade(symbol, order_info)
            )
            asyncio.create_task(self.sync_balance())

    # ==================== Trade Recording ====================

    async def _record_closed_trade(
        self, symbol: str, order_info: Optional[dict]
    ) -> None:
        """Fetch closed position from exchange history and record it."""
        max_retries = 2
        for attempt in range(max_retries):
            try:
                pos_id = (order_info or {}).get("position_id")
                history = await self.exchange.get_history_positions(symbol)
                if not history:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2.0)
                        continue
                    logger.warning(f"No history returned for {symbol} after {max_retries} attempts")
                    return

                # Search by position_id if available
                if pos_id:
                    for h in history:  # search ALL, not [:5]
                        if str(h.get("positionId")) == str(pos_id):
                            self.trade_history.record_trade(h)
                            return
                    logger.warning(
                        f"Position {pos_id} not found in {len(history)} history items for {symbol}"
                    )
                else:
                    # No position_id — record any unknown trades for this symbol
                    logger.warning(f"No position_id for {symbol} — scanning history for unrecorded trades")
                    recorded = 0
                    for h in history:
                        pid = str(h.get("positionId", ""))
                        if pid and pid not in self.trade_history.known_position_ids:
                            self.trade_history.record_trade(h)
                            recorded += 1
                            if recorded >= 3:  # limit to avoid recording stale trades
                                break
                    if recorded:
                        logger.info(f"Recorded {recorded} orphan trade(s) for {symbol}")
                return
            except Exception as e:
                logger.warning(f"Failed to record trade for {symbol} (attempt {attempt+1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2.0)

    # ==================== Trade History Reconciliation ====================

    async def reconcile_trade_history(self) -> None:
        """Periodic fallback: fetch ALL closed positions from exchange
        and record any the bot missed (WS gap, restart, race condition).
        Called from the main loop every few minutes."""
        try:
            history = await self.exchange.get_history_positions()  # no symbol filter = all
            if not history:
                return
            recorded = 0
            for h in history:
                pid = str(h.get("positionId", ""))
                if pid and pid not in self.trade_history.known_position_ids:
                    self.trade_history.record_trade(h)
                    recorded += 1
            if recorded:
                logger.info(
                    f"🔄 [RECONCILE] Recorded {recorded} previously-unknown trade(s) "
                    f"from exchange history"
                )
        except Exception as e:
            logger.warning(f"Trade history reconciliation failed: {e}")

    # ==================== Balance ====================

    async def sync_balance(self) -> None:
        """Refresh balance from exchange."""
        try:
            balance = await self.exchange.get_balance()
            if balance is None:
                logger.warning("⚠️ Balance sync skipped: API unreachable")
                return
            self.state.balance = balance.equity
            self.state.available = balance.available
            logger.info(f"💰 Equity synced: ${balance.equity:.2f} (available: ${balance.available:.2f})")
        except Exception as e:
            logger.error(f"Failed to sync balance: {e}")
