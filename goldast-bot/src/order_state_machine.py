"""
GoldasT Bot v2 - Order State Machine
Manages trade lifecycle: IDLE → PENDING_ENTRY → AWAITING_FILL → PLACING_TPSL → TRACKING
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Callable, Awaitable, Dict, List

from .models import (
    TradeSignal, Position, OrderState, TradeDirection,
    FVG, Candle
)
from .tpsl_calculator import TPSLLevels


logger = logging.getLogger(__name__)


class OrderEvent(Enum):
    """Events that trigger state transitions"""
    SIGNAL_RECEIVED = "signal_received"
    ORDER_PLACED = "order_placed"
    ORDER_FILLED = "order_filled"
    ORDER_REJECTED = "order_rejected"
    ORDER_TIMEOUT = "order_timeout"
    TPSL_SET = "tpsl_set"
    TPSL_FAILED = "tpsl_failed"
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
    POSITION_CLOSED = "position_closed"
    MANUAL_CANCEL = "manual_cancel"
    ERROR = "error"


@dataclass
class OrderContext:
    """Context for a single order through its lifecycle"""
    order_id: str
    symbol: str
    direction: TradeDirection
    signal: TradeSignal
    fvg: FVG
    tpsl: Optional[TPSLLevels] = None
    
    # State tracking
    state: OrderState = OrderState.IDLE
    state_history: List[tuple] = field(default_factory=list)
    
    # Order details
    quantity: float = 0.0
    entry_price: float = 0.0
    leverage: int = 1
    
    # Fill info (from WS)
    fill_price: Optional[float] = None
    fill_time: Optional[datetime] = None
    position_id: Optional[str] = None
    
    # TP/SL tracking
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    
    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    # Error tracking
    error_count: int = 0
    last_error: Optional[str] = None
    
    def transition_to(self, new_state: OrderState, reason: str = "") -> None:
        """Record state transition"""
        old_state = self.state
        self.state = new_state
        self.updated_at = datetime.now()
        self.state_history.append((old_state, new_state, reason, self.updated_at))
        logger.info(f"Order {self.order_id}: {old_state.value} → {new_state.value} ({reason})")


# Type aliases for callbacks
PlaceOrderCallback = Callable[[OrderContext], Awaitable[str]]  # Returns order_id
SetTPSLCallback = Callable[[OrderContext, TPSLLevels], Awaitable[bool]]
CancelOrderCallback = Callable[[str], Awaitable[bool]]


class OrderStateMachine:
    """
    Manages the state machine for a single order.
    
    States:
        IDLE → PENDING_ENTRY → AWAITING_FILL → PLACING_TPSL → TRACKING
        
    Transitions:
        IDLE + SIGNAL_RECEIVED → PENDING_ENTRY
        PENDING_ENTRY + ORDER_PLACED → AWAITING_FILL
        PENDING_ENTRY + ORDER_REJECTED → IDLE
        AWAITING_FILL + ORDER_FILLED → PLACING_TPSL
        AWAITING_FILL + ORDER_TIMEOUT → IDLE
        PLACING_TPSL + TPSL_SET → TRACKING
        PLACING_TPSL + TPSL_FAILED → TRACKING (with retry flag)
        TRACKING + TP_HIT/SL_HIT → IDLE
        TRACKING + POSITION_CLOSED → IDLE
        * + ERROR → IDLE (with error logged)
    """
    
    # Valid state transitions
    TRANSITIONS = {
        OrderState.IDLE: {
            OrderEvent.SIGNAL_RECEIVED: OrderState.PENDING_ENTRY,
        },
        OrderState.PENDING_ENTRY: {
            OrderEvent.ORDER_PLACED: OrderState.AWAITING_FILL,
            OrderEvent.ORDER_REJECTED: OrderState.IDLE,
            OrderEvent.ERROR: OrderState.IDLE,
        },
        OrderState.AWAITING_FILL: {
            OrderEvent.ORDER_FILLED: OrderState.PLACING_TPSL,
            OrderEvent.ORDER_TIMEOUT: OrderState.IDLE,
            OrderEvent.ERROR: OrderState.IDLE,
        },
        OrderState.PLACING_TPSL: {
            OrderEvent.TPSL_SET: OrderState.TRACKING,
            OrderEvent.TPSL_FAILED: OrderState.TRACKING,  # Continue tracking without TP/SL
            OrderEvent.ERROR: OrderState.TRACKING,
        },
        OrderState.TRACKING: {
            OrderEvent.TP_HIT: OrderState.IDLE,
            OrderEvent.SL_HIT: OrderState.IDLE,
            OrderEvent.POSITION_CLOSED: OrderState.IDLE,
            OrderEvent.MANUAL_CANCEL: OrderState.IDLE,
        },
    }
    
    def __init__(
        self,
        context: OrderContext,
        place_order_fn: PlaceOrderCallback,
        set_tpsl_fn: SetTPSLCallback,
        cancel_order_fn: Optional[CancelOrderCallback] = None,
        fill_timeout_seconds: float = 60.0,
        tpsl_retry_count: int = 3,
    ):
        self.ctx = context
        self.place_order = place_order_fn
        self.set_tpsl = set_tpsl_fn
        self.cancel_order = cancel_order_fn
        self.fill_timeout = fill_timeout_seconds
        self.tpsl_retries = tpsl_retry_count
        
        self._fill_event = asyncio.Event()
        self._tpsl_event = asyncio.Event()
        self._close_event = asyncio.Event()
    
    def can_transition(self, event: OrderEvent) -> bool:
        """Check if transition is valid for current state"""
        valid_events = self.TRANSITIONS.get(self.ctx.state, {})
        return event in valid_events
    
    async def handle_event(self, event: OrderEvent, data: dict = None) -> OrderState:
        """
        Process an event and transition to new state.
        
        Args:
            event: The event that occurred
            data: Additional data for the event
            
        Returns:
            New state after transition
        """
        data = data or {}
        
        if not self.can_transition(event):
            logger.warning(
                f"Invalid transition: {self.ctx.state.value} + {event.value} "
                f"(order {self.ctx.order_id})"
            )
            return self.ctx.state
        
        new_state = self.TRANSITIONS[self.ctx.state][event]
        reason = data.get("reason", event.value)
        
        # Execute transition
        self.ctx.transition_to(new_state, reason)
        
        # Handle state-specific logic
        await self._on_enter_state(new_state, data)
        
        return new_state
    
    async def _on_enter_state(self, state: OrderState, data: dict) -> None:
        """Execute logic when entering a state"""
        
        if state == OrderState.PENDING_ENTRY:
            # Place the order
            await self._execute_entry()
            
        elif state == OrderState.AWAITING_FILL:
            # Start fill timeout
            asyncio.create_task(self._wait_for_fill())
            
        elif state == OrderState.PLACING_TPSL:
            # Set TP/SL orders
            self.ctx.fill_price = data.get("fill_price")
            self.ctx.fill_time = data.get("fill_time")
            self.ctx.position_id = data.get("position_id")
            await self._execute_tpsl()
            
        elif state == OrderState.TRACKING:
            # Position is live, track it
            logger.info(
                f"🎯 Order {self.ctx.order_id} now tracking position {self.ctx.position_id}"
            )
            
        elif state == OrderState.IDLE:
            # Order completed or cancelled
            self._fill_event.set()
            self._close_event.set()
    
    async def _execute_entry(self) -> None:
        """Place the entry order"""
        try:
            order_id = await self.place_order(self.ctx)
            self.ctx.order_id = order_id
            logger.info(f"📤 Order placed: {order_id} for {self.ctx.symbol}")
            await self.handle_event(OrderEvent.ORDER_PLACED, {"order_id": order_id})
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            self.ctx.last_error = str(e)
            self.ctx.error_count += 1
            await self.handle_event(OrderEvent.ORDER_REJECTED, {"reason": str(e)})
    
    async def _wait_for_fill(self) -> None:
        """Wait for order fill with timeout"""
        try:
            await asyncio.wait_for(
                self._fill_event.wait(),
                timeout=self.fill_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"Order {self.ctx.order_id} fill timeout")
            await self.handle_event(OrderEvent.ORDER_TIMEOUT)
    
    async def _execute_tpsl(self) -> None:
        """Set TP/SL orders with retries"""
        if not self.ctx.tpsl:
            logger.warning(f"No TP/SL levels for order {self.ctx.order_id}")
            await self.handle_event(OrderEvent.TPSL_FAILED, {"reason": "No levels"})
            return
        
        for attempt in range(self.tpsl_retries):
            try:
                success = await self.set_tpsl(self.ctx, self.ctx.tpsl)
                if success:
                    logger.info(
                        f"✅ TP/SL set for {self.ctx.order_id}: "
                        f"TP={self.ctx.tpsl.tp_price:.4f}, SL={self.ctx.tpsl.sl_price:.4f}"
                    )
                    await self.handle_event(OrderEvent.TPSL_SET)
                    return
            except Exception as e:
                logger.warning(f"TP/SL attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(1.0 * (attempt + 1))
        
        logger.error(f"Failed to set TP/SL after {self.tpsl_retries} attempts")
        self.ctx.last_error = "TP/SL placement failed"
        await self.handle_event(OrderEvent.TPSL_FAILED)
    
    def notify_fill(self, fill_price: float, fill_time: datetime, position_id: str) -> None:
        """Called when order fill is confirmed via WS"""
        self.ctx.fill_price = fill_price
        self.ctx.fill_time = fill_time
        self.ctx.position_id = position_id
        self._fill_event.set()
        
        # Trigger state transition
        asyncio.create_task(
            self.handle_event(
                OrderEvent.ORDER_FILLED,
                {
                    "fill_price": fill_price,
                    "fill_time": fill_time,
                    "position_id": position_id,
                }
            )
        )
    
    def notify_close(self, close_type: str, pnl: float = 0.0) -> None:
        """Called when position is closed (TP/SL hit or manual)"""
        self._close_event.set()
        
        event_map = {
            "tp": OrderEvent.TP_HIT,
            "sl": OrderEvent.SL_HIT,
            "manual": OrderEvent.MANUAL_CANCEL,
            "liquidation": OrderEvent.POSITION_CLOSED,
        }
        event = event_map.get(close_type, OrderEvent.POSITION_CLOSED)
        
        logger.info(f"Position closed ({close_type}): PnL = ${pnl:.2f}")
        asyncio.create_task(
            self.handle_event(event, {"close_type": close_type, "pnl": pnl})
        )
    
    async def wait_until_complete(self) -> OrderState:
        """Wait until order reaches terminal state (IDLE after tracking)"""
        await self._close_event.wait()
        return self.ctx.state


class OrderManager:
    """
    Manages multiple OrderStateMachine instances.
    One per symbol to prevent duplicate positions.
    """
    
    def __init__(
        self,
        place_order_fn: PlaceOrderCallback,
        set_tpsl_fn: SetTPSLCallback,
        cancel_order_fn: Optional[CancelOrderCallback] = None,
        max_concurrent_orders: int = 5,
    ):
        self.place_order = place_order_fn
        self.set_tpsl = set_tpsl_fn
        self.cancel_order = cancel_order_fn
        self.max_concurrent = max_concurrent_orders
        
        self._machines: Dict[str, OrderStateMachine] = {}  # symbol -> machine
        self._order_lookup: Dict[str, str] = {}  # order_id -> symbol
        self._lock = asyncio.Lock()
    
    async def start_order(
        self,
        signal: TradeSignal,
        fvg: FVG,
        tpsl: TPSLLevels,
        quantity: float,
        leverage: int,
    ) -> Optional[OrderStateMachine]:
        """
        Start a new order for a signal.
        
        Returns:
            OrderStateMachine if started, None if blocked
        """
        async with self._lock:
            # Check if symbol already has active order
            if signal.symbol in self._machines:
                existing = self._machines[signal.symbol]
                if existing.ctx.state not in (OrderState.IDLE,):
                    logger.warning(
                        f"Symbol {signal.symbol} already has active order "
                        f"in state {existing.ctx.state.value}"
                    )
                    return None
            
            # Check concurrent limit
            active_count = sum(
                1 for m in self._machines.values()
                if m.ctx.state not in (OrderState.IDLE,)
            )
            if active_count >= self.max_concurrent:
                logger.warning(
                    f"Max concurrent orders ({self.max_concurrent}) reached"
                )
                return None
            
            # Create context
            context = OrderContext(
                order_id=f"temp_{signal.symbol}_{datetime.now().timestamp()}",
                symbol=signal.symbol,
                direction=signal.direction,
                signal=signal,
                fvg=fvg,
                tpsl=tpsl,
                quantity=quantity,
                entry_price=signal.entry_price,
                leverage=leverage,
            )
            
            # Create state machine
            machine = OrderStateMachine(
                context=context,
                place_order_fn=self.place_order,
                set_tpsl_fn=self.set_tpsl,
                cancel_order_fn=self.cancel_order,
            )
            
            self._machines[signal.symbol] = machine
            
            # Start the order
            await machine.handle_event(OrderEvent.SIGNAL_RECEIVED)
            
            return machine
    
    def get_machine(self, symbol: str) -> Optional[OrderStateMachine]:
        """Get the state machine for a symbol"""
        return self._machines.get(symbol)
    
    def get_machine_by_order_id(self, order_id: str) -> Optional[OrderStateMachine]:
        """Get the state machine by order ID"""
        symbol = self._order_lookup.get(order_id)
        if symbol:
            return self._machines.get(symbol)
        return None
    
    def handle_fill(
        self,
        symbol: str,
        order_id: str,
        fill_price: float,
        fill_time: datetime,
        position_id: str,
    ) -> None:
        """Handle fill notification from WS"""
        machine = self._machines.get(symbol)
        if machine and machine.ctx.state == OrderState.AWAITING_FILL:
            machine.notify_fill(fill_price, fill_time, position_id)
            self._order_lookup[order_id] = symbol
    
    def handle_close(
        self,
        symbol: str,
        close_type: str,
        pnl: float = 0.0,
    ) -> None:
        """Handle position close notification from WS"""
        machine = self._machines.get(symbol)
        if machine and machine.ctx.state == OrderState.TRACKING:
            machine.notify_close(close_type, pnl)
    
    def get_active_symbols(self) -> List[str]:
        """Get symbols with active orders"""
        return [
            symbol for symbol, machine in self._machines.items()
            if machine.ctx.state not in (OrderState.IDLE,)
        ]
    
    def get_tracking_symbols(self) -> List[str]:
        """Get symbols currently tracking positions"""
        return [
            symbol for symbol, machine in self._machines.items()
            if machine.ctx.state == OrderState.TRACKING
        ]
