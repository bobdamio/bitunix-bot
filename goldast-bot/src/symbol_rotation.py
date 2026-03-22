"""
Symbol Rotation -- Daily automatic symbol selection.

Runs the FVG scanner in-process, scores all available pairs,
selects the top N, fetches precision from exchange API, and
hot-swaps the active symbol list without restart.

Components:
1. Scanner: scores pairs on 7 FVG-suitability metrics
2. Precision fetcher: gets basePrecision/quotePrecision from API
3. Hot-swap: updates symbol_states, WS subscriptions, precision dicts
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SymbolInfo:
    """Exchange symbol metadata."""
    symbol: str
    price_precision: int  # quotePrecision (decimal places for price)
    qty_precision: int    # basePrecision (decimal places for qty)
    min_trade_volume: float
    max_leverage: int


@dataclass
class ScanResult:
    """Result of scanning a single symbol."""
    symbol: str
    score: float
    price: float
    vol_24h: float
    fvg_density: float
    fill_rate: float
    bounce_rate: float
    atr_pct: float
    vol_spike: float
    trend_clarity: float
    wick_body: float
    avg_r_achieved: float = 0.0       # Average max R move after fill (key profitability metric)
    trend_aligned_pct: float = 0.0    # % of FVGs aligned with EMA trend direction
    signal_rate: float = 0.0          # Predicted tradeable signals per 10h window
    avg_vol_ratio: float = 0.0        # Average volume ratio on FVG candles
    proximity_score: float = 0.0      # How close current price is to nearest valid FVG (0-100)
    recent_fvg_pct: float = 0.0       # % of FVGs formed in last 20 candles (recent activity)
    retest_speed: float = 0.0         # Avg candles until first retest (lower = faster fills)
    zone_approach_rate: float = 0.0   # % of FVGs where price comes within 0.5% of zone edge
    current_trend_strength: float = 0.0  # Abs trend score right now (0-1, higher = stronger trend)
    actionable_proximity: float = 0.0    # Proximity ONLY to zones where trend passes threshold (0-100)
    actionable_zone_count: int = 0       # Number of current FVGs whose direction passes trend filter
    bos_aligned_count: int = 0           # FVGs whose direction matches recent BOS
    htf_confluent_count: int = 0         # FVGs overlapping with 1h FVG zone
    convergence_score: float = 0.0       # Is price moving TOWARD nearest zone? (0-100)


class SymbolRotation:
    """Manages daily symbol rotation with FVG scoring."""

    def __init__(self, exchange, config):
        """
        Args:
            exchange: ExchangeAdapter instance (for API calls)
            config: Full Config object
        """
        self.exchange = exchange
        self.config = config
        self._config_path = "config.yaml"  # Will be updated from bot
        self._proven_path = "data/proven_symbols.json"
        self._last_rotation: Optional[datetime] = None
        self._symbol_info_cache: Dict[str, SymbolInfo] = {}
        # Core symbols -- always active, never removed
        self._pinned_symbols: Set[str] = set(getattr(config, 'core_symbols', []))
        # Blacklist -- never added by rotation
        self._blacklist: Set[str] = set(getattr(config, 'blacklist', []))
        # Proven symbols -- profitable rotation symbols, kept indefinitely
        self._proven_stats: Dict[str, dict] = {}  # {symbol: {promoted_at, net_pnl, trades, win_rate}}
        self._proven_symbols: Set[str] = self._load_proven()
        # PnL-based ban: {symbol: ban_until_datetime}
        self._pnl_ban_until: Dict[str, datetime] = {}
        # Rotation cooldown: recently removed symbols can't come back immediately
        # {symbol: removed_at_datetime}
        self._removed_cooldown: Dict[str, datetime] = {}

    # ==================== Proven Symbols Persistence ====================

    def _load_proven(self) -> Set[str]:
        """Load proven symbols from data/proven_symbols.json.
        
        Handles both formats:
        - Legacy list: ["SYMBOL1", "SYMBOL2"]
        - Rich dict: {"SYMBOL1": {"promoted_at": "...", ...}, ...}
        """
        try:
            path = Path(self._proven_path)
            if path.exists():
                with open(path, 'r') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    # Legacy format — migrate to dict on next save
                    symbols = set(data)
                    self._proven_stats = {s: {"promoted_at": datetime.now().strftime("%Y-%m-%d")} for s in symbols}
                elif isinstance(data, dict):
                    symbols = set(data.keys())
                    self._proven_stats = data
                else:
                    symbols = set()
                    self._proven_stats = {}
                # Remove any blacklisted or core (they have their own tier)
                symbols -= set(getattr(self.config, 'blacklist', []))
                symbols -= set(getattr(self.config, 'core_symbols', []))
                self._proven_stats = {k: v for k, v in self._proven_stats.items() if k in symbols}
                if symbols:
                    logger.info(f"⭐ Loaded {len(symbols)} proven symbols: {', '.join(sorted(symbols))}")
                return symbols
        except Exception as e:
            logger.warning(f"Failed to load proven symbols: {e}")
        self._proven_stats = {}
        return set()

    def _save_proven(self) -> None:
        """Persist proven symbols with stats to data/proven_symbols.json."""
        try:
            path = Path(self._proven_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Keep only symbols still in proven set
            stats = {s: self._proven_stats.get(s, {}) for s in sorted(self._proven_symbols)}
            with open(path, 'w') as f:
                json.dump(stats, f, indent=2)
            logger.info(f"💾 Saved {len(self._proven_symbols)} proven symbols")
        except Exception as e:
            logger.error(f"Failed to save proven symbols: {e}")

    def is_proven(self, symbol: str) -> bool:
        """Check if symbol has proven status (public API for strategy engine)."""
        return symbol in self._proven_symbols

    def get_proven_symbols(self) -> Set[str]:
        """Return current set of proven symbols."""
        return set(self._proven_symbols)

    # ==================== Exchange Info ====================

    async def fetch_all_symbol_info(self) -> Dict[str, SymbolInfo]:
        """Fetch trading pair details (precision, min qty) from exchange."""
        try:
            data = await self.exchange._api._request(
                "GET", "/api/v1/futures/market/trading_pairs", {}
            )
            if not isinstance(data, list):
                logger.warning("Failed to fetch trading pairs info")
                return self._symbol_info_cache

            result = {}
            for item in data:
                sym = item.get("symbol", "")
                if not sym:
                    continue
                result[sym] = SymbolInfo(
                    symbol=sym,
                    price_precision=int(item.get("quotePrecision", 2)),
                    qty_precision=int(item.get("basePrecision", 4)),
                    min_trade_volume=float(item.get("minTradeVolume", "0.0001")),
                    max_leverage=int(item.get("maxLeverage", 20)),
                )
            self._symbol_info_cache = result
            logger.info(f"📋 Fetched precision for {len(result)} trading pairs")
            return result

        except Exception as e:
            logger.error(f"Failed to fetch symbol info: {e}")
            return self._symbol_info_cache

    def get_precision(self, symbol: str) -> Tuple[int, int]:
        """Get (price_precision, qty_precision) for a symbol.
        
        Returns cached values or safe defaults.
        """
        info = self._symbol_info_cache.get(symbol)
        if info:
            return info.price_precision, info.qty_precision
        return 2, 4  # safe defaults

    # ==================== FVG Scanner ====================

    @staticmethod
    def _calculate_atr(candles: list, period: int = 14) -> float:
        """Calculate ATR from raw candle dicts."""
        if len(candles) < 2:
            return 0.0
        trs = []
        for i in range(1, len(candles)):
            h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        recent = trs[-period:] if len(trs) >= period else trs
        return sum(recent) / len(recent) if recent else 0.0

    @staticmethod
    def _ema_full(values: list, period: int) -> list:
        """Calculate EMA aligned to full input array (None for indices < period-1)."""
        result = [None] * len(values)
        if len(values) < period or period <= 0:
            return result
        sma = sum(values[:period]) / period
        result[period - 1] = sma
        mult = 2.0 / (period + 1)
        for i in range(period, len(values)):
            result[i] = values[i] * mult + result[i - 1] * (1 - mult)
        return result

    @staticmethod
    def _analyze_symbol(
        candles: list,
        min_gap: float = 0.0005,
        min_gap_atr_mult: float = 0.0,
        min_volume_ratio: float = 1.3,
        entry_zone_min: float = 0.005,
        entry_zone_max: float = 0.85,
        bounce_r: float = 2.5,
        ema_fast: int = 8,
        ema_slow: int = 21,
        min_strength: float = 0.70,
        entry_threshold: float = 0.25,
        long_entry_threshold: float = 0.30,
        weight_15m: float = 0.20,
        weight_1h: float = 0.80,
        max_zone_distance: float = 0.025,
        max_entry_distance: float = 0.020,
        bos_enabled: bool = False,
        bos_max_age_candles: int = 20,
        bos_soft_mode: bool = False,
        bos_direction_override: bool = False,
        htf_fvg_enabled: bool = False,
        htf_soft_mode: bool = False,
        htf_fvg_min_gap: float = 0.001,
        htf_zones: Optional[list] = None,
    ) -> Optional[Dict]:
        """Run FVG suitability metrics with FULL strategy pipeline simulation.

        v5 changes (CRITICAL -- scanner must match bot's entry pipeline):
        - FVG strength calculated like bot (gap×0.4 + vol_ratio×0.3 + trend×0.3)
        - FVGs rejected if strength < min_strength (matches bot's filter)
        - Trend score checked against actual entry_threshold / long_entry_threshold
        - signal_rate only counts FVGs that pass ALL bot filters
        - Returns None if 0 valid FVGs after filtering (don't waste a slot)
        - Proximity only measured to VALID FVGs (passing strength + trend)
        """
        n = len(candles)
        if n < 30:
            return None

        price = candles[-1]["c"]
        atr = SymbolRotation._calculate_atr(candles)
        atr_pct = (atr / price * 100) if price > 0 else 0

        # ATR-dynamic minimum gap (mirrors fvg_detector._compute_min_gap)
        # If min_gap_atr_mult > 0, effective min gap = max(pct_floor, atr × mult)
        if min_gap_atr_mult > 0 and atr > 0:
            min_gap = max(min_gap * price, atr * min_gap_atr_mult) / price

        # Average volume
        volumes = [c["v"] for c in candles if c["v"] > 0]
        avg_vol = sum(volumes) / len(volumes) if volumes else 1.0

        # Wick/Body ratio
        wick_body_ratios = []
        for c in candles:
            body = abs(c["c"] - c["o"])
            upper_wick = c["h"] - max(c["o"], c["c"])
            lower_wick = min(c["o"], c["c"]) - c["l"]
            total_wick = upper_wick + lower_wick
            if body > 0:
                wick_body_ratios.append(total_wick / body)
        avg_wick_body = sum(wick_body_ratios) / len(wick_body_ratios) if wick_body_ratios else 999

        # --- EMA Trend Calculation ---
        # 15m EMA fast/slow for local trend
        # 1h-equivalent EMA (×4 periods on 15m candles) for higher-timeframe trend
        closes = [c["c"] for c in candles]

        ema_f_15m = SymbolRotation._ema_full(closes, ema_fast)       # EMA 8 on 15m
        ema_s_15m = SymbolRotation._ema_full(closes, ema_slow)       # EMA 21 on 15m
        ema_f_1h = SymbolRotation._ema_full(closes, ema_fast * 4)    # EMA 32 on 15m ≈ EMA 8 on 1h
        ema_s_1h = SymbolRotation._ema_full(closes, ema_slow * 4)    # EMA 84 on 15m ≈ EMA 21 on 1h

        def get_trend_at(idx: int) -> Optional[float]:
            """Get combined trend score at candle index (-1..+1). None if insufficient data."""
            score_15m = None
            if idx < len(ema_f_15m) and ema_f_15m[idx] is not None and ema_s_15m[idx] is not None:
                diff = (ema_f_15m[idx] - ema_s_15m[idx]) / ema_s_15m[idx]
                score_15m = max(min(diff * 50, 1.0), -1.0)

            score_1h = None
            if idx < len(ema_f_1h) and ema_f_1h[idx] is not None and ema_s_1h[idx] is not None:
                diff = (ema_f_1h[idx] - ema_s_1h[idx]) / ema_s_1h[idx]
                score_1h = max(min(diff * 50, 1.0), -1.0)

            if score_15m is not None and score_1h is not None:
                return score_15m * weight_15m + score_1h * weight_1h
            elif score_1h is not None:
                return score_1h
            elif score_15m is not None:
                return score_15m
            return None

        # Trend clarity (legacy metric -- measures trendiness vs chop)
        if n >= 21:
            sma_window = 21
            above_below = []
            for i in range(sma_window, n):
                sma = sum(closes[i - sma_window:i]) / sma_window
                above_below.append(1 if closes[i] > sma else -1)
            changes = sum(1 for i in range(1, len(above_below)) if above_below[i] != above_below[i - 1])
            trend_clarity = 1.0 - (changes / max(len(above_below) - 1, 1))
        else:
            trend_clarity = 0.5

        # --- FVG Detection with Volume Ratio Filter ---
        fvgs = []
        fvg_vol_ratios = []

        for i in range(n - 2):
            c1, c2, c3 = candles[i], candles[i + 1], candles[i + 2]

            def _check_vol_ratio(mid_idx: int) -> float:
                """Calculate volume ratio of middle candle vs neighbors."""
                nb_start = max(0, mid_idx - 5)
                nb_end = min(n, mid_idx + 6)
                nb_vols = [
                    candles[j]["v"] for j in range(nb_start, nb_end)
                    if j != mid_idx and candles[j]["v"] > 0
                ]
                if not nb_vols:
                    return 1.0
                return c2["v"] / (sum(nb_vols) / len(nb_vols))

            def _calc_strength(gap_pct: float, vol_ratio: float, trend_aligned: bool,
                               c2_candle: dict) -> float:
                """Calculate FVG strength matching fvg_detector._calculate_strength().
                   strength = gap_size×0.3 + volume_ratio×0.25 + trend_alignment×0.25 + impulse×0.20
                """
                gap_score = min(gap_pct * 100, 1.0)  # Cap at 1% = max
                volume_score = min(vol_ratio / 2, 1.0)  # 2x avg = max
                trend_score = 1.0 if trend_aligned else 0.0
                # Impulse: body/range ratio of gap candle
                c2_range = c2_candle["h"] - c2_candle["l"]
                if c2_range > 0:
                    imp_ratio = abs(c2_candle["c"] - c2_candle["o"]) / c2_range
                else:
                    imp_ratio = 0.0
                impulse_score = min(imp_ratio / 0.5, 1.0)  # threshold 0.5
                return gap_score * 0.3 + volume_score * 0.25 + trend_score * 0.25 + impulse_score * 0.20

            # Bullish FVG
            bull_gap = c3["l"] - c1["h"]
            if bull_gap > 0 and (bull_gap / c2["c"]) >= min_gap:
                vol_ratio = _check_vol_ratio(i + 1)
                if vol_ratio >= min_volume_ratio:
                    trend = get_trend_at(i + 2)
                    trend_aligned = trend is not None and trend > 0
                    gap_pct = bull_gap / c2["c"]
                    strength = _calc_strength(gap_pct, vol_ratio, trend_aligned, c2)
                    # Check trend passes actual entry threshold (LONG needs score >= long_entry_threshold)
                    # When bos_direction_override=true, live engine skips trend gate entirely
                    trend_passes = True if bos_direction_override else (trend is not None and trend >= long_entry_threshold)
                    # v11: Check if zone was violated by subsequent price action
                    # (matching live engine's detect_fvg_sliding_window behavior)
                    violated = False
                    for j in range(i + 3, n):
                        if candles[j]["l"] <= c1["h"]:  # price broke below zone bottom
                            violated = True
                            break
                    fvgs.append({
                        "idx": i + 2, "dir": "LONG",
                        "top": c3["l"], "bottom": c1["h"],
                        "vol": c2["v"], "vol_ratio": vol_ratio,
                        "strength": strength,
                        "trend_aligned": trend_aligned,
                        "trend_passes": trend_passes,
                        "trend_score": trend,
                        "violated": violated,
                    })
                    fvg_vol_ratios.append(vol_ratio)

            # Bearish FVG
            bear_gap = c1["l"] - c3["h"]
            if bear_gap > 0 and (bear_gap / c2["c"]) >= min_gap:
                vol_ratio = _check_vol_ratio(i + 1)
                if vol_ratio >= min_volume_ratio:
                    trend = get_trend_at(i + 2)
                    trend_aligned = trend is not None and trend < 0
                    gap_pct = bear_gap / c2["c"]
                    strength = _calc_strength(gap_pct, vol_ratio, trend_aligned, c2)
                    # Check trend passes actual entry threshold (SHORT needs |score| >= entry_threshold)
                    # When bos_direction_override=true, live engine skips trend gate entirely
                    trend_passes = True if bos_direction_override else (trend is not None and abs(trend) >= entry_threshold)
                    # v11: Check if zone was violated by subsequent price action
                    violated = False
                    for j in range(i + 3, n):
                        if candles[j]["h"] >= c1["l"]:  # price broke above zone top
                            violated = True
                            break
                    fvgs.append({
                        "idx": i + 2, "dir": "SHORT",
                        "top": c1["l"], "bottom": c3["h"],
                        "vol": c2["v"], "vol_ratio": vol_ratio,
                        "strength": strength,
                        "trend_aligned": trend_aligned,
                        "trend_passes": trend_passes,
                        "trend_score": trend,
                        "violated": violated,
                    })
                    fvg_vol_ratios.append(vol_ratio)

        # * CRITICAL: Filter FVGs by min_strength -- matches bot's fvg_detector filter
        all_fvgs = fvgs
        fvgs = [f for f in fvgs if f["strength"] >= min_strength]
        fvg_count = len(fvgs)
        fvg_density = fvg_count / n * 100 if n > 0 else 0

        # If no FVGs pass strength filter, symbol is useless -- reject early
        if fvg_count == 0:
            return None

        # --- BOS (Break of Structure) detection ---
        # 5-candle fractal pattern → detect swing highs/lows → find most recent BOS
        bos_direction = None   # "BULLISH" | "BEARISH" | None
        bos_candles_ago = 9999
        if bos_enabled and n >= 10:
            swing_highs = []  # (idx, price)
            swing_lows = []   # (idx, price)
            for i in range(2, n - 2):
                mid = candles[i]
                # Swing High: mid.h > all 4 neighbors
                if (candles[i-2]["h"] < mid["h"] and candles[i-1]["h"] < mid["h"]
                        and candles[i+1]["h"] < mid["h"] and candles[i+2]["h"] < mid["h"]):
                    swing_highs.append((i, mid["h"]))
                # Swing Low: mid.l < all 4 neighbors
                if (candles[i-2]["l"] > mid["l"] and candles[i-1]["l"] > mid["l"]
                        and candles[i+1]["l"] > mid["l"] and candles[i+2]["l"] > mid["l"]):
                    swing_lows.append((i, mid["l"]))

            # Walk forward and find BOS events (most recent wins)
            for j in range(5, n):
                price = candles[j]["c"]
                # Check bullish BOS: price > last unbroken swing high
                valid_highs = [(idx, p) for (idx, p) in swing_highs if idx < j]
                if valid_highs:
                    last_high_idx, last_high_price = valid_highs[-1]
                    if price > last_high_price:
                        bos_direction = "BULLISH"
                        bos_candles_ago = n - 1 - j
                        # Mark as broken (remove so we don't trigger again)
                        swing_highs = [(i2, p2) for (i2, p2) in swing_highs
                                       if not (i2 == last_high_idx and p2 == last_high_price)]
                # Check bearish BOS: price < last unbroken swing low
                valid_lows = [(idx, p) for (idx, p) in swing_lows if idx < j]
                if valid_lows:
                    last_low_idx, last_low_price = valid_lows[-1]
                    if price < last_low_price:
                        bos_direction = "BEARISH"
                        bos_candles_ago = n - 1 - j
                        swing_lows = [(i2, p2) for (i2, p2) in swing_lows
                                      if not (i2 == last_low_idx and p2 == last_low_price)]

        bos_recent = bos_candles_ago <= bos_max_age_candles

        # --- HTF (1h) FVG confluence check per 15m FVG ---
        # For each 15m FVG, check if it overlaps with a same-direction 1h zone
        htf_zones_list = htf_zones or []

        # Pre-compute per-FVG flags
        bos_aligned_count = 0
        htf_confluent_count = 0
        for fvg in fvgs:
            # BOS alignment check
            fvg_bos_ok = True  # default pass if BOS disabled
            if bos_enabled:
                if bos_recent and bos_direction:
                    if fvg["dir"] == "LONG":
                        fvg_bos_ok = bos_direction == "BULLISH"
                    else:
                        fvg_bos_ok = bos_direction == "BEARISH"
                else:
                    fvg_bos_ok = False   # no recent BOS = not aligned
            if fvg_bos_ok and bos_enabled:
                bos_aligned_count += 1
            fvg["bos_ok"] = fvg_bos_ok

            # HTF confluence check
            fvg_htf_ok = True  # default pass if HTF disabled
            if htf_fvg_enabled and htf_zones_list:
                fvg_htf_ok = False
                for hz in htf_zones_list:
                    if hz["direction"] != fvg["dir"]:
                        continue
                    # Check overlap: 15m zone [bottom, top] vs 1h zone [bottom, top]
                    overlap_top = min(fvg["top"], hz["top"])
                    overlap_bottom = max(fvg["bottom"], hz["bottom"])
                    if overlap_top > overlap_bottom:
                        fvg_htf_ok = True
                        break
                    # Also check if 15m FVG mid falls inside 1h zone
                    fvg_mid = (fvg["top"] + fvg["bottom"]) / 2
                    if hz["bottom"] <= fvg_mid <= hz["top"]:
                        fvg_htf_ok = True
                        break
            elif htf_fvg_enabled and not htf_zones_list:
                fvg_htf_ok = False   # no 1h zones found = fail
            if fvg_htf_ok and htf_fvg_enabled:
                htf_confluent_count += 1
            fvg["htf_ok"] = fvg_htf_ok

            # Combined: fully actionable = trend + BOS + HTF
            fvg["fully_actionable"] = fvg.get("trend_passes", False) and fvg_bos_ok and fvg_htf_ok

        # Average volume ratio on FVG candles (only strong ones)
        fvg_vol_ratios = [f["vol_ratio"] for f in fvgs]
        avg_vol_ratio = sum(fvg_vol_ratios) / len(fvg_vol_ratios) if fvg_vol_ratios else 0.0

        # Volume spike (legacy, kept for ScanResult compatibility)
        fvg_vols = [f["vol"] for f in fvgs if f["vol"] > 0]
        vol_spike = (sum(fvg_vols) / len(fvg_vols)) / avg_vol if fvg_vols and avg_vol > 0 else 1.0

        # Trend alignment: % of FVGs in trend direction
        trend_aligned_count = sum(1 for f in fvgs if f.get("trend_aligned", False))
        trend_aligned_pct = trend_aligned_count / fvg_count * 100 if fvg_count > 0 else 0

        # Fill Rate, Bounce Rate (at bounce_r), Avg Max-R, Signal Rate
        filled = 0
        bounced = 0
        r_values = []
        tradeable_signals = 0  # Filled + trend-aligned (= bot would actually trade)
        lookforward = 40       # 40 × 15m = 10 hours

        for fvg in fvgs:
            idx, top, bottom = fvg["idx"], fvg["top"], fvg["bottom"]
            zone_size = top - bottom
            if zone_size <= 0:
                continue

            got_fill = False
            max_r = 0.0

            for j in range(idx + 1, min(idx + 1 + lookforward, n)):
                cj = candles[j]

                if fvg["dir"] == "LONG":
                    if cj["l"] <= bottom:
                        break  # zone invalidated
                    fill = (top - cj["l"]) / zone_size
                    if entry_zone_min <= fill <= entry_zone_max:
                        got_fill = True
                        entry = cj["l"]
                        risk = entry - bottom
                        if risk <= 0:
                            break
                        for k in range(j + 1, min(idx + 1 + lookforward, n)):
                            ck = candles[k]
                            if ck["l"] <= bottom:
                                break  # SL hit
                            r_now = (ck["h"] - entry) / risk
                            if r_now > max_r:
                                max_r = r_now
                        break
                else:  # SHORT
                    if cj["h"] >= top:
                        break  # zone invalidated
                    fill = (cj["h"] - bottom) / zone_size
                    if entry_zone_min <= fill <= entry_zone_max:
                        got_fill = True
                        entry = cj["h"]
                        risk = top - entry
                        if risk <= 0:
                            break
                        for k in range(j + 1, min(idx + 1 + lookforward, n)):
                            ck = candles[k]
                            if ck["h"] >= top:
                                break  # SL hit
                            r_now = (entry - ck["l"]) / risk
                            if r_now > max_r:
                                max_r = r_now
                        break

            if got_fill:
                filled += 1
                r_values.append(max_r)
                if max_r >= bounce_r:
                    bounced += 1
                # * Only count as tradeable if trend passes ACTUAL threshold
                if fvg.get("trend_passes", False):
                    tradeable_signals += 1

        fill_rate = filled / fvg_count * 100 if fvg_count > 0 else 0
        bounce_rate = bounced / filled * 100 if filled > 0 else 0
        avg_r_achieved = sum(r_values) / len(r_values) if r_values else 0.0

        # Signal rate: tradeable signals per 10h window
        if len(candles) >= 2 and candles[-1]["t"] > candles[0]["t"]:
            span_hours = (candles[-1]["t"] - candles[0]["t"]) / 3_600_000  # ms->hours
            if span_hours <= 0:
                span_hours = n * 0.25  # fallback: 15m per candle
        else:
            span_hours = n * 0.25
        signal_rate = tradeable_signals / span_hours * 10 if span_hours > 0 else 0

        # --- Proximity Score (v5: direction-aware, config max_zone_distance) ---
        # Only measure distance to zones where price is on the correct side
        # LONG: price should be above or near zone (falling INTO it)
        # SHORT: price should be below or near zone (rising INTO it)
        # v11: Only consider RECENT FVGs for proximity (last 80 candles = 20h on 15m)
        # so scanner proximity matches what the live engine would actually see.
        # Older FVGs are still used for historical stats (fill/bounce/signal rate).
        current_price = candles[-1]["c"]
        best_proximity = 0.0
        best_actionable_proximity = 0.0
        actionable_zone_count = 0
        max_dist_pct = max_zone_distance * 100  # e.g. 0.05 -> 5.0%
        best_zone_for_convergence = None  # track nearest zone for convergence check
        prox_recency_cutoff = max(n - 80, 0)  # only FVGs from last 80 candles for proximity

        # v7: Get CURRENT trend to check actionability (not trend at FVG formation!)
        current_trend = get_trend_at(n - 1)

        for fvg in fvgs:
            zone_top, zone_bottom = fvg["top"], fvg["bottom"]

            # Direction-aware distance: how far is price from the zone entry edge?
            if fvg["dir"] == "LONG":
                dist = current_price - zone_top  # positive = above zone (waiting to drop)
                dist_pct = abs(dist) / current_price * 100
            else:  # SHORT
                dist = zone_bottom - current_price  # positive = below zone (waiting to rise)
                dist_pct = abs(dist) / current_price * 100

            # Score: 100 if at zone edge, 0 if beyond max_zone_distance
            prox = max(0, 100 * (1 - dist_pct / max_dist_pct))

            # v11: Only RECENT + UNVIOLATED FVGs contribute to proximity
            # Violated zones won't be visible to the live engine.
            # Older zones won't be visible either (live buffer is ~50-100 candles).
            is_recent = fvg["idx"] >= prox_recency_cutoff
            is_valid = not fvg.get("violated", False)
            if is_recent and is_valid and prox > best_proximity:
                best_proximity = prox
                best_zone_for_convergence = fvg

            # v12: ACTIONABLE check — SCANNER ALWAYS USES SOFT MODE
            # Scanner evaluates a snapshot every 2-4h.  BOS can flip in 15min,
            # HTF 1h zones appear/disappear within 1h.  Hard-gating on BOS+HTF
            # in the scanner made actP=0 for virtually ALL symbols → proven
            # symbols perma-benched, trial selection broken.
            # Fix: trend alignment = actionable for scanner purposes.
            # BOS/HTF contribute to score via bos_aligned_count/htf_confluent_count
            # but do NOT gate actionability in the scanner.
            trend_allows_now = False
            if bos_direction_override:
                trend_allows_now = True
            elif current_trend is not None:
                if fvg["dir"] == "LONG" and current_trend >= long_entry_threshold:
                    trend_allows_now = True
                elif fvg["dir"] == "SHORT" and current_trend <= -entry_threshold:
                    trend_allows_now = True

            # Scanner: trend alone = actionable.  BOS/HTF boost score, not gate.
            fully_ok = trend_allows_now
            if fully_ok:
                actionable_zone_count += 1
                # v11: proximity only from recent + unviolated FVGs
                if is_recent and is_valid and prox > best_actionable_proximity:
                    best_actionable_proximity = prox

        # --- NEW: Recent FVG Activity ---
        # % of FVGs formed in last 20 candles (5 hours on 15m)
        # Higher = symbol is actively creating new zones = more opportunity
        recent_candle_threshold = max(n - 20, 0)
        recent_fvgs = sum(1 for f in fvgs if f["idx"] >= recent_candle_threshold)
        recent_fvg_pct = recent_fvgs / max(fvg_count, 1) * 100

        # --- NEW v9: Convergence — is price MOVING TOWARD nearest zone? ---
        # Compare distance-to-zone 5 candles ago vs now.
        # Positive = converging (closer), 0 = static or moving away.
        convergence_score = 0.0
        if best_zone_for_convergence is not None and n >= 6:
            zt = best_zone_for_convergence["top"]
            zb = best_zone_for_convergence["bottom"]
            p_now = candles[-1]["c"]
            p_ago = candles[max(0, n - 6)]["c"]  # 5 candles = 1.25h on 15m

            if best_zone_for_convergence["dir"] == "LONG":
                # LONG zone: price falls from above → zone_top is entry edge
                d_now = max(0.0, p_now - zt)
                d_ago = max(0.0, p_ago - zt)
            else:
                # SHORT zone: price rises from below → zone_bottom is entry edge
                d_now = max(0.0, zb - p_now)
                d_ago = max(0.0, zb - p_ago)

            if d_ago > 1e-12:
                # +1.0 = reached zone, 0 = no change, <0 = moving away
                delta = (d_ago - d_now) / d_ago
                convergence_score = max(0.0, min(100.0, delta * 150))
            elif d_now <= 1e-12:
                convergence_score = 100.0  # Already at zone edge

        # --- NEW: Retest Speed ---
        # Average number of candles from FVG formation to first price touch
        # Lower = price retests zones faster = less waiting
        retest_candles = []
        for fvg in fvgs:
            idx, top, bottom = fvg["idx"], fvg["top"], fvg["bottom"]
            for j in range(idx + 1, min(idx + 1 + lookforward, n)):
                cj = candles[j]
                # Check if price touched the zone
                if cj["l"] <= top and cj["h"] >= bottom:
                    retest_candles.append(j - idx)
                    break
        avg_retest_speed = sum(retest_candles) / len(retest_candles) if retest_candles else 40.0

        # --- NEW: Zone Approach Rate ---
        # % of FVGs where price comes within 0.5% of the zone edge WITHOUT
        # necessarily filling to entry_zone_min. Measures "price tends to
        # come close to zones" — even if it doesn't fully enter.
        # Higher = more zone-magnetic symbol = more trade opportunities.
        zone_approaches = 0
        approach_threshold = 0.005  # 0.5% of price = "close enough"
        for fvg in fvgs:
            idx, top, bottom = fvg["idx"], fvg["top"], fvg["bottom"]
            approached = False
            for j in range(idx + 1, min(idx + 1 + lookforward, n)):
                cj = candles[j]
                if fvg["dir"] == "LONG":
                    if cj["l"] <= bottom:
                        break  # invalidated
                    # How close did price get to zone top (entry edge)?
                    dist_to_zone = (cj["l"] - top) / top if cj["l"] > top else 0
                    if dist_to_zone <= approach_threshold or cj["l"] <= top:
                        approached = True
                        break
                else:  # SHORT
                    if cj["h"] >= top:
                        break  # invalidated
                    dist_to_zone = (bottom - cj["h"]) / bottom if cj["h"] < bottom else 0
                    if dist_to_zone <= approach_threshold or cj["h"] >= bottom:
                        approached = True
                        break
            if approached:
                zone_approaches += 1
        zone_approach_rate = zone_approaches / fvg_count * 100 if fvg_count > 0 else 0

        # current_trend already computed above in proximity block
        current_trend_strength = abs(current_trend) if current_trend is not None else 0

        return {
            "fvg_count": fvg_count,
            "fvg_density": fvg_density,
            "fill_rate": fill_rate,
            "bounce_rate": bounce_rate,
            "avg_r_achieved": avg_r_achieved,
            "atr_pct": atr_pct,
            "vol_spike": vol_spike,
            "trend_clarity": trend_clarity,
            "wick_body": avg_wick_body,
            "trend_aligned_pct": trend_aligned_pct,
            "signal_rate": signal_rate,
            "avg_vol_ratio": avg_vol_ratio,
            "proximity_score": best_proximity,
            "recent_fvg_pct": recent_fvg_pct,
            "retest_speed": avg_retest_speed,
            "zone_approach_rate": zone_approach_rate,
            "current_trend_strength": current_trend_strength,
            "actionable_proximity": best_actionable_proximity,
            "actionable_zone_count": actionable_zone_count,
            "bos_aligned_count": bos_aligned_count,
            "htf_confluent_count": htf_confluent_count,
            "bos_direction": bos_direction,
            "bos_candles_ago": bos_candles_ago,
            "convergence_score": convergence_score,
        }

    @staticmethod
    def _compute_score(m: Dict, rot_cfg=None) -> float:
        """Composite score (0-100) — v11: PROXIMITY-FIRST.

        v11 philosophy: "Is this coin READY TO TRADE NOW?" > "Did it trade well historically?"

        v10 was too profitability-heavy (38pts sr+br+avg_r) — coins with great
        history but prices 3% from zones scored high, wasted slots, zero trades.
        v11 makes proximity the dominant factor so coins NEAR actionable zones
        always outrank historically-good-but-far coins.

        Hard Gates:
        - No FVGs → reject
        - sr=0 AND fr=0 → reject
        - fr < 10% → reject
        - sr=0 → reject
        - bounce=0 AND avg_r < 0.5 → reject
        - proximity too low → reject (tighter gate than v10)

        Weights (total 100):
        PROXIMITY TIER (40pts) — "Can we trade soon?"
        - Actionable Proximity (25pts) — price near zone with all filters
        - Any-Zone Proximity (8pts) — price near any zone
        - Convergence (7pts) — price moving toward zone

        QUALITY TIER (30pts) — "Does FVG strategy work here?"
        - Signal Rate (10pts) — tradeable signals per window
        - Bounce Rate (7pts) — do fills reach TP?
        - Fill Rate (6pts) — do zones get retested?
        - Avg R Achieved (4pts) — risk:reward
        - Retest Speed (3pts) — speed of zone retest

        FILTER TIER (20pts) — "Are conditions aligned?"
        - Actionable Zones Exist (5pts)
        - BOS Confirmed (5pts)
        - HTF Confluent (5pts)
        - Zone Approach Rate (5pts) — price historically reaches zones

        LIQUIDITY TIER (10pts)
        - ATR (3pts)
        - Vol24h (3pts)
        - VolStr (2pts)
        - Cheap coin bonus (2pts)
        """
        # * HARD GATE 1: reject symbols with no valid FVGs
        fvg_count = m.get("fvg_count", 0)
        if fvg_count == 0:
            return 0.0

        # * HARD GATE 2: reject symbols where zones are never retested
        sr = m.get("signal_rate", 0)
        fr = m.get("fill_rate", 0)
        if sr == 0 and fr == 0:
            return 0.0

        # * HARD GATE 3: fill_rate < 10% = zones exist but price never reaches them
        if fr < 10:
            return 0.0

        # * HARD GATE 4: sr=0 = trend never aligns with zone direction
        #   Bot will NEVER trade this coin — wasting a slot
        if sr == 0:
            return 0.0

        # * HARD GATE 5: bounce_rate < 15% = FVG zones don't bounce enough
        #   Zones get filled but price doesn't reverse = guaranteed SL
        #   Stricter than v10 (was: br=0 AND avg_r<0.5)
        br = m.get("bounce_rate", 0)
        avg_r = m.get("avg_r_achieved", 0)
        if br < 15:
            return 0.0

        # * HARD GATE 6: proximity too low = zones unreachable
        #   v12: with scanner using soft mode, actP should be populated much more
        #   often.  Fallback threshold lowered from 70 to 50 — BOS/HTF change fast.
        act_prox = m.get("actionable_proximity", 0)
        any_prox = m.get("proximity_score", 0)
        max_entry_dist = m.get("max_entry_distance_pct", 2.0)
        max_zone_dist = m.get("max_zone_distance_pct", 5.0)
        prox_gate = max(10.0, (1.0 - max_entry_dist / max_zone_dist) * 100)
        if act_prox > 0:
            if act_prox < prox_gate:
                return 0.0
        else:
            if any_prox < max(prox_gate, 50):
                return 0.0

        score = 0.0

        # ═══════════════════════════════════════════════════════
        # PROXIMITY TIER (40pts) — "Can we trade this coin SOON?"
        # ═══════════════════════════════════════════════════════

        # 1. ACTIONABLE PROXIMITY — price near zone with ALL filters passing (25pts)
        #    THE dominant factor. Power-1.5 scaling rewards being very close.
        act_norm = act_prox / 100.0
        score += (act_norm ** 1.5) * 25  # ^1.5: 0.5% → ~9pts, 2% → ~2pts

        # 2. ANY-ZONE PROXIMITY — price near ANY FVG zone (8pts)
        any_norm = any_prox / 100.0
        score += any_norm * 8

        # 3. CONVERGENCE — is price MOVING TOWARD nearest zone? (7pts)
        conv = m.get("convergence_score", 0)
        score += conv / 100.0 * 7

        # ═══════════════════════════════════════════════════════
        # QUALITY TIER (30pts) — "Does FVG strategy work here?"
        # ═══════════════════════════════════════════════════════

        # 4. SIGNAL RATE — tradeable signals per 10h (10pts, was 20)
        if sr >= 3.0:
            score += 10
        elif sr >= 2.0:
            score += 8
        elif sr >= 1.0:
            score += 6
        elif sr >= 0.5:
            score += 4
        elif sr > 0:
            score += 2

        # 5. BOUNCE RATE — filled FVGs reaching target R (7pts, was 10)
        if br >= 80:
            score += 7
        elif br >= 60:
            score += 5.5
        elif br >= 40:
            score += 4
        elif br >= 20:
            score += 2.5
        elif br > 0:
            score += 1

        # 6. FILL RATE — do zones get retested? (6pts, was 8)
        if fr >= 60:
            score += 6
        elif fr >= 45:
            score += 5
        elif fr >= 35:
            score += 4
        elif fr >= 25:
            score += 3
        elif fr >= 15:
            score += 2
        elif fr >= 10:
            score += 1

        # 7. AVG R ACHIEVED — profitability per trade (4pts, was 8)
        if avg_r >= 3.0:
            score += 4
        elif avg_r >= 2.0:
            score += 3
        elif avg_r >= 1.5:
            score += 2.5
        elif avg_r >= 1.0:
            score += 1.5
        elif avg_r >= 0.5:
            score += 0.5

        # 8. RETEST SPEED — how fast price touches zones (3pts, was 4)
        rs = m.get("retest_speed", 40)
        if rs <= 3:
            score += 3
        elif rs <= 6:
            score += 2.5
        elif rs <= 10:
            score += 2
        elif rs <= 20:
            score += 1
        elif rs <= 30:
            score += 0.5

        # ═══════════════════════════════════════════════════════
        # FILTER TIER (20pts) — "Are conditions aligned?"
        # ═══════════════════════════════════════════════════════

        # 9. ACTIONABLE ZONES EXIST — trend+BOS+HTF all pass for ≥1 FVG (5pts)
        azc = m.get("actionable_zone_count", 0)
        if azc >= 3:
            score += 5
        elif azc >= 2:
            score += 4
        elif azc >= 1:
            score += 3

        # 10. BOS CONFIRMED — recent BOS aligns with FVG direction (5pts)
        bos_z = m.get("bos_aligned_count", 0)
        if bos_z >= 3:
            score += 5
        elif bos_z >= 2:
            score += 4
        elif bos_z >= 1:
            score += 3

        # 11. HTF CONFLUENT — 15m FVGs backed by 1h zones (5pts)
        htf_z = m.get("htf_confluent_count", 0)
        if htf_z >= 3:
            score += 5
        elif htf_z >= 2:
            score += 4
        elif htf_z >= 1:
            score += 3

        # 12. ZONE APPROACH RATE — % of FVGs where price comes close (5pts)
        zar = m.get("zone_approach_rate", 0)
        if zar >= 70:
            score += 5
        elif zar >= 50:
            score += 3.5
        elif zar >= 35:
            score += 2.5
        elif zar >= 20:
            score += 1.5
        elif zar >= 10:
            score += 0.5

        # ═══════════════════════════════════════════════════════
        # LIQUIDITY TIER (10pts)
        # ═══════════════════════════════════════════════════════

        # 13. ATR — volatility suitability (3pts)
        atr = m.get("atr_pct", 0)
        if 0.20 <= atr <= 1.50:
            score += 3
        elif 0.15 <= atr <= 2.00:
            score += 2
        elif atr >= 0.10:
            score += 1

        # 14. 24h Volume — liquidity (3pts)
        vol_24h = m.get("vol_24h", 0)
        if vol_24h >= 100_000_000:
            score += 3
        elif vol_24h >= 50_000_000:
            score += 2.5
        elif vol_24h >= 10_000_000:
            score += 1.5
        elif vol_24h >= 5_000_000:
            score += 1

        # 15. Volume Strength — avg volume ratio on FVG candles (2pts)
        vr = m.get("avg_vol_ratio", 0)
        if vr >= 2.0:
            score += 2
        elif vr >= 1.5:
            score += 1.5
        elif vr >= 1.0:
            score += 0.5

        # 16. Cheap coin bonus (3pts)
        if rot_cfg is not None:
            price = m.get("price", 999)
            if price < rot_cfg.cheap_coin_threshold:
                score += rot_cfg.cheap_coin_bonus

        return round(score, 1)

    @staticmethod
    def _detect_htf_zones_raw(candles_1h: list, min_gap_pct: float, max_zones: int) -> list:
        """Detect 1h FVG zones from raw candle dicts (same logic as strategy_engine).

        Returns list of dicts with keys: direction, top, bottom.
        """
        if len(candles_1h) < 3:
            return []

        zones = []
        for i in range(len(candles_1h) - 2):
            c1, c2, c3 = candles_1h[i], candles_1h[i + 1], candles_1h[i + 2]

            # Bullish FVG: c1.high < c3.low (gap up)
            bull_gap = c3["l"] - c1["h"]
            if bull_gap > 0:
                gap_pct = bull_gap / c2["c"] if c2["c"] > 0 else 0
                if gap_pct >= min_gap_pct:
                    zone_top = c3["l"]
                    zone_bottom = c1["h"]
                    # Check not violated by subsequent candles
                    violated = False
                    for j in range(i + 3, len(candles_1h)):
                        if candles_1h[j]["l"] <= zone_bottom:
                            violated = True
                            break
                    if not violated:
                        zones.append({"direction": "LONG", "top": zone_top, "bottom": zone_bottom})

            # Bearish FVG: c1.low > c3.high (gap down)
            bear_gap = c1["l"] - c3["h"]
            if bear_gap > 0:
                gap_pct = bear_gap / c2["c"] if c2["c"] > 0 else 0
                if gap_pct >= min_gap_pct:
                    zone_top = c1["l"]
                    zone_bottom = c3["h"]
                    violated = False
                    for j in range(i + 3, len(candles_1h)):
                        if candles_1h[j]["h"] >= zone_top:
                            violated = True
                            break
                    if not violated:
                        zones.append({"direction": "SHORT", "top": zone_top, "bottom": zone_bottom})

        # Keep most recent zones up to max
        return zones[-max_zones:] if len(zones) > max_zones else zones

    async def scan_all_symbols(
        self,
        top_n: int = 30,
        candle_count: int = 200,
        min_gap: float = 0.0005,
    ) -> List[ScanResult]:
        """Scan top-volume pairs and return scored results.
        
        Args:
            top_n: How many top-volume pairs to scan
            candle_count: Number of candles per symbol
            min_gap: Minimum FVG gap %
            
        Returns:
            List of ScanResult sorted by score descending
        """
        # Use trading timeframe from config ("5m" -> "5min", "15m" -> "15min")
        scan_interval = self.config.fvg.timeframe.replace("m", "min")
        # Get all tickers sorted by volume
        try:
            tickers = await self.exchange._api._request(
                "GET", "/api/v1/futures/market/tickers", {}
            )
        except Exception as e:
            logger.error(f"Failed to fetch tickers for scan: {e}")
            return []

        if not isinstance(tickers, list):
            return []

        # Filter & sort by volume
        # Only keep USDT-margined pairs; exclude USDC/USD duplicates and stablecoins
        for t in tickers:
            t["_vol"] = float(t.get("quoteVol", 0) or 0)
            t["_price"] = float(t.get("lastPrice", 0) or 0)
        # Volume floor from config (default $10M) -- reject illiquid pairs
        min_vol = getattr(self.config.rotation, 'min_24h_volume', 10_000_000)
        tickers = [
            t for t in tickers
            if t["_vol"] >= min_vol
            and t.get("symbol", "").endswith("USDT")
            and t.get("symbol", "") not in self._blacklist
        ]
        logger.info(
            f"📊 Volume filter: {len(tickers)} pairs with 24h vol ≥ ${min_vol/1e6:.0f}M"
        )
        # Deduplicate base assets (keep highest-volume variant)
        seen_bases: set = set()
        deduped: list = []
        tickers.sort(key=lambda x: x["_vol"], reverse=True)
        for t in tickers:
            sym = t.get("symbol", "")
            base = sym.replace("USDT", "")
            if base not in seen_bases:
                seen_bases.add(base)
                deduped.append(t)
        candidates = deduped[:top_n]

        logger.info(f"🔍 Scanning {len(candidates)} pairs for FVG suitability...")

        results: List[ScanResult] = []
        skipped_reasons: Dict[str, int] = {"no_candles": 0, "no_fvgs": 0, "score_zero": 0, "low_score": 0}
        for t in candidates:
            symbol = t["symbol"]
            try:
                raw = await self.exchange._api.get_klines(
                    symbol=symbol, interval=scan_interval, limit=candle_count,
                )
                if not isinstance(raw, list) or len(raw) < 20:
                    continue

                # Parse into dicts
                candles = []
                for c in raw:
                    if isinstance(c, dict):
                        candles.append({
                            "t": int(c.get("time", c.get("t", c.get("ts", 0)))),
                            "o": float(c.get("open", c.get("o", 0))),
                            "h": float(c.get("high", c.get("h", 0))),
                            "l": float(c.get("low", c.get("l", 0))),
                            "c": float(c.get("close", c.get("c", 0))),
                            "v": float(c.get("baseVol", c.get("volume", c.get("v", c.get("vol", c.get("quoteVol", 0)))))),
                        })
                    elif isinstance(c, (list, tuple)) and len(c) >= 6:
                        candles.append({
                            "t": int(c[0]) if len(c) > 0 else 0,
                            "o": float(c[1]), "h": float(c[2]),
                            "l": float(c[3]), "c": float(c[4]),
                            "v": float(c[5]),
                        })

                candles.sort(key=lambda x: x.get("t", x.get("ts", 0)))  # sort by timestamp

                # --- Fetch 1h candles for HTF FVG confluence (if enabled) ---
                htf_zones = []
                if self.config.trend.htf_fvg_enabled:
                    try:
                        raw_1h = await self.exchange._api.get_klines(
                            symbol=symbol, interval="1h", limit=100,
                        )
                        if isinstance(raw_1h, list) and len(raw_1h) >= 3:
                            candles_1h = []
                            for c in raw_1h:
                                if isinstance(c, dict):
                                    candles_1h.append({
                                        "h": float(c.get("high", c.get("h", 0))),
                                        "l": float(c.get("low", c.get("l", 0))),
                                        "c": float(c.get("close", c.get("c", 0))),
                                    })
                                elif isinstance(c, (list, tuple)) and len(c) >= 5:
                                    candles_1h.append({
                                        "h": float(c[2]), "l": float(c[3]), "c": float(c[4]),
                                    })
                            htf_min_gap = self.config.trend.htf_fvg_min_gap_percent
                            htf_max_zones = self.config.trend.htf_fvg_max_zones
                            htf_zones = self._detect_htf_zones_raw(
                                candles_1h, htf_min_gap, htf_max_zones
                            )
                    except Exception as e:
                        logger.debug(f"HTF candle fetch failed for {symbol}: {e}")

                metrics = self._analyze_symbol(
                    candles,
                    min_gap=min_gap,
                    min_gap_atr_mult=getattr(self.config.fvg, 'min_gap_atr_mult', 0.0),
                    min_volume_ratio=self.config.fvg.min_volume_ratio,
                    entry_zone_min=self.config.fvg.entry_zone_min,
                    entry_zone_max=self.config.fvg.entry_zone_max,
                    bounce_r=self.config.tpsl.min_rr,
                    ema_fast=self.config.trend.ema_fast,
                    ema_slow=self.config.trend.ema_slow,
                    min_strength=self.config.fvg.min_strength,
                    entry_threshold=self.config.trend.entry_threshold,
                    long_entry_threshold=self.config.trend.long_entry_threshold,
                    weight_15m=self.config.trend.weight_15m,
                    weight_1h=self.config.trend.weight_1h,
                    max_zone_distance=self.config.fvg.max_zone_distance,
                    max_entry_distance=self.config.fvg.max_entry_distance,
                    bos_enabled=self.config.trend.bos_enabled,
                    bos_max_age_candles=self.config.trend.bos_max_age_candles,
                    bos_soft_mode=self.config.trend.bos_soft_mode,
                    bos_direction_override=getattr(self.config.trend, 'bos_direction_override', False),
                    htf_fvg_enabled=self.config.trend.htf_fvg_enabled,
                    htf_soft_mode=self.config.trend.htf_soft_mode,
                    htf_fvg_min_gap=self.config.trend.htf_fvg_min_gap_percent,
                    htf_zones=htf_zones,
                )
                if metrics:
                    # Inject distance config for HARD GATE 6 proximity check
                    metrics["max_entry_distance_pct"] = self.config.fvg.max_entry_distance * 100
                    metrics["max_zone_distance_pct"] = self.config.fvg.max_zone_distance * 100
                if not metrics:
                    skipped_reasons["no_fvgs"] += 1
                    continue

                # Inject 24h volume and price for scoring
                metrics["vol_24h"] = t["_vol"]
                metrics["price"] = t["_price"]
                score = self._compute_score(metrics, self.config.rotation)
                
                # * Skip symbols with score 0 (failed hard gate)
                if score <= 0:
                    skipped_reasons["score_zero"] += 1
                    logger.debug(f"  {symbol}: score=0 (hard gate) fvg={metrics.get('fvg_count',0)} sr={metrics.get('signal_rate',0):.1f}")
                    continue
                    
                results.append(ScanResult(
                    symbol=symbol,
                    score=score,
                    price=t["_price"],
                    vol_24h=t["_vol"],
                    fvg_density=metrics["fvg_density"],
                    fill_rate=metrics["fill_rate"],
                    bounce_rate=metrics["bounce_rate"],
                    atr_pct=metrics["atr_pct"],
                    vol_spike=metrics["vol_spike"],
                    trend_clarity=metrics["trend_clarity"],
                    wick_body=metrics["wick_body"],
                    avg_r_achieved=metrics.get("avg_r_achieved", 0.0),
                    trend_aligned_pct=metrics.get("trend_aligned_pct", 0.0),
                    signal_rate=metrics.get("signal_rate", 0.0),
                    avg_vol_ratio=metrics.get("avg_vol_ratio", 0.0),
                    proximity_score=metrics.get("proximity_score", 0.0),
                    recent_fvg_pct=metrics.get("recent_fvg_pct", 0.0),
                    retest_speed=metrics.get("retest_speed", 0.0),
                    zone_approach_rate=metrics.get("zone_approach_rate", 0.0),
                    current_trend_strength=metrics.get("current_trend_strength", 0.0),
                    actionable_proximity=metrics.get("actionable_proximity", 0.0),
                    actionable_zone_count=metrics.get("actionable_zone_count", 0),
                    bos_aligned_count=metrics.get("bos_aligned_count", 0),
                    htf_confluent_count=metrics.get("htf_confluent_count", 0),
                    convergence_score=metrics.get("convergence_score", 0.0),
                ))
            except Exception as e:
                logger.debug(f"Scan error for {symbol}: {e}")

            await asyncio.sleep(0.15)  # rate limit

        results.sort(key=lambda x: x.score, reverse=True)

        # Diagnostic: how many symbols pass each filter individually
        n_with_bos = sum(1 for r in results if r.bos_aligned_count > 0)
        n_with_htf = sum(1 for r in results if r.htf_confluent_count > 0)
        n_with_act = sum(1 for r in results if r.actionable_zone_count > 0)
        logger.info(
            f"📊 Scan results: {len(results)} passed / {len(candidates)} scanned | "
            f"rejected: no_fvgs={skipped_reasons['no_fvgs']} score_zero={skipped_reasons['score_zero']}"
        )
        logger.info(
            f"📊 Filter breakdown: BOS>0={n_with_bos} | HTF>0={n_with_htf} | "
            f"actZ>0 (all 3)={n_with_act} / {len(results)} symbols"
        )
        if results:
            for r in results[:20]:
                logger.info(
                    f"  {r.symbol:<14} score={r.score:.1f} sr={r.signal_rate:.1f} "
                    f"fill={r.fill_rate:.0f}% zar={r.zone_approach_rate:.0f}% "
                    f"actZ={r.actionable_zone_count} bosZ={r.bos_aligned_count} "
                    f"htfZ={r.htf_confluent_count} actP={r.actionable_proximity:.0f} "
                    f"prox={r.proximity_score:.0f} conv={r.convergence_score:.0f} "
                    f"trend={r.current_trend_strength:.2f}"
                )
        return results

    # ==================== Rotation Logic ====================

    async def maybe_rotate(
        self,
        symbol_states: dict,
        ws_handler,
        bot_state,
        trade_history=None,
        signal_tracker=None,
        force: bool = False,
    ) -> Optional[List[str]]:
        """Check if rotation is due and perform it if needed.
        
        PnL-aware rotation logic:
        1. Symbols with open positions -> PROTECTED (never removed)
        2. Symbols with positive PnL -> PROTECTED (keep winners)
        3. Symbols with negative PnL -> CANDIDATES for replacement
        4. Symbols with no trades -> CANDIDATES (untested)
        5. New symbols chosen from scanner top scorers
        
        Args:
            symbol_states: Dict[str, SymbolState] -- modifiable in-place
            ws_handler: WebSocketHandler for resubscribing
            bot_state: BotState
            trade_history: TradeHistory for per-symbol PnL analysis
            force: Force rotation regardless of timing
            
        Returns:
            New symbol list if rotated, None if skipped
        """
        rotation_cfg = self.config.rotation
        if not rotation_cfg.enabled and not force:
            return None

        now = datetime.now()

        # Check timing
        if not force and self._last_rotation:
            hours_since = (now - self._last_rotation).total_seconds() / 3600
            if hours_since < rotation_cfg.interval_hours:
                return None

        logger.info("🔄 Starting daily symbol rotation scan...")

        # 1. Fetch precision info from exchange
        await self.fetch_all_symbol_info()

        # 2. Analyze current symbols' PnL
        symbol_pnl = {}
        if trade_history:
            lookback = getattr(rotation_cfg, 'pnl_lookback_hours', 72)
            symbol_pnl = trade_history.get_symbol_pnl(lookback_hours=lookback)
            logger.info(f"📊 PnL analysis ({lookback}h lookback):")
            for sym in sorted(symbol_states.keys()):
                pnl_data = symbol_pnl.get(sym)
                # Signal stats annotation
                sig_str = ""
                if signal_tracker:
                    st = signal_tracker.get_stats(sym)
                    if st:
                        h = signal_tracker.hours_since_activation(sym)
                        rate = st.zone_hits / max(h, 1.0)
                        sig_str = f" | hits={st.zone_hits}({rate:.1f}/h) trades={st.trades_executed}"
                if pnl_data:
                    tag = "✅" if pnl_data['profitable'] else "❌"
                    logger.info(
                        f"  {tag} {sym:<14} PnL=${pnl_data['net_pnl']:+.2f} "
                        f"trades={pnl_data['trades']} WR={pnl_data['win_rate']:.0f}%"
                        f"{sig_str}"
                    )
                else:
                    logger.info(f"  ⬜ {sym:<14} no trades in last {lookback}h{sig_str}")

        # 3. Classify symbols into 3 tiers: Core -> Proven -> Trial
        #    Accumulative growth: proven symbols stay, losers get replaced,
        #    list grows up to max_symbols (15) over time
        protected = set()
        protect_reasons: Dict[str, str] = {}
        force_remove = set()  # Symbols that MUST go (consecutive losses, silent)
        promoted = set()      # Symbols promoted to proven THIS cycle
        demoted = set()       # Symbols demoted from proven THIS cycle

        max_losing = getattr(rotation_cfg, 'max_losing_trades', 3)
        proven_min_pnl = getattr(rotation_cfg, 'proven_min_pnl', 0.50)
        proven_min_trades = getattr(rotation_cfg, 'proven_min_trades', 2)

        # Signal-based silent ban: symbols active >N hours with 0 zone hits
        silent_symbols: set = set()
        if signal_tracker:
            ban_active_h = getattr(rotation_cfg, 'signal_ban_min_active_hours', 8.0)
            ban_no_hit_h = getattr(rotation_cfg, 'signal_ban_no_hit_hours', 8.0)
            silent_list = signal_tracker.get_silent_symbols(
                active_symbols=list(symbol_states.keys()),
                min_active_hours=ban_active_h,
                no_signal_hours=ban_no_hit_h,
            )
            silent_symbols = set(silent_list)
            if silent_symbols:
                logger.info(
                    f"  🔇 Silent symbols (no zone hits >{ban_no_hit_h:.0f}h): "
                    f"{', '.join(sorted(silent_symbols))}"
                )

        for sym, state in symbol_states.items():
            # Core/pinned -- always protected (own tier)
            if sym in self._pinned_symbols:
                protected.add(sym)
                protect_reasons[sym] = "core"
                continue

            # Open positions always protected
            if state.has_position:
                protected.add(sym)
                protect_reasons[sym] = "open position"
                continue

            # Check consecutive losing streak -> force remove + demote
            # Trial symbols use stricter limit (trial_max_losing_trades)
            if trade_history and max_losing > 0:
                trial_max_losing = getattr(rotation_cfg, 'trial_max_losing_trades', max_losing)
                is_proven_sym = sym in self._proven_symbols
                effective_max_losing = max_losing if is_proven_sym else trial_max_losing
                streak = trade_history.get_recent_streak(sym, lookback_hours=24)
                if streak <= -effective_max_losing:
                    force_remove.add(sym)
                    if is_proven_sym:
                        demoted.add(sym)
                    tier_label = "proven" if is_proven_sym else "trial"
                    logger.info(
                        f"  🚫 {sym}: {abs(streak)} consecutive losses "
                        f"(limit={effective_max_losing} for {tier_label})"
                        f" -> forced removal{' (demoted from proven)' if is_proven_sym else ''}"
                    )
                    continue

            # Silent zone ban -> force remove (but NEVER demote proven)
            # Proven symbols earned status via PnL — 0 zone hits is a market
            # condition, not poor performance.  They keep proven status and
            # will be re-added next rotation when zones reappear.
            if sym in silent_symbols:
                force_remove.add(sym)
                is_proven_sym = sym in self._proven_symbols
                active_h = signal_tracker.hours_since_activation(sym) if signal_tracker else 0
                logger.info(
                    f"  🔇 {sym}: 0 zone hits in {active_h:.1f}h "
                    f"-> forced removal{' (proven status KEPT)' if is_proven_sym else ''}"
                )
                continue

            # Check for proven promotion: profitable + enough trades + WR
            pnl_data = symbol_pnl.get(sym)
            proven_min_wr = getattr(rotation_cfg, 'proven_min_wr', 40.0)
            if (pnl_data and pnl_data['net_pnl'] >= proven_min_pnl
                    and pnl_data['trades'] >= proven_min_trades
                    and pnl_data['win_rate'] >= proven_min_wr):
                # Promote to proven (or reconfirm) — save stats
                if sym not in self._proven_symbols:
                    promoted.add(sym)
                    logger.info(
                        f"  🆙 {sym}: promoted to proven "
                        f"(PnL=${pnl_data['net_pnl']:+.2f}, {pnl_data['trades']} trades, "
                        f"WR={pnl_data['win_rate']:.0f}%)"
                    )
                self._proven_stats[sym] = {
                    "promoted_at": datetime.now().strftime("%Y-%m-%d"),
                    "net_pnl": round(pnl_data['net_pnl'], 2),
                    "trades": pnl_data['trades'],
                    "win_rate": round(pnl_data['win_rate'], 1),
                }
                protected.add(sym)
                protect_reasons[sym] = f"proven PnL=${pnl_data['net_pnl']:+.2f}"
                continue

            # Already proven but not currently meeting promotion threshold -> still protected (grace)
            if sym in self._proven_symbols:
                protected.add(sym)
                pnl_str = f"PnL=${pnl_data['net_pnl']:+.2f}" if pnl_data else "no recent trades"
                protect_reasons[sym] = f"proven (grace: {pnl_str})"
                continue

            # Profitable but below proven threshold -> protected (keep winners)
            if getattr(rotation_cfg, 'protect_profitable', True):
                if pnl_data and pnl_data['profitable']:
                    protected.add(sym)
                    protect_reasons[sym] = f"profitable PnL=${pnl_data['net_pnl']:+.2f}"
                    continue

            # Everything else -> replaceable (negative PnL, untested trial symbols)

        # Apply proven promotions and demotions
        self._proven_symbols |= promoted
        self._proven_symbols -= demoted
        self._proven_symbols -= self._pinned_symbols  # core symbols have their own tier
        self._proven_symbols -= self._blacklist

        # 4. Determine replaceable symbols (negative PnL, untested, or force-removed)
        replaceable = (set(symbol_states.keys()) - protected) | force_remove

        # 4.5 PnL-based ban: ban ALL rotation symbols with significant losses
        pnl_ban_cfg_enabled = getattr(rotation_cfg, 'pnl_ban_enabled', False)
        pnl_ban_hours = getattr(rotation_cfg, 'pnl_ban_hours', 24)
        pnl_ban_threshold = getattr(rotation_cfg, 'pnl_ban_threshold', -2.0)
        if pnl_ban_cfg_enabled and symbol_pnl:
            # Ban any rotation symbol (not core) with net PnL below threshold
            rotation_syms_with_pnl = {
                sym: data for sym, data in symbol_pnl.items()
                if sym not in self._pinned_symbols and data.get('trades', 0) >= 2
            }
            for sym, data in rotation_syms_with_pnl.items():
                if data['net_pnl'] < pnl_ban_threshold:
                    ban_until = now + timedelta(hours=pnl_ban_hours)
                    self._pnl_ban_until[sym] = ban_until
                    force_remove.add(sym)
                    replaceable.add(sym)
                    # Demote from proven if applicable
                    if sym in self._proven_symbols:
                        self._proven_symbols.discard(sym)
                        demoted.add(sym)
                    logger.info(
                        f"  💀 {sym}: PnL=${data['net_pnl']:+.2f} < ${pnl_ban_threshold:.0f} "
                        f"-> banned for {pnl_ban_hours}h (until {ban_until.strftime('%H:%M')})"
                        f"{' -- demoted from proven' if sym in demoted else ''}"
                    )

        # Clean up expired PnL bans
        expired_bans = [s for s, t in self._pnl_ban_until.items() if now >= t]
        for s in expired_bans:
            del self._pnl_ban_until[s]
            logger.info(f"  🔓 {s}: PnL ban expired")

        # Save proven state (promotions/demotions may have changed it)
        if promoted or demoted:
            self._save_proven()

        # Log 3-tier classification
        core_count = len(self._pinned_symbols)
        proven_active = self._proven_symbols - force_remove - set(self._pnl_ban_until.keys())
        logger.info(
            f"🔄 Classification: {core_count} core | "
            f"{len(proven_active)} proven | "
            f"{len(replaceable)} replaceable | "
            f"{len(force_remove)} force-removed"
        )
        for sym in sorted(self._pinned_symbols):
            logger.info(f"  📌 {sym}: core (pinned)")
        for sym in sorted(proven_active):
            logger.info(f"  ⭐ {sym}: {protect_reasons.get(sym, 'proven')}")
        for sym in sorted(protected - self._pinned_symbols - proven_active):
            logger.info(f"  🛡️ {sym}: {protect_reasons.get(sym, 'protected')}")
        for sym in sorted(replaceable):
            pnl_data = symbol_pnl.get(sym)
            reason = f"PnL=${pnl_data['net_pnl']:+.2f}" if pnl_data else "no trades"
            logger.info(f"  🔄 {sym}: replaceable ({reason})")

        # 5. Scan all symbols
        results = await self.scan_all_symbols(
            top_n=rotation_cfg.scan_top_n,
            candle_count=rotation_cfg.scan_candles,
            min_gap=self.config.fvg.min_gap_percent,
        )

        if not results:
            logger.warning("Rotation scan returned no results, keeping current symbols")
            self._last_rotation = now
            return None

        # 5.5 Apply PnL adjustments — penalise losers, reward winners
        # Negative PnL → score reduced; Positive PnL → score boosted (capped)
        if symbol_pnl:
            pnl_penalty_per_dollar = getattr(rotation_cfg, 'pnl_penalty_per_dollar', 5.0)
            pnl_bonus_per_dollar = getattr(rotation_cfg, 'pnl_bonus_per_dollar', 3.0)
            pnl_bonus_cap = getattr(rotation_cfg, 'pnl_bonus_cap', 15.0)
            for r in results:
                pnl_data = symbol_pnl.get(r.symbol)
                if not pnl_data:
                    continue
                net = pnl_data['net_pnl']
                old_score = r.score
                if net < 0:
                    penalty = abs(net) * pnl_penalty_per_dollar
                    r.score = max(0.0, r.score - penalty)
                    if penalty > 0:
                        logger.debug(
                            f"  📉 {r.symbol}: score {old_score:.1f} → {r.score:.1f} "
                            f"(PnL penalty -{penalty:.1f} from ${net:+.2f})"
                        )
                elif net > 0:
                    bonus = min(net * pnl_bonus_per_dollar, pnl_bonus_cap)
                    r.score += bonus
                    if bonus > 0:
                        logger.debug(
                            f"  📈 {r.symbol}: score {old_score:.1f} → {r.score:.1f} "
                            f"(PnL bonus +{bonus:.1f} from ${net:+.2f})"
                        )
            # Re-sort after PnL adjustments
            results.sort(key=lambda x: x.score, reverse=True)

        # 5.6 Clean up expired rotation cooldowns (default 3h)
        cooldown_hours = getattr(rotation_cfg, 'removed_cooldown_hours', 3.0)
        expired_cd = [s for s, t in self._removed_cooldown.items()
                      if (now - t).total_seconds() / 3600 >= cooldown_hours]
        for s in expired_cd:
            del self._removed_cooldown[s]

        # 6. Build new symbol list -- ACCUMULATIVE GROWTH
        # Core (always) + Proven (earned their spot) + Protected (open positions) + New trials
        # Key: proven symbols DON'T consume trial slots -> list grows over time
        rotation_pool_size = getattr(rotation_cfg, 'rotation_pool_size', 6)
        max_symbols = getattr(rotation_cfg, 'max_symbols', 15)
        min_score = rotation_cfg.min_score
        min_vol = getattr(rotation_cfg, 'min_24h_volume', 10_000_000)
        score_map = {r.symbol: r.score for r in results}
        vol_map = {r.symbol: r.vol_24h for r in results}

        new_symbols = set()

        # Tier 1: Always keep core/pinned symbols
        for sym in self._pinned_symbols:
            new_symbols.add(sym)

        # Tier 2: Keep proven symbols — ALWAYS
        # v12: Proven symbols earned their spot through actual trading PnL.
        # The scanner snapshot is too transient (BOS/HTF change every 15-60min)
        # to justify benching profitable symbols.  Previously ALL proven symbols
        # were perma-benched because actionable_proximity was always 0.
        benched_proven = set()
        for sym in self._proven_symbols:
            if sym in force_remove or sym in self._pnl_ban_until or sym in self._blacklist:
                continue
            new_symbols.add(sym)

        # Tier 2.5: Keep protected symbols (open positions, profitable-but-not-proven)
        for sym in protected:
            new_symbols.add(sym)

        # Tier 3: Fill with NEW trial symbols from scanner
        # Proven symbols DON'T reduce trial slots -- only hard cap limits growth
        available_new_slots = min(rotation_pool_size, max_symbols - len(new_symbols))

        proven_kept = len(proven_active)
        logger.info(
            f"🔄 Building list: {len(new_symbols)} kept "
            f"({core_count} core + {proven_kept} proven + "
            f"{len(new_symbols) - core_count - proven_kept} other) "
            f"-> {available_new_slots} slots for new trials (max {max_symbols})"
        )

        added_trials = 0
        if available_new_slots > 0:
            # Get eligible candidates: not in list, not blacklisted, not banned,
            # not in cooldown, volume OK
            eligible = [
                r for r in results
                if r.score >= min_score
                and r.vol_24h >= min_vol
                and r.symbol not in new_symbols
                and r.symbol not in self._blacklist
                and r.symbol not in self._pnl_ban_until
                and r.symbol not in self._removed_cooldown
            ]

            for r in eligible:
                if added_trials >= available_new_slots:
                    break
                new_symbols.add(r.symbol)
                added_trials += 1
                logger.info(
                    f"  🆕 {r.symbol}: new trial (score={r.score:.1f}, "
                    f"vol=${r.vol_24h/1e6:.0f}M)"
                )

        # Fallback: relax score if not enough trials — use 75% floor + require bounce > 0
        #   Prevents filling slots with zero-bounce or score-4 trash during overnight hours.
        remaining_slots = available_new_slots - added_trials
        relaxed_min = min_score * 0.75
        if remaining_slots > 0:
            for r in results:
                if remaining_slots <= 0:
                    break
                if (r.symbol not in new_symbols and r.symbol not in self._blacklist
                        and r.symbol not in self._pnl_ban_until
                        and r.symbol not in self._removed_cooldown
                        and r.vol_24h >= min_vol
                        and r.score >= relaxed_min
                        and r.bounce_rate > 0.0):
                    new_symbols.add(r.symbol)
                    remaining_slots -= 1
                    added_trials += 1
                    logger.info(
                        f"  🆕 {r.symbol}: new trial (relaxed score={r.score:.1f}, "
                        f"vol=${r.vol_24h/1e6:.0f}M)"
                    )

        new_list = sorted(new_symbols)
        old_list = sorted(symbol_states.keys())

        added = set(new_list) - set(old_list)
        removed = set(old_list) - set(new_list)

        # Safety: never remove core/pinned symbols, never add blacklisted
        removed -= self._pinned_symbols
        added -= self._blacklist

        # Add removed symbols to cooldown (prevent immediate re-selection)
        for sym in removed:
            self._removed_cooldown[sym] = now
            pnl_data = symbol_pnl.get(sym)
            pnl_str = f"PnL=${pnl_data['net_pnl']:+.2f}" if pnl_data else "no PnL"
            logger.info(
                f"  ⏳ {sym}: cooldown {cooldown_hours:.0f}h ({pnl_str})"
            )

        if not added and not removed:
            logger.info("🔄 Rotation: no changes needed")
            self._last_rotation = now
            return None

        # 7. Log changes with tier context
        def _removed_info(s):
            p = symbol_pnl.get(s, {}).get("net_pnl", 0)
            tier = "proven" if s in demoted else "trial"
            return f"{s} ({tier}, PnL=${p:+.2f})"

        added_str = ', '.join(f'{s} (score={score_map.get(s, 0):.1f})' for s in sorted(added)) or 'none'
        removed_str = ', '.join(_removed_info(s) for s in sorted(removed)) or 'none'

        logger.info(
            f"🔄 Symbol rotation: {len(old_list)} -> {len(new_list)} symbols "
            f"(growth: {len(new_list) - len(old_list):+d})\n"
            f"   Added:    {added_str}\n"
            f"   Removed:  {removed_str}\n"
            f"   Proven:   {', '.join(sorted(self._proven_symbols)) or 'none'}\n"
            f"   Capacity: {len(new_list)}/{max_symbols}"
        )

        # 8. Apply changes
        await self._apply_rotation(
            new_list=new_list,
            removed=removed,
            added=added,
            symbol_states=symbol_states,
            ws_handler=ws_handler,
            score_map=score_map,
            signal_tracker=signal_tracker,
        )

        self._last_rotation = now

        # 9. Log final symbol list with tiers + scores + PnL
        for r in results[:len(new_list) + 5]:
            in_list = "✅" if r.symbol in new_list else "  "
            pnl_data = symbol_pnl.get(r.symbol)
            pnl_str = f"PnL=${pnl_data['net_pnl']:+.2f}" if pnl_data else "no trades"
            vol_str = f"vol=${r.vol_24h/1e6:.0f}M"
            # Tier label
            if r.symbol in self._pinned_symbols:
                tier = "CORE"
            elif r.symbol in self._proven_symbols:
                tier = "PROVEN"
            elif r.symbol in new_list:
                tier = "TRIAL"
            else:
                tier = ""
            tier_str = f" [{tier}]" if tier else ""
            logger.info(
                f"  {in_list} {r.symbol:<14} score={r.score:5.1f} {vol_str} "
                f"fill={r.fill_rate:.0f}% bounce={r.bounce_rate:.0f}% "
                f"avgR={r.avg_r_achieved:.1f} trend={r.trend_aligned_pct:.0f}% "
                f"sig/10h={r.signal_rate:.1f} | {pnl_str}{tier_str}"
            )

        return new_list

    async def _apply_rotation(
        self,
        new_list: list,
        removed: set,
        added: set,
        symbol_states: dict,
        ws_handler,
        score_map: dict = None,
        signal_tracker=None,
    ) -> None:
        """Apply symbol list changes: update states, precision, WS subs."""
        from .models import SymbolState
        from . import exchange_adapter

        # Remove old symbols (only those without positions)
        for sym in removed:
            state = symbol_states.get(sym)
            if state and not state.has_position:
                del symbol_states[sym]
                logger.info(f"  ➖ Removed {sym}")

        # Add new symbols
        for sym in added:
            if sym not in symbol_states:
                symbol_states[sym] = SymbolState(symbol=sym)
                logger.info(f"  ➕ Added {sym}")
                # Reset signal stats for fresh start
                if signal_tracker:
                    signal_tracker.activate(sym)

        # Update precision dicts from cache
        for sym in new_list:
            info = self._symbol_info_cache.get(sym)
            if info:
                exchange_adapter.PRICE_PRECISION[sym] = info.price_precision
                exchange_adapter.QTY_PRECISION[sym] = info.qty_precision

        # Update config symbols list
        self.config.symbols = list(symbol_states.keys())

        # Persist to config.yaml so restarts keep the new list
        self._save_symbols_to_yaml(list(symbol_states.keys()), score_map=score_map or {})

        # Subscribe new symbols to WS kline channel
        if added:
            await ws_handler.subscribe_new_symbols(list(added))
        # Remove old symbols from WS tracking
        actually_removed = [s for s in removed if s not in symbol_states]
        if actually_removed:
            ws_handler.unsubscribe_symbols(actually_removed)
        logger.info(f"🔄 Active symbols: {sorted(symbol_states.keys())}")

    def _save_symbols_to_yaml(self, symbols: list, score_map: dict = None) -> None:
        """Persist current symbol list to config.yaml.
        
        Replaces the `symbols:` block while preserving all other config.
        Uses regex to find and replace only the symbols section.
        """
        try:
            with open(self._config_path, 'r') as f:
                content = f.read()

            # Build new symbols block
            lines = []
            lines.append(f"# Trading Symbols ({len(symbols)} symbols \u2014 rotation {datetime.now().strftime('%b %d %H:%M')})")
            lines.append("symbols:")
            for sym in symbols:
                score = score_map.get(sym, 0) if score_map else 0
                score_str = f"  # score={score:.1f}" if score > 0 else ""
                lines.append(f"  - {sym}{score_str}")
            
            new_block = "\n".join(lines)

            # Replace from "# Trading Symbols" through end of symbols list
            # Pattern: match comment line + symbols: + all indented lines (entries + comments)
            pattern = r'# Trading Symbols[^\n]*\nsymbols:\n(?:  [^\n]*\n)*'
            
            match = re.search(pattern, content)
            if match:
                content = content[:match.start()] + new_block + "\n" + content[match.end():]
            else:
                # Fallback: match symbols: + all indented lines
                pattern2 = r'symbols:\n(?:  [^\n]*\n)+'
                match2 = re.search(pattern2, content)
                if match2:
                    symbols_only = "symbols:\n" + "\n".join(f"  - {s}" for s in symbols) + "\n"
                    content = content[:match2.start()] + symbols_only + content[match2.end():]
                else:
                    logger.warning("Could not find symbols section in config.yaml")
                    return

            with open(self._config_path, 'w') as f:
                f.write(content)
            
            logger.info(f"💾 Saved {len(symbols)} symbols to {self._config_path}")
        except Exception as e:
            logger.error(f"Failed to save symbols to yaml: {e}")

    # ==================== Opportunity Scanner ====================

    async def opportunity_scan(
        self,
        symbol_states: dict,
        ws_handler,
        signal_tracker=None,
    ) -> Optional[List[str]]:
        """Fast market-wide scan for immediate trading opportunities.

        Runs every 15 min between full rotations. Scans top symbols by
        volume, finds ones with price RIGHT AT an FVG zone, and hot-swaps
        them in replacing the worst "cold" trial symbols.

        Key differences from full rotation:
        - No PnL review, promotions, or demotions
        - Fewer candles (100 vs 200) — faster scan
        - Only swaps if opportunity has HIGH actionable proximity
        - Max N swaps per scan (prevents thrashing)
        - Never touches core/proven/protected symbols

        Returns:
            List of newly added symbols, or None if no changes.
        """
        rot_cfg = self.config.rotation
        if not getattr(rot_cfg, 'opportunity_scan_enabled', True):
            return None

        top_n = getattr(rot_cfg, 'opportunity_scan_top_n', 50)
        candle_count = getattr(rot_cfg, 'opportunity_scan_candles', 100)
        min_proximity = getattr(rot_cfg, 'opportunity_min_proximity', 70.0)
        max_swaps = getattr(rot_cfg, 'opportunity_max_swaps', 2)
        min_score_advantage = getattr(rot_cfg, 'opportunity_min_score_advantage', 10.0)

        # 1. Identify trial symbols (candidates for swap-out)
        #    Never swap out: core, proven, symbols with open positions
        trial_symbols = [
            sym for sym in symbol_states
            if sym not in self._pinned_symbols
            and sym not in self._proven_symbols
            and not symbol_states[sym].has_position
        ]

        if not trial_symbols:
            logger.debug("🎯 Opportunity scan: no trial symbols to swap out")
            return None

        # 2. Run lightweight scan (fewer candles, wider market)
        logger.info(f"🎯 Opportunity scan: scanning {top_n} symbols ({candle_count} candles)...")
        results = await self.scan_all_symbols(
            top_n=top_n,
            candle_count=candle_count,
            min_gap=self.config.fvg.min_gap_percent,
        )

        if not results:
            logger.info("🎯 Opportunity scan: no results from scanner")
            return None

        # 3. Find high-proximity opportunities NOT already in our list
        current_symbols = set(symbol_states.keys())
        min_score = rot_cfg.min_score
        min_vol = getattr(rot_cfg, 'min_24h_volume', 10_000_000)

        opportunities = [
            r for r in results
            if r.symbol not in current_symbols
            and r.symbol not in self._blacklist
            and r.symbol not in self._pnl_ban_until
            and r.symbol not in self._removed_cooldown
            and r.actionable_proximity >= min_proximity
            and r.score >= min_score
            and r.vol_24h >= min_vol
        ]

        if not opportunities:
            n_non_active = sum(1 for r in results if r.symbol not in current_symbols)
            best_non_active = max(
                (r for r in results if r.symbol not in current_symbols),
                key=lambda r: r.actionable_proximity,
                default=None,
            )
            best_info = (
                f"best={best_non_active.symbol} actP={best_non_active.actionable_proximity:.0f} "
                f"score={best_non_active.score:.1f}"
                if best_non_active else "none"
            )
            logger.info(
                f"🎯 Opportunity scan: no high-proximity opportunities "
                f"(threshold={min_proximity:.0f}, checked {n_non_active} non-active, "
                f"{best_info})"
            )
            return None

        # 4. Score current trial symbols (from the same scan)
        trial_scores = {}
        for sym in trial_symbols:
            scan_result = next((r for r in results if r.symbol == sym), None)
            trial_scores[sym] = scan_result.score if scan_result else 0.0

        # Sort trials worst-first (candidates for removal)
        worst_trials = sorted(trial_symbols, key=lambda s: trial_scores.get(s, 0))

        # 5. Match opportunities with worst trials
        added = set()
        removed = set()
        swaps = 0

        for opp in sorted(opportunities, key=lambda r: r.score, reverse=True):
            if swaps >= max_swaps or not worst_trials:
                break

            worst_sym = worst_trials[0]
            worst_score = trial_scores.get(worst_sym, 0)

            # Only swap if opportunity is significantly better
            if opp.score > worst_score + min_score_advantage:
                worst_trials.pop(0)
                added.add(opp.symbol)
                removed.add(worst_sym)
                swaps += 1
                logger.info(
                    f"  🎯 Opportunity swap: {worst_sym} (score={worst_score:.1f}) "
                    f"→ {opp.symbol} (score={opp.score:.1f}, "
                    f"actP={opp.actionable_proximity:.0f}, "
                    f"conv={opp.convergence_score:.0f})"
                )

        if not added:
            logger.info(
                f"🎯 Opportunity scan: {len(opportunities)} opportunities found but "
                f"none beats current trials by >{min_score_advantage:.0f}pts"
            )
            return None

        # 6. Apply the swap
        score_map = {r.symbol: r.score for r in results}
        new_list = sorted((current_symbols - removed) | added)

        # Add removed symbols to cooldown
        now = datetime.now()
        cooldown_hours = getattr(rot_cfg, 'removed_cooldown_hours', 3.0)
        for sym in removed:
            self._removed_cooldown[sym] = now

        await self._apply_rotation(
            new_list=new_list,
            removed=removed,
            added=added,
            symbol_states=symbol_states,
            ws_handler=ws_handler,
            score_map=score_map,
            signal_tracker=signal_tracker,
        )

        logger.info(
            f"🎯 Opportunity scan complete: {len(added)} swaps | "
            f"out={sorted(removed)} | in={sorted(added)}"
        )

        return list(added)
