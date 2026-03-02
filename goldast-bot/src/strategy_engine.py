"""
GoldasT Bot v2 - Strategy Engine
FVG/IFVG detection, entry condition checking, and trade execution.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, TYPE_CHECKING

from .models import FVG, SymbolState, BotState, TradeDirection, OrderState
from . import fmt_price
from .fvg_detector import FVGDetector
from .tpsl_calculator import TPSLCalculator
from .position_sizer import PositionSizer
from .order_state_machine import OrderManager
from .exchange_adapter import ExchangeAdapter
from .websocket_handler import WebSocketHandler, KlineMessage
from .error_recovery import ResilientExecutor, CircuitBreakerOpen
from .config import Config


logger = logging.getLogger(__name__)

# Higher-timeframe trend states (derived from continuous score for logging)
HTF_UPTREND = "uptrend"      # score > +threshold
HTF_DOWNTREND = "downtrend"  # score < -threshold
HTF_RANGING = "ranging"      # |score| <= threshold


class StrategyEngine:
    """
    Handles all FVG/IFVG strategy logic:
    - Kline callback routing (live ticks + closed candles)
    - FVG detection on candle close
    - Live entry checking against active FVGs
    - Trade execution (market order → fill confirmation → TP/SL)
    - 1h trend filter to block counter-trend entries
    """

    def __init__(
        self,
        config: Config,
        exchange: ExchangeAdapter,
        ws_handler: WebSocketHandler,
        fvg_detector: FVGDetector,
        tpsl_calculator: TPSLCalculator,
        position_sizer: PositionSizer,
        order_manager: OrderManager,
        executor: ResilientExecutor,
        state: BotState,
        symbol_states: Dict[str, SymbolState],
        signal_tracker=None,
    ):
        self.config = config
        self.exchange = exchange
        self.signal_tracker = signal_tracker  # Optional[SignalTracker]
        self.ws_handler = ws_handler
        self.fvg_detector = fvg_detector
        self.tpsl_calculator = tpsl_calculator
        self.position_sizer = position_sizer
        self.order_manager = order_manager
        self.executor = executor
        self.state = state
        self.symbol_states = symbol_states

        # Higher-timeframe trend: {symbol: "uptrend"/"downtrend"/"ranging"}
        self._htf_trend: Dict[str, str] = {}   # 1h trend label (derived from score)
        self._mtf_trend: Dict[str, str] = {}   # 15m trend label (derived from score)
        # Continuous trend scores: {symbol: float} — range [-1.0, +1.0]
        self._score_15m: Dict[str, float] = {}  # 15m trend score
        self._score_1h: Dict[str, float] = {}   # 1h trend score
        self._htf_last_refresh: Optional[datetime] = None

        # 15m RSI cache: {symbol: rsi_value}
        self._rsi_cache: Dict[str, float] = {}

        # Per-symbol last loss timestamp for post-loss cooldown
        self._last_loss_time: Dict[str, datetime] = {}
        # Per-symbol last win timestamp for win-based cooldown
        self._last_win_time: Dict[str, datetime] = {}

        # === Zone Cooldown: spent zones that hit SL/BE ===
        # {symbol: [(bottom, top, direction_str, timestamp), ...]}
        self._spent_zones: Dict[str, list] = {}

        # === Global Entry Burst Limiter ===
        self._last_global_entry_time: Optional[datetime] = None
        
        # Dynamic risk sizing (Pine Script: adjust ±10% after each win/loss)
        self._current_risk_percent: float = config.position.risk_percent  # Start at config default
        self._base_risk_percent: float = config.position.risk_percent
        self._min_risk_percent: float = max(0.02, config.position.risk_percent * config.position.risk_min_multiplier)
        self._max_risk_percent: float = config.position.risk_percent * config.position.risk_max_multiplier
        self._risk_adjustment_rate: float = config.position.risk_adjustment_rate
        
        # Throttling log for entry checks
        self._last_entry_check_log: Dict[str, datetime] = {}
        
        # Per-symbol lock to prevent concurrent trailing API calls
        self._trailing_lock: Dict[str, bool] = {}
        
        # === Adaptive Direction Nerfing ===
        # Track recent trade PnL per direction (LONG/SHORT) globally
        # Format: deque of (timestamp, pnl_float)
        from collections import deque
        self._direction_trades: Dict[str, deque] = {
            "LONG": deque(maxlen=10),   # Last 10 LONG trades
            "SHORT": deque(maxlen=10),  # Last 10 SHORT trades
        }
        # When penalty was activated (per direction) — for timeout
        self._direction_penalty_start: Dict[str, Optional[datetime]] = {
            "LONG": None,
            "SHORT": None,
        }
        
        # Telegram bot reference (set after construction via set_telegram())
        self._telegram = None
        
        # Per-symbol consecutive loss counter
        self._symbol_consecutive_losses: Dict[str, int] = {}
        self._symbol_loss_ban_until: Dict[str, datetime] = {}

        # Per-symbol entry time tracker (for max_entries_per_symbol limit)
        self._symbol_entry_times: Dict[str, list] = {}  # {symbol: [datetime, ...]}

    def set_telegram(self, telegram_bot) -> None:
        """Set Telegram bot reference for trade notifications."""
        self._telegram = telegram_bot

    # ==================== Zone Cooldown ====================

    def record_spent_zone(self, symbol: str, fvg_bottom: float, fvg_top: float,
                          direction: str, close_type: str) -> None:
        """Record a zone that hit SL or BE — don't re-enter it.
        
        Called from position_manager when a trade closes by SL or BE.
        TP winners are NOT recorded (successful zones can be re-used).
        """
        if close_type not in ("sl", "be"):
            return
        if symbol not in self._spent_zones:
            self._spent_zones[symbol] = []
        self._spent_zones[symbol].append(
            (fvg_bottom, fvg_top, direction, datetime.now())
        )
        logger.info(
            f"🔒 Zone spent: {symbol} {direction} "
            f"zone={fmt_price(fvg_bottom)}-{fmt_price(fvg_top)} "
            f"({close_type.upper()}) — cooldown {self.config.cooldowns.zone_cooldown_seconds}s"
        )

    def _is_zone_spent(self, symbol: str, fvg) -> bool:
        """Check if an FVG overlaps with a recently-spent zone.
        
        Returns True if the zone should be skipped (>50% overlap with a
        spent zone of the same direction).
        """
        spent = self._spent_zones.get(symbol, [])
        if not spent:
            return False

        cooldown = self.config.cooldowns.zone_cooldown_seconds
        overlap_threshold = self.config.cooldowns.zone_overlap_threshold
        now = datetime.now()
        fvg_dir = fvg.direction.value if hasattr(fvg.direction, 'value') else str(fvg.direction)

        # Clean expired entries while checking
        active_spent = []
        for (bottom, top, direction, ts) in spent:
            if (now - ts).total_seconds() < cooldown:
                active_spent.append((bottom, top, direction, ts))
                # Check overlap only for same direction
                if direction != fvg_dir:
                    continue
                # Calculate overlap ratio
                overlap_start = max(fvg.bottom, bottom)
                overlap_end = min(fvg.top, top)
                if overlap_end > overlap_start:
                    overlap_size = overlap_end - overlap_start
                    zone_size = fvg.top - fvg.bottom
                    if zone_size > 0 and (overlap_size / zone_size) >= overlap_threshold:
                        remaining = cooldown - (now - ts).total_seconds()
                        logger.info(
                            f"🚫 Zone cooldown: {symbol} {fvg_dir} "
                            f"zone={fmt_price(fvg.bottom)}-{fmt_price(fvg.top)} overlaps "
                            f"spent zone={fmt_price(bottom)}-{fmt_price(top)} "
                            f"({remaining:.0f}s remaining)"
                        )
                        self._spent_zones[symbol] = active_spent
                        return True

        # Update with only non-expired entries
        self._spent_zones[symbol] = active_spent
        return False

    def _update_symbol_loss_streak(self, symbol: str, is_loss: bool) -> None:
        """Track consecutive losses per symbol. Ban symbol after N consecutive losses.
        
        Core symbols get a shorter ban (core_symbol_loss_ban_seconds).
        Rotation symbols get a longer ban (symbol_loss_ban_seconds).
        """
        if is_loss:
            self._symbol_consecutive_losses[symbol] = self._symbol_consecutive_losses.get(symbol, 0) + 1
            max_losses = self.config.risk.symbol_max_consecutive_losses
            if self._symbol_consecutive_losses[symbol] >= max_losses:
                core_symbols = set(self.config.core_symbols)
                if symbol in core_symbols:
                    ban_s = self.config.risk.core_symbol_loss_ban_seconds
                else:
                    ban_s = self.config.risk.symbol_loss_ban_seconds
                self._symbol_loss_ban_until[symbol] = datetime.now() + timedelta(seconds=ban_s)
                label = "CORE" if symbol in core_symbols else "ROTATION"
                logger.warning(
                    f"🚫 Symbol {symbol} [{label}] BANNED for {ban_s//60}min — "
                    f"{self._symbol_consecutive_losses[symbol]} consecutive losses"
                )
        else:
            # Win resets the streak
            self._symbol_consecutive_losses[symbol] = 0
            if symbol in self._symbol_loss_ban_until:
                del self._symbol_loss_ban_until[symbol]

    # ==================== Higher-TF Trend Filter ====================

    def adjust_risk_after_trade(self, is_win: bool) -> None:
        """Adjust dynamic risk percent after a trade closes.
        
        Uses additive model: +step on win, -step on loss.
        Additive is symmetric: win+loss cycle returns to the same value,
        unlike multiplicative which ratchets down over time.
        """
        old_risk = self._current_risk_percent
        step = self._base_risk_percent * self._risk_adjustment_rate  # e.g. 0.01 * 0.10 = 0.001
        if is_win:
            self._current_risk_percent = min(
                self._current_risk_percent + step, self._max_risk_percent
            )
        else:
            self._current_risk_percent = max(
                self._current_risk_percent - step, self._min_risk_percent
            )
        logger.info(
            f"📊 Dynamic risk: {'WIN' if is_win else 'LOSS'} → "
            f"risk {old_risk*100:.2f}% → {self._current_risk_percent*100:.2f}%"
        )

    def record_direction_trade(self, direction: str, pnl: float) -> None:
        """Record a trade result for adaptive direction nerfing.
        
        Called from position_manager when a trade closes.
        direction: "LONG" or "SHORT"
        pnl: realized PnL (positive = win, negative = loss)
        """
        dir_key = direction.upper()
        if dir_key not in self._direction_trades:
            return
        self._direction_trades[dir_key].append((datetime.now(), pnl))
        
        # Check if penalty should activate or deactivate
        trades = list(self._direction_trades[dir_key])
        if pnl > 0:
            # A win clears the penalty for this direction
            if self._direction_penalty_start[dir_key] is not None:
                logger.info(
                    f"✅ Direction penalty CLEARED for {dir_key} — "
                    f"win trade (PnL=${pnl:+.2f})"
                )
                self._direction_penalty_start[dir_key] = None
        else:
            # Check for consecutive losses
            threshold = self.config.risk.direction_nerf_consecutive
            recent = trades[-threshold:]
            if (len(recent) >= threshold
                    and all(t[1] < 0 for t in recent)):
                if self._direction_penalty_start[dir_key] is None:
                    self._direction_penalty_start[dir_key] = datetime.now()
                    total_loss = sum(t[1] for t in recent)
                    mult = self.config.risk.direction_nerf_multiplier
                    logger.warning(
                        f"⚠️ Direction penalty ACTIVATED for {dir_key} — "
                        f"{threshold} consecutive losses "
                        f"(total=${total_loss:+.2f}), threshold ×{mult}"
                    )

    def _is_direction_nerfed(self, direction: str) -> tuple:
        """Check if a direction is currently penalized.
        
        Returns: (is_nerfed: bool, penalty_multiplier: float)
        """
        dir_key = direction.upper()
        penalty_start = self._direction_penalty_start.get(dir_key)
        if penalty_start is None:
            return False, 1.0
        
        # Check timeout
        elapsed = (datetime.now() - penalty_start).total_seconds()
        duration = self.config.risk.direction_nerf_duration_seconds
        if elapsed > duration:
            logger.info(
                f"⏰ Direction penalty EXPIRED for {dir_key} — "
                f"{elapsed:.0f}s > {duration}s timeout"
            )
            self._direction_penalty_start[dir_key] = None
            return False, 1.0
        
        remaining = duration - elapsed
        return True, self.config.risk.direction_nerf_multiplier

    def _update_15m_score_from_buffer(self, symbol: str) -> None:
        """Recompute 15m trend score from WS candle buffer on candle close.
        
        This gives real-time trend detection at each 15m boundary,
        instead of waiting for the periodic 15min HTTP refresh.
        """
        candles = self.ws_handler.get_candle_buffer(symbol)
        if not candles or len(candles) < 26:  # Need at least ema_slow + 5
            return
        
        trend_cfg = self.config.trend
        score_15m = self._compute_trend_score(
            candles, trend_cfg.ema_fast, trend_cfg.ema_slow
        )
        old_score = self._score_15m.get(symbol, 0.0)
        self._score_15m[symbol] = score_15m
        self._mtf_trend[symbol] = self._score_to_label(
            score_15m, trend_cfg.entry_threshold
        )
        
        # Log if score changed significantly (>0.05)
        if abs(score_15m - old_score) > 0.05:
            score_1h = self._score_1h.get(symbol, 0.0)
            combined = trend_cfg.weight_15m * score_15m + trend_cfg.weight_1h * score_1h
            logger.info(
                f"📐 RT trend update {symbol}: 15m={old_score:+.2f}→{score_15m:+.2f} | "
                f"combined={combined:+.2f} ({self._score_to_label(combined, trend_cfg.entry_threshold)})"
            )

    @staticmethod
    def _ema(values: list, period: int) -> float:
        """Calculate EMA of a list of values."""
        if len(values) < period:
            return sum(values) / len(values) if values else 0.0
        k = 2 / (period + 1)
        ema = sum(values[:period]) / period  # SMA seed
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
        return ema

    @staticmethod
    def _rsi(closes: list, period: int = 14) -> float:
        """Calculate RSI from a list of closing prices."""
        if len(closes) < period + 1:
            return 50.0  # neutral default
        gains, losses = [], []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))
        recent_gains = gains[-period:]
        recent_losses = losses[-period:]
        avg_gain = sum(recent_gains) / period
        avg_loss = sum(recent_losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _compute_trend(closes: list) -> str:
        """Compute trend from closing prices using EMA8/EMA21 (legacy label)."""
        if len(closes) < 21:
            return HTF_RANGING
        ema8 = StrategyEngine._ema(closes, 8)
        ema21 = StrategyEngine._ema(closes, 21)
        price = closes[-1]
        if price > ema8 > ema21:
            return HTF_UPTREND
        elif price < ema8 < ema21:
            return HTF_DOWNTREND
        return HTF_RANGING

    def _compute_trend_score(self, candles_list, ema_fast: int = 8, ema_slow: int = 21) -> float:
        """Compute continuous trend score from -1.0 to +1.0.

        Components (ATR-normalized, symmetric for both directions):
        1. EMA Alignment: (EMA_fast - EMA_slow) / ATR × 0.50
           → Positive when fast EMA above slow (bullish structure)
           → Zero at crossover = trend change point
        2. Price Momentum: (price - EMA_fast) / ATR × 0.30
           → Shows immediate momentum direction
        3. EMA Slope: (EMA_fast_now - EMA_fast_5bars_ago) / ATR × 0.20
           → Catches reversals early (acceleration/deceleration)

        Returns: score clamped to [-1.0, +1.0]
        Positive = bullish, Negative = bearish
        """
        min_bars = ema_slow + 5
        if len(candles_list) < min_bars:
            return 0.0

        closes = [c.close for c in candles_list]
        price = closes[-1]

        # ATR for normalization (prevents different volatility symbols from skewing)
        from .tpsl_calculator import TPSLCalculator
        atr = TPSLCalculator.calculate_atr(candles_list, period=self.config.trend.atr_period)
        if atr <= 0:
            return 0.0

        # Component 1: EMA alignment — trend structure
        ema_f = self._ema(closes, ema_fast)
        ema_s = self._ema(closes, ema_slow)
        alignment = (ema_f - ema_s) / atr

        # Component 2: Price vs fast EMA — immediate momentum
        momentum = (price - ema_f) / atr

        # Component 3: EMA slope — trend acceleration
        lookback = min(5, len(closes) - ema_fast)
        if lookback > 0:
            closes_prev = closes[:-lookback]
            ema_f_prev = self._ema(closes_prev, ema_fast) if len(closes_prev) >= ema_fast else ema_f
            slope = (ema_f - ema_f_prev) / atr
        else:
            slope = 0.0

        # Weighted combination (weights from config)
        trend_cfg = self.config.trend
        raw_score = trend_cfg.score_weight_alignment * alignment + trend_cfg.score_weight_momentum * momentum + trend_cfg.score_weight_slope * slope

        # Clamp to [-1.0, +1.0]
        return max(-1.0, min(1.0, raw_score))

    @staticmethod
    def _score_to_label(score: float, threshold: float = 0.25, strong_multiplier: float = 2.0) -> str:
        """Convert continuous trend score to human-readable label."""
        if score > threshold * strong_multiplier:
            return "STRONG_UP"
        elif score > threshold:
            return "UPTREND"
        elif score < -threshold * strong_multiplier:
            return "STRONG_DN"
        elif score < -threshold:
            return "DOWNTREND"
        return "RANGING"

    async def refresh_htf_trends(self) -> None:
        """Fetch 1h + 15m klines and compute trend scores for each symbol.

        Symmetric trend scoring system:
        - Continuous score [-1.0, +1.0] per timeframe
        - Components: EMA alignment + price momentum + EMA slope (ATR-normalized)
        - Combined score = weight_15m × score_15m + weight_1h × score_1h
        - LONG: combined > +threshold, SHORT: combined < -threshold
        """
        trend_cfg = self.config.trend
        ema_fast = trend_cfg.ema_fast
        ema_slow = trend_cfg.ema_slow
        threshold = trend_cfg.entry_threshold

        for symbol in self.symbol_states:
            # --- 1h score ---
            try:
                candles_1h = await self.exchange.get_historical_candles(
                    symbol=symbol, limit=100, interval="1h",
                )
                score_1h = self._compute_trend_score(candles_1h, ema_fast, ema_slow)
                self._score_1h[symbol] = score_1h
                self._htf_trend[symbol] = self._score_to_label(score_1h, threshold)
            except Exception as e:
                logger.warning(f"1h trend fetch failed for {symbol}: {e}")
                self._score_1h.setdefault(symbol, 0.0)
                score_1h = self._score_1h.get(symbol, 0.0)
                self._htf_trend.setdefault(symbol, HTF_RANGING)

            # --- 15m score ---
            try:
                candles_15m = await self.exchange.get_historical_candles(
                    symbol=symbol, limit=30, interval="15m",
                )
                score_15m = self._compute_trend_score(candles_15m, ema_fast, ema_slow)
                self._score_15m[symbol] = score_15m
                self._mtf_trend[symbol] = self._score_to_label(score_15m, threshold)
            except Exception as e:
                logger.warning(f"15m trend fetch failed for {symbol}: {e}")
                self._score_15m.setdefault(symbol, 0.0)
                score_15m = self._score_15m.get(symbol, 0.0)
                self._mtf_trend.setdefault(symbol, HTF_RANGING)

            # Combined score
            combined = trend_cfg.weight_15m * score_15m + trend_cfg.weight_1h * score_1h
            combined_label = self._score_to_label(combined, threshold)

            logger.info(
                f"📐 Trend {symbol}: 15m={score_15m:+.2f} ({self._score_to_label(score_15m, threshold)}) | "
                f"1h={score_1h:+.2f} ({self._score_to_label(score_1h, threshold)}) | "
                f"combined={combined:+.2f} ({combined_label})"
            )

        # Refresh 15m RSI for each symbol
        for symbol in self.symbol_states:
            candles = self.ws_handler.get_candle_buffer(symbol)
            if candles and len(candles) >= 15:
                closes_15m = [c.close for c in candles[-30:]]
                self._rsi_cache[symbol] = self._rsi(closes_15m, 14)

        self._htf_last_refresh = datetime.now()

    def _get_trend_info(self, symbol: str) -> str:
        """Get formatted trend info string for logging."""
        s15 = self._score_15m.get(symbol, 0.0)
        s1h = self._score_1h.get(symbol, 0.0)
        threshold = self.config.trend.entry_threshold
        combined = self.config.trend.weight_15m * s15 + self.config.trend.weight_1h * s1h
        return f"15m:{s15:+.2f} 1h:{s1h:+.2f} comb:{combined:+.2f}({self._score_to_label(combined, threshold)})"

    # ==================== Kline Callback ====================

    def on_kline(self, symbol: str, msg: KlineMessage) -> None:
        """Handle kline update from WebSocket.

        Called for every WS kline push (~500ms).
        - Live ticks (is_closed=False): update last_price, check active FVG entry
        - Closed candles (is_closed=True): add to buffer, detect new FVGs
        """
        state = self.symbol_states.get(symbol)
        if state:
            state.last_price = msg.close
            state.last_update = datetime.now()

        if msg.is_closed:
            logger.info(
                f"🕯️ Candle closed: {symbol} "
                f"O={fmt_price(msg.open)} H={fmt_price(msg.high)} L={fmt_price(msg.low)} "
                f"C={fmt_price(msg.close)} V={msg.volume:.1f}"
            )
            candle = msg.to_candle()
            self.ws_handler.add_candle(symbol, candle)

            # Refresh RSI cache (used by HTF trend analysis)
            candles = self.ws_handler.get_candle_buffer(symbol)
            if candles and len(candles) >= 15:
                closes_15m = [c.close for c in candles[-30:]]
                self._rsi_cache[symbol] = self._rsi(closes_15m, 14)

            # Real-time 15m trend score update from WS buffer
            self._update_15m_score_from_buffer(symbol)

            asyncio.create_task(self._detect_fvg_on_close(symbol))
        else:
            if state and state.active_fvg and not state.has_position:
                asyncio.create_task(self._check_live_entry(symbol, msg.close))
                # Mid-tick FVG rescan: if zone is far from price, try to find closer one
                # Throttled: max once per 5 min per symbol to avoid expire-rescan loop
                fvg = state.active_fvg
                if fvg and fvg.fill_percent == 0.0:
                    dist = abs(msg.close - fvg.mid_price) / msg.close
                    if dist > self.config.trend.trend_rescan_distance:  # far away → maybe rescan
                        now = datetime.now()
                        last = self._last_entry_check_log.get(f"{symbol}_rescan")
                        if not last or (now - last).total_seconds() > 300:
                            self._last_entry_check_log[f"{symbol}_rescan"] = now
                            asyncio.create_task(self._detect_fvg_on_close(symbol))
            # Check trailing SL / partial TP on every tick for open positions
            elif state and state.has_position and state.current_order:
                asyncio.create_task(self._check_trailing_on_tick(symbol, state))

    # ==================== FVG Detection ====================

    async def _detect_fvg_on_close(self, symbol: str) -> None:
        """Detect new FVGs when a candle closes.
        
        Strategy:
        1. Expire stale FVGs when price moves too far from zone
        2. Check last 3 candles for a fresh FVG → aggressive immediate entry
        3. If none found, scan all candles with sliding window
        4. Replace active FVG if new one is closer to current price
        """
        candles = self.ws_handler.get_candle_buffer(symbol)
        if len(candles) < 3:
            return

        state = self.symbol_states.get(symbol)
        if not state:
            return

        current_price = state.last_price or candles[-1].close

        # --- Expire stale FVGs (price moved too far from zone) ---
        max_zone_distance = self.config.fvg.max_zone_distance
        old_fvg = state.active_fvg
        if old_fvg and not old_fvg.entry_triggered:
            if old_fvg.direction == TradeDirection.LONG:
                dist = (current_price - old_fvg.top) / old_fvg.top if current_price > old_fvg.top else 0
            else:
                dist = (old_fvg.bottom - current_price) / old_fvg.bottom if current_price < old_fvg.bottom else 0
            if dist > max_zone_distance:
                # Don't log every expire to reduce spam (mid-tick rescan causes repeats)
                now = datetime.now()
                last_expire_log = self._last_entry_check_log.get(f"{symbol}_expire")
                if not last_expire_log or (now - last_expire_log).total_seconds() > 300:
                    logger.info(
                        f"🗑️ Expired stale FVG: {symbol} {old_fvg.direction.value} "
                        f"zone={fmt_price(old_fvg.bottom)}-{fmt_price(old_fvg.top)} | "
                        f"price={fmt_price(current_price)} dist={dist*100:.2f}%"
                    )
                    self._last_entry_check_log[f"{symbol}_expire"] = now
                state.active_fvg = None
                old_fvg = None

        # --- Always scan for FVGs (no skip-scan; stale zones were the #1 trade killer) ---
        fvg = self.fvg_detector.detect_fvg(candles, symbol)
        if not fvg and len(candles) >= 5:
            fvg = self.fvg_detector.detect_fvg_sliding_window(
                candles, symbol, current_price
            )

        # Fallback: Order Block detection
        if not fvg and len(candles) >= 13:
            fvg = self.fvg_detector.detect_order_blocks(
                candles, symbol, current_price, pivot_len=6
            )

        if fvg:
            # --- Skip spent zones (recently hit SL/BE — zone cooldown) ---
            if self._is_zone_spent(symbol, fvg):
                fvg = None  # Treat as no FVG found

        if fvg:
            # --- Replace only if new FVG is closer to price or old one is gone ---
            should_replace = True
            if old_fvg and not old_fvg.entry_triggered and not old_fvg.is_violated:
                old_dist = abs(current_price - old_fvg.mid_price) / current_price
                new_dist = abs(current_price - fvg.mid_price) / current_price
                if new_dist >= old_dist:
                    should_replace = False  # keep old FVG, it's closer

            if should_replace:
                trend_info = self._get_trend_info(symbol)
                replaced_tag = ""
                if old_fvg and old_fvg != fvg:
                    old_dist_pct = abs(current_price - old_fvg.mid_price) / current_price * 100
                    new_dist_pct = abs(current_price - fvg.mid_price) / current_price * 100
                    replaced_tag = f" (replaced old {old_fvg.direction.value} dist={old_dist_pct:.2f}% → {new_dist_pct:.2f}%)"
                state.active_fvg = fvg
                logger.info(
                    f"🔍 FVG detected on {symbol}: {fvg.fvg_type.value} "
                    f"{fvg.direction.value} zone={fmt_price(fvg.bottom)}-{fmt_price(fvg.top)} "
                    f"strength={fvg.strength:.2f} gap={fvg.gap_percent:.4f}% "
                    f"vol_ratio={fvg.volume_ratio:.2f} [{trend_info}]{replaced_tag}"
                )
        elif not old_fvg:
            c1, c2, c3 = candles[-3], candles[-2], candles[-1]
            bull_gap = c3.low - c1.high
            bear_gap = c1.low - c3.high
            logger.info(
                f"📊 {symbol} scan: no FVG | "
                f"bull_gap={bull_gap:.4f} bear_gap={bear_gap:.4f}"
            )

    # ==================== Entry Logic ====================

    async def _check_live_entry(self, symbol: str, current_price: float) -> None:
        """Check if live price triggers entry into an active FVG."""
        state = self.symbol_states.get(symbol)
        if not state or not state.active_fvg:
            return

        fvg = state.active_fvg

        if state.has_position:
            return
        machine = self.order_manager.get_machine(symbol)
        if machine and machine.ctx.state not in (OrderState.IDLE,):
            return

        should_enter, reason = self.fvg_detector.check_entry_conditions(
            fvg, current_price
        )

        if not should_enter:
            # Throttle info logs to show user activity without spam (log every 60s)
            now = datetime.now()
            last_log = self._last_entry_check_log.get(symbol)
            if not last_log or (now - last_log).total_seconds() > 60:
                # Add distance context if fill is 0
                msg = f"{reason}"
                if fvg.fill_percent == 0.0:
                    dist = 0.0
                    if fvg.direction == TradeDirection.LONG:
                        dist = (current_price - fvg.top) / fvg.top * 100
                        msg = f"Price {fmt_price(current_price)} above zone top {fmt_price(fvg.top)} (Dist: {dist:.2f}%)"
                    else:
                        dist = (fvg.bottom - current_price) / fvg.bottom * 100
                        msg = f"Price {fmt_price(current_price)} below zone bottom {fmt_price(fvg.bottom)} (Dist: {dist:.2f}%)"
                
                logger.info(f"⏳ {symbol} waiting: {msg}")
                self._last_entry_check_log[symbol] = now
            return

        if should_enter:
            # Track zone hit (price entered FVG zone — signal opportunity)
            if self.signal_tracker:
                self.signal_tracker.record_zone_hit(symbol)

            # === FILTER -1: ICT Killzone Session Filter ===
            if self.config.session.enabled:
                is_active, zone_name = self.config.session.is_killzone_now()
                if not is_active:
                    now = datetime.now()
                    last_log = self._last_entry_check_log.get("_session")
                    if not last_log or (now - last_log).total_seconds() > 300:
                        logger.info(
                            f"🚫 Session filter: {symbol} {fvg.direction.value} blocked — "
                            f"{zone_name} (outside killzones)"
                        )
                        self._last_entry_check_log["_session"] = now
                    return

            # === FILTER 0: Direction filter (SHORT-only / LONG-only / BOTH) ===
            allowed_dir = self.config.fvg.allowed_direction.upper()
            if allowed_dir != 'BOTH':
                if allowed_dir == 'SHORT' and fvg.direction == TradeDirection.LONG:
                    now = datetime.now()
                    last_log = self._last_entry_check_log.get(f"{symbol}_dirfilter")
                    if not last_log or (now - last_log).total_seconds() > 120:
                        logger.info(f"🚫 Direction filter: {symbol} LONG blocked — SHORT-only mode")
                        self._last_entry_check_log[f"{symbol}_dirfilter"] = now
                    return
                if allowed_dir == 'LONG' and fvg.direction == TradeDirection.SHORT:
                    now = datetime.now()
                    last_log = self._last_entry_check_log.get(f"{symbol}_dirfilter")
                    if not last_log or (now - last_log).total_seconds() > 120:
                        logger.info(f"🚫 Direction filter: {symbol} SHORT blocked — LONG-only mode")
                        self._last_entry_check_log[f"{symbol}_dirfilter"] = now
                    return

            # === FILTER 1: Daily loss limit (safety net) ===
            max_daily_loss = self.state.balance * self.config.risk.max_daily_loss_percent / 100
            if self.state.daily_pnl < 0 and abs(self.state.daily_pnl) >= max_daily_loss:
                now = datetime.now()
                last_log = self._last_entry_check_log.get("_dailyloss")
                if not last_log or (now - last_log).total_seconds() > 300:
                    logger.warning(
                        f"🚫 Daily loss limit: daily PnL=${self.state.daily_pnl:.2f} "
                        f"exceeds -{max_daily_loss:.2f} — all entries blocked"
                    )
                    self._last_entry_check_log["_dailyloss"] = now
                return

            # === FILTER 2: Entry cooldown (shorter after win, normal otherwise) ===
            last_win = self._last_win_time.get(symbol)
            win_cooldown = self.config.cooldowns.win_cooldown_seconds
            entry_cooldown = self.config.cooldowns.entry_cooldown_seconds
            # Use shorter cooldown if last trade on this symbol was a win
            if last_win and state.last_entry_time and last_win >= state.last_entry_time:
                effective_cooldown = win_cooldown
            else:
                effective_cooldown = entry_cooldown
            if state.last_entry_time and effective_cooldown > 0:
                elapsed = (datetime.now() - state.last_entry_time).total_seconds()
                if elapsed < effective_cooldown:
                    return

            # === FILTER 3: Post-loss cooldown (15 min pause after SL hit) ===
            loss_cooldown = self.config.cooldowns.loss_cooldown_seconds
            last_loss = self._last_loss_time.get(symbol)
            if last_loss and loss_cooldown > 0:
                elapsed = (datetime.now() - last_loss).total_seconds()
                if elapsed < loss_cooldown:
                    remaining = loss_cooldown - elapsed
                    now = datetime.now()
                    last_log = self._last_entry_check_log.get(f"{symbol}_losscool")
                    if not last_log or (now - last_log).total_seconds() > 120:
                        logger.info(
                            f"🚫 SL cooldown: {symbol} — {remaining:.0f}s remaining "
                            f"(loss at {last_loss.strftime('%H:%M:%S')}, cooldown={loss_cooldown}s)"
                        )
                        self._last_entry_check_log[f"{symbol}_losscool"] = now
                    return

            # === FILTER 3.1: Zone Cooldown (don't re-enter spent zones) ===
            if self._is_zone_spent(symbol, fvg):
                return  # Log already emitted by _is_zone_spent()

            # === FILTER 3.2: Global Entry Burst Limiter ===
            global_cd = self.config.cooldowns.global_entry_cooldown_seconds
            if global_cd > 0 and self._last_global_entry_time:
                elapsed = (datetime.now() - self._last_global_entry_time).total_seconds()
                if elapsed < global_cd:
                    remaining = global_cd - elapsed
                    now = datetime.now()
                    last_log = self._last_entry_check_log.get("_globalcd")
                    if not last_log or (now - last_log).total_seconds() > 60:
                        logger.info(
                            f"🚫 Global burst cooldown: {symbol} — {remaining:.0f}s remaining "
                            f"(last entry {elapsed:.0f}s ago, limit={global_cd}s)"
                        )
                        self._last_entry_check_log["_globalcd"] = now
                    return

            # === FILTER 3.5: Max total positions (hard cap on exposure) ===
            max_positions = self.config.multi_symbol.max_concurrent_positions
            total_positions = sum(
                1 for s, st in self.symbol_states.items()
                if st.has_position
            )
            if total_positions >= max_positions:
                now = datetime.now()
                last_log = self._last_entry_check_log.get("_maxpos")
                if not last_log or (now - last_log).total_seconds() > 120:
                    logger.info(
                        f"🚫 Max positions: {total_positions}/{max_positions} "
                        f"— skipping {symbol}"
                    )
                    self._last_entry_check_log["_maxpos"] = now
                return

            # === FILTER 3.6: Per-symbol consecutive loss ban ===
            ban_until = self._symbol_loss_ban_until.get(symbol)
            if ban_until and datetime.now() < ban_until:
                now = datetime.now()
                last_log = self._last_entry_check_log.get(f"{symbol}_symban")
                if not last_log or (now - last_log).total_seconds() > 300:
                    remaining = (ban_until - now).total_seconds() / 60
                    losses = self._symbol_consecutive_losses.get(symbol, 0)
                    logger.info(
                        f"🚫 Symbol ban: {symbol} — {losses} consecutive losses, "
                        f"banned for {remaining:.0f}min more"
                    )
                    self._last_entry_check_log[f"{symbol}_symban"] = now
                return

            # === FILTER 3.7: Per-symbol entry limit (max N entries per window) ===
            max_entries = self.config.cooldowns.max_entries_per_symbol
            window_hours = self.config.cooldowns.max_entries_window_hours
            if max_entries > 0:
                now = datetime.now()
                cutoff = now - timedelta(hours=window_hours)
                # Clean old entries and count recent ones
                recent = [t for t in self._symbol_entry_times.get(symbol, []) if t > cutoff]
                self._symbol_entry_times[symbol] = recent
                if len(recent) >= max_entries:
                    last_log = self._last_entry_check_log.get(f"{symbol}_entrylimit")
                    if not last_log or (now - last_log).total_seconds() > 300:
                        logger.info(
                            f"🚫 Entry limit: {symbol} — {len(recent)}/{max_entries} entries "
                            f"in last {window_hours}h — blocking"
                        )
                        self._last_entry_check_log[f"{symbol}_entrylimit"] = now
                    return

            # === FILTER 4: Max same-direction exposure (correlated risk cap) ===
            max_same = self.config.multi_symbol.max_same_direction
            same_dir_count = sum(
                1 for s, st in self.symbol_states.items()
                if st.has_position and st.current_order
                and st.current_order.get('direction') == fvg.direction
            )
            if same_dir_count >= max_same:
                now = datetime.now()
                last_log = self._last_entry_check_log.get(f"_dircap_{fvg.direction.value}")
                if not last_log or (now - last_log).total_seconds() > 120:
                    # Log once per 2min per direction instead of every tick
                    blocked_syms = [
                        s for s, st in self.symbol_states.items()
                        if not st.has_position and st.active_fvg
                        and st.active_fvg.direction == fvg.direction
                    ]
                    logger.info(
                        f"🚫 Same-direction cap: {same_dir_count}/{max_same} "
                        f"{fvg.direction.value} positions — blocking {len(blocked_syms)} symbols: "
                        f"{', '.join(blocked_syms[:6])}"
                    )
                    self._last_entry_check_log[f"_dircap_{fvg.direction.value}"] = now
                return

            # === FILTER 5: Symmetric trend score filter + Adaptive Direction Penalty ===
            # Combined score = weight_15m × score_15m + weight_1h × score_1h
            # LONG: combined > +threshold, SHORT: combined < -threshold
            # If direction is nerfed (consecutive losses), multiply threshold
            trend_cfg = self.config.trend
            score_15m = self._score_15m.get(symbol, 0.0)
            score_1h = self._score_1h.get(symbol, 0.0)
            combined_score = trend_cfg.weight_15m * score_15m + trend_cfg.weight_1h * score_1h
            threshold = trend_cfg.entry_threshold
            trend_info = self._get_trend_info(symbol)

            long_threshold = trend_cfg.long_entry_threshold

            # Apply adaptive direction penalty if direction is losing
            dir_str = fvg.direction.value  # "LONG" or "SHORT"
            is_nerfed, penalty_mult = self._is_direction_nerfed(dir_str)
            if is_nerfed:
                long_threshold *= penalty_mult
                threshold *= penalty_mult
                trend_info += f" ⚠️NERFED×{penalty_mult:.0f}"

            # === FILTER 5.5: BTC Market Leader Filter ===
            # If BTC is bullish, nerf SHORT entries on all symbols (and vice versa)
            btc_leader = trend_cfg.btc_leader_enabled
            btc_nerf_mult = trend_cfg.btc_leader_nerf_multiplier
            btc_nerfed = False
            if btc_leader and symbol != "BTCUSDT":
                btc_score_15m = self._score_15m.get("BTCUSDT", 0.0)
                btc_score_1h = self._score_1h.get("BTCUSDT", 0.0)
                btc_combined = trend_cfg.weight_15m * btc_score_15m + trend_cfg.weight_1h * btc_score_1h
                # BTC bullish → nerf SHORT, BTC bearish → nerf LONG
                if fvg.direction == TradeDirection.SHORT and btc_combined > trend_cfg.entry_threshold:
                    threshold *= btc_nerf_mult
                    btc_nerfed = True
                    trend_info += f" 🔶BTC↑{btc_combined:+.2f}→SHORT×{btc_nerf_mult:.0f}"
                elif fvg.direction == TradeDirection.LONG and btc_combined < -trend_cfg.entry_threshold:
                    long_threshold *= btc_nerf_mult
                    btc_nerfed = True
                    trend_info += f" 🔶BTC↓{btc_combined:+.2f}→LONG×{btc_nerf_mult:.0f}"

            # === FILTER 5.7: Trend Direction Flip ===
            # Instead of blocking counter-trend entries, flip direction to match strong trend
            trend_flip_enabled = trend_cfg.trend_flip_enabled
            trend_flip_threshold = trend_cfg.trend_flip_threshold
            flipped = False

            if trend_flip_enabled:
                if fvg.direction == TradeDirection.LONG and combined_score < -trend_flip_threshold:
                    # FVG says LONG but strong bearish trend → flip to SHORT
                    fvg.direction = TradeDirection.SHORT
                    flipped = True
                    logger.info(
                        f"🔄 Direction flip: {symbol} LONG→SHORT — "
                        f"score {combined_score:+.2f} < -{trend_flip_threshold} "
                        f"(strong bearish trend) [{trend_info}]"
                    )
                elif fvg.direction == TradeDirection.SHORT and combined_score > trend_flip_threshold:
                    # FVG says SHORT but strong bullish trend → flip to LONG
                    fvg.direction = TradeDirection.LONG
                    flipped = True
                    logger.info(
                        f"🔄 Direction flip: {symbol} SHORT→LONG — "
                        f"score {combined_score:+.2f} > +{trend_flip_threshold} "
                        f"(strong bullish trend) [{trend_info}]"
                    )

            if fvg.direction == TradeDirection.LONG and combined_score <= long_threshold:
                now = datetime.now()
                last_log = self._last_entry_check_log.get(f"{symbol}_trend")
                if not last_log or (now - last_log).total_seconds() > 60:
                    nerf_tag = ""
                    if is_nerfed:
                        nerf_tag += f" [NERFED ×{penalty_mult:.0f}]"
                    if btc_nerfed:
                        nerf_tag += f" [BTC-LEADER ×{btc_nerf_mult:.0f}]"
                    logger.info(
                        f"🚫 LONG blocked: {symbol} — score {combined_score:+.2f} ≤ "
                        f"+{long_threshold:.2f}{nerf_tag} [{trend_info}]"
                    )
                    self._last_entry_check_log[f"{symbol}_trend"] = now
                return

            if fvg.direction == TradeDirection.SHORT and combined_score >= -threshold:
                now = datetime.now()
                last_log = self._last_entry_check_log.get(f"{symbol}_trend")
                if not last_log or (now - last_log).total_seconds() > 60:
                    nerf_tag = ""
                    if is_nerfed:
                        nerf_tag += f" [NERFED ×{penalty_mult:.0f}]"
                    if btc_nerfed:
                        nerf_tag += f" [BTC-LEADER ×{btc_nerf_mult:.0f}]"
                    logger.info(
                        f"🚫 SHORT blocked: {symbol} — score {combined_score:+.2f} ≥ "
                        f"-{threshold:.2f}{nerf_tag} [{trend_info}]"
                    )
                    self._last_entry_check_log[f"{symbol}_trend"] = now
                return

            # === FILTER 6: Minimum FVG strength ===
            min_strength = self.config.fvg.min_strength
            if min_strength > 0 and fvg.strength < min_strength:
                now = datetime.now()
                last_log = self._last_entry_check_log.get(f"{symbol}_strength")
                if not last_log or (now - last_log).total_seconds() > 120:
                    logger.info(
                        f"🚫 Weak FVG: {symbol} strength={fvg.strength:.2f} < "
                        f"{min_strength} — skipping"
                    )
                    self._last_entry_check_log[f"{symbol}_strength"] = now
                return

            # === FILTER 7: RSI extremes (avoid overbought/oversold entries) ===
            # Block LONG when RSI > overbought (price unlikely to go higher)
            # Block SHORT when RSI < oversold (price unlikely to go lower)
            rsi = self._rsi_cache.get(symbol)
            rsi_ob = self.config.trend.rsi_overbought
            rsi_os = self.config.trend.rsi_oversold
            if rsi is not None:
                if fvg.direction == TradeDirection.LONG and rsi > rsi_ob:
                    now = datetime.now()
                    last_log = self._last_entry_check_log.get(f"{symbol}_rsi")
                    if not last_log or (now - last_log).total_seconds() > 120:
                        logger.info(
                            f"🚫 RSI filter: {symbol} LONG blocked — RSI={rsi:.1f} > {rsi_ob} (overbought)"
                        )
                        self._last_entry_check_log[f"{symbol}_rsi"] = now
                    return
                if fvg.direction == TradeDirection.SHORT and rsi < rsi_os:
                    now = datetime.now()
                    last_log = self._last_entry_check_log.get(f"{symbol}_rsi")
                    if not last_log or (now - last_log).total_seconds() > 120:
                        logger.info(
                            f"🚫 RSI filter: {symbol} SHORT blocked — RSI={rsi:.1f} < {rsi_os} (oversold)"
                        )
                        self._last_entry_check_log[f"{symbol}_rsi"] = now
                    return

            # === FILTER 8: Minimum candle body (volatility confirmation) ===
            # Last closed candle body must be > min_candle_body_atr_ratio × ATR
            # NOTE: Direction check REMOVED — FVG entries are retracements,
            # so the last candle naturally opposes trade direction
            candles = self.ws_handler.get_candle_buffer(symbol)
            atr_period = self.config.trend.atr_period
            body_ratio = self.config.trend.min_candle_body_atr_ratio
            if candles and len(candles) >= 15:
                from .tpsl_calculator import TPSLCalculator
                atr = TPSLCalculator.calculate_atr(candles, period=atr_period)
                last_candle = candles[-1]
                candle_body = abs(last_candle.close - last_candle.open)
                min_body = atr * body_ratio
                if candle_body < min_body:
                    logger.debug(
                        f"🕯️ Filter 8: {symbol} weak candle "
                        f"body={candle_body:.4f} < {min_body:.4f} ({body_ratio}×ATR={atr:.4f})"
                    )
                    return

            logger.info(
                f"🎯 Entry triggered: {symbol} "
                f"{fvg.direction.value} @ {current_price:.4f} ({reason})"
            )
            fvg.entry_triggered = True
            state.last_entry_time = datetime.now()
            self._last_global_entry_time = datetime.now()  # Global burst limiter

            # === RACE CONDITION FIX: Reserve position slot BEFORE async order ===
            # asyncio is single-threaded but cooperative: when _execute_entry
            # awaits the API call, other tasks can run and pass the same-direction
            # filter. By setting has_position=True AND a stub current_order with
            # `direction` NOW (before any await), filter 4 (same-direction cap)
            # will correctly count this slot as taken.
            state.has_position = True
            state.current_order = {"direction": fvg.direction, "_reserved": True}
            await self._execute_entry(symbol, fvg, current_price)
        elif fvg.is_violated:
            ifvg = self.fvg_detector.detect_ifvg(fvg)
            if ifvg:
                logger.info(
                    f"🔄 IFVG created on {symbol}: {ifvg.direction.value} "
                    f"zone={fmt_price(ifvg.bottom)}-{fmt_price(ifvg.top)}"
                )
                state.active_fvg = ifvg
            else:
                state.active_fvg = None

    # ==================== Trade Execution ====================

    async def _execute_entry(
        self,
        symbol: str,
        fvg: FVG,
        entry_price: float,
    ) -> None:
        """Execute a trade entry: market order → confirm fill → set TP/SL."""
        state = self.symbol_states.get(symbol)
        try:
            # Check balance
            balance = await self.exchange.get_balance()
            if balance is None:
                logger.warning(f"API unreachable — skipping {symbol} entry")
                if state:
                    state.has_position = False  # release reserved slot
                    state.current_order = None
                return
            if balance.available < self.config.position.min_position_usd:
                logger.warning(f"Insufficient balance: ${balance.available:.2f}")
                if state:
                    state.has_position = False  # release reserved slot
                    state.current_order = None
                return

            leverage = self.fvg_detector.calculate_leverage(fvg)

            # Apply killzone leverage override (e.g., Asian session = 5x)
            if self.config.session.enabled:
                lev_override = self.config.session.get_killzone_leverage_override()
                if lev_override is not None and lev_override < leverage:
                    logger.info(
                        f"📉 Leverage override: {symbol} {leverage}x→{lev_override}x "
                        f"(killzone leverage limit)"
                    )
                    leverage = lev_override

            # Calculate TP/SL (with adaptive R:R based on HTF trend)
            candles = self.ws_handler.get_candle_buffer(symbol)
            htf_trend = self._htf_trend.get(symbol, HTF_RANGING)
            tpsl = self.tpsl_calculator.calculate(
                entry_price=entry_price,
                fvg=fvg,
                candles=candles,
                htf_trend=htf_trend,
            )

            # Position size (use dynamic risk percent via risk_override — no config mutation)
            sl_distance_pct = abs(tpsl.sl_price - entry_price) / entry_price
            position_size = self.position_sizer.calculate(
                balance=balance.available,
                entry_price=entry_price,
                sl_distance_percent=sl_distance_pct,
                leverage=leverage,
                symbol=symbol,
                risk_override=self._current_risk_percent,
            )

            # --- Step 1: Place market order ---
            logger.info(
                f"📤 Placing market order: {symbol} {fvg.direction.value} "
                f"qty={position_size.quantity:.6f} leverage={leverage}x"
            )
            order_result = await self.exchange.place_market_order(
                symbol=symbol,
                direction=fvg.direction,
                quantity=position_size.quantity,
                leverage=leverage,
            )
            if not order_result.success:
                logger.error(f"Order failed for {symbol}: {order_result.error}")
                if state:
                    state.has_position = False  # release reserved slot
                    state.current_order = None
                return

            order_id = order_result.order_id
            logger.info(f"✅ Market order placed: {symbol} id={order_id}")

            # has_position already set True by caller (slot reservation).
            # Set _order_pending to guard against false WS close events
            # during the REST confirmation window.
            if state:
                state._order_pending = True

            # --- Step 2: Confirm fill via REST ---
            position_id = None
            avg_open_price = entry_price
            for attempt in range(5):
                await asyncio.sleep(1.0)
                positions = await self.exchange.get_positions(symbol=symbol)
                if positions:
                    pos = positions[0]
                    position_id = pos.get("positionId")
                    avg_open_price = float(pos.get("avgOpenPrice", entry_price))
                    qty = float(pos.get("qty", 0))
                    side = pos.get("side", "")
                    logger.info(
                        f"📥 Position confirmed: {symbol} {side} "
                        f"qty={qty} @ {avg_open_price:.4f} posId={position_id}"
                    )
                    break
            else:
                logger.error(
                    f"Position not found for {symbol} after order {order_id}"
                )
                # BUG FIX: clear _order_pending so symbol isn't stuck
                if state:
                    state._order_pending = False
                    state.has_position = False
                return

            # --- Step 3: Set TP/SL ---
            actual_notional = position_size.quantity * avg_open_price
            tpsl = self.tpsl_calculator.calculate(
                entry_price=avg_open_price,
                fvg=fvg,
                candles=candles,
                htf_trend=htf_trend,
                notional_usd=actual_notional,
            )

            for attempt in range(3):
                try:
                    success = await self.exchange.set_position_tpsl(
                        symbol=symbol,
                        position_id=position_id,
                        tp_price=tpsl.tp_price,
                        sl_price=tpsl.sl_price,
                    )
                    if success:
                        logger.info(
                            f"✅ TP/SL set for {symbol}: "
                            f"TP={tpsl.tp_price:.4f}, SL={tpsl.sl_price:.4f} "
                            f"(R:R={tpsl.risk_reward_ratio:.2f}:1)"
                        )
                        break
                except Exception as e:
                    logger.warning(f"TP/SL attempt {attempt + 1} failed: {e}")
                    await asyncio.sleep(1.0)
            else:
                logger.error(
                    f"Failed to set TP/SL for {symbol} after 3 attempts!"
                )

            # --- Update bot state ---
            self.state.total_trades += 1
            # Record entry time for per-symbol entry limit
            if symbol not in self._symbol_entry_times:
                self._symbol_entry_times[symbol] = []
            self._symbol_entry_times[symbol].append(datetime.now())
            state = self.symbol_states.get(symbol)
            if state:
                state.has_position = True
                state._order_pending = False  # REST confirmed, WS close events are real now
                state.current_order = {
                    "order_id": order_id,
                    "position_id": position_id,
                    "entry_price": avg_open_price,
                    "tp_price": tpsl.tp_price,
                    "sl_price": tpsl.sl_price,
                    "original_risk": abs(avg_open_price - tpsl.sl_price),  # Fixed 1R for all calculations
                    "quantity": position_size.quantity,
                    "leverage": leverage,
                    "direction": fvg.direction,
                    "fvg_bottom": fvg.bottom,  # For zone-edge breakeven
                    "fvg_top": fvg.top,        # For zone-edge breakeven
                    "entry_time": datetime.now(),  # For hold-time calculation in close notifications
                }

            # Reset trailing state for new position
            if state:
                state.trailing_state = "initial"
                state.trailing_sl_price = tpsl.sl_price
                state.partial_tp_done = False
                state.original_qty = position_size.quantity

            logger.info(
                f"🎯 Trade complete: {symbol} {fvg.direction.value} "
                f"entry={avg_open_price:.4f} TP={tpsl.tp_price:.4f} "
                f"SL={tpsl.sl_price:.4f} qty={position_size.quantity:.6f} "
                f"lev={leverage}x"
            )

            # Track actual trade execution
            if self.signal_tracker:
                self.signal_tracker.record_trade(symbol)

            # Send Telegram notification
            if self._telegram:
                try:
                    position_usd = avg_open_price * position_size.quantity
                    risk = abs(avg_open_price - tpsl.sl_price)
                    reward = abs(tpsl.tp_price - avg_open_price)
                    rr_ratio = reward / risk if risk > 0 else 0
                    await self._telegram.notify_trade(
                        symbol=symbol,
                        side=fvg.direction.value,
                        entry_price=avg_open_price,
                        amount=position_size.quantity,
                        tp_price=tpsl.tp_price,
                        sl_price=tpsl.sl_price,
                        leverage=leverage,
                        position_usd=position_usd,
                        rr_ratio=rr_ratio,
                    )
                except Exception as tg_err:
                    logger.debug(f"Telegram notify failed: {tg_err}")

        except CircuitBreakerOpen as e:
            logger.error(f"Circuit breaker open: {e}")
            if state:
                state.has_position = False  # release reserved slot
                state.current_order = None
        except Exception as e:
            logger.error(f"Failed to execute signal for {symbol}: {e}")
            if state:
                state.has_position = False  # release reserved slot
                state.current_order = None

    # ==================== 3-Phase Trailing SL + Trailing TP ====================
    #
    # Phase 1 (Initial):  SL = ATR-based, TP = 3R
    # Phase 2 (Breakeven): At ≥1R → SL = entry + 0.25R, TP stays 3R
    # Phase 3 (Runner):    At ≥2R → SL trails 1R behind price, TP slides +1.5R ahead
    #
    # No partial TP — full position rides the runner.

    async def _check_trailing_on_tick(self, symbol: str, state: SymbolState) -> None:
        """Fast trailing check on every WS tick (~500ms).

        Only triggers API calls when a phase transition or meaningful
        SL/TP move is detected (hysteresis = trailing_step_r).
        """
        if self._trailing_lock.get(symbol, False):
            return

        try:
            order = state.current_order
            if not isinstance(order, dict):
                return

            entry_price = order.get("entry_price", 0)
            direction = order.get("direction")
            current_price = state.last_price

            if not entry_price or not direction or current_price <= 0:
                return

            sl_price = order.get("sl_price", 0)
            risk = order.get("original_risk", 0) or abs(entry_price - sl_price)
            if risk <= 0:
                return

            if direction == TradeDirection.LONG:
                profit_r = (current_price - entry_price) / risk
            else:
                profit_r = (entry_price - current_price) / risk

            tpsl_cfg = self.config.tpsl
            if not tpsl_cfg.trailing_enabled:
                return

            needs_action = False

            # Partial TP: trigger when not yet done and profit >= partial_tp_at_r
            if (tpsl_cfg.partial_tp_enabled
                    and not state.partial_tp_done
                    and profit_r >= tpsl_cfg.partial_tp_at_r):
                needs_action = True

            # Phase 2: Initial → Breakeven
            if (state.trailing_state == "initial"
                    and profit_r >= tpsl_cfg.trailing_breakeven_at_r):
                needs_action = True

            # Phase 3: Breakeven → Runner
            runner_at = tpsl_cfg.trailing_runner_at_r
            if (state.trailing_state == "breakeven"
                    and profit_r >= runner_at):
                needs_action = True

            # Runner: continuous SL+TP sliding
            if state.trailing_state == "runner":
                sl_dist_r = tpsl_cfg.trailing_runner_sl_distance_r
                step_r = tpsl_cfg.trailing_step_r
                current_trail = state.trailing_sl_price or sl_price
                if direction == TradeDirection.LONG:
                    candidate_sl = current_price - (risk * sl_dist_r)
                    if candidate_sl > current_trail + (risk * step_r):
                        needs_action = True
                else:
                    candidate_sl = current_price + (risk * sl_dist_r)
                    if candidate_sl < current_trail - (risk * step_r):
                        needs_action = True

            if needs_action:
                self._trailing_lock[symbol] = True
                try:
                    await self._manage_position_trailing(symbol, state)
                finally:
                    self._trailing_lock[symbol] = False

        except Exception as e:
            self._trailing_lock[symbol] = False
            logger.debug(f"Trailing tick check error for {symbol}: {e}")

    async def manage_open_positions(self) -> None:
        """Manage trailing SL/TP for all open positions (called every 30s)."""
        tpsl_cfg = self.config.tpsl
        if not tpsl_cfg.trailing_enabled:
            return

        for symbol, state in self.symbol_states.items():
            if not state.has_position or not state.current_order:
                continue
            if self._trailing_lock.get(symbol, False):
                continue

            try:
                self._trailing_lock[symbol] = True
                await self._manage_position_trailing(symbol, state)
            except Exception as e:
                logger.error(f"Trailing SL error for {symbol}: {e}")
            finally:
                self._trailing_lock[symbol] = False

    async def _manage_position_trailing(
        self, symbol: str, state: SymbolState
    ) -> None:
        """3-phase trailing SL + trailing TP logic for a single position.
        
        Phase 1 (initial):   Exchange SL/TP untouched (set at entry)
        Phase 2 (breakeven):  SL → entry + lock_r, TP stays (3R)
        Phase 3 (runner):     SL = price - sl_dist_r, TP = price + tp_dist_r (both slide)
        """
        order = state.current_order
        if not isinstance(order, dict):
            return

        entry_price = order.get("entry_price", 0)
        sl_price = order.get("sl_price", 0)
        tp_price = order.get("tp_price", 0)
        direction = order.get("direction")
        position_id = order.get("position_id")

        if not entry_price or not position_id or not direction:
            return

        current_price = state.last_price
        if current_price <= 0:
            return

        risk = order.get("original_risk", 0) or abs(entry_price - sl_price)
        if risk <= 0:
            return

        if direction == TradeDirection.LONG:
            profit_r = (current_price - entry_price) / risk
        else:
            profit_r = (entry_price - current_price) / risk

        tpsl_cfg = self.config.tpsl
        if not tpsl_cfg.trailing_enabled:
            return

        new_sl = None
        new_tp = None
        phase_change = None

        # ── Partial TP: close 50% at 1R ──
        if (
            tpsl_cfg.partial_tp_enabled
            and not state.partial_tp_done
            and profit_r >= tpsl_cfg.partial_tp_at_r
        ):
            close_pct = tpsl_cfg.partial_tp_percent
            close_qty = state.original_qty * close_pct
            if close_qty > 0:
                logger.info(
                    f"💰 Partial TP: {symbol} closing {close_pct*100:.0f}% "
                    f"({close_qty:.6f}) at {profit_r:.1f}R "
                    f"(price={current_price:.4f})"
                )
                result = await self.exchange.close_position(
                    symbol=symbol,
                    direction=direction,
                    quantity=close_qty,
                    position_id=position_id,
                )
                if result.success:
                    state.partial_tp_done = True
                    remaining_qty = state.original_qty - close_qty
                    pnl_partial = close_qty * risk * tpsl_cfg.partial_tp_at_r
                    if direction == TradeDirection.SHORT:
                        pnl_partial = close_qty * risk * tpsl_cfg.partial_tp_at_r
                    logger.info(
                        f"✅ Partial TP filled: {symbol} closed {close_qty:.6f}, "
                        f"remaining {remaining_qty:.6f} (~${pnl_partial:.2f} locked)"
                    )
                    # Send Telegram notification
                    if self._telegram:
                        try:
                            await self._telegram.send_notification(
                                f"💰 *Partial TP* {symbol}\n"
                                f"Closed {close_pct*100:.0f}% at {profit_r:.1f}R\n"
                                f"Price: {current_price:.4f}\n"
                                f"Remaining: {remaining_qty:.6f}"
                            )
                        except Exception:
                            pass
                else:
                    logger.warning(
                        f"⚠️ Partial TP failed for {symbol}: {result.error}"
                    )
            else:
                logger.warning(
                    f"⚠️ Partial TP skipped for {symbol}: original_qty={state.original_qty}, "
                    f"close_qty=0 — marking as done to avoid blocking"
                )
                state.partial_tp_done = True

        # ── Phase 2: Initial → Breakeven ──
        if state.trailing_state == "initial":
            if profit_r >= tpsl_cfg.trailing_breakeven_at_r:
                lock_r = tpsl_cfg.trailing_be_lock_r
                if direction == TradeDirection.LONG:
                    new_sl = entry_price + (risk * lock_r)
                else:
                    new_sl = entry_price - (risk * lock_r)
                # TP stays at original 3R — no change
                phase_change = "breakeven"
                logger.info(
                    f"🔒 Phase 2 (BE): {symbol} SL→{new_sl:.4f} "
                    f"(entry+{lock_r}R locked, profit={profit_r:.1f}R)"
                )

        # ── Phase 3: Breakeven → Runner ──
        elif state.trailing_state == "breakeven":
            runner_at = tpsl_cfg.trailing_runner_at_r
            if profit_r >= runner_at:
                sl_dist_r = tpsl_cfg.trailing_runner_sl_distance_r
                tp_dist_r = tpsl_cfg.trailing_runner_tp_distance_r
                if direction == TradeDirection.LONG:
                    new_sl = current_price - (risk * sl_dist_r)
                    new_tp = current_price + (risk * tp_dist_r)
                else:
                    new_sl = current_price + (risk * sl_dist_r)
                    new_tp = current_price - (risk * tp_dist_r)
                phase_change = "runner"
                logger.info(
                    f"🚀 Phase 3 (Runner): {symbol} SL→{new_sl:.4f} "
                    f"TP→{new_tp:.4f} (profit={profit_r:.1f}R, "
                    f"SL={sl_dist_r}R behind, TP={tp_dist_r}R ahead)"
                )

        # ── Runner: continuous SL+TP sliding ──
        elif state.trailing_state == "runner":
            sl_dist_r = tpsl_cfg.trailing_runner_sl_distance_r
            tp_dist_r = tpsl_cfg.trailing_runner_tp_distance_r
            step_r = tpsl_cfg.trailing_step_r
            current_trail = state.trailing_sl_price or sl_price

            if direction == TradeDirection.LONG:
                candidate_sl = current_price - (risk * sl_dist_r)
                candidate_tp = current_price + (risk * tp_dist_r)
                # Only move SL up (never down), and only if meaningful step
                if candidate_sl > current_trail + (risk * step_r):
                    new_sl = candidate_sl
                    new_tp = candidate_tp
            else:
                candidate_sl = current_price + (risk * sl_dist_r)
                candidate_tp = current_price - (risk * tp_dist_r)
                # Only move SL down (never up), and only if meaningful step
                if candidate_sl < current_trail - (risk * step_r):
                    new_sl = candidate_sl
                    new_tp = candidate_tp

        # ── Apply changes to exchange ──
        if new_sl is not None:
            # Determine TP to send: keep current TP unless we have a new one
            send_tp = new_tp if new_tp is not None else tp_price

            state._order_pending = True
            try:
                success = await self.exchange.modify_position_tpsl(
                    symbol=symbol,
                    position_id=position_id,
                    tp_price=send_tp,
                    sl_price=new_sl,
                )
            finally:
                state._order_pending = False

            if success:
                state.trailing_sl_price = new_sl
                order["sl_price"] = new_sl
                if new_tp is not None:
                    order["tp_price"] = new_tp
                if phase_change:
                    state.trailing_state = phase_change

                if state.trailing_state == "runner":
                    logger.info(
                        f"📈 Runner slide: {symbol} SL={new_sl:.4f} "
                        f"TP={send_tp:.4f} ({profit_r:.1f}R)"
                    )
            else:
                # API failed — check if position still exists
                positions = await self.exchange.get_positions(symbol=symbol)
                if positions is None:
                    logger.warning(f"⚠️ Trailing: API unreachable — keeping {symbol} state")
                    return
                pos_exists = any(
                    str(p.get("positionId")) == str(position_id)
                    for p in positions
                )
                if not pos_exists:
                    logger.info(
                        f"🔄 Trailing: {symbol} position {position_id} gone "
                        f"— clearing stale state"
                    )
                    state.has_position = False
                    state.current_order = None
                    state.trailing_state = "initial"
                    state.partial_tp_done = False
                    state._order_pending = False
                    return
                if profit_r >= self.config.tpsl.force_close_at_r:
                    logger.warning(
                        f"Trailing failed for {symbol} at {profit_r:.1f}R, force-closing"
                    )
                    closed = await self.exchange.force_close_symbol(symbol)
                    if closed:
                        state.has_position = False
                        state._order_pending = False
                        return
                else:
                    logger.warning(f"Failed to update trailing for {symbol}")
