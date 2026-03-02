"""
GoldasT Bot v2 - FVG/IFVG Trading Strategy

A clean, production-ready trading bot for Bitunix Futures.
"""

from .models import (
    Candle, FVG, FVGType, TradeSignal, TradeDirection,
    Position, SymbolState, BotState, OrderState
)


def fmt_price(price: float) -> str:
    """Smart price formatting: auto-select decimals based on magnitude.

    Cheap tokens like 1000PEPE (~0.004) need more decimals than BTC (~68000).
    """
    if price == 0:
        return "0"
    ap = abs(price)
    if ap < 0.001:
        return f"{price:.8f}"
    if ap < 0.1:
        return f"{price:.6f}"
    if ap < 1:
        return f"{price:.4f}"
    if ap < 1000:
        return f"{price:.2f}"
    return f"{price:,.2f}"
from .config import load_config, Config
from .fvg_detector import FVGDetector
from .tpsl_calculator import TPSLCalculator, TPSLLevels
from .position_sizer import PositionSizer, PositionSize
from .order_state_machine import (
    OrderManager, OrderStateMachine, OrderContext, OrderEvent
)
from .exchange_adapter import ExchangeAdapter, OrderResult, AccountBalance
from .websocket_handler import WebSocketHandler
from .error_recovery import (
    CircuitBreaker, RetryHandler, ResilientExecutor, with_retry
)
from .strategy_engine import StrategyEngine
from .position_manager import PositionManager
from .bot import GoldastBot


__version__ = "2.0.0"
__all__ = [
    # Models
    "Candle", "FVG", "FVGType", "TradeSignal", "TradeDirection",
    "Position", "SymbolState", "BotState", "OrderState",
    
    # Config
    "load_config", "Config",
    
    # Strategy
    "FVGDetector", "TPSLCalculator", "TPSLLevels",
    "PositionSizer", "PositionSize",
    "StrategyEngine",
    
    # Position Management
    "PositionManager",
    
    # Order Management
    "OrderManager", "OrderStateMachine", "OrderContext", "OrderEvent",
    
    # Exchange
    "ExchangeAdapter", "OrderResult", "AccountBalance",
    
    # WebSocket
    "WebSocketHandler",
    
    # Error Recovery
    "CircuitBreaker", "RetryHandler", "ResilientExecutor", "with_retry",
    
    # Bot
    "GoldastBot",
]
