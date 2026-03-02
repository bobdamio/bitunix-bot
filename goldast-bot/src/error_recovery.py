"""
GoldasT Bot v2 - Error Recovery
Circuit breaker and retry patterns for resilient operations
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, TypeVar, Optional, List, Any
from functools import wraps


logger = logging.getLogger(__name__)


T = TypeVar("T")


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreakerOpen(Exception):
    """Raised when circuit breaker is open"""
    pass


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker"""
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 60.0
    half_open_max_calls: int = 3


class CircuitBreaker:
    """
    Async circuit breaker for fault tolerance.
    
    States:
        CLOSED: Normal operation, counts failures
        OPEN: Rejecting all calls, waiting for recovery
        HALF_OPEN: Testing with limited calls
    """
    
    def __init__(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None,
    ):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.half_open_calls = 0
        
        self.last_failure_time: Optional[datetime] = None
        self.last_state_change: datetime = datetime.now()
    
    @property
    def is_closed(self) -> bool:
        return self.state == CircuitState.CLOSED
    
    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN
    
    async def execute(
        self,
        func: Callable[..., T],
        *args,
        **kwargs,
    ) -> T:
        """
        Execute function with circuit breaker protection.
        
        Raises:
            CircuitBreakerOpen: If circuit is open
        """
        if not await self._can_execute():
            raise CircuitBreakerOpen(f"Circuit '{self.name}' is OPEN")
        
        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            
            self._on_success()
            return result
            
        except Exception as e:
            self._on_failure(e)
            raise
    
    async def _can_execute(self) -> bool:
        """Check if execution is allowed"""
        if self.state == CircuitState.CLOSED:
            return True
        
        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if self._should_attempt_reset():
                self._transition_to(CircuitState.HALF_OPEN)
                return True
            return False
        
        if self.state == CircuitState.HALF_OPEN:
            # Allow limited calls in half-open state
            if self.half_open_calls < self.config.half_open_max_calls:
                self.half_open_calls += 1
                return True
            return False
        
        return False
    
    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to try recovery"""
        if not self.last_failure_time:
            return True
        
        elapsed = (datetime.now() - self.last_failure_time).total_seconds()
        return elapsed >= self.config.recovery_timeout_seconds
    
    def _on_success(self) -> None:
        """Handle successful execution"""
        self.success_count += 1
        
        if self.state == CircuitState.HALF_OPEN:
            # Recovery successful, close circuit
            self._transition_to(CircuitState.CLOSED)
            self.failure_count = 0
        elif self.state == CircuitState.CLOSED:
            # Reset failure count on success
            self.failure_count = 0
    
    def _on_failure(self, error: Exception) -> None:
        """Handle failed execution"""
        self.failure_count += 1
        self.last_failure_time = datetime.now()
        
        logger.warning(
            f"Circuit '{self.name}' failure #{self.failure_count}: {error}"
        )
        
        if self.state == CircuitState.HALF_OPEN:
            # Recovery failed, reopen circuit
            self._transition_to(CircuitState.OPEN)
            
        elif self.state == CircuitState.CLOSED:
            # Check if threshold reached
            if self.failure_count >= self.config.failure_threshold:
                self._transition_to(CircuitState.OPEN)
    
    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to new state"""
        old_state = self.state
        self.state = new_state
        self.last_state_change = datetime.now()
        self.half_open_calls = 0
        
        logger.info(
            f"Circuit '{self.name}': {old_state.value} → {new_state.value}"
        )
    
    def reset(self) -> None:
        """Manually reset circuit to closed state"""
        self._transition_to(CircuitState.CLOSED)
        self.failure_count = 0
        self.success_count = 0


@dataclass
class RetryConfig:
    """Configuration for retry logic"""
    max_retries: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True


class RetryHandler:
    """
    Async retry handler with exponential backoff.
    """
    
    def __init__(self, config: Optional[RetryConfig] = None):
        self.config = config or RetryConfig()
    
    async def execute(
        self,
        func: Callable[..., T],
        *args,
        retryable_exceptions: tuple = (Exception,),
        **kwargs,
    ) -> T:
        """
        Execute function with retry logic.
        
        Args:
            func: Function to execute
            retryable_exceptions: Exceptions that trigger retry
            
        Returns:
            Function result
            
        Raises:
            Last exception if all retries fail
        """
        last_exception: Optional[Exception] = None
        
        for attempt in range(self.config.max_retries + 1):
            try:
                if asyncio.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                else:
                    return func(*args, **kwargs)
                    
            except retryable_exceptions as e:
                last_exception = e
                
                if attempt >= self.config.max_retries:
                    logger.error(
                        f"All {self.config.max_retries} retries failed: {e}"
                    )
                    raise
                
                delay = self._calculate_delay(attempt)
                logger.warning(
                    f"Retry {attempt + 1}/{self.config.max_retries} "
                    f"after {delay:.1f}s: {e}"
                )
                await asyncio.sleep(delay)
        
        raise last_exception
    
    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay with exponential backoff"""
        import random
        
        delay = self.config.base_delay_seconds * (
            self.config.exponential_base ** attempt
        )
        delay = min(delay, self.config.max_delay_seconds)
        
        if self.config.jitter:
            delay *= (0.5 + random.random())
        
        return delay


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    retryable_exceptions: tuple = (Exception,),
):
    """
    Decorator for adding retry logic to async functions.
    
    Usage:
        @with_retry(max_retries=3)
        async def my_function():
            ...
    """
    def decorator(func: Callable) -> Callable:
        handler = RetryHandler(RetryConfig(
            max_retries=max_retries,
            base_delay_seconds=base_delay,
        ))
        
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await handler.execute(
                func, *args,
                retryable_exceptions=retryable_exceptions,
                **kwargs
            )
        
        return wrapper
    
    return decorator


class ErrorTracker:
    """
    Tracks errors for monitoring and alerting.
    """
    
    def __init__(self, max_history: int = 100):
        self.max_history = max_history
        self._errors: List[dict] = []
        self._error_counts: dict = {}
    
    def record(
        self,
        error: Exception,
        context: Optional[str] = None,
    ) -> None:
        """Record an error"""
        error_type = type(error).__name__
        
        entry = {
            "timestamp": datetime.now(),
            "type": error_type,
            "message": str(error),
            "context": context,
        }
        
        self._errors.append(entry)
        self._error_counts[error_type] = self._error_counts.get(error_type, 0) + 1
        
        # Trim history
        if len(self._errors) > self.max_history:
            self._errors = self._errors[-self.max_history:]
    
    def get_recent(self, count: int = 10) -> List[dict]:
        """Get recent errors"""
        return self._errors[-count:]
    
    def get_counts(self) -> dict:
        """Get error counts by type"""
        return self._error_counts.copy()
    
    def get_count_since(self, since: datetime) -> int:
        """Get error count since timestamp"""
        return sum(
            1 for e in self._errors
            if e["timestamp"] >= since
        )
    
    def clear(self) -> None:
        """Clear error history"""
        self._errors.clear()
        self._error_counts.clear()


class ResilientExecutor:
    """
    Combines circuit breaker and retry for resilient execution.
    """
    
    def __init__(
        self,
        name: str,
        circuit_config: Optional[CircuitBreakerConfig] = None,
        retry_config: Optional[RetryConfig] = None,
    ):
        self.name = name
        self.circuit = CircuitBreaker(name, circuit_config)
        self.retry = RetryHandler(retry_config)
        self.tracker = ErrorTracker()
    
    async def execute(
        self,
        func: Callable[..., T],
        *args,
        retryable_exceptions: tuple = (Exception,),
        bypass_circuit: bool = False,
        **kwargs,
    ) -> T:
        """
        Execute function with circuit breaker and retry.
        
        Args:
            func: Function to execute
            retryable_exceptions: Exceptions that trigger retry
            bypass_circuit: Skip circuit breaker check
        """
        try:
            if bypass_circuit:
                return await self.retry.execute(
                    func, *args,
                    retryable_exceptions=retryable_exceptions,
                    **kwargs
                )
            
            return await self.circuit.execute(
                self.retry.execute,
                func, *args,
                retryable_exceptions=retryable_exceptions,
                **kwargs
            )
            
        except Exception as e:
            self.tracker.record(e, context=self.name)
            raise
    
    def get_status(self) -> dict:
        """Get executor status"""
        return {
            "name": self.name,
            "circuit_state": self.circuit.state.value,
            "failure_count": self.circuit.failure_count,
            "success_count": self.circuit.success_count,
            "recent_errors": len(self.tracker.get_recent()),
        }
