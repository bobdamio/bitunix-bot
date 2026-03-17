"""
Exness Bot - FVG/IFVG + Supply/Demand Trading Strategy for MT5

A production-ready trading bot for Exness MetaTrader 5.
Strategy: Multi-timeframe FVG/IFVG detection within Supply/Demand zones.
"""

from .models import (
    Candle, FVG, FVGType, TradeDirection, TradeSignal,
    Position, SymbolState, BotState, OrderState,
    SupplyDemandZone, ZoneType,
)


def fmt_price(price: float, symbol: str = "") -> str:
    """Smart price formatting based on magnitude."""
    if price == 0:
        return "0"
    ap = abs(price)
    if ap < 0.001:
        return f"{price:.8f}"
    if ap < 0.1:
        return f"{price:.6f}"
    if ap < 1:
        return f"{price:.4f}"
    if ap < 100:
        return f"{price:.2f}"
    if ap < 10000:
        return f"{price:.2f}"
    return f"{price:,.2f}"


__version__ = "1.0.0"
__all__ = [
    "Candle", "FVG", "FVGType", "TradeDirection", "TradeSignal",
    "Position", "SymbolState", "BotState", "OrderState",
    "SupplyDemandZone", "ZoneType",
    "fmt_price",
]
