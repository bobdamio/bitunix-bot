"""
Signal Tracker — тracking per-symbol signal activity for pipeline rotation.

Tracks how often each symbol generates live entry signals (price entering
FVG zone), how many are blocked by filters, and how many lead to actual
trades. Used by symbol rotation to replace "dead zone" symbols (no signals
despite being active) with better candidates from the scanner.

Persistence: data/signal_stats.json
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_THROTTLE_SECONDS = 60  # Min seconds between counting consecutive zone hits


@dataclass
class SymbolSignalStats:
    """Signal statistics for one symbol."""
    symbol: str
    # Counters (rolling, reset on activation)
    zone_hits: int = 0          # Price entered FVG zone (entry eligible)
    trades_executed: int = 0    # Actual trades placed
    blocked: int = 0            # In zone but blocked by filter      
    # Timestamps (ISO strings for JSON serialisation)
    activated_at: str = ""      # When symbol was added to active list
    last_zone_hit: str = ""     # Last time price entered zone
    last_trade_at: str = ""     # Last actual trade
    last_throttle: str = ""     # Internal throttle for zone-hit dedup

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SymbolSignalStats":
        return cls(**{k: d.get(k, v) for k, v in asdict(cls(symbol=d.get("symbol",""))).items()})


class SignalTracker:
    """
    Tracks per-symbol signal activity for intelligent rotation.

    Integration points:
    - strategy_engine._check_live_entry: call record_zone_hit() when
      entry conditions are met (price is in zone)
    - strategy_engine._execute_entry: call record_trade() when order placed
    - symbol_rotation: call get_silent_symbols() to find dead symbols
    - symbol_rotation: call activate(symbol) when symbol is added to list
    """

    def __init__(self, data_dir: str = "data"):
        self._data_dir = data_dir
        self._path = os.path.join(data_dir, "signal_stats.json")
        self._stats: Dict[str, SymbolSignalStats] = {}
        self._load()

    # ─────────────────── Recording ───────────────────

    def activate(self, symbol: str) -> None:
        """Call when symbol is added to the active list. Resets counters."""
        stats = self._stats.get(symbol)
        now_str = datetime.now().isoformat()
        if stats:
            # Preserve last_trade_at history; reset only activity counters
            stats.zone_hits = 0
            stats.trades_executed = 0
            stats.blocked = 0
            stats.activated_at = now_str
            stats.last_zone_hit = ""
            stats.last_throttle = ""
        else:
            stats = SymbolSignalStats(symbol=symbol, activated_at=now_str)
            self._stats[symbol] = stats
        self._save()
        logger.debug(f"📡 SignalTracker: {symbol} activated, counters reset")

    def record_zone_hit(self, symbol: str) -> None:
        """
        Call when price enters FVG zone (entry conditions met, before filters).
        Throttled: counts at most once per _THROTTLE_SECONDS per symbol.
        """
        stats = self._get_or_create(symbol)
        now = datetime.now()

        # Throttle: skip if same zone hit was already counted recently
        if stats.last_throttle:
            try:
                last = datetime.fromisoformat(stats.last_throttle)
                if (now - last).total_seconds() < _THROTTLE_SECONDS:
                    return
            except ValueError:
                pass

        stats.zone_hits += 1
        stats.last_zone_hit = now.isoformat()
        stats.last_throttle = now.isoformat()
        self._save()

    def record_trade(self, symbol: str) -> None:
        """Call when an actual order is placed."""
        stats = self._get_or_create(symbol)
        stats.trades_executed += 1
        stats.last_trade_at = datetime.now().isoformat()
        self._save()

    def record_blocked(self, symbol: str) -> None:
        """Call when price is in zone but a filter blocked entry."""
        stats = self._get_or_create(symbol)
        stats.blocked += 1
        self._save()

    # ─────────────────── Queries ───────────────────

    def get_stats(self, symbol: str) -> Optional[SymbolSignalStats]:
        return self._stats.get(symbol)

    def get_all_stats(self) -> Dict[str, SymbolSignalStats]:
        return dict(self._stats)

    def hours_since_activation(self, symbol: str) -> float:
        """Return hours since symbol was activated. 0 if unknown."""
        stats = self._stats.get(symbol)
        if not stats or not stats.activated_at:
            return 0.0
        try:
            activated = datetime.fromisoformat(stats.activated_at)
            return (datetime.now() - activated).total_seconds() / 3600
        except ValueError:
            return 0.0

    def hours_since_last_zone_hit(self, symbol: str) -> float:
        """Hours since last zone hit. Returns large number if never hit."""
        stats = self._stats.get(symbol)
        if not stats or not stats.last_zone_hit:
            return 9999.0
        try:
            last = datetime.fromisoformat(stats.last_zone_hit)
            return (datetime.now() - last).total_seconds() / 3600
        except ValueError:
            return 9999.0

    def get_silent_symbols(
        self,
        active_symbols: List[str],
        min_active_hours: float = 8.0,
        no_signal_hours: float = 8.0,
    ) -> List[str]:
        """
        Return symbols that are "dead zones" — active long enough but
        never generated a signal in the recent window.

        Args:
            active_symbols: Currently active symbol list
            min_active_hours: Symbol must have been active this long
            no_signal_hours: If no zone hit in this window → silent

        Returns:
            List of symbols to replace
        """
        silent = []
        for sym in active_symbols:
            active_hours = self.hours_since_activation(sym)
            if active_hours < min_active_hours:
                # Too new — give it time
                continue

            hit_hours = self.hours_since_last_zone_hit(sym)
            if hit_hours > no_signal_hours:
                stats = self._stats.get(sym)
                total_hits = stats.zone_hits if stats else 0
                logger.debug(
                    f"🔇 {sym}: silent {hit_hours:.1f}h "
                    f"(active {active_hours:.1f}h, total hits={total_hits})"
                )
                silent.append(sym)

        return silent

    def get_summary_lines(self, active_symbols: List[str]) -> List[str]:
        """Return formatted summary lines for logging."""
        lines = []
        for sym in sorted(active_symbols):
            stats = self._stats.get(sym)
            if not stats:
                lines.append(f"  📡 {sym:<14} no stats")
                continue
            active_h = self.hours_since_activation(sym)
            hit_h = self.hours_since_last_zone_hit(sym)
            hit_h_str = f"{hit_h:.1f}h ago" if hit_h < 999 else "never"
            per_h = stats.zone_hits / max(active_h, 1.0)
            lines.append(
                f"  📡 {sym:<14} hits={stats.zone_hits:3d} "
                f"({per_h:.1f}/h) trades={stats.trades_executed} "
                f"blocked={stats.blocked} last_hit={hit_h_str} "
                f"active={active_h:.1f}h"
            )
        return lines

    # ─────────────────── Internal ───────────────────

    def _get_or_create(self, symbol: str) -> SymbolSignalStats:
        if symbol not in self._stats:
            self._stats[symbol] = SymbolSignalStats(
                symbol=symbol,
                activated_at=datetime.now().isoformat(),
            )
        return self._stats[symbol]

    def purge_stale(self, active_symbols: List[str], max_age_hours: float = 72.0) -> int:
        """Remove stats for symbols no longer active and older than max_age_hours.

        Prevents _stats dict from growing indefinitely as symbols rotate.

        Args:
            active_symbols: Currently active symbol list
            max_age_hours: Remove inactive symbols older than this

        Returns:
            Number of symbols purged
        """
        active_set = set(active_symbols)
        now = datetime.now()
        to_remove = []

        for sym, stats in self._stats.items():
            if sym in active_set:
                continue  # still active — keep
            # Check age
            try:
                activated = datetime.fromisoformat(stats.activated_at) if stats.activated_at else datetime.min
                age_h = (now - activated).total_seconds() / 3600
                if age_h > max_age_hours:
                    to_remove.append(sym)
            except (ValueError, TypeError):
                to_remove.append(sym)

        for sym in to_remove:
            del self._stats[sym]

        if to_remove:
            self._save()
            logger.info(
                f"📡 SignalTracker: purged {len(to_remove)} stale symbols "
                f"(>{max_age_hours:.0f}h inactive): {', '.join(sorted(to_remove))}"
            )
        return len(to_remove)

    def _save(self) -> None:
        os.makedirs(self._data_dir, exist_ok=True)
        try:
            data = {sym: s.to_dict() for sym, s in self._stats.items()}
            with open(self._path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"SignalTracker: failed to save: {e}")

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            for sym, d in data.items():
                d["symbol"] = sym
                self._stats[sym] = SymbolSignalStats.from_dict(d)
            logger.info(f"📡 SignalTracker: loaded stats for {len(self._stats)} symbols")
        except Exception as e:
            logger.warning(f"SignalTracker: failed to load {self._path}: {e}")
