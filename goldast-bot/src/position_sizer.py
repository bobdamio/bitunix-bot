"""
GoldasT Bot v2 - Position Sizer
Risk-based position sizing with constraints
"""

import logging
import random
from dataclasses import dataclass
from typing import Optional

from .models import TradeDirection
from .config import PositionConfig, RandomizationConfig


logger = logging.getLogger(__name__)


@dataclass
class PositionSize:
    """Calculated position size"""
    quantity: float         # In base currency (e.g., BTC)
    quantity_usd: float     # In USD
    leverage: int
    notional_value: float   # quantity * price * leverage
    margin_required: float  # notional_value / leverage
    risk_amount: float      # Amount at risk (USD)
    applied_jitter: float   # Jitter applied (for anti-detection)


class PositionSizer:
    """
    Calculates position size based on risk management rules.
    
    Formula:
        position_usd = (balance × risk_percent) / sl_distance_pct
        
    Constraints applied:
        1. Config max: max_position_usd
        2. Balance cap: balance × max_balance_percent
        3. Config min: min_position_usd
        4. Symbol-specific min quantities
    """
    
    def __init__(
        self,
        config: PositionConfig,
        randomization_config: Optional[RandomizationConfig] = None,
    ):
        self.config = config
        self.randomization = randomization_config
    
    def calculate(
        self,
        balance: float,
        entry_price: float,
        sl_distance_percent: float,
        leverage: int,
        symbol: str,
        risk_override: Optional[float] = None,
    ) -> PositionSize:
        """
        Calculate position size for a trade using SL-based risk sizing.
        
        Formula: position_usd = (balance × risk%) / sl_distance_pct
        This ensures every trade risks the SAME dollar amount regardless of SL width.
        
        Args:
            balance: Account balance in USD
            entry_price: Trade entry price
            sl_distance_percent: Stop loss distance as percentage (e.g., 0.008 = 0.8%)
            leverage: Trade leverage
            symbol: Trading symbol (for min quantity lookup)
            risk_override: If set, use this instead of config.risk_percent (avoids race condition)
            
        Returns:
            PositionSize with calculated values
        """
        # Step 1: Risk-based calculation
        # position_usd = risk_usd / sl_distance, so a hit SL always loses ~risk_usd
        risk_percent = risk_override if risk_override is not None else self.config.risk_percent
        risk_usd = balance * risk_percent
        
        # Guard against extremely tight SL to avoid absurd position sizes
        safe_sl = max(sl_distance_percent, self.config.min_sl_guard_pct)
        position_usd = risk_usd / safe_sl
        
        logger.debug(
            f"Risk-based size: ${position_usd:.2f} "
            f"(risk=${risk_usd:.2f}, sl={safe_sl*100:.2f}%, bal=${balance:.2f}, risk%={risk_percent*100:.1f}%)"
        )
        
        # Step 2: Apply constraints
        original_size = position_usd
        
        # Constraint 1: Config max
        position_usd = min(position_usd, self.config.max_position_usd)
        
        # Constraint 2: Balance cap (HARD MARGIN LIMIT)
        # We cannot open a position larger than (Balance * Leverage)
        # We use 95% of max capacity to leave room for fees/fluctuations
        max_theoretical_position = balance * leverage * self.config.margin_safety_factor
        
        # Also respect the config's 'max_balance_percent' if it acts as a soft limit per trade
        # Interpreting max_balance_percent as "Max % of balance to use as margin for this trade"
        # If max_balance_percent is 20.0 (20%), then max margin = balance * 0.2
        soft_margin_limit = balance * (self.config.max_balance_percent / 100.0)
        max_soft_position = soft_margin_limit * leverage
        
        # Take the tighter of the two
        max_position_cap = min(max_theoretical_position, max_soft_position)
        
        position_usd = min(position_usd, max_position_cap)
        
        # Constraint 3: Config min
        position_usd = max(position_usd, self.config.min_position_usd)
        
        if position_usd != original_size:
            logger.debug(
                f"Position size adjusted: ${original_size:.2f} → ${position_usd:.2f} "
                f"(constraints applied)"
            )
        
        # Step 3: Apply randomization (anti-detection)
        jitter = 0.0
        if self.randomization and self.randomization.enabled:
            jitter_range = position_usd * self.randomization.size_jitter_percent
            jitter = random.uniform(-jitter_range, jitter_range)
            position_usd += jitter
        
        # Step 4: Convert to base currency quantity
        quantity = position_usd / entry_price
        
        # Step 5: Apply symbol-specific minimum quantity
        min_qty = self.config.min_quantities.get(symbol, self.config.default_min_qty)
        if quantity < min_qty:
            logger.warning(
                f"Position quantity {quantity:.6f} below minimum {min_qty} for {symbol}, "
                f"adjusting to minimum"
            )
            quantity = min_qty
            position_usd = quantity * entry_price
        
        # Step 6: Calculate notional and margin
        # position_usd already includes leverage (margin * leverage)
        # So notional = position_usd, margin = position_usd / leverage
        notional_value = position_usd
        margin_required = position_usd / leverage
        
        result = PositionSize(
            quantity=quantity,
            quantity_usd=position_usd,
            leverage=leverage,
            notional_value=notional_value,
            margin_required=margin_required,
            risk_amount=position_usd * sl_distance_percent,
            applied_jitter=jitter,
        )
        
        logger.info(
            f"📏 Position size for {symbol}: "
            f"qty={quantity:.6f} (${position_usd:.2f}) "
            f"leverage={leverage}x notional=${notional_value:.2f}"
        )
        
        return result
    
    def validate_against_balance(
        self,
        position_size: PositionSize,
        available_balance: float,
    ) -> bool:
        """
        Validate that position size doesn't exceed available balance.
        
        Returns:
            True if position is valid, False otherwise
        """
        if position_size.margin_required > available_balance:
            logger.warning(
                f"Position margin ${position_size.margin_required:.2f} exceeds "
                f"available balance ${available_balance:.2f}"
            )
            return False
        return True
    
    def get_min_quantity(self, symbol: str) -> float:
        """Get minimum quantity for a symbol"""
        return self.config.min_quantities.get(symbol, self.config.default_min_qty)
    
    def round_quantity(
        self,
        quantity: float,
        symbol: str,
        step_size: float = None,
    ) -> float:
        """
        Round quantity to valid step size for the symbol.
        
        Args:
            quantity: Raw quantity
            symbol: Trading symbol
            step_size: Minimum quantity increment
            
        Returns:
            Rounded quantity
        """
        if step_size is None:
            step_size = self.config.default_step_size
        # Round down to step size
        rounded = int(quantity / step_size) * step_size
        
        # Ensure minimum
        min_qty = self.get_min_quantity(symbol)
        return max(rounded, min_qty)
