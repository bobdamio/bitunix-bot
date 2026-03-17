"""
Exness Bot - Position Sizer
Risk-based lot size calculation for MT5 forex/commodities.
"""

import logging
from typing import Optional

from .models import TradeDirection
from .config import PositionConfig

logger = logging.getLogger(__name__)


class PositionSizer:
    """
    Calculates lot size based on risk management.

    Formula:
        risk_usd = balance × risk_percent
        sl_usd = sl_distance × contract_size × lot
        lot = risk_usd / (sl_distance × contract_size)

    Constraints:
        - min_lot / max_lot from config
        - Symbol-specific lot_step rounding
        - Max positions limit
    """

    def __init__(self, config: PositionConfig):
        self.config = config

    def calculate_lot_size(
        self,
        balance: float,
        entry_price: float,
        sl_price: float,
        symbol_info: dict,
        direction: TradeDirection,
    ) -> float:
        """
        Calculate position lot size based on risk.

        Args:
            balance: Account balance
            entry_price: Entry price
            sl_price: Stop loss price
            symbol_info: MT5 symbol info dict (contract_size, lot_min, lot_step, etc.)
            direction: Trade direction

        Returns:
            Calculated lot size (rounded to lot_step)
        """
        if direction == TradeDirection.LONG:
            sl_distance = entry_price - sl_price
        else:
            sl_distance = sl_price - entry_price

        if sl_distance <= 0:
            logger.error(f"Invalid SL distance: {sl_distance}")
            return self.config.min_lot

        risk_usd = balance * self.config.risk_percent
        contract_size = symbol_info.get("trade_contract_size", 1.0)

        # lot = risk_usd / (sl_distance_in_price × contract_size)
        lot_size = risk_usd / (sl_distance * contract_size)

        # Constraints
        lot_min = symbol_info.get("lot_min", self.config.min_lot)
        lot_max = symbol_info.get("lot_max", self.config.max_lot)
        lot_step = symbol_info.get("lot_step", 0.01)

        lot_size = max(lot_size, lot_min)
        lot_size = min(lot_size, self.config.max_lot)
        lot_size = min(lot_size, lot_max)

        # Round to lot step
        if lot_step > 0:
            lot_size = round(round(lot_size / lot_step) * lot_step, 8)

        logger.debug(
            f"Lot size: {lot_size} | risk=${risk_usd:.2f} | "
            f"SL dist={sl_distance:.5f} | contract={contract_size}"
        )

        return lot_size
