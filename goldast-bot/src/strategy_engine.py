"""
GoldasT Bot v2 - Strategy Engine
EMA crossover trend-following with ATR-based TP/SL.
Replaces FVG-based signals (46% accuracy) with EMA(9)/EMA(21) crossover.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, TYPE_CHECKING

from .models import FVG, FVGType, SymbolState, BotState, TradeDirection, OrderState
from . import fmt_price
from .fvg_detector import FVGDetector
from .tpsl_calculator import TPSLCalculator
from .position_sizer import PositionSizer
from .order_state_machine import OrderManager
from .exchange_adapter import ExchangeAdapter, PRICE_PRECISION
from .websocket_handler import WebSocketHandler, KlineMessage
from .error_recovery import ResilientExecutor, CircuitBreakerOpen
from .config import Config
from .market_structure import MarketStructure, TrendDirection as MSTrendDirection


logger = logging.getLogger(__name__)

# Higher-timeframe trend states (derived from continuous score for logging)
HTF_UPTREND = "uptrend"      # score > +threshold
HTF_DOWNTREND = "downtrend"  # score < -threshold
HTF_RANGING = "ranging"      # |score| <= threshold

# Correlation groups: symbols that move together (same underlying/sector)
# Only 1 position per group per direction allowed (prevents double SL hit)
CORRELATION_GROUPS = {
    "GOLD": {"XAUTUSDT", "XAUUSDT", "PAXGUSDT"},
    "SILVER": {"XAGUSDT"},
    "BTC_ECOSYSTEM": {"BTCUSDT"},
    "ETH_ECOSYSTEM": {"ETHUSDT", "ETCUSDT"},
    "SOL_ECOSYSTEM": {"SOLUSDT", "JUPUSDT"},
    "AI_SECTOR": {"VIRTUALUSDT", "AIUSDT"},
    "MEME": {"DOGEUSDT", "1000PEPEUSDT", "WIFUSDT", "FARTCOINUSDT", "HIPPOUSDT"},
    "INDEXES": {"SPXUSDT"},
    "DEFI": {"AAVEUSDT", "UNIUSDT", "LINKUSDT"},
    "L1": {"SUIUSDT", "AVAXUSDT", "DOTUSDT", "ADAUSDT"},
}


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
        self._position_manager = None  # set via set_position_manager()
        
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
        
        # Symbol rotation reference (set after construction via set_rotation())
        self._rotation = None
        
        # Per-symbol consecutive loss counter
        self._symbol_consecutive_losses: Dict[str, int] = {}
        self._symbol_loss_ban_until: Dict[str, datetime] = {}

        # Global consecutive loss counter — pause trading after streak
        self._global_consecutive_losses: int = 0
        self._global_loss_pause_until: Optional[datetime] = None

        # Per-symbol entry time tracker (for max_entries_per_symbol limit)
        self._symbol_entry_times: Dict[str, list] = {}  # {symbol: [datetime, ...]}

        # === Market Structure (BOS) per symbol ===
        self._market_structures: Dict[str, MarketStructure] = {}

        # === HTF (1h) FVG Zones per symbol ===
        # {symbol: [FVG, ...]} — 1h FVG zones detected from hourly candles
        self._htf_fvg_zones: Dict[str, list] = {}

        # === EMA Crossover Tracking ===
        # Previous candle's EMA values for crossover detection
        self._prev_ema9: Dict[str, float] = {}
        self._prev_ema21: Dict[str, float] = {}

    def set_telegram(self, telegram_bot) -> None:
        """Set Telegram bot reference for trade notifications."""
        self._telegram = telegram_bot

    def set_position_manager(self, pm) -> None:
        """Inject PositionManager reference for persistent state."""
        self._position_manager = pm

    def set_rotation(self, rotation) -> None:
        """Set SymbolRotation reference for proven/trial symbol checks."""
        self._rotation = rotation

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
        """Track consecutive losses per symbol AND globally.
        
        Per-symbol: Ban symbol after N consecutive losses.
        Global: Pause ALL trading after global_max_consecutive_losses in a row.
        
        Core symbols get a shorter ban (core_symbol_loss_ban_seconds).
        Rotation symbols get a longer ban (symbol_loss_ban_seconds).
        """
        if is_loss:
            self._symbol_consecutive_losses[symbol] = self._symbol_consecutive_losses.get(symbol, 0) + 1
            self._global_consecutive_losses += 1
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
            # Global loss streak check
            global_max = getattr(self.config.risk, 'global_max_consecutive_losses', 4)
            global_pause = getattr(self.config.risk, 'global_loss_pause_seconds', 1800)
            if self._global_consecutive_losses >= global_max:
                self._global_loss_pause_until = datetime.now() + timedelta(seconds=global_pause)
                logger.warning(
                    f"🛑 GLOBAL LOSS STREAK: {self._global_consecutive_losses} consecutive losses — "
                    f"ALL trading paused for {global_pause // 60} min"
                )
        else:
            # Win resets the streak
            self._symbol_consecutive_losses[symbol] = 0
            if symbol in self._symbol_loss_ban_until:
                del self._symbol_loss_ban_until[symbol]
            self._global_consecutive_losses = 0
            self._global_loss_pause_until = None

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
        """Record a trade result for adaptive direction blocking.
        
        Called from position_manager when a trade closes.
        Uses net PnL over a sliding window — a tiny win no longer resets the counter.
        """
        dir_key = direction.upper()
        if dir_key not in self._direction_trades:
            return
        self._direction_trades[dir_key].append((datetime.now(), pnl))
        
        # Check net PnL over last N trades
        window = self.config.risk.direction_nerf_window
        trades = list(self._direction_trades[dir_key])
        recent = trades[-window:]
        if len(recent) < 2:
            return
        
        net_pnl = sum(t[1] for t in recent)
        threshold = self.config.risk.direction_nerf_net_pnl_threshold
        
        if net_pnl < threshold:
            if self._direction_penalty_start[dir_key] is None:
                self._direction_penalty_start[dir_key] = datetime.now()
                duration = self.config.risk.direction_nerf_duration_seconds
                logger.warning(
                    f"🚫 Direction BLOCKED: {dir_key} — "
                    f"net PnL ${net_pnl:+.2f} < ${threshold:+.2f} "
                    f"over last {len(recent)} trades → blocked for {duration//60}min"
                )
        elif net_pnl >= 0 and self._direction_penalty_start[dir_key] is not None:
            logger.info(
                f"✅ Direction UNBLOCKED: {dir_key} — "
                f"net PnL ${net_pnl:+.2f} recovered over last {len(recent)} trades"
            )
            self._direction_penalty_start[dir_key] = None

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

    def _update_market_structure(self, symbol: str) -> None:
        """Update market structure (BOS) on 15m candle close."""
        if not self.config.trend.bos_enabled:
            return
        candles = self.ws_handler.get_candle_buffer(symbol)
        if not candles or len(candles) < 10:
            return
        if symbol not in self._market_structures:
            self._market_structures[symbol] = MarketStructure(symbol=symbol, lookback=50)
        ms = self._market_structures[symbol]
        old_bos_dir = ms.last_bos_direction
        ms.update(candles)
        # Log BOS events (only when new BOS detected)
        if ms.last_bos_direction != old_bos_dir or ms._candles_since_bos == 0:
            if ms._candles_since_bos == 0:
                logger.info(f"🏗️ {symbol} {ms.get_bos_info()}")

    def _detect_htf_fvgs(self, symbol: str, candles_1h: list) -> None:
        """Detect 1h FVG zones from hourly candles.
        
        These zones serve as structural confluence — 15m FVGs inside a 1h FVG
        zone are higher quality (bigger imbalance backing them up).
        """
        if not self.config.trend.htf_fvg_enabled:
            return
        if len(candles_1h) < 3:
            return

        from .models import Candle as CandleModel
        min_gap = self.config.trend.htf_fvg_min_gap_percent
        max_zones = self.config.trend.htf_fvg_max_zones

        zones = []
        # Slide through all 3-candle windows
        for i in range(len(candles_1h) - 2):
            c1, c2, c3 = candles_1h[i], candles_1h[i + 1], candles_1h[i + 2]

            # Bullish FVG: c1.high < c3.low (gap up)
            bull_gap = c3.low - c1.high
            if bull_gap > 0:
                gap_pct = bull_gap / c2.close
                if gap_pct >= min_gap:
                    zone_top = c3.low
                    zone_bottom = c1.high
                    # Check not violated by subsequent candles
                    violated = False
                    for j in range(i + 3, len(candles_1h)):
                        if candles_1h[j].low <= zone_bottom:
                            violated = True
                            break
                    if not violated:
                        zones.append({
                            "direction": "LONG",
                            "top": zone_top,
                            "bottom": zone_bottom,
                            "mid": (zone_top + zone_bottom) / 2,
                            "gap_pct": gap_pct,
                            "candle_idx": i + 1,
                        })

            # Bearish FVG: c1.low > c3.high (gap down)
            bear_gap = c1.low - c3.high
            if bear_gap > 0:
                gap_pct = bear_gap / c2.close
                if gap_pct >= min_gap:
                    zone_top = c1.low
                    zone_bottom = c3.high
                    # Check not violated
                    violated = False
                    for j in range(i + 3, len(candles_1h)):
                        if candles_1h[j].high >= zone_top:
                            violated = True
                            break
                    if not violated:
                        zones.append({
                            "direction": "SHORT",
                            "top": zone_top,
                            "bottom": zone_bottom,
                            "mid": (zone_top + zone_bottom) / 2,
                            "gap_pct": gap_pct,
                            "candle_idx": i + 1,
                        })

        # Keep most recent N zones
        zones = zones[-max_zones:]
        old_count = len(self._htf_fvg_zones.get(symbol, []))
        self._htf_fvg_zones[symbol] = zones

        if zones:
            logger.info(
                f"📊 HTF FVG {symbol}: {len(zones)} 1h zones "
                f"(LONG={sum(1 for z in zones if z['direction']=='LONG')}, "
                f"SHORT={sum(1 for z in zones if z['direction']=='SHORT')})"
            )

    def _check_htf_confluence(self, symbol: str, fvg) -> tuple:
        """Check if 15m FVG is inside or overlapping a 1h FVG zone.
        
        Returns: (is_confluent: bool, overlap_pct: float, htf_zone_info: str)
        """
        if not self.config.trend.htf_fvg_enabled:
            return True, 1.0, "HTF:off"  # Pass-through when disabled
        
        htf_zones = self._htf_fvg_zones.get(symbol, [])
        if not htf_zones:
            return False, 0.0, "HTF:no_zones"
        
        dir_str = fvg.direction.value  # "LONG" or "SHORT"
        
        # Find best overlapping 1h zone with same direction
        best_overlap = 0.0
        best_zone = None
        for zone in htf_zones:
            if zone["direction"] != dir_str:
                continue
            # Calculate overlap between 15m FVG and 1h zone
            overlap_top = min(fvg.top, zone["top"])
            overlap_bottom = max(fvg.bottom, zone["bottom"])
            if overlap_top > overlap_bottom:
                # There is overlap
                overlap_size = overlap_top - overlap_bottom
                fvg_size = fvg.top - fvg.bottom
                overlap_pct = overlap_size / fvg_size if fvg_size > 0 else 0
                if overlap_pct > best_overlap:
                    best_overlap = overlap_pct
                    best_zone = zone
        
        # Also check if 15m FVG mid_price is INSIDE a 1h zone (even without exact overlap)
        if best_overlap == 0:
            for zone in htf_zones:
                if zone["direction"] != dir_str:
                    continue
                if zone["bottom"] <= fvg.mid_price <= zone["top"]:
                    best_overlap = 0.5  # Mid-price inside = partial confluence
                    best_zone = zone
                    break
        
        if best_zone:
            info = (
                f"HTF:✓ {best_zone['direction']} "
                f"[{best_zone['bottom']:.4f}-{best_zone['top']:.4f}] "
                f"ovlp={best_overlap*100:.0f}%"
            )
            return True, best_overlap, info
        
        return False, 0.0, f"HTF:no_{dir_str}_zone"

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
    def _calc_adx(candles, period: int = 14) -> float:
        """Calculate ADX (Average Directional Index) — trend strength.
        
        ADX >= 20: market is trending (good for EMA crossover)
        ADX < 20:  market is choppy/ranging (skip entry)
        """
        if len(candles) < period + 2:
            return 0.0
        pdm, mdm, trs = [], [], []
        for i in range(1, len(candles)):
            h = candles[i].high if hasattr(candles[i], 'high') else candles[i]['h']
            l = candles[i].low if hasattr(candles[i], 'low') else candles[i]['l']
            ph = candles[i-1].high if hasattr(candles[i-1], 'high') else candles[i-1]['h']
            pl = candles[i-1].low if hasattr(candles[i-1], 'low') else candles[i-1]['l']
            pc = candles[i-1].close if hasattr(candles[i-1], 'close') else candles[i-1]['c']
            up, down = h - ph, pl - l
            pdm.append(up if (up > down and up > 0) else 0)
            mdm.append(down if (down > up and down > 0) else 0)
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if len(trs) < period:
            return 0.0
        atr = sum(trs[:period]) / period
        ps = sum(pdm[:period]) / period
        ms = sum(mdm[:period]) / period
        dxs = []
        for i in range(period, len(trs)):
            atr = (atr * (period - 1) + trs[i]) / period
            ps = (ps * (period - 1) + pdm[i]) / period
            ms = (ms * (period - 1) + mdm[i]) / period
            if atr == 0:
                continue
            pdi = 100 * ps / atr
            mdi = 100 * ms / atr
            s = pdi + mdi
            if s == 0:
                continue
            dxs.append(100 * abs(pdi - mdi) / s)
        if not dxs:
            return 0.0
        adx = sum(dxs[:period]) / min(period, len(dxs))
        for dx in dxs[period:]:
            adx = (adx * (period - 1) + dx) / period
        return adx

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

    def _detect_exhaustion(self, symbol: str, candles: list) -> tuple:
        """Detect momentum exhaustion — 3+ consecutive strong candles in same direction.

        When price makes 3-4 big candles in one direction, it often signals
        imminent reversal. Used to:
        - BOOST entries opposite to exhaustion (reversal play)
        - BLOCK entries chasing the exhaustion (momentum trap)

        Returns:
            (direction, count, avg_body_ratio):
            - direction: "BULLISH" or "BEARISH" if exhaustion detected, None otherwise
            - count: number of consecutive strong candles
            - avg_body_ratio: average body/ATR ratio of the streak
        """
        cfg = getattr(self.config.trend, 'exhaustion_min_candles', 3)
        body_threshold = getattr(self.config.trend, 'exhaustion_body_atr_ratio', 0.5)

        if not candles or len(candles) < cfg + 5:
            return None, 0, 0.0

        atr = TPSLCalculator.calculate_atr(candles, period=self.config.trend.atr_period)
        if atr <= 0:
            return None, 0, 0.0

        # Walk backwards from the last closed candle (skip index -1 which may be live/unclosed)
        # Check up to 8 candles back for the exhaustion pattern
        check_range = candles[-8:-1] if len(candles) > 8 else candles[:-1]

        consecutive_bull = 0
        consecutive_bear = 0
        bull_bodies = []
        bear_bodies = []

        for c in reversed(check_range):
            body = c.close - c.open
            body_ratio = abs(body) / atr

            if body > 0 and body_ratio >= body_threshold:
                # Bullish candle
                if consecutive_bear > 0:
                    break  # Direction changed, stop
                consecutive_bull += 1
                bull_bodies.append(body_ratio)
            elif body < 0 and body_ratio >= body_threshold:
                # Bearish candle
                if consecutive_bull > 0:
                    break  # Direction changed, stop
                consecutive_bear += 1
                bear_bodies.append(body_ratio)
            else:
                break  # Weak candle breaks the streak

        if consecutive_bull >= cfg:
            return "BULLISH", consecutive_bull, sum(bull_bodies) / len(bull_bodies)
        elif consecutive_bear >= cfg:
            return "BEARISH", consecutive_bear, sum(bear_bodies) / len(bear_bodies)

        return None, 0, 0.0

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

        # Build symbol list: always include BTCUSDT for market regime detection
        refresh_symbols = list(self.symbol_states.keys())
        if trend_cfg.btc_leader_enabled and "BTCUSDT" not in refresh_symbols:
            refresh_symbols.append("BTCUSDT")

        for symbol in refresh_symbols:
            # --- 1h score ---
            try:
                candles_1h = await self.exchange.get_historical_candles(
                    symbol=symbol, limit=100, interval="1h",
                )
                score_1h = self._compute_trend_score(candles_1h, ema_fast, ema_slow)
                self._score_1h[symbol] = score_1h
                self._htf_trend[symbol] = self._score_to_label(score_1h, threshold)
                # Detect 1h FVG zones for confluence filter
                self._detect_htf_fvgs(symbol, candles_1h)
            except Exception as e:
                logger.warning(f"1h trend fetch failed for {symbol}: {e}")
                self._score_1h.setdefault(symbol, 0.0)
                score_1h = self._score_1h.get(symbol, 0.0)
                self._htf_trend.setdefault(symbol, HTF_RANGING)

            # --- 15m score ---
            try:
                candles_15m = await self.exchange.get_historical_candles(
                    symbol=symbol, limit=50, interval="15m",
                )
                score_15m = self._compute_trend_score(candles_15m, ema_fast, ema_slow)
                self._score_15m[symbol] = score_15m
                self._mtf_trend[symbol] = self._score_to_label(score_15m, threshold)

                # Warm up BOS from historical 15m candles (instant, no 2.5h wait)
                if self.config.trend.bos_enabled and candles_15m:
                    if symbol not in self._market_structures:
                        self._market_structures[symbol] = MarketStructure(
                            symbol=symbol, lookback=50
                        )
                    ms = self._market_structures[symbol]
                    if ms._total_candles_seen == 0:
                        ms.warmup(candles_15m)
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

            # Update market structure (BOS detection) on candle close
            self._update_market_structure(symbol)

            # === EMA Crossover Signal (replaces FVG detection) ===
            # FVG signals had 46% direction accuracy over 500 trades = noise.
            # EMA crossover follows actual trend direction = higher accuracy.
            asyncio.create_task(self._check_ema_signal_on_close(symbol))
        else:
            # On live tick: manage trailing SL for open positions
            # (No more FVG zone entry checking — EMA entries happen on candle close)
            if state and state.has_position and state.current_order:
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

    # ==================== EMA Crossover Signal ====================

    async def _check_ema_signal_on_close(self, symbol: str) -> None:
        """Detect EMA(9)/EMA(21) crossover on candle close and trigger entry.
        
        Replaces FVG-based signal generation. EMA crossover is a proven
        trend-following approach with >50% directional accuracy on crypto.
        FVG signals had only 46% accuracy over 500 trades.
        
        Signal: EMA(9) crosses EMA(21) → enter in crossover direction.
        TP/SL: purely ATR-based (1.5 ATR stop, 3R target).
        """
        candles = self.ws_handler.get_candle_buffer(symbol)
        if not candles or len(candles) < 25:
            return

        state = self.symbol_states.get(symbol)
        if not state:
            return

        # Don't generate signals if we already have a position
        if state.has_position:
            return

        # Don't generate signals if an order is pending
        machine = self.order_manager.get_machine(symbol)
        if machine and machine.ctx.state not in (OrderState.IDLE,):
            return

        closes = [c.close for c in candles]
        current_price = closes[-1]

        # Compute current EMAs
        ema9 = self._ema(closes, 9)
        ema21 = self._ema(closes, 21)

        # Get previous EMAs
        prev_ema9 = self._prev_ema9.get(symbol)
        prev_ema21 = self._prev_ema21.get(symbol)

        # Store for next candle
        self._prev_ema9[symbol] = ema9
        self._prev_ema21[symbol] = ema21

        if prev_ema9 is None or prev_ema21 is None:
            return  # First candle — need at least 2 to detect cross

        # Detect crossover
        direction = None
        if prev_ema9 <= prev_ema21 and ema9 > ema21:
            direction = TradeDirection.LONG   # Bullish crossover
        elif prev_ema9 >= prev_ema21 and ema9 < ema21:
            direction = TradeDirection.SHORT  # Bearish crossover

        if direction is None:
            return  # No crossover this candle

        dir_str = direction.value
        trend_info = self._get_trend_info(symbol)

        # === ADX chop filter: skip entry in ranging/choppy market ===
        adx = self._calc_adx(candles)
        adx_min = 20  # ADX < 20 = choppy market, EMA crosses are noise
        if adx < adx_min:
            logger.info(
                f"🚫 EMA cross {symbol} {dir_str} blocked — ADX={adx:.1f} < {adx_min} (choppy market)"
            )
            return

        # === RSI confirmation: skip overbought BUYs and oversold SELLs ===
        rsi = self._rsi_cache.get(symbol)
        rsi_ob = self.config.trend.rsi_overbought
        rsi_os = self.config.trend.rsi_oversold
        if rsi is not None:
            if direction == TradeDirection.LONG and rsi > rsi_ob:
                logger.info(
                    f"🚫 EMA cross {symbol} LONG blocked — RSI={rsi:.1f} > {rsi_ob} (overbought)"
                )
                return
            if direction == TradeDirection.SHORT and rsi < rsi_os:
                logger.info(
                    f"🚫 EMA cross {symbol} SHORT blocked — RSI={rsi:.1f} < {rsi_os} (oversold)"
                )
                return

        # === Daily loss limit ===
        max_daily_loss = self.state.balance * self.config.risk.max_daily_loss_percent / 100
        if self.state.daily_pnl < 0 and abs(self.state.daily_pnl) >= max_daily_loss:
            return

        # === Global consecutive loss pause ===
        if self._global_loss_pause_until:
            if datetime.now() < self._global_loss_pause_until:
                return
            else:
                self._global_loss_pause_until = None

        # === Entry cooldown ===
        last_win = self._last_win_time.get(symbol)
        win_cooldown = self.config.cooldowns.win_cooldown_seconds
        entry_cooldown = self.config.cooldowns.entry_cooldown_seconds
        if last_win and state.last_entry_time and last_win >= state.last_entry_time:
            effective_cooldown = win_cooldown
        else:
            effective_cooldown = entry_cooldown
        if state.last_entry_time and effective_cooldown > 0:
            elapsed = (datetime.now() - state.last_entry_time).total_seconds()
            if elapsed < effective_cooldown:
                return

        # === Post-loss cooldown ===
        loss_cooldown = self.config.cooldowns.loss_cooldown_seconds
        last_loss = self._last_loss_time.get(symbol)
        if last_loss and loss_cooldown > 0:
            elapsed = (datetime.now() - last_loss).total_seconds()
            if elapsed < loss_cooldown:
                return

        # === Global burst limiter ===
        global_cd = self.config.cooldowns.global_entry_cooldown_seconds
        if global_cd > 0 and self._last_global_entry_time:
            elapsed = (datetime.now() - self._last_global_entry_time).total_seconds()
            if elapsed < global_cd:
                return

        # === Max total positions ===
        max_positions = self.config.multi_symbol.max_concurrent_positions
        total_positions = sum(
            1 for s, st in self.symbol_states.items()
            if st.has_position
        )
        if total_positions >= max_positions:
            logger.info(
                f"🚫 EMA cross {symbol} {dir_str} blocked — "
                f"{total_positions}/{max_positions} positions"
            )
            return

        # === Per-symbol ban ===
        ban_until = self._symbol_loss_ban_until.get(symbol)
        if ban_until and datetime.now() < ban_until:
            return

        # === Max same-direction ===
        max_same = self.config.multi_symbol.max_same_direction
        same_dir_count = sum(
            1 for s, st in self.symbol_states.items()
            if st.has_position and st.current_order
            and st.current_order.get('direction') == direction
        )
        if same_dir_count >= max_same:
            return

        # === Correlation guard ===
        if self.config.multi_symbol.correlation_guard_enabled:
            max_corr = self.config.multi_symbol.max_correlated_same_dir
            sym_group = None
            for grp_name, grp_symbols in CORRELATION_GROUPS.items():
                if symbol in grp_symbols:
                    sym_group = grp_name
                    break
            if sym_group:
                grp_syms = CORRELATION_GROUPS[sym_group]
                corr_count = sum(
                    1 for s, st in self.symbol_states.items()
                    if s != symbol and s in grp_syms
                    and st.has_position and st.current_order
                    and st.current_order.get('direction') == direction
                )
                if corr_count >= max_corr:
                    return

        # === Session/killzone filter ===
        if self.config.session.enabled:
            is_active, zone_name = self.config.session.is_killzone_now()
            if not is_active:
                return

        # All filters passed — create virtual FVG and execute entry
        atr = TPSLCalculator.calculate_atr(candles, period=self.config.trend.atr_period)

        fvg = FVG(
            symbol=symbol,
            direction=direction,
            top=current_price + atr * 0.01,
            bottom=current_price - atr * 0.01,
            created_at=datetime.now(),
            candle_index=len(candles) - 1,
            fvg_type=FVGType.BULLISH if direction == TradeDirection.LONG else FVGType.BEARISH,
            gap_percent=0.0,
            signal_source="ema",
        )
        fvg.strength = 0.5
        fvg.entry_triggered = True

        state.active_fvg = fvg

        logger.info(
            f"📊 EMA CROSS: {symbol} {dir_str} | "
            f"EMA9={ema9:.6f} EMA21={ema21:.6f} | "
            f"price={current_price:.6f} RSI={f'{rsi:.1f}' if rsi else 'N/A'} "
            f"ADX={adx:.1f} [{trend_info}]"
        )

        # Reserve position slot and execute
        state.has_position = True
        state.current_order = {"direction": direction, "_reserved": True}
        state.last_entry_time = datetime.now()
        self._last_global_entry_time = datetime.now()

        # Track entry for per-symbol limit
        if symbol not in self._symbol_entry_times:
            self._symbol_entry_times[symbol] = []
        self._symbol_entry_times[symbol].append(datetime.now())

        await self._execute_entry(symbol, fvg, current_price, 1.0)

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

            # --- Track zone proximity for signal_ban even when not entering ---
            # Count zone hit if price is within 1% of zone edge (approaching).
            # This feeds signal_ban with activity data even for blocked entries.
            if self.signal_tracker and fvg.fill_percent == 0.0:
                if fvg.direction == TradeDirection.LONG:
                    prox = (current_price - fvg.top) / fvg.top
                else:
                    prox = (fvg.bottom - current_price) / fvg.bottom
                if 0 < prox <= 0.01:  # within 1% of zone edge
                    self.signal_tracker.record_zone_hit(symbol)

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

            # === FILTER 1.5: Global consecutive loss pause ===
            if self._global_loss_pause_until:
                now = datetime.now()
                if now < self._global_loss_pause_until:
                    remaining = (self._global_loss_pause_until - now).total_seconds()
                    last_log = self._last_entry_check_log.get("_globalloss")
                    if not last_log or (now - last_log).total_seconds() > 120:
                        logger.warning(
                            f"🛑 Global loss pause: {self._global_consecutive_losses} consecutive losses — "
                            f"{remaining:.0f}s remaining, all entries blocked"
                        )
                        self._last_entry_check_log["_globalloss"] = now
                    return
                else:
                    # Pause expired
                    self._global_loss_pause_until = None

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

            # === FILTER 4.5: Correlation Guard ===
            # Block opening correlated positions in same direction
            # e.g. XAUTUSDT LONG + PAXGUSDT LONG = double gold exposure
            if self.config.multi_symbol.correlation_guard_enabled:
                max_corr = self.config.multi_symbol.max_correlated_same_dir
                # Find which group this symbol belongs to
                sym_group = None
                for grp_name, grp_symbols in CORRELATION_GROUPS.items():
                    if symbol in grp_symbols:
                        sym_group = grp_name
                        break
                if sym_group:
                    grp_syms = CORRELATION_GROUPS[sym_group]
                    corr_count = sum(
                        1 for s, st in self.symbol_states.items()
                        if s != symbol and s in grp_syms
                        and st.has_position and st.current_order
                        and st.current_order.get('direction') == fvg.direction
                    )
                    if corr_count >= max_corr:
                        now = datetime.now()
                        last_log = self._last_entry_check_log.get(f"{symbol}_corr")
                        if not last_log or (now - last_log).total_seconds() > 120:
                            open_syms = [
                                s for s, st in self.symbol_states.items()
                                if s in grp_syms and st.has_position
                                and st.current_order
                                and st.current_order.get('direction') == fvg.direction
                            ]
                            logger.info(
                                f"🚫 Correlation guard: {symbol} {fvg.direction.value} blocked — "
                                f"group '{sym_group}' already has {corr_count} {fvg.direction.value}: "
                                f"{', '.join(open_syms)}"
                            )
                            self._last_entry_check_log[f"{symbol}_corr"] = now
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
                # Full block — direction is losing money
                now = datetime.now()
                last_log = self._last_entry_check_log.get(f"dirblock_{dir_str}")
                if not last_log or (now - last_log).total_seconds() > 120:
                    remaining = self.config.risk.direction_nerf_duration_seconds - (
                        now - self._direction_penalty_start.get(dir_str, now)
                    ).total_seconds()
                    logger.info(
                        f"🚫 Direction block: {symbol} {dir_str} — "
                        f"{dir_str} is losing money, blocked for {remaining/60:.0f}min"
                    )
                    self._last_entry_check_log[f"dirblock_{dir_str}"] = now
                return

            # === FILTER 5.5: BTC Market Regime Detector ===
            # Dynamic market regime: BTC 1h trend determines bull/bear market
            # Bull market → LONGs get boost (easier entry), SHORTs get nerfed (harder)
            # Bear market → SHORTs get boost, LONGs get nerfed
            btc_leader = trend_cfg.btc_leader_enabled
            btc_nerf_mult = trend_cfg.btc_leader_nerf_multiplier
            btc_boost_div = getattr(trend_cfg, 'btc_leader_boost_divisor', 1.5)
            btc_nerfed = False
            btc_boosted = False
            if btc_leader and symbol != "BTCUSDT":
                btc_score_15m = self._score_15m.get("BTCUSDT", 0.0)
                btc_score_1h = self._score_1h.get("BTCUSDT", 0.0)
                btc_combined = trend_cfg.weight_15m * btc_score_15m + trend_cfg.weight_1h * btc_score_1h
                # NERF counter-regime direction (make it harder to enter against BTC trend)
                if fvg.direction == TradeDirection.SHORT and btc_combined > trend_cfg.entry_threshold:
                    threshold *= btc_nerf_mult
                    btc_nerfed = True
                    trend_info += f" 🔶BTC↑{btc_combined:+.2f}→SHORT×{btc_nerf_mult:.0f}"
                elif fvg.direction == TradeDirection.LONG and btc_combined < -trend_cfg.entry_threshold:
                    long_threshold *= btc_nerf_mult
                    btc_nerfed = True
                    trend_info += f" 🔶BTC↓{btc_combined:+.2f}→LONG×{btc_nerf_mult:.0f}"
                # BOOST with-regime direction (make it easier to enter with BTC trend)
                if not btc_nerfed and btc_boost_div > 1.0:
                    if fvg.direction == TradeDirection.LONG and btc_combined > trend_cfg.entry_threshold:
                        long_threshold /= btc_boost_div
                        btc_boosted = True
                        trend_info += f" 🟢BTC↑{btc_combined:+.2f}→LONG÷{btc_boost_div:.1f}"
                    elif fvg.direction == TradeDirection.SHORT and btc_combined < -trend_cfg.entry_threshold:
                        threshold /= btc_boost_div
                        btc_boosted = True
                        trend_info += f" 🟢BTC↓{btc_combined:+.2f}→SHORT÷{btc_boost_div:.1f}"

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

            # Skip trend threshold when BOS will override direction anyway
            _skip_trend_gate = (trend_cfg.bos_enabled and
                                getattr(trend_cfg, 'bos_direction_override', False))

            if not _skip_trend_gate and fvg.direction == TradeDirection.LONG and combined_score <= long_threshold:
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

            if not _skip_trend_gate and fvg.direction == TradeDirection.SHORT and combined_score >= -threshold:
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

            # === Confluence sizing multiplier (soft BOS/HTF mode) ===
            confluence_mult = 1.0
            bos_passed = True
            htf_passed = True

            # === FILTER 5.8: BOS (Break of Structure) ===
            # bos_direction_override=True: BOS DETERMINES trade direction.
            #   BULLISH BOS → LONG, BEARISH BOS → SHORT. FVG only provides entry level.
            #   No recent BOS → use FVG direction with soft penalty (half size).
            # bos_direction_override=False: legacy filter mode.
            if trend_cfg.bos_enabled:
                ms = self._market_structures.get(symbol)
                bos_max_age = trend_cfg.bos_max_age_candles
                if ms and ms._total_candles_seen >= 10:
                    bos_recent = ms.is_bos_recent(bos_max_age)

                    bos_min_hold = getattr(trend_cfg, 'bos_min_hold_candles', 3)

                    if trend_cfg.bos_direction_override:
                        # === BOS-as-direction mode ===
                        # BOS aligned → enter normally. BOS contradicts FVG → BLOCK.
                        # FVG zone edges define the entry level AND the direction.
                        # Overriding direction creates wrong-direction entries
                        # (e.g. shorting at bullish support → instant SL hit).
                        if bos_recent:
                            # Check BOS stability — prevent flip-flop entries
                            if not ms.is_bos_stable(bos_min_hold):
                                bos_passed = False
                                now = datetime.now()
                                last_log = self._last_entry_check_log.get(f"{symbol}_bos_unstable")
                                if not last_log or (now - last_log).total_seconds() > 120:
                                    logger.info(
                                        f"🚫 BOS unstable: {symbol} {dir_str} — "
                                        f"BOS changed {ms._candles_since_bos_change}c ago, "
                                        f"need {bos_min_hold}c hold [{ms.get_bos_info()}]"
                                    )
                                    self._last_entry_check_log[f"{symbol}_bos_unstable"] = now
                                if not trend_cfg.bos_soft_mode:
                                    return
                                confluence_mult *= trend_cfg.soft_mode_size_mult
                            else:
                                from src.market_structure import TrendDirection
                                bos_dir = ms.last_bos_direction
                                new_dir = TradeDirection.LONG if bos_dir == TrendDirection.BULLISH else TradeDirection.SHORT
                                if new_dir != fvg.direction:
                                    now = datetime.now()
                                    last_log = self._last_entry_check_log.get(f"{symbol}_bos_conflict")
                                    if not last_log or (now - last_log).total_seconds() > 120:
                                        logger.info(
                                            f"🚫 BOS conflict: {symbol} FVG={fvg.direction.value} "
                                            f"but BOS={new_dir.value} — blocking "
                                            f"(entry level wrong for opposite direction) "
                                            f"[{ms.get_bos_info()}]"
                                        )
                                        self._last_entry_check_log[f"{symbol}_bos_conflict"] = now
                                    return
                                # BOS aligned with FVG + stable → perfect, full size
                        else:
                            # No recent BOS
                            bos_passed = False
                            if trend_cfg.bos_soft_mode:
                                # Soft: use FVG direction but half size
                                confluence_mult *= trend_cfg.soft_mode_size_mult
                                now = datetime.now()
                                last_log = self._last_entry_check_log.get(f"{symbol}_bos")
                                if not last_log or (now - last_log).total_seconds() > 120:
                                    logger.info(
                                        f"⚠️ BOS soft: {symbol} {dir_str} — no recent BOS, "
                                        f"using FVG direction, position ×{trend_cfg.soft_mode_size_mult} "
                                        f"[{ms.get_bos_info()}]"
                                    )
                                    self._last_entry_check_log[f"{symbol}_bos"] = now
                            else:
                                # Hard: block entry completely
                                now = datetime.now()
                                last_log = self._last_entry_check_log.get(f"{symbol}_bos")
                                if not last_log or (now - last_log).total_seconds() > 120:
                                    logger.info(
                                        f"🚫 BOS hard: {symbol} {dir_str} blocked — "
                                        f"no recent BOS within {bos_max_age} candles "
                                        f"[{ms.get_bos_info()}]"
                                    )
                                    self._last_entry_check_log[f"{symbol}_bos"] = now
                                return
                    else:
                        # === Legacy filter mode ===
                        bos_aligned = ms.is_bos_aligned(dir_str, max_age_candles=bos_max_age)
                        if not bos_aligned:
                            counter_bos = bos_recent and not bos_aligned

                            if counter_bos:
                                now = datetime.now()
                                last_log = self._last_entry_check_log.get(f"{symbol}_bos")
                                if not last_log or (now - last_log).total_seconds() > 120:
                                    logger.info(
                                        f"🚫 BOS counter: {symbol} {dir_str} blocked — "
                                        f"BOS is opposite direction "
                                        f"[{ms.get_bos_info()}]"
                                    )
                                    self._last_entry_check_log[f"{symbol}_bos"] = now
                                return

                            bos_passed = False
                            if trend_cfg.bos_soft_mode:
                                confluence_mult *= trend_cfg.soft_mode_size_mult
                                now = datetime.now()
                                last_log = self._last_entry_check_log.get(f"{symbol}_bos")
                                if not last_log or (now - last_log).total_seconds() > 120:
                                    logger.info(
                                        f"⚠️ BOS soft: {symbol} {dir_str} — no BOS, "
                                        f"position ×{trend_cfg.soft_mode_size_mult} "
                                        f"[{ms.get_bos_info()}]"
                                    )
                                    self._last_entry_check_log[f"{symbol}_bos"] = now
                            else:
                                now = datetime.now()
                                last_log = self._last_entry_check_log.get(f"{symbol}_bos")
                                if not last_log or (now - last_log).total_seconds() > 120:
                                    logger.info(
                                        f"🚫 BOS filter: {symbol} {dir_str} blocked — "
                                        f"no {dir_str} BOS within {bos_max_age} candles "
                                        f"[{ms.get_bos_info()}]"
                                    )
                                    self._last_entry_check_log[f"{symbol}_bos"] = now
                                return
                # ms not initialized yet (warmup) → block
                else:
                    now = datetime.now()
                    last_log = self._last_entry_check_log.get(f"{symbol}_bos_init")
                    if not last_log or (now - last_log).total_seconds() > 300:
                        logger.info(
                            f"🚫 BOS filter: {symbol} {dir_str} blocked — "
                            f"market structure not initialized yet"
                        )
                        self._last_entry_check_log[f"{symbol}_bos_init"] = now
                    return

            # === FILTER 5.85: Liquidity Sweep (bonus-only) ===
            # Check if price recently swept a swing point (took out liquidity) before entering.
            # Sweep confirmed → bonus ×1.25. No sweep → no penalty (×1.0).
            # Rationale: absence of sweep doesn't make entry worse, just less confirmed.
            if trend_cfg.sweep_enabled:
                ms = self._market_structures.get(symbol)
                if ms and ms._total_candles_seen >= 10:
                    candles = self.ws_handler.get_candle_buffer(symbol)
                    if candles and len(candles) >= 5:
                        swept, sweep_info = ms.check_liquidity_sweep(
                            candles, dir_str, max_age_candles=trend_cfg.sweep_max_age_candles
                        )
                        if swept:
                            confluence_mult *= trend_cfg.sweep_bonus_mult
                            trend_info += f" [🧹{sweep_info}]"

            # === FILTER 5.9: HTF (1h) FVG Zone Confluence ===
            # 15m FVG must overlap with a 1h FVG zone of the same direction.
            # This ensures we only trade at structurally significant imbalance levels.
            if trend_cfg.htf_fvg_enabled:
                is_confluent, overlap_pct, htf_info = self._check_htf_confluence(symbol, fvg)
                if is_confluent:
                    htf_passed = True
                    # Boost FVG strength for confluent entries
                    bonus = trend_cfg.htf_fvg_strength_bonus * overlap_pct
                    fvg.strength = min(1.0, fvg.strength + bonus)
                    trend_info += f" [{htf_info}]"
                else:
                    htf_passed = False
                    if trend_cfg.htf_soft_mode:
                        # Soft mode: reduce position size instead of blocking
                        confluence_mult *= trend_cfg.soft_mode_size_mult
                        now = datetime.now()
                        last_log = self._last_entry_check_log.get(f"{symbol}_htf")
                        if not last_log or (now - last_log).total_seconds() > 120:
                            logger.info(
                                f"⚠️ HTF soft: {symbol} {dir_str} — no 1h zone, "
                                f"position ×{trend_cfg.soft_mode_size_mult} "
                                f"[{htf_info}]"
                            )
                            self._last_entry_check_log[f"{symbol}_htf"] = now
                    else:
                        # Hard mode: block entry completely
                        now = datetime.now()
                        last_log = self._last_entry_check_log.get(f"{symbol}_htf")
                        if not last_log or (now - last_log).total_seconds() > 120:
                            logger.info(
                                f"🚫 HTF confluence: {symbol} {dir_str} blocked — "
                                f"no 1h FVG zone for {dir_str} [{htf_info}]"
                            )
                            self._last_entry_check_log[f"{symbol}_htf"] = now
                        return

            # === Confluence bonus: both BOS + HTF confirmed → bigger position ===
            if bos_passed and htf_passed and trend_cfg.bos_enabled and trend_cfg.htf_fvg_enabled:
                confluence_mult *= trend_cfg.confluence_bonus_mult

            # === FILTER 5.95: Trial Symbol Filter ===
            # Non-proven, non-core symbols get reduced position size (soft mode)
            # instead of hard blocks — lets them prove profitability for promotion
            is_proven_sym = (
                symbol in set(self.config.core_symbols)
                or (self._rotation and self._rotation.is_proven(symbol))
            )
            if not is_proven_sym:
                rotation_cfg = self.config.rotation
                # Baseline trial size reduction — unproven symbols always trade smaller
                trial_mult = getattr(rotation_cfg, 'trial_size_multiplier', 0.5)
                confluence_mult *= trial_mult
                # Extra penalty if BOS/HTF missing (on top of baseline)
                if getattr(rotation_cfg, 'trial_require_confluence', True):
                    if not bos_passed or not htf_passed:
                        confluence_mult *= 0.30  # additional ×0.30 → total ×0.15
                        now = datetime.now()
                        last_log = self._last_entry_check_log.get(f"{symbol}_trial")
                        if not last_log or (now - last_log).total_seconds() > 120:
                            missing = []
                            if not bos_passed:
                                missing.append("BOS")
                            if not htf_passed:
                                missing.append("HTF")
                            logger.info(
                                f"⚠️ Trial soft: {symbol} {dir_str} — "
                                f"missing {'+'.join(missing)}, position ×{trial_mult*0.30:.2f}"
                            )
                            self._last_entry_check_log[f"{symbol}_trial"] = now

                # Deeper fill requirement for trial symbols (still hard block)
                trial_min_fill = getattr(rotation_cfg, 'trial_min_fill', 0.30)
                if fvg.fill_percent < trial_min_fill:
                    now = datetime.now()
                    last_log = self._last_entry_check_log.get(f"{symbol}_trialfill")
                    if not last_log or (now - last_log).total_seconds() > 120:
                        logger.info(
                            f"🚫 Trial filter: {symbol} {dir_str} — "
                            f"fill {fvg.fill_percent*100:.0f}% < {trial_min_fill*100:.0f}% "
                            f"(proven symbols: {self.config.fvg.entry_zone_min*100:.0f}%)"
                        )
                        self._last_entry_check_log[f"{symbol}_trialfill"] = now
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

            # === FILTER 9: Exhaustion Reversal Detection ===
            # 3+ consecutive strong candles in one direction = momentum exhaustion
            # - Entry OPPOSITE to exhaustion (reversal) → confluence boost ×1.5
            # - Entry SAME direction (chasing exhaustion) → BLOCK
            # - No exhaustion → normal entry (no change)
            exhaustion_enabled = getattr(self.config.trend, 'exhaustion_enabled', True)
            if exhaustion_enabled and candles and len(candles) >= 10:
                exh_dir, exh_count, exh_avg = self._detect_exhaustion(symbol, candles)
                if exh_dir:
                    exh_boost = getattr(self.config.trend, 'exhaustion_boost_mult', 1.5)
                    is_reversal = (
                        (exh_dir == "BULLISH" and fvg.direction == TradeDirection.SHORT) or
                        (exh_dir == "BEARISH" and fvg.direction == TradeDirection.LONG)
                    )
                    is_chasing = (
                        (exh_dir == "BULLISH" and fvg.direction == TradeDirection.LONG) or
                        (exh_dir == "BEARISH" and fvg.direction == TradeDirection.SHORT)
                    )
                    if is_reversal:
                        # Reversal play: entering opposite to exhaustion → boost confidence
                        confluence_mult *= exh_boost
                        now = datetime.now()
                        last_log = self._last_entry_check_log.get(f"{symbol}_exh")
                        if not last_log or (now - last_log).total_seconds() > 120:
                            logger.info(
                                f"🔥 Exhaustion reversal: {symbol} {fvg.direction.value} — "
                                f"{exh_count}× {exh_dir.lower()} candles "
                                f"(avg body {exh_avg:.1f}×ATR) → position ×{exh_boost}"
                            )
                            self._last_entry_check_log[f"{symbol}_exh"] = now
                    elif is_chasing:
                        # Chasing exhaustion: dangerous, block entry
                        now = datetime.now()
                        last_log = self._last_entry_check_log.get(f"{symbol}_exh")
                        if not last_log or (now - last_log).total_seconds() > 120:
                            logger.info(
                                f"🚫 Exhaustion chase: {symbol} {fvg.direction.value} blocked — "
                                f"{exh_count}× {exh_dir.lower()} candles "
                                f"(avg body {exh_avg:.1f}×ATR), don't chase"
                            )
                            self._last_entry_check_log[f"{symbol}_exh"] = now
                        return

            # === CONFLUENCE FLOOR: block trades with stacked soft-mode penalties ===
            # When BOS(×0.5) + HTF(×0.5) + Trial(×0.5) stack → ×0.125 = micro-position
            # that pays the same fee but wins ~$0.12. Block instead of entering tiny.
            min_confluence = 0.30
            if confluence_mult < min_confluence:
                now = datetime.now()
                last_log = self._last_entry_check_log.get(f"{symbol}_conffloor")
                if not last_log or (now - last_log).total_seconds() > 120:
                    logger.info(
                        f"🚫 Confluence floor: {symbol} {dir_str} — "
                        f"conf×{confluence_mult:.2f} < {min_confluence} "
                        f"(too many soft penalties stacked, blocking)"
                    )
                    self._last_entry_check_log[f"{symbol}_conffloor"] = now
                return

            # Build entry context info
            bos_info = ""
            if trend_cfg.bos_enabled:
                ms = self._market_structures.get(symbol)
                if ms:
                    bos_info = f" [{ms.get_bos_info()}]"
            htf_info_tag = ""
            if trend_cfg.htf_fvg_enabled:
                htf_zones = self._htf_fvg_zones.get(symbol, [])
                htf_info_tag = f" [HTF:{len(htf_zones)}z]"

            # Confluence sizing tag for log
            conf_tag = ""
            if confluence_mult != 1.0:
                conf_tag = f" [conf×{confluence_mult:.2f}]"

            # Symbol tier tag for log
            tier_tag = ""
            if symbol in set(self.config.core_symbols):
                tier_tag = " [CORE]"
            elif is_proven_sym:
                tier_tag = " [PROVEN]"
            else:
                tier_tag = " [TRIAL]"

            logger.info(
                f"🎯 Entry triggered: {symbol} "
                f"{fvg.direction.value} @ {current_price:.4f} ({reason})"
                f"{bos_info}{htf_info_tag}{conf_tag}{tier_tag}"
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
            await self._execute_entry(symbol, fvg, current_price, confluence_mult)
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
        confluence_mult: float = 1.0,
    ) -> None:
        """Execute a trade entry: market order → confirm fill → set TP/SL.
        
        Args:
            confluence_mult: Position size multiplier from BOS/HTF soft mode.
                            <1.0 = missing confluence (smaller position)
                            >1.0 = full confluence bonus (larger position)
        """
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

            if fvg.signal_source == "ema":
                # === EMA signal: pure ATR-based TP/SL ===
                from .tpsl_calculator import TPSLLevels
                atr = TPSLCalculator.calculate_atr(candles, period=self.config.tpsl.atr_period)
                atr = max(atr, entry_price * self.config.tpsl.atr_floor_pct)
                sl_distance = atr * 1.5  # 1.5 ATR stop
                tp_distance = sl_distance * self.config.tpsl.force_close_at_r  # 3R target

                if fvg.direction == TradeDirection.LONG:
                    sl_price = entry_price - sl_distance
                    tp_price = entry_price + tp_distance
                else:
                    sl_price = entry_price + sl_distance
                    tp_price = entry_price - tp_distance

                sl_price = max(sl_price, 0.0001)
                tp_price = max(tp_price, 0.0001)

                tpsl = TPSLLevels(
                    tp_price=tp_price,
                    sl_price=sl_price,
                    risk_amount=sl_distance,
                    reward_amount=tp_distance,
                    atr=atr,
                    method="ema_atr",
                )
                logger.info(
                    f"📊 EMA TP/SL [{symbol} {fvg.direction.value}]: "
                    f"SL={fmt_price(sl_price)} ({sl_distance/entry_price*100:.3f}%, 1.5×ATR) "
                    f"TP={fmt_price(tp_price)} ({tp_distance/entry_price*100:.3f}%, {self.config.tpsl.force_close_at_r}R) "
                    f"ATR={fmt_price(atr)}"
                )
            else:
                # === FVG signal: zone-based TP/SL (legacy) ===
                tpsl = self.tpsl_calculator.calculate(
                    entry_price=entry_price,
                    fvg=fvg,
                    candles=candles,
                    htf_trend=htf_trend,
                )

            # Guard: reject entry if SL distance is too wide (high ATR = too volatile)
            sl_distance_pct = abs(tpsl.sl_price - entry_price) / entry_price
            max_sl_pct = 0.025  # 2.5% max SL distance — wider means TP > 7.5% (unrealistic for 15m FVG)
            if sl_distance_pct > max_sl_pct:
                logger.info(
                    f"🚫 Skipping {symbol} {fvg.direction.value}: "
                    f"SL too wide {sl_distance_pct*100:.2f}% > {max_sl_pct*100:.1f}% max "
                    f"(high ATR = volatile, entry would be noise-killed)"
                )
                if state:
                    state.has_position = False
                    state.current_order = None
                return

            # Guard: reject entry if 1R < min_ticks — price granularity too coarse for meaningful R:R
            # BOME ($0.0004, 4 decimals) → tick=0.0001 → if 1R=0.0001 that's 1 tick, R:R distorted ±50%
            min_ticks = 4
            price_decimals = PRICE_PRECISION.get(symbol, 2)
            tick_size = 10 ** (-price_decimals)
            risk_abs = abs(tpsl.sl_price - entry_price)
            risk_ticks = risk_abs / tick_size if tick_size > 0 else 999
            if risk_ticks < min_ticks:
                logger.info(
                    f"🚫 Skipping {symbol} {fvg.direction.value}: "
                    f"1R={fmt_price(risk_abs)} = {risk_ticks:.1f} ticks "
                    f"(min {min_ticks} ticks, tick={tick_size}) — price too coarse"
                )
                if state:
                    state.has_position = False
                    state.current_order = None
                return

            # Apply confluence multiplier to risk (soft BOS/HTF mode)
            effective_risk = self._current_risk_percent * confluence_mult
            if confluence_mult != 1.0:
                logger.info(
                    f"📊 Confluence sizing: {symbol} risk {self._current_risk_percent*100:.1f}% "
                    f"× {confluence_mult:.2f} = {effective_risk*100:.1f}%"
                )

            position_size = self.position_sizer.calculate(
                balance=balance.available,
                entry_price=entry_price,
                sl_distance_percent=sl_distance_pct,
                leverage=leverage,
                symbol=symbol,
                risk_override=effective_risk,
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
                # Auto-blacklist symbols rejected with [20012] (not tradeable)
                if "20012" in str(order_result.error) and self._rotation:
                    self._rotation._blacklist.add(symbol)
                    logger.warning(
                        f"🚫 Auto-blacklisted {symbol} — [20012] not tradeable on exchange"
                    )
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

            if fvg.signal_source == "ema":
                # === EMA signal: recalculate with actual fill price ===
                from .tpsl_calculator import TPSLLevels
                atr = TPSLCalculator.calculate_atr(candles, period=self.config.tpsl.atr_period)
                atr = max(atr, avg_open_price * self.config.tpsl.atr_floor_pct)
                sl_distance = atr * 1.5
                tp_distance = sl_distance * self.config.tpsl.force_close_at_r

                if fvg.direction == TradeDirection.LONG:
                    sl_price = avg_open_price - sl_distance
                    tp_price = avg_open_price + tp_distance
                else:
                    sl_price = avg_open_price + sl_distance
                    tp_price = avg_open_price - tp_distance

                sl_price = max(sl_price, 0.0001)
                tp_price = max(tp_price, 0.0001)

                tpsl = TPSLLevels(
                    tp_price=tp_price,
                    sl_price=sl_price,
                    risk_amount=sl_distance,
                    reward_amount=tp_distance,
                    atr=atr,
                    method="ema_atr",
                )
            else:
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
                            f"TP={fmt_price(tpsl.tp_price)}, SL={fmt_price(tpsl.sl_price)} "
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

            # Persist original_risk for restart recovery
            if position_id and self._position_manager:
                self._position_manager.save_open_position(str(position_id), {
                    "symbol": symbol,
                    "original_risk": abs(avg_open_price - tpsl.sl_price),
                    "entry_price": avg_open_price,
                    "tp_price": tpsl.tp_price,
                    "sl_price": tpsl.sl_price,
                    "direction": fvg.direction.value,
                })

            logger.info(
                f"🎯 Trade complete: {symbol} {fvg.direction.value} "
                f"entry={fmt_price(avg_open_price)} TP={fmt_price(tpsl.tp_price)} "
                f"SL={fmt_price(tpsl.sl_price)} qty={position_size.quantity:.6f} "
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

            # --- Post-entry verification ---
            # WS close events are dropped during _order_pending window.
            # If the position was SL'd/TP'd during that window (~5-10s),
            # the close event is lost forever. Verify via REST now.
            await asyncio.sleep(2.0)
            if state and state.has_position:
                try:
                    verify_positions = await self.exchange.get_positions(symbol=symbol)
                    still_open = verify_positions and any(
                        float(p.get("qty", 0)) > 0 for p in verify_positions
                    )
                    if not still_open:
                        logger.warning(
                            f"⚠️ [POST-ENTRY] {symbol} already closed during order setup! "
                            f"Clearing phantom position."
                        )
                        state.has_position = False
                        state.active_fvg = None
                        state.current_order = None
                        state.trailing_state = "initial"
                        state.partial_tp_done = False
                        state.original_qty = 0.0
                        state.trailing_sl_price = 0.0
                        state._order_pending = False
                except Exception as ve:
                    logger.debug(f"Post-entry verify failed for {symbol}: {ve}")

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

    # ==================== Fixed 3R TP + BE Protection ====================
    #
    # Phase 1 (Initial):    SL = ATR-based, TP = 3R (exchange closes at 3R)
    # Phase 2 (Breakeven):  At ≥1.5R → SL = entry + 0.05R (protect capital)
    # Phase 3 (Runner):     DISABLED (runner_at=99R, never activates)
    #
    # Strategy: let winners hit 3R TP on exchange. No trailing interference.
    # The trailing system produced 111 micro-BE exits ($0.14 avg) from 215 wins.
    # Fixed 3R TP simulation: +$129 over 500 trades (+$6.13/day) vs -$77 with trailing.
    #
    # Phase transitions clamp SL to never lose locked profit.

    def _get_runner_sl_distance(self, profit_r: float) -> float:
        """Get dynamic runner SL distance — two-stage tightening.

        Stage 1: runner_at → tighten_at: base_dist → mid_dist (tighten_min_distance_r)
        Stage 2: tighten_at → force_close_at_r: mid_dist → final_dist (tighten_final_distance_r)
        """
        tpsl = self.config.tpsl
        base_dist = tpsl.trailing_runner_sl_distance_r
        runner_at = tpsl.trailing_runner_at_r
        tighten_at = tpsl.trailing_tighten_at_r
        mid_dist = tpsl.trailing_tighten_min_distance_r
        final_dist = tpsl.trailing_tighten_final_distance_r
        tp_r = tpsl.force_close_at_r

        if profit_r <= runner_at:
            return base_dist

        if profit_r <= tighten_at and tighten_at > runner_at:
            # Stage 1: base_dist → mid_dist
            progress = (profit_r - runner_at) / (tighten_at - runner_at)
            return base_dist - (base_dist - mid_dist) * progress

        if tp_r > tighten_at:
            # Stage 2: mid_dist → final_dist
            progress = min(1.0, (profit_r - tighten_at) / (tp_r - tighten_at))
            return mid_dist - (mid_dist - final_dist) * progress

        return mid_dist

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
            be_at = tpsl_cfg.trailing_breakeven_at_r
            runner_at = tpsl_cfg.trailing_runner_at_r
            direct_runner_from_initial = (
                state.trailing_state == "initial"
                and runner_at <= be_at
                and profit_r >= runner_at
            )

            # Partial TP: trigger when not yet done and profit >= partial_tp_at_r
            if (tpsl_cfg.partial_tp_enabled
                    and not state.partial_tp_done
                    and profit_r >= tpsl_cfg.partial_tp_at_r):
                needs_action = True

            # Phase 2: Initial → Breakeven
            if (state.trailing_state == "initial"
                    and profit_r >= be_at):
                needs_action = True

            # Direct Phase 1 → Runner when runner threshold comes first.
            if direct_runner_from_initial:
                needs_action = True

            # Phase 3: Breakeven → Runner
            if (state.trailing_state == "breakeven"
                    and profit_r >= runner_at):
                needs_action = True

            # Progressive BE: slide SL during breakeven phase
            if (state.trailing_state == "breakeven"
                    and profit_r < runner_at):
                be_trail_offset = tpsl_cfg.trailing_be_trail_offset_r
                candidate_lock_r = max(tpsl_cfg.trailing_be_lock_r, profit_r - be_trail_offset)
                step_r = tpsl_cfg.trailing_step_r
                current_trail = state.trailing_sl_price or sl_price
                if direction == TradeDirection.LONG:
                    candidate_sl = entry_price + (risk * candidate_lock_r)
                    if candidate_sl > current_trail + (risk * step_r):
                        needs_action = True
                else:
                    candidate_sl = entry_price - (risk * candidate_lock_r)
                    if candidate_sl < current_trail - (risk * step_r):
                        needs_action = True

            # Runner: continuous SL+TP sliding (tightens past 50% TP)
            if state.trailing_state == "runner":
                sl_dist_r = self._get_runner_sl_distance(profit_r)
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

        be_at = tpsl_cfg.trailing_breakeven_at_r
        runner_at = tpsl_cfg.trailing_runner_at_r

        # ── Phase 2/3 from Initial ──
        if state.trailing_state == "initial":
            if runner_at <= be_at and profit_r >= runner_at:
                sl_dist_r = self._get_runner_sl_distance(profit_r)
                current_trail = state.trailing_sl_price or sl_price
                if direction == TradeDirection.LONG:
                    candidate_sl = current_price - (risk * sl_dist_r)
                    new_sl = max(candidate_sl, current_trail)
                else:
                    candidate_sl = current_price + (risk * sl_dist_r)
                    new_sl = min(candidate_sl, current_trail)
                # TP already set at force_close_at_r on entry — no need to change it
                phase_change = "runner"
                logger.info(
                    f"🚀 Phase 3 (Runner): {symbol} SL→{new_sl:.4f} "
                    f"(profit={profit_r:.1f}R, "
                    f"direct from initial because runner_at={runner_at:.2f}R "
                    f"<= be_at={be_at:.2f}R)"
                )
            elif profit_r >= be_at:
                lock_r = tpsl_cfg.trailing_be_lock_r
                if direction == TradeDirection.LONG:
                    new_sl = entry_price + (risk * lock_r)
                else:
                    new_sl = entry_price - (risk * lock_r)
                # TP already set at force_close_at_r on entry — no need to change it
                phase_change = "breakeven"
                logger.info(
                    f"🔒 Phase 2 (BE): {symbol} SL→{new_sl:.4f} "
                    f"(entry+{lock_r}R locked, profit={profit_r:.1f}R)"
                )

        # ── Phase 3: Breakeven → Runner ──
        elif state.trailing_state == "breakeven":
            if profit_r >= runner_at:
                sl_dist_r = self._get_runner_sl_distance(profit_r)
                current_trail = state.trailing_sl_price or sl_price
                if direction == TradeDirection.LONG:
                    candidate_sl = current_price - (risk * sl_dist_r)
                    new_sl = max(candidate_sl, current_trail)
                else:
                    candidate_sl = current_price + (risk * sl_dist_r)
                    new_sl = min(candidate_sl, current_trail)
                # TP already set at force_close_at_r on entry — no need to change it
                phase_change = "runner"
                logger.info(
                    f"🚀 Phase 3 (Runner): {symbol} SL→{new_sl:.4f} "
                    f"(profit={profit_r:.1f}R, SL={sl_dist_r:.2f}R behind, "
                    f"clamped={'yes' if new_sl != candidate_sl else 'no'})"
                )
            else:
                # Progressive BE: slide SL as profit grows
                be_trail_offset = tpsl_cfg.trailing_be_trail_offset_r
                candidate_lock_r = max(tpsl_cfg.trailing_be_lock_r, profit_r - be_trail_offset)
                step_r = tpsl_cfg.trailing_step_r
                current_trail = state.trailing_sl_price or sl_price
                if direction == TradeDirection.LONG:
                    new_sl_candidate = entry_price + (risk * candidate_lock_r)
                    if new_sl_candidate > current_trail + (risk * step_r):
                        new_sl = new_sl_candidate
                        logger.info(
                            f"🔒 Progressive BE: {symbol} SL→{new_sl:.4f} "
                            f"(lock={candidate_lock_r:.2f}R, profit={profit_r:.1f}R)"
                        )
                else:
                    new_sl_candidate = entry_price - (risk * candidate_lock_r)
                    if new_sl_candidate < current_trail - (risk * step_r):
                        new_sl = new_sl_candidate
                        logger.info(
                            f"🔒 Progressive BE: {symbol} SL→{new_sl:.4f} "
                            f"(lock={candidate_lock_r:.2f}R, profit={profit_r:.1f}R)"
                        )

        # ── Runner: continuous SL sliding (tightens past 50% TP) ──
        elif state.trailing_state == "runner":
            sl_dist_r = self._get_runner_sl_distance(profit_r)
            step_r = tpsl_cfg.trailing_step_r
            current_trail = state.trailing_sl_price or sl_price

            if direction == TradeDirection.LONG:
                candidate_sl = current_price - (risk * sl_dist_r)
                # Only move SL up (never down), and only if meaningful step
                if candidate_sl > current_trail + (risk * step_r):
                    new_sl = candidate_sl
            else:
                candidate_sl = current_price + (risk * sl_dist_r)
                # Only move SL down (never up), and only if meaningful step
                if candidate_sl < current_trail - (risk * step_r):
                    new_sl = candidate_sl

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
                    current_price=current_price,
                    direction=direction.value if direction else None,
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
                    sl_dist_r = self._get_runner_sl_distance(profit_r)
                    logger.info(
                        f"📈 Runner slide: {symbol} SL={new_sl:.4f} "
                        f"({profit_r:.1f}R, trail={sl_dist_r:.2f}R)"
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
