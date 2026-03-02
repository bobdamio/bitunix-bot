"""
GoldasT Bot v2 - Trade History
Persists closed trades fetched from the exchange to JSON + CSV.
All data (PnL, fees, prices) comes directly from Bitunix — no local calculation.
Files stored in data/ (Docker volume).
"""

import csv
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Set

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))

CSV_FIELDS = [
    "positionId", "symbol", "side", "leverage",
    "entryPrice", "closePrice", "qty", "maxQty",
    "realizedPNL", "fee", "funding", "margin",
    "marginMode", "ctime", "mtime",
]


# Max trades to keep in memory and in trade_history.json.
# Older trades are archived to trade_history_archive.json on disk (CSV always keeps all).
MAX_TRADES_IN_MEMORY = 500


class TradeHistory:
    """
    Tracks which position IDs the bot has seen.
    When a position closes, fetches its history from exchange and saves it.
    Memory-safe: keeps only the last MAX_TRADES_IN_MEMORY trades in RAM.
    """

    def __init__(self, data_dir: Optional[str] = None):
        self._dir = Path(data_dir) if data_dir else DATA_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._json_path = self._dir / "trade_history.json"
        self._csv_path = self._dir / "trade_history.csv"
        self._archive_path = self._dir / "trade_history_archive.json"
        self._known_ids: Set[str] = set()  # position IDs already recorded
        self._trades: List[Dict[str, Any]] = []
        self._load()

    # ---------- public ----------

    @property
    def known_position_ids(self) -> Set[str]:
        return self._known_ids

    def record_trade(self, position_data: Dict[str, Any]) -> None:
        """Save a closed position (raw exchange data)."""
        pid = str(position_data.get("positionId", ""))
        if pid in self._known_ids:
            return  # already recorded

        self._known_ids.add(pid)
        self._trades.append(position_data)

        # Trim old trades from memory if over cap
        if len(self._trades) > MAX_TRADES_IN_MEMORY:
            self._archive_and_trim()

        self._save_json()
        self._append_csv(position_data)

        pnl = position_data.get("realizedPNL", "?")
        symbol = position_data.get("symbol", "?")
        side = position_data.get("side", "?")
        entry = position_data.get("entryPrice", "?")
        close = position_data.get("closePrice", "?")
        logger.info(
            f"💾 Trade saved: {symbol} {side} entry={entry} close={close} "
            f"PnL={pnl} fee={position_data.get('fee', '?')}"
        )

    def get_summary(self) -> Dict[str, Any]:
        """Quick stats from recorded trades."""
        if not self._trades:
            return {"total": 0}
        pnls = [float(t.get("realizedPNL", 0)) for t in self._trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        return {
            "total": len(self._trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
            "total_pnl": round(sum(pnls), 4),
            "best": round(max(pnls), 4) if pnls else 0,
            "worst": round(min(pnls), 4) if pnls else 0,
        }

    def get_symbol_pnl(self, lookback_hours: int = 72) -> Dict[str, Dict[str, Any]]:
        """Get per-symbol PnL analysis for the last N hours.
        
        Returns:
            Dict[symbol] = {
                'net_pnl': float,       # Total realized PnL (after fees)
                'trades': int,          # Number of trades
                'wins': int,
                'losses': int,
                'win_rate': float,      # 0-100%
                'avg_pnl': float,       # Average PnL per trade
                'profitable': bool,     # net_pnl > 0
            }
        """
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(hours=lookback_hours)
        
        symbol_data: Dict[str, list] = {}
        for t in self._trades:
            # Parse timestamp
            ts_str = t.get("ctime", t.get("closeTime", t.get("timestamp", "")))
            try:
                if isinstance(ts_str, (int, float)):
                    ts = datetime.fromtimestamp(int(ts_str) / 1000 if int(ts_str) > 1e12 else int(ts_str))
                elif isinstance(ts_str, str) and ts_str:
                    ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00').replace('+00:00', ''))
                else:
                    ts = datetime.min  # include if no timestamp
            except (ValueError, TypeError):
                ts = datetime.min
            
            if ts < cutoff and ts != datetime.min:
                continue
            
            sym = t.get("symbol", "")
            if sym:
                symbol_data.setdefault(sym, []).append(t)
        
        result = {}
        for sym, trades in symbol_data.items():
            pnls = []
            for t in trades:
                pnl = float(t.get("realizedPNL", 0))
                fee = float(t.get("fee", 0))
                net = pnl - abs(fee)  # Subtract fees
                pnls.append(net)
            
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            net_pnl = sum(pnls)
            
            result[sym] = {
                'net_pnl': round(net_pnl, 4),
                'trades': len(pnls),
                'wins': len(wins),
                'losses': len(losses),
                'win_rate': round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
                'avg_pnl': round(net_pnl / len(pnls), 4) if pnls else 0,
                'profitable': net_pnl > 0,
            }
        return result

    def get_recent_streak(self, symbol: str, lookback_hours: int = 24) -> int:
        """Get current losing streak for a symbol (count of consecutive losses from most recent).
        
        Returns:
            Negative number = consecutive losses (e.g. -3 means 3 losses in a row)
            Positive = consecutive wins
            0 = no trades
        """
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(hours=lookback_hours)
        
        recent = []
        for t in self._trades:
            sym = t.get("symbol", "")
            if sym != symbol:
                continue
            ts_str = t.get("mtime", t.get("ctime", ""))
            try:
                if isinstance(ts_str, (int, float)):
                    ts = datetime.fromtimestamp(int(ts_str) / 1000 if int(ts_str) > 1e12 else int(ts_str))
                else:
                    ts = datetime.min
            except (ValueError, TypeError):
                ts = datetime.min
            if ts >= cutoff:
                pnl = float(t.get("realizedPNL", 0))
                recent.append((ts, pnl))
        
        if not recent:
            return 0
        
        # Sort by time descending (newest first)
        recent.sort(key=lambda x: x[0], reverse=True)
        
        streak = 0
        for _, pnl in recent:
            if pnl <= 0:
                streak -= 1
            else:
                break
        
        return streak

    # ---------- persistence ----------

    def _load(self) -> None:
        if self._json_path.exists():
            try:
                with open(self._json_path, "r") as f:
                    self._trades = json.load(f)
                self._known_ids = {
                    str(t.get("positionId", "")) for t in self._trades
                }
                # Also load archived IDs to prevent duplicates
                if self._archive_path.exists():
                    try:
                        with open(self._archive_path, "r") as f:
                            archived = json.load(f)
                        for t in archived:
                            self._known_ids.add(str(t.get("positionId", "")))
                    except Exception:
                        pass
                logger.info(
                    f"📂 Loaded {len(self._trades)} trades in memory "
                    f"({len(self._known_ids)} total known IDs)"
                )
            except Exception as e:
                logger.warning(f"Failed to load trade history: {e}")

    def _archive_and_trim(self) -> None:
        """Move oldest trades to archive file, keep last MAX_TRADES_IN_MEMORY in memory."""
        if len(self._trades) <= MAX_TRADES_IN_MEMORY:
            return

        overflow = len(self._trades) - MAX_TRADES_IN_MEMORY
        to_archive = self._trades[:overflow]
        self._trades = self._trades[overflow:]

        try:
            # Append to archive file
            existing_archive = []
            if self._archive_path.exists():
                try:
                    with open(self._archive_path, "r") as f:
                        existing_archive = json.load(f)
                except Exception:
                    pass

            existing_archive.extend(to_archive)
            tmp = self._archive_path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(existing_archive, f, indent=2, default=str)
            tmp.replace(self._archive_path)

            logger.info(
                f"📦 Archived {overflow} old trades → {self._archive_path.name} "
                f"(total archived: {len(existing_archive)}, in memory: {len(self._trades)})"
            )
        except Exception as e:
            logger.error(f"Failed to archive trades: {e}")

    def _save_json(self) -> None:
        try:
            tmp = self._json_path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(self._trades, f, indent=2, default=str)
            tmp.replace(self._json_path)
        except Exception as e:
            logger.error(f"Failed to write trade JSON: {e}")

    def _append_csv(self, trade: Dict[str, Any]) -> None:
        try:
            write_header = not self._csv_path.exists() or self._csv_path.stat().st_size == 0
            with open(self._csv_path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                if write_header:
                    w.writeheader()
                w.writerow(trade)
        except Exception as e:
            logger.error(f"Failed to write trade CSV: {e}")

