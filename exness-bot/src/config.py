"""
Exness Bot - Configuration Loader
Validates and loads configuration from YAML for MT5 trading.
"""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Any, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class MT5Config:
    server: str = "Exness-MT5Trial15"
    login: int = 0
    password: str = ""
    timeout: int = 30000
    portable: bool = False
    terminal_path: str = ""


@dataclass
class AccountConfig:
    hedge_mode: bool = True
    magic_number: int = 202603
    deviation: int = 20
    fill_type: str = "IOC"


@dataclass
class FVGConfig:
    entry_zone_min: float = 0.25
    entry_zone_max: float = 0.85
    min_strength: float = 0.55
    min_gap_percent: float = 0.0005
    min_gap_atr_mult: float = 0.3
    max_active_fvgs: int = 3
    lookback_candles: int = 50
    min_volume_ratio: float = 0.5
    edge_entry_tolerance: float = 0.0010
    min_candle_age: int = 1
    allowed_direction: str = "BOTH"
    max_zone_distance: float = 0.030
    max_entry_distance: float = 0.015
    ifvg_threshold_pct: float = 0.5
    impulse_body_ratio: float = 0.5


@dataclass
class SupplyDemandConfig:
    enabled: bool = True
    lookback_candles: int = 100
    min_zone_strength: float = 0.5
    min_impulse_atr_mult: float = 1.5
    max_base_candles: int = 5
    min_base_candles: int = 2
    zone_touch_invalidation: int = 3
    max_zones_per_side: int = 5
    fresh_zone_bonus: float = 0.20


@dataclass
class MTFConfig:
    enabled: bool = True
    htf_timeframe: str = "M15"
    mtf_timeframe: str = "M5"
    ltf_timeframe: str = "M1"
    htf_weight: float = 0.50
    mtf_weight: float = 0.30
    ltf_weight: float = 0.20
    require_htf_alignment: bool = True
    min_confluence_score: float = 0.60


@dataclass
class PendingOrderConfig:
    enabled: bool = True
    buy_stop_offset_atr: float = 0.3
    sell_stop_offset_atr: float = 0.3
    expiration_candles: int = 10
    max_pending_per_symbol: int = 2


@dataclass
class TrendConfig:
    ema_fast: int = 8
    ema_slow: int = 21
    entry_threshold: float = 0.20
    atr_period: int = 14
    bos_enabled: bool = True
    bos_soft_mode: bool = True
    bos_max_age_candles: int = 15
    bos_min_hold_candles: int = 3


@dataclass
class TPSLConfig:
    sl_buffer_atr_mult: float = 0.15
    sl_min_atr_mult: float = 0.5
    sl_max_atr_mult: float = 2.0
    default_rr: float = 2.0
    min_rr: float = 1.0
    max_rr: float = 3.0
    tp_min_atr_mult: float = 1.0
    trailing_enabled: bool = True
    trailing_breakeven_at_r: float = 1.0
    trailing_be_lock_r: float = 0.3
    trailing_runner_at_r: float = 2.0
    trailing_runner_sl_distance_r: float = 1.5
    trailing_step_r: float = 0.20
    atr_period: int = 14
    atr_fallback_pct: float = 0.0015
    atr_floor_pct: float = 0.0001


@dataclass
class PositionConfig:
    risk_percent: float = 0.02
    min_lot: float = 0.01
    max_lot: float = 1.0
    max_positions: int = 3
    max_positions_per_symbol: int = 2


@dataclass
class CooldownConfig:
    entry_cooldown_seconds: int = 120
    loss_cooldown_seconds: int = 300
    zone_cooldown_seconds: int = 3600


@dataclass
class SessionConfig:
    enabled: bool = True
    killzones: list = field(default_factory=lambda: [
        {"name": "London", "start_hour": 7, "end_hour": 10},
        {"name": "NY", "start_hour": 12, "end_hour": 16},
        {"name": "Late_NY", "start_hour": 20, "end_hour": 23},
    ])
    weekdays_only: bool = True

    def is_killzone_now(self) -> tuple:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if self.weekdays_only and now.weekday() >= 5:
            return False, "weekend"
        hour = now.hour
        for kz in self.killzones:
            start = kz.get("start_hour", 0)
            end = kz.get("end_hour", 24)
            if start <= hour < end:
                return True, kz.get("name", f"{start}-{end}")
        return False, "outside_killzones"


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    allowed_users: list = field(default_factory=list)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/exness_bot.log"
    max_size_mb: int = 10
    backup_count: int = 5


@dataclass
class Config:
    mt5: MT5Config = field(default_factory=MT5Config)
    account: AccountConfig = field(default_factory=AccountConfig)
    symbols: List[str] = field(default_factory=lambda: ["XAUUSD", "USOIL"])
    fvg: FVGConfig = field(default_factory=FVGConfig)
    supply_demand: SupplyDemandConfig = field(default_factory=SupplyDemandConfig)
    mtf: MTFConfig = field(default_factory=MTFConfig)
    pending_orders: PendingOrderConfig = field(default_factory=PendingOrderConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    tpsl: TPSLConfig = field(default_factory=TPSLConfig)
    position: PositionConfig = field(default_factory=PositionConfig)
    cooldowns: CooldownConfig = field(default_factory=CooldownConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _resolve_env_vars(value: str) -> str:
    """Resolve ${ENV_VAR} patterns in string values."""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_name = value[2:-1]
        env_val = os.environ.get(env_name, "")
        if not env_val:
            logger.warning(f"Environment variable {env_name} not set")
        return env_val
    return value


def _apply_dict(target, data: dict) -> None:
    """Apply dict values to a dataclass, resolving env vars."""
    for key, value in data.items():
        if hasattr(target, key):
            if isinstance(value, str):
                value = _resolve_env_vars(value)
            setattr(target, key, value)


def load_config(path: str = "config.yaml") -> Config:
    """Load configuration from YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        logger.warning(f"Config file not found: {path}, using defaults")
        return Config()

    with open(config_path, "r") as f:
        raw = yaml.safe_load(f) or {}

    config = Config()

    section_map = {
        "mt5": config.mt5,
        "account": config.account,
        "fvg": config.fvg,
        "supply_demand": config.supply_demand,
        "mtf": config.mtf,
        "pending_orders": config.pending_orders,
        "trend": config.trend,
        "tpsl": config.tpsl,
        "position": config.position,
        "cooldowns": config.cooldowns,
        "session": config.session,
        "telegram": config.telegram,
        "logging": config.logging,
    }

    for section_name, section_obj in section_map.items():
        if section_name in raw and isinstance(raw[section_name], dict):
            _apply_dict(section_obj, raw[section_name])

    if "symbols" in raw:
        config.symbols = raw["symbols"]

    # Resolve MT5 credentials from env vars
    config.mt5.server = _resolve_env_vars(str(config.mt5.server))
    login_str = _resolve_env_vars(str(config.mt5.login))
    if login_str.isdigit():
        config.mt5.login = int(login_str)
    config.mt5.password = _resolve_env_vars(str(config.mt5.password))

    logger.info(f"Config loaded from {path}: {len(config.symbols)} symbols")
    return config
