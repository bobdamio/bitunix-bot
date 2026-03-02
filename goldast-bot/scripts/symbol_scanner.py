"""
Symbol Scanner — Analyze which pairs are best suited for FVG strategy.

Fetches kline data for top-volume pairs and scores them using
the same strategy-aligned filters as the in-bot rotation scanner:
1. FVG detection with volume ratio filter (min_volume_ratio)
2. EMA 8/21 trend alignment (simulates bot's 70% 1h + 30% 15m weight)
3. Bounce at configurable R (matches bot's min_rr)
4. Trend-aligned signal rate (predicted tradeable signals per 10h)
5. Strategy-aware composite scoring

Usage:
    python scripts/symbol_scanner.py [--top N] [--min-gap 0.0015]
    python scripts/symbol_scanner.py --config config.yaml
"""

import asyncio
import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import List, Dict, Optional

# Add parent dir so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.bitunix_client import BitunixClient


@dataclass
class Candle:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class SymbolScore:
    symbol: str
    price: float = 0.0
    vol_24h: float = 0.0
    candle_count: int = 0
    # Metrics
    fvg_density: float = 0.0        # FVGs per 100 candles (volume-filtered)
    fill_rate: float = 0.0          # % FVGs where price enters zone
    bounce_rate: float = 0.0        # given fill, % that produce min_rr profit
    atr_pct: float = 0.0            # ATR as % of price
    volume_spike: float = 0.0       # FVG candle vol / avg vol
    trend_clarity: float = 0.0      # % time in clean trend
    wick_body_ratio: float = 0.0    # avg wick / body
    avg_r_achieved: float = 0.0     # Average max R move after fill
    trend_aligned_pct: float = 0.0  # % of FVGs aligned with EMA trend
    signal_rate: float = 0.0        # Predicted tradeable signals per 10h
    avg_vol_ratio: float = 0.0      # Avg volume ratio on FVG candles
    # Composite
    total_score: float = 0.0
    fvg_count: int = 0
    filled_count: int = 0
    bounce_count: int = 0


def parse_candles(raw_candles: list) -> List[Candle]:
    """Parse raw kline data into Candle objects."""
    candles = []
    for c in raw_candles:
        try:
            if isinstance(c, dict):
                candles.append(Candle(
                    ts=int(c.get('time', c.get('t', 0))),
                    o=float(c.get('open', c.get('o', 0))),
                    h=float(c.get('high', c.get('h', 0))),
                    l=float(c.get('low', c.get('l', 0))),
                    c=float(c.get('close', c.get('c', 0))),
                    v=float(c.get('volume', c.get('v', c.get('vol', 0)))),
                ))
            elif isinstance(c, (list, tuple)) and len(c) >= 6:
                candles.append(Candle(
                    ts=int(c[0]), o=float(c[1]), h=float(c[2]),
                    l=float(c[3]), c=float(c[4]), v=float(c[5]),
                ))
        except (ValueError, TypeError):
            continue
    # Sort by timestamp ascending
    candles.sort(key=lambda x: x.ts)
    return candles


def calculate_atr(candles: List[Candle], period: int = 14) -> float:
    """Calculate ATR."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i].h - candles[i].l,
            abs(candles[i].h - candles[i-1].c),
            abs(candles[i].l - candles[i-1].c),
        )
        trs.append(tr)
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent) if recent else 0.0


def ema_full(values: list, period: int) -> list:
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


def analyze_symbol(
    candles: List[Candle],
    min_gap: float = 0.0015,
    min_volume_ratio: float = 1.3,
    entry_zone_min: float = 0.005,
    entry_zone_max: float = 0.85,
    bounce_r: float = 2.5,
    ema_fast: int = 8,
    ema_slow: int = 21,
) -> Dict:
    """Run strategy-aligned FVG metrics on a symbol's candle data.

    Mirrors the in-bot rotation scanner logic (symbol_rotation.py v3):
    - FVGs filtered by volume ratio (matches bot's min_volume_ratio)
    - EMA trend alignment (simulates bot's 70% 1h + 30% 15m weight)
    - Bounce at configurable R (matches bot's min_rr)
    - Signal rate: predicted tradeable signals per 10h window
    """
    n = len(candles)
    if n < 30:
        return {}

    price = candles[-1].c
    atr = calculate_atr(candles)
    atr_pct = (atr / price * 100) if price > 0 else 0

    # Average volume
    volumes = [c.v for c in candles if c.v > 0]
    avg_vol = sum(volumes) / len(volumes) if volumes else 1.0

    # Wick/Body ratio
    wick_body_ratios = []
    for c in candles:
        body = abs(c.c - c.o)
        upper_wick = c.h - max(c.o, c.c)
        lower_wick = min(c.o, c.c) - c.l
        total_wick = upper_wick + lower_wick
        if body > 0:
            wick_body_ratios.append(total_wick / body)
    avg_wick_body = sum(wick_body_ratios) / len(wick_body_ratios) if wick_body_ratios else 999

    # --- EMA Trend Calculation ---
    closes = [c.c for c in candles]

    ema_f_15m = ema_full(closes, ema_fast)       # EMA 8 on 15m
    ema_s_15m = ema_full(closes, ema_slow)       # EMA 21 on 15m
    ema_f_1h = ema_full(closes, ema_fast * 4)    # EMA 32 on 15m ≈ EMA 8 on 1h
    ema_s_1h = ema_full(closes, ema_slow * 4)    # EMA 84 on 15m ≈ EMA 21 on 1h

    def get_trend_at(idx: int):
        """Combined trend score at candle index (-1..+1). None if insufficient data."""
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

    # Trend clarity (trendiness vs chop)
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
            nb_start = max(0, mid_idx - 5)
            nb_end = min(n, mid_idx + 6)
            nb_vols = [
                candles[j].v for j in range(nb_start, nb_end)
                if j != mid_idx and candles[j].v > 0
            ]
            if not nb_vols:
                return 1.0
            return c2.v / (sum(nb_vols) / len(nb_vols))

        # Bullish FVG
        bull_gap = c3.l - c1.h
        if bull_gap > 0 and (bull_gap / c2.c) >= min_gap:
            vol_ratio = _check_vol_ratio(i + 1)
            if vol_ratio >= min_volume_ratio:
                trend = get_trend_at(i + 2)
                fvgs.append({
                    'idx': i + 2, 'dir': 'LONG',
                    'top': c3.l, 'bottom': c1.h,
                    'vol': c2.v, 'vol_ratio': vol_ratio,
                    'trend_aligned': trend is not None and trend > 0,
                })
                fvg_vol_ratios.append(vol_ratio)

        # Bearish FVG
        bear_gap = c1.l - c3.h
        if bear_gap > 0 and (bear_gap / c2.c) >= min_gap:
            vol_ratio = _check_vol_ratio(i + 1)
            if vol_ratio >= min_volume_ratio:
                trend = get_trend_at(i + 2)
                fvgs.append({
                    'idx': i + 2, 'dir': 'SHORT',
                    'top': c1.l, 'bottom': c3.h,
                    'vol': c2.v, 'vol_ratio': vol_ratio,
                    'trend_aligned': trend is not None and trend < 0,
                })
                fvg_vol_ratios.append(vol_ratio)

    fvg_count = len(fvgs)
    fvg_density = fvg_count / n * 100 if n > 0 else 0

    # Average volume ratio on FVG candles
    avg_vol_ratio = sum(fvg_vol_ratios) / len(fvg_vol_ratios) if fvg_vol_ratios else 0.0

    # Volume spike (legacy)
    fvg_vols = [f['vol'] for f in fvgs if f['vol'] > 0]
    vol_spike = (sum(fvg_vols) / len(fvg_vols)) / avg_vol if fvg_vols and avg_vol > 0 else 1.0

    # Trend alignment: % of FVGs in trend direction
    trend_aligned_count = sum(1 for f in fvgs if f.get('trend_aligned', False))
    trend_aligned_pct = trend_aligned_count / fvg_count * 100 if fvg_count > 0 else 0

    # Fill Rate, Bounce Rate (at bounce_r), Avg Max-R, Signal Rate
    filled = 0
    bounced = 0
    r_values = []
    tradeable_signals = 0
    lookforward = 40  # 40 × 15m = 10 hours

    for fvg in fvgs:
        idx, top, bottom = fvg['idx'], fvg['top'], fvg['bottom']
        zone_size = top - bottom
        if zone_size <= 0:
            continue

        got_fill = False
        max_r = 0.0

        for j in range(idx + 1, min(idx + 1 + lookforward, n)):
            cj = candles[j]

            if fvg['dir'] == 'LONG':
                if cj.l <= bottom:
                    break
                fill = (top - cj.l) / zone_size
                if entry_zone_min <= fill <= entry_zone_max:
                    got_fill = True
                    entry = cj.l
                    risk = entry - bottom
                    if risk <= 0:
                        break
                    for k in range(j + 1, min(idx + 1 + lookforward, n)):
                        ck = candles[k]
                        if ck.l <= bottom:
                            break
                        r_now = (ck.h - entry) / risk
                        if r_now > max_r:
                            max_r = r_now
                    break
            else:  # SHORT
                if cj.h >= top:
                    break
                fill = (cj.h - bottom) / zone_size
                if entry_zone_min <= fill <= entry_zone_max:
                    got_fill = True
                    entry = cj.h
                    risk = top - entry
                    if risk <= 0:
                        break
                    for k in range(j + 1, min(idx + 1 + lookforward, n)):
                        ck = candles[k]
                        if ck.h >= top:
                            break
                        r_now = (entry - ck.l) / risk
                        if r_now > max_r:
                            max_r = r_now
                    break

        if got_fill:
            filled += 1
            r_values.append(max_r)
            if max_r >= bounce_r:
                bounced += 1
            if fvg.get('trend_aligned', False):
                tradeable_signals += 1

    fill_rate = filled / fvg_count * 100 if fvg_count > 0 else 0
    bounce_rate = bounced / filled * 100 if filled > 0 else 0
    avg_r_achieved = sum(r_values) / len(r_values) if r_values else 0.0

    # Signal rate: tradeable signals per 10h window
    if len(candles) >= 2 and candles[-1].ts > candles[0].ts:
        span_hours = (candles[-1].ts - candles[0].ts) / 3_600_000
        if span_hours <= 0:
            span_hours = n * 0.25
    else:
        span_hours = n * 0.25
    signal_rate = tradeable_signals / span_hours * 10 if span_hours > 0 else 0

    return {
        'fvg_count': fvg_count,
        'fvg_density': fvg_density,
        'filled': filled,
        'fill_rate': fill_rate,
        'bounced': bounced,
        'bounce_rate': bounce_rate,
        'atr_pct': atr_pct,
        'vol_spike': vol_spike,
        'trend_clarity': trend_clarity,
        'wick_body': avg_wick_body,
        'avg_r_achieved': avg_r_achieved,
        'trend_aligned_pct': trend_aligned_pct,
        'signal_rate': signal_rate,
        'avg_vol_ratio': avg_vol_ratio,
    }


def compute_total_score(m: Dict) -> float:
    """
    Composite score (0-100) — mirrors symbol_rotation.py v3 scoring.

    Strategy-aligned weights that predict actual bot performance:
    - Fill Rate (20): how often price retests FVG zones
    - Bounce at min_rr (15): win rate proxy at target R:R
    - Avg R Achieved (20): best profitability predictor
    - Signal Rate (10): predicted tradeable signals per 10h
    - Volume Strength (5): avg volume ratio on FVG candles
    - Trend Alignment (10): % of FVGs matching EMA trend
    - FVG Density (5): opportunity count
    - ATR (5): volatility suitability
    - Vol 24h (10): liquidity

    Total: 100
    """
    score = 0.0

    # 1. Fill Rate (weight: 20)
    fr = m.get('fill_rate', 0)
    score += min(fr / 50, 1.0) * 20

    # 2. Bounce Rate at min_rr (weight: 15)
    br = m.get('bounce_rate', 0)
    score += min(br / 50, 1.0) * 15

    # 3. Avg R Achieved (weight: 20)
    avg_r = m.get('avg_r_achieved', 0)
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

    # 4. Signal Rate — tradeable signals per 10h (weight: 10)
    sr = m.get('signal_rate', 0)
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

    # 5. Volume Strength (weight: 5)
    vr = m.get('avg_vol_ratio', 0)
    if vr >= 2.0:
        score += 5
    elif vr >= 1.5:
        score += 4
    elif vr >= 1.3:
        score += 3
    elif vr >= 1.0:
        score += 1

    # 6. Trend Alignment (weight: 10)
    ta = m.get('trend_aligned_pct', 50)
    score += min(ta / 80, 1.0) * 10

    # 7. FVG Density (weight: 5)
    d = m.get('fvg_density', 0)
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

    # 8. ATR (weight: 5)
    atr = m.get('atr_pct', 0)
    if 0.20 <= atr <= 1.50:
        score += 5
    elif 0.15 <= atr <= 2.00:
        score += 3
    elif atr >= 0.10:
        score += 1

    # 9. 24h Volume (weight: 10)
    vol_24h = m.get('vol_24h', 0)
    if vol_24h >= 100_000_000:
        score += 10
    elif vol_24h >= 50_000_000:
        score += 8
    elif vol_24h >= 25_000_000:
        score += 5
    elif vol_24h >= 10_000_000:
        score += 3

    return round(score, 1)


async def main():
    parser = argparse.ArgumentParser(description="Symbol Scanner for FVG Strategy (v3)")
    parser.add_argument("--top", type=int, default=25, help="Scan top N pairs by volume")
    parser.add_argument("--min-gap", type=float, default=0.0015, help="Min gap %% for FVG detection")
    parser.add_argument("--min-vol-ratio", type=float, default=1.3, help="Min volume ratio for FVG candle")
    parser.add_argument("--bounce-r", type=float, default=2.5, help="Bounce target in R multiples")
    parser.add_argument("--entry-zone-min", type=float, default=0.005, help="Min fill %% for entry")
    parser.add_argument("--entry-zone-max", type=float, default=0.85, help="Max fill %% for entry")
    parser.add_argument("--ema-fast", type=int, default=8, help="Fast EMA period")
    parser.add_argument("--ema-slow", type=int, default=21, help="Slow EMA period")
    parser.add_argument("--candles", type=int, default=200, help="Number of candles to fetch")
    parser.add_argument("--interval", type=str, default="15min", help="Kline interval: 5min, 15min, 1h")
    parser.add_argument("--min-24h-vol", type=float, default=10_000_000, help="Min 24h volume ($)")
    args = parser.parse_args()

    client = BitunixClient(
        api_key=os.environ.get('BITUNIX_API_KEY', ''),
        api_secret=os.environ.get('BITUNIX_SECRET', ''),
    )

    try:
        # 1. Get all tickers, sort by volume
        print(f"Fetching tickers...")
        tickers = await client._request('GET', '/api/v1/futures/market/tickers', {})

        if not isinstance(tickers, list):
            print("Failed to fetch tickers")
            return

        # Filter and sort
        EXCLUDE = {'BTCUSDC', 'ETHUSDC', 'SOLUSDC', 'DOGEUSD'}  # duplicates
        for t in tickers:
            t['_vol'] = float(t.get('quoteVol', 0) or 0)
            t['_price'] = float(t.get('lastPrice', 0) or 0)
        tickers = [t for t in tickers if t['_vol'] >= args.min_24h_vol and t.get('symbol') not in EXCLUDE]
        tickers.sort(key=lambda x: x['_vol'], reverse=True)
        candidates = tickers[:args.top]

        print(f"Scanning {len(candidates)} pairs ({args.candles}×{args.interval} candles)")
        print(f"  min_gap={args.min_gap*100:.2f}% min_vol_ratio={args.min_vol_ratio:.1f} "
              f"bounce_r={args.bounce_r:.1f} EMA {args.ema_fast}/{args.ema_slow}")
        print()

        results: List[SymbolScore] = []

        for t in candidates:
            symbol = t['symbol']
            try:
                raw = await client.get_klines(
                    symbol=symbol,
                    interval=args.interval,
                    limit=args.candles,
                )
                candles = parse_candles(raw if isinstance(raw, list) else [])
                if len(candles) < 20:
                    continue

                metrics = analyze_symbol(
                    candles,
                    min_gap=args.min_gap,
                    min_volume_ratio=args.min_vol_ratio,
                    entry_zone_min=args.entry_zone_min,
                    entry_zone_max=args.entry_zone_max,
                    bounce_r=args.bounce_r,
                    ema_fast=args.ema_fast,
                    ema_slow=args.ema_slow,
                )
                if not metrics:
                    continue

                # Inject vol_24h for scoring
                metrics['vol_24h'] = t['_vol']

                s = SymbolScore(
                    symbol=symbol,
                    price=t['_price'],
                    vol_24h=t['_vol'],
                    candle_count=len(candles),
                    fvg_density=metrics['fvg_density'],
                    fill_rate=metrics['fill_rate'],
                    bounce_rate=metrics['bounce_rate'],
                    atr_pct=metrics['atr_pct'],
                    volume_spike=metrics['vol_spike'],
                    trend_clarity=metrics['trend_clarity'],
                    wick_body_ratio=metrics['wick_body'],
                    avg_r_achieved=metrics.get('avg_r_achieved', 0.0),
                    trend_aligned_pct=metrics.get('trend_aligned_pct', 0.0),
                    signal_rate=metrics.get('signal_rate', 0.0),
                    avg_vol_ratio=metrics.get('avg_vol_ratio', 0.0),
                    fvg_count=metrics['fvg_count'],
                    filled_count=metrics['filled'],
                    bounce_count=metrics['bounced'],
                )Fill%':>5} {'Bnce%':>5} {'AvgR':>4} "
              f"{'Trend%':>6} {'Sig/10h':>7} {'VolR':>4} {'ATR%':>5}  Status")
        print("─" * 105)

        for i, s in enumerate(results):
            status = "◀ ACTIVE" if s.symbol in ACTIVE else ""
            print(
                f"{i+1:2d}  {s.symbol:<14} ${s.price:>9,.2f} {s.total_score:>5.1f}  "
                f"{s.fvg_count:>4} {s.fill_rate:>4.0f}% {s.bounce_rate:>4.0f}% {s.avg_r_achieved:>4.1f} "
                f"{s.trend_aligned_pct:>5.0f}% {s.signal_rate:>7.1f} {s.avg_vol_ratio:>4.1f} {s.atr_pct:>5.2f}  "
                f"{status}"
            )

        # Summary
        print()
        print(f"Top 8 non-active candidates (bounce target: {args.bounce_r:.1f}R):")
        non_active = [s for s in results if s.symbol not in ACTIVE]
        for s in non_active[:8]:
            print(f"  {s.symbol:<14} score={s.total_score:.1f}  "
                  f"FVGs={s.fvg_count} fills={s.filled_count} avgR={s.avg_r_achieved:.1f} "
                  f"trend={s.trend_aligned_pct:.0f}% sig/10h={s.signal_rate:.1f}
        for i, s in enumerate(results):
            status = "◀ ACTIVE" if s.symbol in ACTIVE else ""
            print(
                f"{i+1:2d}  {s.symbol:<14} ${s.price:>9,.2f} {s.total_score:>5.1f}  "
                f"{s.fvg_count:>4} {s.fvg_density:>4.1f}% {s.fill_rate:>4.0f}% {s.bounce_rate:>4.0f}% "
                f"{s.atr_pct:>5.2f} {s.volume_spike:>6.1f}x {s.trend_clarity:>5.2f} {s.wick_body_ratio:>4.1f}  "
                f"{s.filled_count:>5}  {status}"
            )

        # Summary
        print()
        print("Top 8 non-active candidates:")
        non_active = [s for s in results if s.symbol not in ACTIVE]
        for s in non_active[:8]:
            print(f"  {s.symbol:<14} score={s.total_score:.1f}  "
                  f"FVGs={s.fvg_count} fills={s.filled_count} fill%={s.fill_rate:.0f}% bounce%={s.bounce_rate:.0f}%")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
