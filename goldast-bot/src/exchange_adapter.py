"""
GoldasT Bot v2 - Exchange Adapter
Clean async interface to Bitunix Futures API.
Uses native async BitunixClient — no thread pool, no legacy imports.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

from .bitunix_client import BitunixClient, BitunixAPIError, OrderError
from .models import TradeDirection, Candle
from .order_state_machine import OrderContext
from .tpsl_calculator import TPSLLevels
from .config import APIConfig


logger = logging.getLogger(__name__)

# Price precision per symbol (decimal places for TP/SL prices)
PRICE_PRECISION = {
    "XAUTUSDT": 1,
    "XAGUSDT": 2,
    "ETHUSDT": 2,
    "UNIUSDT": 3,
    "BTCUSDT": 1,
    "XRPUSDT": 4,
    "AVAXUSDT": 3,
    "LINKUSDT": 3,
    "ICPUSDT": 3,
    "SUIUSDT": 4,
    # Legacy (for existing positions closing)
    "SOLUSDT": 2,
    "HYPEUSDT": 3,
    "PUMPFUNUSDT": 6,
    "TAOUSDT": 2,
    "KASUSDT": 5,
    "1000PEPEUSDT": 7,
    "BNBUSDT": 2,
    "DOGEUSDT": 5,
    "ADAUSDT": 5,
    "AAVEUSDT": 2,
    "TRXUSDT": 5,
    "DOTUSDT": 3,
    "AXSUSDT": 3,
    "LTCUSDT": 2,
    "WLDUSDT": 4,
    "WIFUSDT": 4,
    "BCHUSDT": 2,
}

# Quantity precision per symbol (decimal places for order qty)
QTY_PRECISION = {
    "XAUTUSDT": 3,
    "XAGUSDT": 3,
    "ETHUSDT": 3,
    "UNIUSDT": 0,
    "BTCUSDT": 4,
    "XRPUSDT": 1,
    "AVAXUSDT": 0,
    "LINKUSDT": 2,
    "ICPUSDT": 0,
    "SUIUSDT": 1,
    # Legacy (for existing positions closing)
    "SOLUSDT": 2,
    "HYPEUSDT": 2,
    "PUMPFUNUSDT": 0,
    "TAOUSDT": 3,
    "KASUSDT": 0,
    "1000PEPEUSDT": 0,
    "BNBUSDT": 3,
    "DOGEUSDT": 0,
    "ADAUSDT": 0,
    "AAVEUSDT": 2,
    "TRXUSDT": 0,
    "DOTUSDT": 1,
    "AXSUSDT": 0,
    "LTCUSDT": 3,
    "WLDUSDT": 0,
    "WIFUSDT": 1,
    "BCHUSDT": 3,
}


def round_price(symbol: str, price: float) -> float:
    """Round price to exchange-required precision."""
    decimals = PRICE_PRECISION.get(symbol, 2)
    return round(price, decimals)


def round_qty(symbol: str, qty: float) -> float:
    """Round quantity to exchange-required precision."""
    decimals = QTY_PRECISION.get(symbol, 4)
    return round(qty, decimals)


@dataclass
class OrderResult:
    """Result of order placement"""
    success: bool
    order_id: Optional[str] = None
    error: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


@dataclass
class AccountBalance:
    """Account balance information"""
    total: float          # Wallet balance (available + frozen + margin)
    available: float      # Free for new trades
    used: float           # Frozen (orders) + margin (positions)
    equity: float = 0.0   # Wallet balance + unrealized PnL
    margin: float = 0.0   # Locked in positions
    unrealized_pnl: float = 0.0  # Total unrealized PnL
    margin_coin: str = "USDT"


class ExchangeAdapter:
    """
    Async wrapper providing clean trading interface.
    All methods are natively async via aiohttp.
    """

    def __init__(self, config: APIConfig, sl_correction_pct: float = 0.003):
        self.config = config
        self.sl_correction_pct = sl_correction_pct
        self._api = BitunixClient(
            api_key=config.key,
            api_secret=config.secret,
            timeout=config.timeout,
        )
        logger.info("Exchange adapter initialized (mainnet)")

    # ==================== Account ====================

    async def get_balance(self) -> Optional[AccountBalance]:
        """Get account balance with retry on transient errors."""
        last_err = None
        for attempt in range(3):
            try:
                result = await self._api.get_balance()
                return AccountBalance(
                    total=result.get("total", 0.0),
                    available=result.get("available", 0.0),
                    used=result.get("used", 0.0),
                    equity=result.get("equity", 0.0),
                    margin=result.get("margin", 0.0),
                    unrealized_pnl=result.get("unrealized_pnl", 0.0),
                )
            except Exception as e:
                last_err = e
                logger.warning(f"get_balance attempt {attempt+1}/3 failed: {e}")
                if attempt < 2:
                    await asyncio.sleep(1.0)
        logger.error(f"Failed to get balance after 3 attempts: {last_err}")
        return None

    async def has_sufficient_balance(self, required: float) -> bool:
        """Check if account has sufficient balance"""
        balance = await self.get_balance()
        if balance is None:
            return False
        return balance.available >= required

    # ==================== Leverage ====================

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for symbol"""
        try:
            await self._api.set_leverage(symbol=symbol, leverage=leverage)
            logger.info(f"Leverage set to {leverage}x for {symbol}")
            return True
        except Exception as e:
            logger.error(f"Failed to set leverage for {symbol}: {e}")
            return False

    # ==================== Order Placement ====================

    async def place_market_order(
        self,
        symbol: str,
        direction: TradeDirection,
        quantity: float,
        leverage: int,
    ) -> OrderResult:
        """Place a market order with leverage"""
        # Set leverage first
        if not await self.set_leverage(symbol, leverage):
            return OrderResult(success=False, error="Failed to set leverage")

        side = "BUY" if direction == TradeDirection.LONG else "SELL"

        try:
            result = await self._api.place_market_order(
                symbol=symbol, side=side, quantity=quantity,
            )
            order_id = str(result.get("orderId", result))
            logger.info(f"Market order placed: {side} {quantity} {symbol} (id={order_id})")
            return OrderResult(success=True, order_id=order_id, data=result)
        except OrderError as e:
            logger.warning(f"Order rejected: {e}")
            return OrderResult(success=False, error=str(e))
        except Exception as e:
            logger.error(f"Order failed: {e}")
            return OrderResult(success=False, error=str(e))

    async def place_limit_order(
        self,
        symbol: str,
        direction: TradeDirection,
        quantity: float,
        leverage: int,
        price: float,
    ) -> OrderResult:
        """Place a limit order (maker fee) with leverage"""
        if not await self.set_leverage(symbol, leverage):
            return OrderResult(success=False, error="Failed to set leverage")

        side = "BUY" if direction == TradeDirection.LONG else "SELL"
        rounded_price = round_price(symbol, price)

        try:
            result = await self._api.place_order(
                symbol=symbol, side=side, order_type="LIMIT",
                qty=str(quantity), price=str(rounded_price),
            )
            order_id = str(result.get("orderId", result))
            logger.info(f"Limit order placed: {side} {quantity} {symbol} @{rounded_price} (id={order_id})")
            return OrderResult(success=True, order_id=order_id, data=result)
        except OrderError as e:
            logger.warning(f"Limit order rejected: {e}")
            return OrderResult(success=False, error=str(e))
        except Exception as e:
            logger.error(f"Limit order failed: {e}")
            return OrderResult(success=False, error=str(e))

    async def place_order_from_context(self, ctx: OrderContext) -> str:
        """Place order: try limit first (maker fee), fallback to market."""
        # Use aggressive limit price: slightly past current price to ensure fill
        # BUY: limit at entry_price (or slightly above) — still gets maker fee
        # SELL: limit at entry_price (or slightly below)
        if ctx.entry_price and ctx.entry_price > 0:
            # Aggressive limit: use entry price from signal
            # Add tiny buffer to ensure fill (0.02% past price)
            buffer = 0.0002
            if ctx.direction == TradeDirection.LONG:
                limit_price = ctx.entry_price * (1 + buffer)
            else:
                limit_price = ctx.entry_price * (1 - buffer)

            result = await self.place_limit_order(
                symbol=ctx.symbol,
                direction=ctx.direction,
                quantity=ctx.quantity,
                leverage=ctx.leverage,
                price=limit_price,
            )
            if result.success:
                return result.order_id
            # Limit failed — fallback to market
            logger.warning(f"Limit order failed for {ctx.symbol}, falling back to market: {result.error}")

        result = await self.place_market_order(
            symbol=ctx.symbol,
            direction=ctx.direction,
            quantity=ctx.quantity,
            leverage=ctx.leverage,
        )
        if not result.success:
            raise OrderError(0, result.error or "Order failed")
        return result.order_id

    # ==================== TP/SL ====================

    async def set_position_tpsl(
        self,
        symbol: str,
        position_id: str,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        direction: Optional[str] = None,
        current_price: Optional[float] = None,
    ) -> bool:
        """Set TP/SL for a position, with price validation and precision rounding."""
        # Round to exchange precision
        if tp_price:
            tp_price = round_price(symbol, tp_price)
        if sl_price:
            sl_price = round_price(symbol, sl_price)

        # Validate SL is on the correct side of current price
        if sl_price and current_price and direction:
            if direction == "SHORT" and sl_price <= current_price:
                adjusted_sl = round_price(symbol, current_price * (1 + self.sl_correction_pct))
                logger.warning(
                    f"⚠️ SL validation: {symbol} SHORT SL=${sl_price:,.2f} <= "
                    f"price=${current_price:,.2f} → adjusted to ${adjusted_sl:,.2f}"
                )
                sl_price = adjusted_sl
            elif direction == "LONG" and sl_price >= current_price:
                adjusted_sl = round_price(symbol, current_price * (1 - self.sl_correction_pct))
                logger.warning(
                    f"⚠️ SL validation: {symbol} LONG SL=${sl_price:,.2f} >= "
                    f"price=${current_price:,.2f} → adjusted to ${adjusted_sl:,.2f}"
                )
                sl_price = adjusted_sl

        try:
            await self._api.place_position_tpsl(
                symbol=symbol,
                position_id=position_id,
                tp_price=str(tp_price) if tp_price else None,
                sl_price=str(sl_price) if sl_price else None,
            )
            logger.info(f"TP/SL set for {symbol} pos={position_id}: TP={tp_price}, SL={sl_price}")
            return True
        except Exception as e:
            logger.error(f"Failed to set TP/SL: {e}")
            return False

    async def set_tpsl_from_context(self, ctx: OrderContext, levels: TPSLLevels) -> bool:
        """Set TP/SL from OrderContext and TPSLLevels"""
        if not ctx.position_id:
            logger.error("Cannot set TP/SL: no position_id")
            return False
        return await self.set_position_tpsl(
            symbol=ctx.symbol,
            position_id=ctx.position_id,
            tp_price=levels.tp_price,
            sl_price=levels.sl_price,
        )

    async def modify_position_tpsl(
        self,
        symbol: str,
        position_id: str,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        current_price: Optional[float] = None,
        direction: Optional[str] = None,
    ) -> bool:
        """Modify existing TP/SL. Falls back to set_position_tpsl if modify fails."""
        # Round to exchange precision
        if tp_price:
            tp_price = round_price(symbol, tp_price)
        if sl_price:
            sl_price = round_price(symbol, sl_price)

        # Validate SL is on the correct side of current price (same as set_position_tpsl)
        if sl_price and current_price and direction:
            if direction == "SHORT" and sl_price <= current_price:
                adjusted_sl = round_price(symbol, current_price * (1 + self.sl_correction_pct))
                logger.warning(
                    f"⚠️ Trailing SL validation: {symbol} SHORT SL=${sl_price:,.4f} <= "
                    f"price=${current_price:,.4f} → adjusted to ${adjusted_sl:,.4f}"
                )
                sl_price = adjusted_sl
            elif direction == "LONG" and sl_price >= current_price:
                adjusted_sl = round_price(symbol, current_price * (1 - self.sl_correction_pct))
                logger.warning(
                    f"⚠️ Trailing SL validation: {symbol} LONG SL=${sl_price:,.4f} >= "
                    f"price=${current_price:,.4f} → adjusted to ${adjusted_sl:,.4f}"
                )
                sl_price = adjusted_sl

        try:
            await self._api.modify_position_tpsl(
                symbol=symbol,
                position_id=position_id,
                tp_price=str(tp_price) if tp_price else None,
                sl_price=str(sl_price) if sl_price else None,
            )
            return True
        except Exception as e:
            logger.warning(f"Modify TP/SL failed ({e}), falling back to set…")
            # Fallback: try setting TP/SL fresh (initial set may have failed)
            try:
                await self._api.place_position_tpsl(
                    symbol=symbol,
                    position_id=position_id,
                    tp_price=str(tp_price) if tp_price else None,
                    sl_price=str(sl_price) if sl_price else None,
                )
                logger.info(f"TP/SL set (fallback) for {symbol}: TP={tp_price}, SL={sl_price}")
                return True
            except Exception as e2:
                logger.error(f"Failed to set TP/SL (fallback): {e2}")
                return False

    # ==================== Position Management ====================

    async def get_positions(self, symbol: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """Get open positions. Returns None on network error (vs [] = no positions)."""
        try:
            return await self._api.get_positions(symbol=symbol)
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return None

    async def has_open_position(self, symbol: str) -> bool:
        """Check if symbol has an open position"""
        try:
            return await self._api.has_open_position(symbol=symbol)
        except Exception as e:
            logger.error(f"Failed to check position: {e}")
            return False

    async def get_history_positions(self, symbol: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """Get closed/historical positions. Returns None on network error."""
        try:
            return await self._api.get_history_positions(symbol=symbol)
        except Exception as e:
            logger.error(f"Failed to get history positions: {e}")
            return None

    async def close_position(
        self, symbol: str, direction: TradeDirection, quantity: float,
        position_id: Optional[str] = None,
    ) -> OrderResult:
        """Close (or partially close) a position with correct qty precision."""
        close_side = "SELL" if direction == TradeDirection.LONG else "BUY"
        quantity = round_qty(symbol, quantity)
        try:
            result = await self._api.place_order(
                symbol=symbol, side=close_side, qty=str(quantity),
                trade_side="CLOSE", position_id=position_id,
            )
            return OrderResult(success=True, order_id=str(result.get("orderId")), data=result)
        except Exception as e:
            logger.warning(f"Close via place_order failed ({e}), trying close_all_positions...")
            # Fallback: use close_all_positions endpoint (closes 100%)
            try:
                await self._api.close_all_positions(symbol=symbol)
                logger.info(f"Force-closed all positions for {symbol}")
                return OrderResult(success=True, order_id="force_closed")
            except Exception as e2:
                logger.error(f"Force close also failed: {e2}")
                return OrderResult(success=False, error=str(e2))

    async def force_close_symbol(self, symbol: str) -> bool:
        """Force close all positions for a symbol using close_all_positions endpoint."""
        try:
            await self._api.close_all_positions(symbol=symbol)
            logger.info(f"Force-closed all positions for {symbol}")
            return True
        except Exception as e:
            logger.error(f"Force close failed for {symbol}: {e}")
            return False

    # ==================== Orders ====================

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open orders"""
        try:
            return await self._api.get_open_orders(symbol=symbol)
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order"""
        try:
            await self._api.cancel_orders(symbol=symbol, order_list=[{"orderId": order_id}])
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        """Cancel all open orders"""
        try:
            await self._api.cancel_all_orders(symbol=symbol)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return False

    # ==================== Market Data ====================

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current last price"""
        try:
            ticker = await self._api.get_ticker(symbol)
            return float(ticker.get("last", ticker.get("lastPx", 0)))
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            return None

    async def get_historical_candles(self, symbol: str, limit: int = 100, interval: str = "15m") -> List[Candle]:
        """Fetch historical klines as Candle objects"""
        try:
            data = await self._api.get_klines(symbol=symbol, interval=interval, limit=limit)
            candles = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        # Bitunix format: {time, open, high, low, close, quoteVol, baseVol}
                        # Use baseVol (USDT value) to match WS candles which also use baseVol
                        candles.append(Candle(
                            timestamp=int(item.get("time", item.get("ts", item.get("t", 0)))),
                            open=float(item.get("open", item.get("o", 0))),
                            high=float(item.get("high", item.get("h", 0))),
                            low=float(item.get("low", item.get("l", 0))),
                            close=float(item.get("close", item.get("c", 0))),
                            volume=float(item.get("baseVol", item.get("quoteVol", item.get("vol", item.get("v", 0))))),
                        ))
                    elif isinstance(item, list) and len(item) >= 5:
                        candles.append(Candle.from_api_data(item))
            # API returns newest-first; reverse to chronological order
            candles.sort(key=lambda c: c.timestamp)
            return candles
        except Exception as e:
            logger.error(f"Failed to get candles for {symbol}: {e}")
            return []

    async def get_depth(self, symbol: str, limit: int = 5) -> Optional[Dict[str, Any]]:
        """Get order book depth"""
        try:
            return await self._api.get_depth(symbol=symbol, limit=limit)
        except Exception as e:
            logger.error(f"Failed to get depth for {symbol}: {e}")
            return None

    # ==================== Lifecycle ====================

    async def close(self) -> None:
        """Cleanup resources"""
        await self._api.close()
        logger.info("Exchange adapter closed")
