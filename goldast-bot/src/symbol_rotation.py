"""
Symbol Rotation — Daily automatic symbol selection.

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
        # Core symbols — always active, never removed
        self._pinned_symbols: Set[str] = set(getattr(config, 'core_symbols', []))
        # Blacklist — never added by rotation
        self._blacklist: Set[str] = set(getattr(config, 'blacklist', []))
        # Proven symbols — profitable rotation symbols, kept indefinitely
        self._proven_symbols: Set[str] = self._load_proven()
        # PnL-based ban: {symbol: ban_until_datetime}
        self._pnl_ban_until: Dict[str, datetime] = {}

    # ==================== Proven Symbols Persistence ====================

    def _load_proven(self) -> Set[str]:
        """Load proven symbols from data/proven_symbols.json."""
        try:
            path = Path(self._proven_path)
            if path.exists():
                with open(path, 'r') as f:
                    data = json.load(f)
                symbols = set(data) if isinstance(data, list) else set()
                # Remove any blacklisted or core (they have their own tier)
                symbols -= set(getattr(self.config, 'blacklist', []))
                symbols -= set(getattr(self.config, 'core_symbols', []))
                if symbols:
                    logger.info(f"⭐ Loaded {len(symbols)} proven symbols: {', '.join(sorted(symbols))}")
                return symbols
        except Exception as e:
            logger.warning(f"Failed to load proven symbols: {e}")
        return set()

    def _save_proven(self) -> None:
        """Persist proven symbols to data/proven_symbols.json."""
        try:
            path = Path(self._proven_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w') as f:
                json.dump(sorted(self._proven_symbols), f, indent=2)
            logger.info(f"💾 Saved {len(self._proven_symbols)} proven symbols")
        except Exception as e:
            logger.error(f"Failed to save proven symbols: {e}")

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
        min_volume_ratio: float = 1.3,
        entry_zone_min: float = 0.005,
        entry_zone_max: float = 0.85,
        bounce_r: float = 2.5,
        ema_fast: int = 8,
        ema_slow: int = 21,
    ) -> Optional[Dict]:
        """Run FVG suitability metrics with strategy-aware filters.

        v3 changes vs v2:
        - FVGs filtered by volume ratio (matches bot's min_volume_ratio filter)
        - EMA trend alignment check (simulates bot's 70% 1h + 30% 15m trend weight)
        - Bounce measured at configurable R (matches bot's min_rr from config)
        - Fill zone uses config entry_zone_min/max instead of hardcoded 5-85%
        - NEW: trend_aligned_pct — % of FVGs matching EMA trend direction
        - NEW: signal_rate — predicted tradeable signals per 10h window
        - NEW: avg_vol_ratio — average volume ratio on FVG candles
        """
        n = len(candles)
        if n < 30:
            return None

        price = candles[-1]["c"]
        atr = SymbolRotation._calculate_atr(candles)
        atr_pct = (atr / price * 100) if price > 0 else 0

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
                return score_15m * 0.3 + score_1h * 0.7
            elif score_1h is not None:
                return score_1h
            elif score_15m is not None:
                return score_15m
            return None

        # Trend clarity (legacy metric — measures trendiness vs chop)
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

            # Bullish FVG
            bull_gap = c3["l"] - c1["h"]
            if bull_gap > 0 and (bull_gap / c2["c"]) >= min_gap:
                vol_ratio = _check_vol_ratio(i + 1)
                if vol_ratio >= min_volume_ratio:
                    trend = get_trend_at(i + 2)
                    fvgs.append({
                        "idx": i + 2, "dir": "LONG",
                        "top": c3["l"], "bottom": c1["h"],
                        "vol": c2["v"], "vol_ratio": vol_ratio,
                        "trend_aligned": trend is not None and trend > 0,
                    })
                    fvg_vol_ratios.append(vol_ratio)

            # Bearish FVG
            bear_gap = c1["l"] - c3["h"]
            if bear_gap > 0 and (bear_gap / c2["c"]) >= min_gap:
                vol_ratio = _check_vol_ratio(i + 1)
                if vol_ratio >= min_volume_ratio:
                    trend = get_trend_at(i + 2)
                    fvgs.append({
                        "idx": i + 2, "dir": "SHORT",
                        "top": c1["l"], "bottom": c3["h"],
                        "vol": c2["v"], "vol_ratio": vol_ratio,
                        "trend_aligned": trend is not None and trend < 0,
                    })
                    fvg_vol_ratios.append(vol_ratio)

        fvg_count = len(fvgs)
        fvg_density = fvg_count / n * 100 if n > 0 else 0

        # Average volume ratio on FVG candles
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
                if fvg.get("trend_aligned", False):
                    tradeable_signals += 1

        fill_rate = filled / fvg_count * 100 if fvg_count > 0 else 0
        bounce_rate = bounced / filled * 100 if filled > 0 else 0
        avg_r_achieved = sum(r_values) / len(r_values) if r_values else 0.0

        # Signal rate: tradeable signals per 10h window
        if len(candles) >= 2 and candles[-1]["t"] > candles[0]["t"]:
            span_hours = (candles[-1]["t"] - candles[0]["t"]) / 3_600_000  # ms→hours
            if span_hours <= 0:
                span_hours = n * 0.25  # fallback: 15m per candle
        else:
            span_hours = n * 0.25
        signal_rate = tradeable_signals / span_hours * 10 if span_hours > 0 else 0

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
        }

    @staticmethod
    def _compute_score(m: Dict) -> float:
        """Composite score (0-100) optimized for STRATEGY-ALIGNED signal production.

        v3 changes:
        - Bounce measured at config min_rr (2.5R default) instead of hardcoded 2R
        - NEW: Signal Rate (10pts) — predicted tradeable signals per 10h window
        - NEW: Volume Strength (5pts) — avg volume ratio on FVG candles
        - NEW: Trend Alignment (10pts) — % of FVGs matching EMA trend direction
        - FVG Density reduced (10→5): raw count less important than quality
        - Vol Spike removed → replaced by Volume Strength (directly from FVG candles)
        - Vol 24h reduced (15→10): liquidity floor already enforced by min_24h_volume

        Weights: Fill(20) + Bounce(15) + AvgR(20) + SignalRate(10) + VolStr(5)
                 + TrendAlign(10) + Density(5) + ATR(5) + Vol24h(10)
        Total: 100
        """
        score = 0.0

        # 1. Fill Rate — how often price retests FVG zones (weight: 20)
        fr = m.get("fill_rate", 0)
        score += min(fr / 50, 1.0) * 20

        # 2. Bounce Rate at min_rr — quality: filled FVGs reaching target R (weight: 15)
        br = m.get("bounce_rate", 0)
        score += min(br / 50, 1.0) * 15

        # 3. Avg R Achieved — profitability: average max-R move after fill (weight: 20)
        avg_r = m.get("avg_r_achieved", 0)
        if avg_r >= 3.0:
            score += 20
        elif avg_r >= 2.5:
            score += 17
        elif avg_r >= 2.0:
            score += 14
        elif avg_r >= 1.5:
            score += 10
        elif avg_r >= 1.0:
            score += 6
        elif avg_r >= 0.5:
            score += 3

        # 4. Signal Rate — predicted tradeable signals per 10h (weight: 10)
        #    Tradeable = filled + trend-aligned + volume-filtered
        sr = m.get("signal_rate", 0)
        if sr >= 3.0:
            score += 10
        elif sr >= 2.0:
            score += 8
        elif sr >= 1.0:
            score += 5
        elif sr >= 0.5:
            score += 3
        elif sr > 0:
            score += 1

        # 5. Volume Strength — avg volume ratio on FVG candles (weight: 5)
        #    Higher vol ratio = more institutional FVGs
        vr = m.get("avg_vol_ratio", 0)
        if vr >= 2.0:
            score += 5
        elif vr >= 1.5:
            score += 4
        elif vr >= 1.3:
            score += 3
        elif vr >= 1.0:
            score += 1

        # 6. Trend Alignment — % of FVGs in EMA trend direction (weight: 10)
        #    80%+ aligned = clearly trending, most FVGs tradeable by bot
        ta = m.get("trend_aligned_pct", 50)
        score += min(ta / 80, 1.0) * 10

        # 7. FVG Density — more FVGs = more chances (weight: 5)
        d = m.get("fvg_density", 0)
        if d <= 0:
            score += 0
        elif d <= 5:
            score += d / 5 * 4
        elif d <= 15:
            score += 5
        elif d <= 25:
            score += max(2, 5 - (d - 15) * 0.3)
        else:
            score += 1

        # 8. ATR — volatility suitability (weight: 5)
        atr = m.get("atr_pct", 0)
        if 0.20 <= atr <= 1.50:
            score += 5
        elif 0.15 <= atr <= 2.00:
            score += 3
        elif atr >= 0.10:
            score += 1

        # 9. 24h Volume — liquidity (weight: 10)
        vol_24h = m.get("vol_24h", 0)
        if vol_24h >= 100_000_000:
            score += 10
        elif vol_24h >= 50_000_000:
            score += 8
        elif vol_24h >= 25_000_000:
            score += 5
        elif vol_24h >= 10_000_000:
            score += 3

        return round(score, 1)

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
        # Volume floor from config (default $10M) — reject illiquid pairs
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
                            "v": float(c.get("volume", c.get("v", c.get("vol", c.get("quoteVol", 0))))),
                        })
                    elif isinstance(c, (list, tuple)) and len(c) >= 6:
                        candles.append({
                            "t": int(c[0]) if len(c) > 0 else 0,
                            "o": float(c[1]), "h": float(c[2]),
                            "l": float(c[3]), "c": float(c[4]),
                            "v": float(c[5]),
                        })

                candles.sort(key=lambda x: x.get("t", x.get("ts", 0)))  # sort by timestamp

                metrics = self._analyze_symbol(
                    candles,
                    min_gap=min_gap,
                    min_volume_ratio=self.config.fvg.min_volume_ratio,
                    entry_zone_min=self.config.fvg.entry_zone_min,
                    entry_zone_max=self.config.fvg.entry_zone_max,
                    bounce_r=self.config.tpsl.min_rr,
                    ema_fast=self.config.trend.ema_fast,
                    ema_slow=self.config.trend.ema_slow,
                )
                if not metrics:
                    continue

                # Inject 24h volume for scoring
                metrics["vol_24h"] = t["_vol"]
                score = self._compute_score(metrics)
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
                ))
            except Exception as e:
                logger.debug(f"Scan error for {symbol}: {e}")

            await asyncio.sleep(0.15)  # rate limit

        results.sort(key=lambda x: x.score, reverse=True)
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
        1. Symbols with open positions → PROTECTED (never removed)
        2. Symbols with positive PnL → PROTECTED (keep winners)
        3. Symbols with negative PnL → CANDIDATES for replacement
        4. Symbols with no trades → CANDIDATES (untested)
        5. New symbols chosen from scanner top scorers
        
        Args:
            symbol_states: Dict[str, SymbolState] — modifiable in-place
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

        # 3. Classify symbols into 3 tiers: Core → Proven → Trial
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
            # Core/pinned — always protected (own tier)
            if sym in self._pinned_symbols:
                protected.add(sym)
                protect_reasons[sym] = "core"
                continue

            # Open positions always protected
            if state.has_position:
                protected.add(sym)
                protect_reasons[sym] = "open position"
                continue

            # Check consecutive losing streak → force remove + demote
            if trade_history and max_losing > 0:
                streak = trade_history.get_recent_streak(sym, lookback_hours=24)
                if streak <= -max_losing:
                    force_remove.add(sym)
                    if sym in self._proven_symbols:
                        demoted.add(sym)
                    logger.info(
                        f"  🚫 {sym}: {abs(streak)} consecutive losses "
                        f"→ forced removal{' (demoted from proven)' if sym in self._proven_symbols else ''}"
                    )
                    continue

            # Silent zone ban → force remove + demote
            if sym in silent_symbols:
                force_remove.add(sym)
                if sym in self._proven_symbols:
                    demoted.add(sym)
                active_h = signal_tracker.hours_since_activation(sym) if signal_tracker else 0
                logger.info(
                    f"  🔇 {sym}: 0 zone hits in {active_h:.1f}h "
                    f"→ forced removal{' (demoted from proven)' if sym in self._proven_symbols else ''}"
                )
                continue

            # Check for proven promotion: profitable + enough trades
            pnl_data = symbol_pnl.get(sym)
            if pnl_data and pnl_data['net_pnl'] >= proven_min_pnl and pnl_data['trades'] >= proven_min_trades:
                # Promote to proven (or reconfirm)
                if sym not in self._proven_symbols:
                    promoted.add(sym)
                    logger.info(
                        f"  🆙 {sym}: promoted to proven "
                        f"(PnL=${pnl_data['net_pnl']:+.2f}, {pnl_data['trades']} trades)"
                    )
                protected.add(sym)
                protect_reasons[sym] = f"proven PnL=${pnl_data['net_pnl']:+.2f}"
                continue

            # Already proven but not currently meeting promotion threshold → still protected (grace)
            if sym in self._proven_symbols:
                protected.add(sym)
                pnl_str = f"PnL=${pnl_data['net_pnl']:+.2f}" if pnl_data else "no recent trades"
                protect_reasons[sym] = f"proven (grace: {pnl_str})"
                continue

            # Profitable but below proven threshold → protected (keep winners)
            if getattr(rotation_cfg, 'protect_profitable', True):
                if pnl_data and pnl_data['profitable']:
                    protected.add(sym)
                    protect_reasons[sym] = f"profitable PnL=${pnl_data['net_pnl']:+.2f}"
                    continue

            # Everything else → replaceable (negative PnL, untested trial symbols)

        # Apply proven promotions and demotions
        self._proven_symbols |= promoted
        self._proven_symbols -= demoted
        self._proven_symbols -= self._pinned_symbols  # core symbols have their own tier
        self._proven_symbols -= self._blacklist

        # 4. Determine replaceable symbols (negative PnL, untested, or force-removed)
        replaceable = (set(symbol_states.keys()) - protected) | force_remove

        # 4.5 PnL-based ban: ban the worst-performing rotation symbol for 24h
        pnl_ban_cfg_enabled = getattr(rotation_cfg, 'pnl_ban_enabled', False)
        pnl_ban_hours = getattr(rotation_cfg, 'pnl_ban_hours', 24)
        if pnl_ban_cfg_enabled and symbol_pnl:
            # Only consider rotation symbols (not core) that have trades
            rotation_syms_with_pnl = {
                sym: data for sym, data in symbol_pnl.items()
                if sym not in self._pinned_symbols and data.get('trades', 0) >= 2
            }
            if rotation_syms_with_pnl:
                worst_sym = min(rotation_syms_with_pnl, key=lambda s: rotation_syms_with_pnl[s]['net_pnl'])
                worst_pnl = rotation_syms_with_pnl[worst_sym]['net_pnl']
                if worst_pnl < 0:
                    ban_until = now + timedelta(hours=pnl_ban_hours)
                    self._pnl_ban_until[worst_sym] = ban_until
                    force_remove.add(worst_sym)
                    replaceable.add(worst_sym)
                    # Demote from proven if applicable
                    if worst_sym in self._proven_symbols:
                        self._proven_symbols.discard(worst_sym)
                        demoted.add(worst_sym)
                    logger.info(
                        f"  💀 {worst_sym}: worst daily PnL=${worst_pnl:+.2f} "
                        f"→ banned for {pnl_ban_hours}h (until {ban_until.strftime('%H:%M')})"
                        f"{' — demoted from proven' if worst_sym in demoted else ''}"
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

        # 6. Build new symbol list — ACCUMULATIVE GROWTH
        # Core (always) + Proven (earned their spot) + Protected (open positions) + New trials
        # Key: proven symbols DON'T consume trial slots → list grows over time
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

        # Tier 2: Keep all surviving proven symbols (not force-removed or banned)
        for sym in self._proven_symbols:
            if sym not in force_remove and sym not in self._pnl_ban_until and sym not in self._blacklist:
                new_symbols.add(sym)

        # Tier 2.5: Keep protected symbols (open positions, profitable-but-not-proven)
        for sym in protected:
            new_symbols.add(sym)

        # Tier 3: Fill with NEW trial symbols from scanner
        # Proven symbols DON'T reduce trial slots — only hard cap limits growth
        available_new_slots = min(rotation_pool_size, max_symbols - len(new_symbols))

        logger.info(
            f"🔄 Building list: {len(new_symbols)} kept "
            f"({core_count} core + {len(proven_active)} proven + "
            f"{len(new_symbols) - core_count - len(proven_active)} other) "
            f"→ {available_new_slots} slots for new trials (max {max_symbols})"
        )

        added_trials = 0
        if available_new_slots > 0:
            # Get eligible candidates: not in list, not blacklisted, not banned, volume OK
            eligible = [
                r for r in results
                if r.score >= min_score
                and r.vol_24h >= min_vol
                and r.symbol not in new_symbols
                and r.symbol not in self._blacklist
                and r.symbol not in self._pnl_ban_until
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

        # Fallback: relax score if not enough trials (but keep volume + blacklist check)
        remaining_slots = available_new_slots - added_trials
        if remaining_slots > 0:
            for r in results:
                if remaining_slots <= 0:
                    break
                if (r.symbol not in new_symbols and r.symbol not in self._blacklist
                        and r.symbol not in self._pnl_ban_until and r.vol_24h >= min_vol):
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
            f"🔄 Symbol rotation: {len(old_list)} → {len(new_list)} symbols "
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
