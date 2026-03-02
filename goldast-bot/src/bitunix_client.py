"""
GoldasT Bot v2 - Bitunix REST API Client
Clean, self-contained implementation based on official Bitunix API docs.

Signing algorithm (double SHA256):
    digest = SHA256(nonce + timestamp + api_key + query_params + body)
    signature = SHA256(digest + secret_key)

Headers: api-key, sign, nonce, timestamp, Content-Type
"""

import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import aiohttp


logger = logging.getLogger(__name__)


# ==================== Errors ====================

class BitunixAPIError(Exception):
    """Base Bitunix API error"""
    def __init__(self, code: int = 0, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class OrderError(BitunixAPIError):
    """Order placement/modification error"""
    pass


# ==================== Auth ====================

def _sha256(data: str) -> str:
    """SHA256 hex digest"""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _generate_nonce() -> str:
    """Generate 32-char random nonce"""
    return uuid.uuid4().hex


def _generate_timestamp() -> str:
    """Millisecond timestamp as string"""
    return str(int(time.time() * 1000))


def _sort_params(params: Dict[str, Any]) -> str:
    """Sort params and concatenate key+value pairs (Bitunix format)"""
    if not params:
        return ""
    return "".join(f"{k}{v}" for k, v in sorted(params.items()))


def _sign(api_key: str, secret_key: str, query_params: str = "", body: str = "") -> Dict[str, str]:
    """
    Generate Bitunix API signature and auth headers.

    Returns dict with: api-key, sign, nonce, timestamp
    """
    nonce = _generate_nonce()
    timestamp = _generate_timestamp()

    digest_input = nonce + timestamp + api_key + query_params + body
    digest = _sha256(digest_input)
    signature = _sha256(digest + secret_key)

    return {
        "api-key": api_key,
        "sign": signature,
        "nonce": nonce,
        "timestamp": timestamp,
    }


def _ws_sign(api_key: str, secret_key: str) -> Dict[str, Any]:
    """Generate WebSocket login signature"""
    nonce = _generate_nonce()
    timestamp = str(int(time.time()))  # Seconds, not millis for WS

    digest_input = nonce + timestamp + api_key
    digest = _sha256(digest_input)
    signature = _sha256(digest + secret_key)

    return {
        "apiKey": api_key,
        "nonce": nonce,
        "timestamp": int(timestamp),
        "sign": signature,
    }


# ==================== REST Client ====================

class BitunixClient:
    """
    Async Bitunix Futures REST API client.
    
    All methods are async. Uses aiohttp for HTTP.
    """

    BASE_URL = "https://fapi.bitunix.com"

    def __init__(self, api_key: str, api_secret: str, timeout: int = 30):
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- Core request ----------

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Make authenticated API request.
        
        GET/DELETE: sign sorted query params
        POST: sign JSON body
        """
        session = await self._ensure_session()
        url = f"{self.BASE_URL}{endpoint}"
        params = params or {}

        if method.upper() in ("GET", "DELETE"):
            query_str = _sort_params(params)
            headers = _sign(self.api_key, self.api_secret, query_params=query_str)
            async with session.request(method, url, params=params, headers=headers) as resp:
                return await self._handle_response(resp)
        else:  # POST
            body = json.dumps(params)
            headers = _sign(self.api_key, self.api_secret, body=body)
            async with session.post(url, data=body, headers=headers) as resp:
                return await self._handle_response(resp)

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> Dict[str, Any]:
        """Parse response and raise on error"""
        if resp.status != 200:
            text = await resp.text()
            raise BitunixAPIError(resp.status, f"HTTP {resp.status}: {text}")

        result = await resp.json()
        code = result.get("code", -1)
        if code != 0:
            msg = result.get("msg", result.get("message", "Unknown error"))
            raise BitunixAPIError(code, msg)

        return result.get("data", {})

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """Strip _PERP suffix if present"""
        return symbol.replace("_PERP", "")

    # ==================== Account ====================

    async def get_account(self, margin_coin: str = "USDT") -> Dict[str, Any]:
        """Get account info"""
        return await self._request("GET", "/api/v1/futures/account", {"marginCoin": margin_coin})

    async def get_balance(self) -> Dict[str, float]:
        """Get account balance as {total, available, used, margin, cross_upnl, isolation_upnl, equity}"""
        data = await self.get_account()
        available = float(data.get("available", 0))
        frozen = float(data.get("frozen", 0))
        margin = float(data.get("margin", 0))
        cross_upnl = float(data.get("crossUnrealizedPNL", 0))
        isolation_upnl = float(data.get("isolationUnrealizedPNL", 0))
        bonus = float(data.get("bonus", 0))
        total_upnl = cross_upnl + isolation_upnl
        # Wallet balance = available + frozen (orders) + margin (positions)
        wallet_balance = available + frozen + margin
        # Equity = wallet balance + unrealized PnL
        equity = wallet_balance + total_upnl
        return {
            "total": wallet_balance,
            "available": available,
            "used": frozen + margin,
            "margin": margin,
            "frozen": frozen,
            "cross_upnl": cross_upnl,
            "isolation_upnl": isolation_upnl,
            "unrealized_pnl": total_upnl,
            "equity": equity,
            "bonus": bonus,
        }

    async def set_leverage(
        self, symbol: str, leverage: int, margin_coin: str = "USDT"
    ) -> Dict[str, Any]:
        """Set leverage for a symbol"""
        return await self._request("POST", "/api/v1/futures/account/change_leverage", {
            "symbol": self._normalize_symbol(symbol),
            "marginCoin": margin_coin,
            "leverage": str(leverage),
        })

    # ==================== Market Data ====================

    async def get_klines(
        self, symbol: str, interval: str = "5m", limit: int = 100,
        start_time: Optional[int] = None, end_time: Optional[int] = None,
        kline_type: str = "LAST_PRICE",
    ) -> List:
        """Get kline/candlestick data"""
        params: Dict[str, Any] = {
            "symbol": self._normalize_symbol(symbol),
            "interval": interval,
            "limit": limit,
            "type": kline_type,
        }
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return await self._request("GET", "/api/v1/futures/market/kline", params)

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get ticker for a single symbol"""
        data = await self._request("GET", "/api/v1/futures/market/tickers", {
            "symbols": self._normalize_symbol(symbol),
        })
        # tickers endpoint returns a list
        if isinstance(data, list) and data:
            return data[0]
        return data if isinstance(data, dict) else {}

    async def get_depth(self, symbol: str, limit: int = 5) -> Dict[str, Any]:
        """Get order book depth"""
        return await self._request("GET", "/api/v1/futures/market/depth", {
            "symbol": self._normalize_symbol(symbol),
            "limit": limit,
        })

    # ==================== Orders ====================

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "MARKET",
        qty: Optional[str] = None,
        price: Optional[str] = None,
        trade_side: str = "OPEN",
        client_id: Optional[str] = None,
        position_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Place an order.
        
        Args:
            symbol: Trading pair
            side: BUY or SELL
            order_type: MARKET or LIMIT
            qty: Quantity in base currency
            price: Limit price (required for LIMIT)
            trade_side: OPEN or CLOSE
            client_id: Optional client order ID
            position_id: Position ID (required when tradeSide=CLOSE)
        """
        params: Dict[str, Any] = {
            "symbol": self._normalize_symbol(symbol),
            "side": side.upper(),
            "orderType": order_type.upper(),
            "tradeSide": trade_side.upper(),
        }
        if qty:
            params["qty"] = str(qty)
        if price:
            params["price"] = str(price)
        if client_id:
            params["clientId"] = client_id
        if position_id:
            params["positionId"] = str(position_id)

        try:
            return await self._request("POST", "/api/v1/futures/trade/place_order", params)
        except BitunixAPIError as e:
            raise OrderError(e.code, e.message)

    async def place_market_order(
        self, symbol: str, side: str, quantity: float
    ) -> Dict[str, Any]:
        """Place a market order"""
        return await self.place_order(
            symbol=symbol,
            side=side,
            order_type="MARKET",
            qty=str(quantity),
        )

    async def cancel_orders(
        self, symbol: str, order_list: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """Cancel specific orders"""
        return await self._request("POST", "/api/v1/futures/trade/cancel_orders", {
            "symbol": self._normalize_symbol(symbol),
            "orderList": order_list,
        })

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Cancel all open orders"""
        params = {}
        if symbol:
            params["symbol"] = self._normalize_symbol(symbol)
        return await self._request("POST", "/api/v1/futures/trade/cancel_all_orders", params)

    async def get_open_orders(self, symbol: Optional[str] = None) -> List:
        """Get open orders"""
        params = {}
        if symbol:
            params["symbol"] = self._normalize_symbol(symbol)
        return await self._request("GET", "/api/v1/futures/trade/get_open_orders", params)

    async def get_order_detail(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Get single order detail"""
        return await self._request("GET", "/api/v1/futures/trade/get_order_detail", {
            "symbol": self._normalize_symbol(symbol),
            "orderId": order_id,
        })

    async def close_all_positions(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Close all positions"""
        params = {}
        if symbol:
            params["symbol"] = self._normalize_symbol(symbol)
        return await self._request("POST", "/api/v1/futures/trade/close_all_position", params)

    # ==================== Positions ====================

    async def get_positions(self, symbol: Optional[str] = None) -> List:
        """Get open positions"""
        params = {}
        if symbol:
            params["symbol"] = self._normalize_symbol(symbol)
        data = await self._request("GET", "/api/v1/futures/position/get_pending_positions", params)
        if isinstance(data, dict):
            return data.get("positionList", [])
        return data if isinstance(data, list) else []

    async def has_open_position(self, symbol: str) -> bool:
        """Check if symbol has an open position"""
        positions = await self.get_positions(symbol)
        return len(positions) > 0

    async def get_history_positions(self, symbol: Optional[str] = None) -> List:
        """Get closed/historical positions.
        
        Response fields per position:
            positionId, symbol, marginCoin, maxQty, qty, entryPrice, closePrice,
            liqQty, side, marginMode, positionMode, leverage, fee, funding,
            realizedPNL, margin, liqPrice, ctime, mtime
        """
        params = {}
        if symbol:
            params["symbol"] = self._normalize_symbol(symbol)
        data = await self._request("GET", "/api/v1/futures/position/get_history_positions", params)
        if isinstance(data, dict):
            return data.get("positionList", [])
        return data if isinstance(data, list) else []

    # ==================== TP/SL ====================

    async def place_position_tpsl(
        self,
        symbol: str,
        position_id: str,
        tp_price: Optional[str] = None,
        sl_price: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Set TP/SL for a position (exchange-managed)"""
        params: Dict[str, Any] = {
            "symbol": self._normalize_symbol(symbol),
            "positionId": position_id,
        }
        if tp_price:
            params["tpPrice"] = str(tp_price)
            params["tpStopType"] = "LAST_PRICE"
        if sl_price:
            params["slPrice"] = str(sl_price)
            params["slStopType"] = "LAST_PRICE"

        logger.info(f"Placing position TP/SL: {params}")
        result = await self._request("POST", "/api/v1/futures/tpsl/position/place_order", params)
        logger.info(f"Position TP/SL response: {result}")
        return result

    async def modify_position_tpsl(
        self,
        symbol: str,
        position_id: str,
        tp_price: Optional[str] = None,
        sl_price: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Modify existing TP/SL for a position"""
        params: Dict[str, Any] = {
            "symbol": self._normalize_symbol(symbol),
            "positionId": position_id,
        }
        if tp_price:
            params["tpPrice"] = str(tp_price)
            params["tpStopType"] = "LAST_PRICE"
        if sl_price:
            params["slPrice"] = str(sl_price)
            params["slStopType"] = "LAST_PRICE"

        logger.info(f"Modifying position TP/SL: {params}")
        result = await self._request("POST", "/api/v1/futures/tpsl/position/modify", params)
        logger.info(f"Modify TP/SL response: {result}")
        return result

    async def get_pending_tpsl_orders(self, symbol: Optional[str] = None) -> List:
        """Get pending TP/SL orders"""
        params = {}
        if symbol:
            params["symbol"] = self._normalize_symbol(symbol)
        return await self._request("GET", "/api/v1/futures/tpsl/get_pending_orders", params)
