"""
GoldasT Bot v2 - TP/SL Calculator
ATR-based TP/SL with FVG structure anchoring.

SL is placed at the FVG zone invalidation level (zone edge + ATR buffer).
TP is risk-multiple based, floored by ATR, scaled by FVG strength.
Both adapt to real market volatility instead of using fixed multipliers.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .models import Candle, FVG, TradeDirection
from .config import TPSLConfig


logger = logging.getLogger(__name__)


@dataclass
class TPSLLevels:
    """TP/SL price levels"""
    tp_price: float
    sl_price: float
    risk_amount: float
    reward_amount: float
    atr: float
    method: str = "atr_structure"  # for logging

    @property
    def risk_reward_ratio(self) -> float:
        return self.reward_amount / self.risk_amount if self.risk_amount > 0 else 0


class TPSLCalculator:
    """
    Smart TP/SL calculator using FVG structure + ATR.

    SL Logic (for LONG — mirror for SHORT):
      1. Start at FVG zone bottom (structural invalidation).
      2. Subtract noise buffer = ATR × sl_buffer_atr_mult (default 0.2).
      3. Enforce min/max SL distance (sl_min/max_atr_mult × ATR).

    TP Logic (FVG zone-edge based):
      1. PRIMARY: TP at opposite FVG zone edge.
         LONG → TP = fvg.top  (zone top = natural imbalance close target)
         SHORT → TP = fvg.bottom
      2. FLOOR: TP ≥ risk × min_rr (R:R safety — kicks in for narrow zones or deep entries).
      3. FLOOR: TP ≥ ATR × tp_min_atr_mult.
      4. CAP:   TP ≤ max_tp_distance_pct.

    Rationale: the opposite zone edge is where the imbalance was created.
    Price filling back to that level closes the FVG — a high-probability structural target
    vs arbitrary ATR multipliers.
    """

    def __init__(self, config: TPSLConfig):
        self.config = config

    # ------------------------------------------------------------------
    # ATR calculation
    # ------------------------------------------------------------------
    @staticmethod
    def calculate_atr(candles: List[Candle], period: int = 14) -> float:
        """
        Calculate Average True Range from candle data.
        True Range = max(high-low, |high-prev_close|, |low-prev_close|)
        """
        if len(candles) < 2:
            return 0.0

        true_ranges: List[float] = []
        for i in range(1, len(candles)):
            high = candles[i].high
            low = candles[i].low
            prev_close = candles[i - 1].close
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            true_ranges.append(tr)

        # Use last `period` values (or all if fewer)
        recent = true_ranges[-period:]
        return sum(recent) / len(recent) if recent else 0.0

    # ------------------------------------------------------------------
    # Main calculation
    # ------------------------------------------------------------------
    def calculate(
        self,
        entry_price: float,
        fvg: FVG,
        candles: Optional[List[Candle]] = None,
        htf_trend: Optional[str] = None,
        notional_usd: Optional[float] = None,
    ) -> TPSLLevels:
        """
        Calculate TP/SL using ATR + FVG structural levels.

        Args:
            entry_price: Actual fill price
            fvg: The FVG that triggered the trade
            candles: Recent candle buffer for ATR (at least 15 recommended)
        """
        # --- ATR ---
        if candles and len(candles) >= 2:
            atr = self.calculate_atr(candles, period=self.config.atr_period)
        else:
            # Fallback: estimate ATR as configured % of price
            atr = entry_price * self.config.atr_fallback_pct
            logger.warning(
                f"No candles for ATR — using estimate: ATR={atr:.4f} "
                f"({self.config.atr_fallback_pct*100:.2f}% of {entry_price:.2f})"
            )

        # Safety floor: ATR must be at least configured floor % of price
        atr = max(atr, entry_price * self.config.atr_floor_pct)

        # --- SL: zone invalidation + noise buffer ---
        noise_buffer = atr * self.config.sl_buffer_atr_mult
        min_sl_distance = atr * self.config.sl_min_atr_mult
        max_sl_distance = atr * self.config.sl_max_atr_mult

        if fvg.direction == TradeDirection.LONG:
            # SL below FVG bottom (zone edge) with buffer
            structural_sl = fvg.bottom - noise_buffer
            sl_distance = entry_price - structural_sl

            # Enforce minimum distance
            if sl_distance < min_sl_distance:
                sl_distance = min_sl_distance

            # Cap SL distance for tight 5m scalping
            if sl_distance > max_sl_distance:
                logger.info(f"📐 SL capped: {sl_distance/entry_price*100:.3f}% → {max_sl_distance/entry_price*100:.3f}% (max {self.config.sl_max_atr_mult}×ATR)")
                sl_distance = max_sl_distance

            sl_price = entry_price - sl_distance
        else:
            # SL above FVG top (zone edge) with buffer
            structural_sl = fvg.top + noise_buffer
            sl_distance = structural_sl - entry_price

            if sl_distance < min_sl_distance:
                sl_distance = min_sl_distance

            # Cap SL distance for tight 5m scalping
            if sl_distance > max_sl_distance:
                logger.info(f"📐 SL capped: {sl_distance/entry_price*100:.3f}% → {max_sl_distance/entry_price*100:.3f}% (max {self.config.sl_max_atr_mult}×ATR)")
                sl_distance = max_sl_distance

            sl_price = entry_price + sl_distance

        risk = sl_distance

        # --- TP: FVG structure-based target (primary) ---
        # The natural TP is the opposite edge of the FVG zone.
        # For LONG: price entered near fvg.bottom → target is fvg.top (close the imbalance).
        # For SHORT: price entered near fvg.top → target is fvg.bottom.
        # This is where the original imbalance was created — the market naturally fills back to it.
        if fvg.direction == TradeDirection.LONG:
            fvg_tp_distance = fvg.top - entry_price   # Distance to zone top
        else:
            fvg_tp_distance = entry_price - fvg.bottom  # Distance to zone bottom

        # FVG distance may be zero or negative if entry is already past the zone edge.
        # In that case fall back to R:R-based distance.
        if fvg_tp_distance <= 0:
            fvg_tp_distance = 0.0
            logger.info(
                f"📐 FVG TP: entry already past zone edge "
                f"(zone={fvg.bottom:.4f}-{fvg.top:.4f}, entry={entry_price:.4f}) — using R:R floor"
            )

        # R:R floor: TP must be at least min_rr × SL distance
        rr_tp_distance = risk * self.config.min_rr

        # Primary TP = max(FVG opposite edge, min_rr floor)
        # If the FVG zone is wide → zone edge gives good R:R automatically.
        # If entry is deep into zone (fill > 50%) or zone is narrow → R:R floor kicks in.
        tp_distance = max(fvg_tp_distance, rr_tp_distance)

        if fvg_tp_distance >= rr_tp_distance:
            logger.info(
                f"📐 FVG TP: zone edge target {fvg_tp_distance/entry_price*100:.3f}% "
                f"≥ R:R floor {rr_tp_distance/entry_price*100:.3f}% → using zone edge"
            )
        else:
            logger.info(
                f"📐 FVG TP: zone edge {fvg_tp_distance/entry_price*100:.3f}% "
                f"< R:R floor {rr_tp_distance/entry_price*100:.3f}% → extended to {self.config.min_rr}R"
            )

        # ATR floor: TP must be at least 1×ATR
        tp_floor = atr * self.config.tp_min_atr_mult
        if tp_distance < tp_floor:
            tp_distance = tp_floor

        # Commission-aware floor #1: TP must exceed min_tp_distance_pct
        # This ensures TP > round-trip fee (0.24%) + profit buffer
        min_tp_distance_pct = self.config.min_tp_distance_pct / 100.0
        min_tp_distance_fee = entry_price * min_tp_distance_pct
        if tp_distance < min_tp_distance_fee:
            logger.info(
                f"📐 TP raised (fee floor): {tp_distance/entry_price*100:.3f}% → "
                f"{min_tp_distance_pct*100:.3f}% (min {min_tp_distance_pct*100:.2f}% > 0.24% fees)"
            )
            tp_distance = min_tp_distance_fee

        # Commission-aware floor #2: TP profit must cover min_tp_usd
        # TP distance as % of price = min_tp_usd / notional_size
        min_tp_usd = self.config.min_tp_usd
        effective_notional = notional_usd if notional_usd and notional_usd > 0 else self.config.fallback_notional_usd
        min_tp_pct = min_tp_usd / effective_notional
        min_tp_distance = entry_price * min_tp_pct
        if tp_distance < min_tp_distance:
            logger.info(
                f"📐 TP raised for USD floor: {tp_distance/entry_price*100:.3f}% → "
                f"{min_tp_distance/entry_price*100:.3f}% (min ${min_tp_usd} profit)"
            )
            tp_distance = min_tp_distance

        # Cap TP at max_tp_distance_pct (configurable, default 2.5%)
        # Audit fix: set to 0.80% to match real 15m FVG bounce distances
        max_tp_pct = self.config.max_tp_distance_pct / 100.0
        max_tp_distance = entry_price * max_tp_pct
        if tp_distance > max_tp_distance:
            logger.info(
                f"📐 TP capped: {tp_distance/entry_price*100:.2f}% → {max_tp_pct*100:.2f}% "
                f"(${tp_distance:,.2f} → ${max_tp_distance:,.2f})"
            )
            tp_distance = max_tp_distance

        # After TP cap: if R:R < min_rr, shrink SL to restore ratio.
        # SL = TP / min_rr preserves risk management even with tight TP cap.
        required_sl = tp_distance / self.config.min_rr
        if risk > required_sl:
            logger.info(
                f"📐 SL tightened to restore R:R: {risk/entry_price*100:.3f}% → "
                f"{required_sl/entry_price*100:.3f}% (TP={tp_distance/entry_price*100:.3f}% / min_rr={self.config.min_rr})"
            )
            risk = required_sl
            if fvg.direction == TradeDirection.LONG:
                sl_price = entry_price - risk
            else:
                sl_price = entry_price + risk

        if fvg.direction == TradeDirection.LONG:
            tp_price = entry_price + tp_distance
        else:
            tp_price = entry_price - tp_distance

        # Ensure positive
        sl_price = max(sl_price, 0.0001)
        tp_price = max(tp_price, 0.0001)

        levels = TPSLLevels(
            tp_price=tp_price,
            sl_price=sl_price,
            risk_amount=risk,
            reward_amount=tp_distance,
            atr=atr,
            method="fvg_zone_edge",
        )

        logger.info(
            f"📊 TP/SL [{fvg.symbol} {fvg.direction.value}]: "
            f"Entry=${entry_price:,.2f}  SL=${sl_price:,.2f} ({risk/entry_price*100:.3f}%)  "
            f"TP=${tp_price:,.2f} ({tp_distance/entry_price*100:.3f}%)  "
            f"R:R={levels.risk_reward_ratio:.2f}:1  ATR=${atr:,.2f}"
        )

        return levels

    # ------------------------------------------------------------------
    # Fallback for startup sync (no FVG available)
    # ------------------------------------------------------------------
    def calculate_from_atr(
        self,
        entry_price: float,
        direction: TradeDirection,
        candles: Optional[List[Candle]] = None,
    ) -> Tuple[float, float]:
        """
        Calculate TP/SL when no FVG is available (e.g. position synced on startup).
        Uses ATR-based percentages directly.

        Returns:
            (tp_price, sl_price)
        """
        if candles and len(candles) >= 2:
            atr = self.calculate_atr(candles, period=self.config.atr_period)
        else:
            atr = entry_price * self.config.atr_fallback_pct

        atr = max(atr, entry_price * self.config.atr_floor_pct)

        sl_distance = atr * self.config.sl_min_atr_mult
        tp_distance = sl_distance * self.config.min_rr

        # Commission-aware floor (same as main calculate)
        min_tp_usd = self.config.min_tp_usd
        min_tp_distance = entry_price * (min_tp_usd / self.config.fallback_notional_usd)
        if tp_distance < min_tp_distance:
            tp_distance = min_tp_distance
            sl_distance = tp_distance / self.config.min_rr

        if direction == TradeDirection.LONG:
            sl_price = entry_price - sl_distance
            tp_price = entry_price + tp_distance
        else:
            sl_price = entry_price + sl_distance
            tp_price = entry_price - tp_distance

        logger.info(
            f"📊 TP/SL [sync fallback {direction.value}]: "
            f"Entry=${entry_price:,.2f}  SL=${sl_price:,.2f}  TP=${tp_price:,.2f}  "
            f"ATR=${atr:,.2f}"
        )
        return tp_price, sl_price

    # ------------------------------------------------------------------
    # Precision helper
    # ------------------------------------------------------------------
    @staticmethod
    def adjust_for_precision(
        tp_price: float,
        sl_price: float,
        tick_size: float = 0.01,
    ) -> Tuple[float, float]:
        """Round TP/SL to exchange tick size."""
        def round_to_tick(price: float, tick: float) -> float:
            return round(price / tick) * tick

        return (
            round_to_tick(tp_price, tick_size),
            round_to_tick(sl_price, tick_size),
        )
