"""
Exness Bot - MT5 Connection and Order Management
Handles all MetaTrader 5 API communication via native MetaTrader5 package.
"""

import logging
import time
from datetime import datetime, timezone
from typing import List, Optional, Dict, Tuple

from .models import Candle, Position, TradeDirection, TradeSignal
from .config import MT5Config, AccountConfig

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
    _HAS_MT5 = True
except ImportError:
    mt5 = None
    _HAS_MT5 = False
    logger.warning("MetaTrader5 package not installed - running in dry-run mode")

# MT5 timeframe constants
_TIMEFRAME_MAP = {}
if _HAS_MT5:
    _TIMEFRAME_MAP = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }
else:
    _TIMEFRAME_MAP = {
        "M1": 1, "M5": 5, "M15": 15,
        "H1": 16385, "H4": 16388, "D1": 16408,
    }


class MT5Client:
    """
    MetaTrader 5 client for Exness broker.
    Uses native MetaTrader5 package (Windows only).
    """

    def __init__(self, config: MT5Config, account_config: AccountConfig):
        self.config = config
        self.account = account_config
        self._connected = False
        self._symbol_info_cache: Dict[str, dict] = {}

    # ==================== Connection ====================

    def connect(self) -> bool:
        """Initialize MT5 connection via native MetaTrader5 package."""
        if not _HAS_MT5:
            logger.error("MetaTrader5 package not available")
            return False

        init_kwargs = {}
        terminal_path = getattr(self.config, 'terminal_path', None)
        if terminal_path:
            init_kwargs['path'] = terminal_path
        if self.config.portable:
            init_kwargs['portable'] = True

        if not mt5.initialize(**init_kwargs):
            err = mt5.last_error()
            logger.error(f"MT5 initialize failed: {err}")
            return False

        authorized = mt5.login(
            login=self.config.login,
            password=self.config.password,
            server=self.config.server,
            timeout=self.config.timeout,
        )

        if not authorized:
            err = mt5.last_error()
            logger.error(f"MT5 login failed: {err}")
            mt5.shutdown()
            return False

        account_info = mt5.account_info()
        if account_info is None:
            logger.error("Failed to get account info")
            mt5.shutdown()
            return False

        self._connected = True
        logger.info(
            f"[OK] MT5 connected: {account_info.server} | "
            f"Account #{account_info.login} | "
            f"Balance: ${account_info.balance:.2f} | "
            f"Leverage: 1:{account_info.leverage} | "
            f"Currency: {account_info.currency}"
        )
        return True

    def disconnect(self) -> None:
        """Shutdown MT5 connection."""
        if self._connected:
            try:
                mt5.shutdown()
            except Exception:
                pass
            self._connected = False
            logger.info("MT5 disconnected")

    def is_connected(self) -> bool:
        """Check if MT5 terminal is connected."""
        try:
            info = mt5.terminal_info()
            return info is not None and info.connected
        except Exception:
            return False

    def reconnect(self) -> bool:
        """Reconnect if connection was lost."""
        if self.is_connected():
            return True
        logger.warning("MT5 connection lost - attempting reconnect...")
        self.disconnect()
        return self.connect()

    # ==================== Account Info ====================

    def get_account_info(self) -> Optional[dict]:
        """Get account balance, equity, margin info."""
        if not self._ensure_connected():
            return None
        info = mt5.account_info()
        if info is None:
            return None
        return {
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "margin_free": info.margin_free,
            "margin_level": info.margin_level,
            "profit": info.profit,
            "leverage": info.leverage,
            "currency": info.currency,
        }

    # ==================== Market Data ====================

    def get_candles(self, symbol: str, timeframe: str, count: int = 100) -> List[Candle]:
        """Get historical candles from MT5."""
        if not self._ensure_connected():
            return []

        tf = _TIMEFRAME_MAP.get(timeframe)
        if tf is None:
            logger.error(f"Unknown timeframe: {timeframe}")
            return []

        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            logger.warning(f"No candle data for {symbol} {timeframe}")
            return []

        candles = [Candle.from_mt5_rate(r) for r in rates]
        return candles

    def get_current_price(self, symbol: str) -> Optional[Tuple[float, float]]:
        """Get current bid/ask price for a symbol. Returns (bid, ask) or None."""
        if not self._ensure_connected():
            return None
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return (tick.bid, tick.ask)

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        """Get symbol specifications (lot size, digits, etc.)."""
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]

        if not self._ensure_connected():
            return None

        info = mt5.symbol_info(symbol)
        if info is None:
            logger.warning(f"Symbol info not available: {symbol}")
            return None

        # Ensure symbol is visible in Market Watch
        if not info.visible:
            if not mt5.symbol_select(symbol, True):
                logger.error(f"Failed to select symbol {symbol}")
                return None
            info = mt5.symbol_info(symbol)

        sym_data = {
            "name": info.name,
            "digits": info.digits,
            "point": info.point,
            "lot_min": info.volume_min,
            "lot_max": info.volume_max,
            "lot_step": info.volume_step,
            "trade_contract_size": info.trade_contract_size,
            "spread": info.spread,
            "swap_long": info.swap_long,
            "swap_short": info.swap_short,
            "margin_initial": info.margin_initial,
        }
        self._symbol_info_cache[symbol] = sym_data
        return sym_data

    # ==================== Order Management ====================

    def place_market_order(
        self,
        symbol: str,
        direction: TradeDirection,
        lot_size: float,
        sl_price: float,
        tp_price: float,
        comment: str = "ExnessBot",
    ) -> Optional[int]:
        """
        Place a market order (buy/sell).
        Returns the ticket number or None on failure.
        """
        if not self._ensure_connected():
            return None

        sym_info = self.get_symbol_info(symbol)
        if sym_info is None:
            return None

        # Validate and round lot size
        lot_size = self._normalize_lot(lot_size, sym_info)
        if lot_size <= 0:
            logger.error(f"Invalid lot size after normalization: {lot_size}")
            return None

        price_info = self.get_current_price(symbol)
        if price_info is None:
            return None
        bid, ask = price_info

        if direction == TradeDirection.LONG:
            order_type = mt5.ORDER_TYPE_BUY
            price = ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = bid

        # Round prices to symbol digits
        digits = sym_info["digits"]
        sl_price = round(sl_price, digits)
        tp_price = round(tp_price, digits)
        price = round(price, digits)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot_size,
            "type": order_type,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "deviation": self.account.deviation,
            "magic": self.account.magic_number,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_type(symbol),
        }

        result = mt5.order_send(request)
        if result is None:
            logger.error(f"Order send returned None: {mt5.last_error()}")
            return None

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(
                f"[FAIL] Order failed: {symbol} {direction.value} {lot_size} lots | "
                f"retcode={result.retcode} comment={result.comment}"
            )
            return None

        logger.info(
            f"[OK] Order filled: {symbol} {direction.value} {lot_size} lots @ {result.price} | "
            f"ticket={result.order} SL={sl_price} TP={tp_price}"
        )
        return result.order

    def place_pending_order(
        self,
        symbol: str,
        direction: TradeDirection,
        order_type: str,
        price: float,
        lot_size: float,
        sl_price: float,
        tp_price: float,
        comment: str = "ExnessBot_Pending",
    ) -> Optional[int]:
        """
        Place a pending order (BUY_STOP / SELL_STOP / BUY_LIMIT / SELL_LIMIT).
        Returns ticket number or None.
        """
        if not self._ensure_connected():
            return None

        sym_info = self.get_symbol_info(symbol)
        if sym_info is None:
            return None

        lot_size = self._normalize_lot(lot_size, sym_info)
        if lot_size <= 0:
            return None

        digits = sym_info["digits"]
        price = round(price, digits)
        sl_price = round(sl_price, digits)
        tp_price = round(tp_price, digits)

        mt5_order_types = {
            "BUY_STOP": mt5.ORDER_TYPE_BUY_STOP,
            "SELL_STOP": mt5.ORDER_TYPE_SELL_STOP,
            "BUY_LIMIT": mt5.ORDER_TYPE_BUY_LIMIT,
            "SELL_LIMIT": mt5.ORDER_TYPE_SELL_LIMIT,
        }

        mt5_type = mt5_order_types.get(order_type)
        if mt5_type is None:
            logger.error(f"Unknown pending order type: {order_type}")
            return None

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": lot_size,
            "type": mt5_type,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "deviation": self.account.deviation,
            "magic": self.account.magic_number,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_type(symbol),
        }

        result = mt5.order_send(request)
        if result is None:
            logger.error(f"Pending order send returned None: {mt5.last_error()}")
            return None

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(
                f"[FAIL] Pending order failed: {symbol} {order_type} {lot_size} @ {price} | "
                f"retcode={result.retcode} comment={result.comment}"
            )
            return None

        logger.info(
            f"Pending order placed: {symbol} {order_type} {lot_size} lots @ {price} | "
            f"ticket={result.order} SL={sl_price} TP={tp_price}"
        )
        return result.order

    def modify_position(
        self,
        ticket: int,
        symbol: str,
        sl_price: float,
        tp_price: float,
    ) -> bool:
        """Modify SL/TP of an existing position."""
        if not self._ensure_connected():
            return False

        sym_info = self.get_symbol_info(symbol)
        digits = sym_info["digits"] if sym_info else 5
        sl_price = round(sl_price, digits)
        tp_price = round(tp_price, digits)

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": ticket,
            "sl": sl_price,
            "tp": tp_price,
            "magic": self.account.magic_number,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error(f"Modify failed ticket={ticket}: {err}")
            return False

        logger.info(f"Modified ticket={ticket}: SL={sl_price} TP={tp_price}")
        return True

    def close_position(self, ticket: int, symbol: str, lot_size: float, direction: TradeDirection) -> bool:
        """Close an open position by ticket."""
        if not self._ensure_connected():
            return False

        price_info = self.get_current_price(symbol)
        if price_info is None:
            return False
        bid, ask = price_info

        # Close is opposite direction
        if direction == TradeDirection.LONG:
            close_type = mt5.ORDER_TYPE_SELL
            price = bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            price = ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot_size,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": self.account.deviation,
            "magic": self.account.magic_number,
            "comment": "ExnessBot_Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_type(symbol),
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error(f"Close failed ticket={ticket}: {err}")
            return False

        logger.info(f"Position closed: ticket={ticket} {symbol} {lot_size} lots @ {result.price}")
        return True

    def cancel_pending_order(self, ticket: int) -> bool:
        """Cancel a pending order by ticket."""
        if not self._ensure_connected():
            return False

        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error(f"Cancel pending failed ticket={ticket}: {err}")
            return False

        logger.info(f"Pending order cancelled: ticket={ticket}")
        return True

    def get_open_positions(self, symbol: Optional[str] = None) -> List[dict]:
        """Get open positions, optionally filtered by symbol."""
        if not self._ensure_connected():
            return []

        if symbol:
            positions = mt5.positions_get(symbol=symbol)
        else:
            positions = mt5.positions_get()

        if positions is None:
            return []

        result = []
        for pos in positions:
            if pos.magic != self.account.magic_number:
                continue  # Only our bot's positions
            result.append({
                "ticket": pos.ticket,
                "symbol": pos.symbol,
                "type": "LONG" if pos.type == mt5.ORDER_TYPE_BUY else "SHORT",
                "volume": pos.volume,
                "price_open": pos.price_open,
                "sl": pos.sl,
                "tp": pos.tp,
                "profit": pos.profit,
                "swap": pos.swap,
                "commission": getattr(pos, 'commission', 0.0),
                "magic": pos.magic,
                "comment": pos.comment,
                "time": pos.time,
            })
        return result

    def get_pending_orders(self, symbol: Optional[str] = None) -> List[dict]:
        """Get pending orders, optionally filtered by symbol."""
        if not self._ensure_connected():
            return []

        if symbol:
            orders = mt5.orders_get(symbol=symbol)
        else:
            orders = mt5.orders_get()

        if orders is None:
            return []

        result = []
        for order in orders:
            if order.magic != self.account.magic_number:
                continue
            order_type_map = {
                mt5.ORDER_TYPE_BUY_STOP: "BUY_STOP",
                mt5.ORDER_TYPE_SELL_STOP: "SELL_STOP",
                mt5.ORDER_TYPE_BUY_LIMIT: "BUY_LIMIT",
                mt5.ORDER_TYPE_SELL_LIMIT: "SELL_LIMIT",
            }
            result.append({
                "ticket": order.ticket,
                "symbol": order.symbol,
                "type": order_type_map.get(order.type, str(order.type)),
                "volume": order.volume_current,
                "price": order.price_open,
                "sl": order.sl,
                "tp": order.tp,
                "magic": order.magic,
                "comment": order.comment,
            })
        return result

    # ==================== Internal Helpers ====================

    def _ensure_connected(self) -> bool:
        """Ensure MT5 is connected, reconnect if needed."""
        if not self._connected or not self.is_connected():
            return self.reconnect()
        return True

    def _normalize_lot(self, lot_size: float, sym_info: dict) -> float:
        """Normalize lot size to symbol constraints."""
        lot_min = sym_info.get("lot_min", 0.01)
        lot_max = sym_info.get("lot_max", 100.0)
        lot_step = sym_info.get("lot_step", 0.01)

        if lot_size < lot_min:
            lot_size = lot_min
        if lot_size > lot_max:
            lot_size = lot_max

        # Round to nearest lot step
        if lot_step > 0:
            lot_size = round(round(lot_size / lot_step) * lot_step, 8)

        return lot_size

    def _get_filling_type(self, symbol: str) -> int:
        """Get the appropriate fill type for a symbol."""
        fill_map = {
            "IOC": mt5.ORDER_FILLING_IOC,
            "FOK": mt5.ORDER_FILLING_FOK,
            "RETURN": mt5.ORDER_FILLING_RETURN,
        }
        return fill_map.get(self.account.fill_type, mt5.ORDER_FILLING_IOC)
