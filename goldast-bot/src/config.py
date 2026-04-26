"""
GoldasT Bot v2 - Configuration Loader
Validates and loads configuration from YAML
"""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Any, Optional

import yaml


logger = logging.getLogger(__name__)


@dataclass
class APIConfig:
    key: str
    secret: str
    base_url: str = "https://fapi.bitunix.com"
    timeout: int = 30                     # HTTP request timeout (seconds)


@dataclass
class FVGConfig:
    timeframe: str = "15m"
    entry_zone_min: float = 0.5
    entry_zone_max: float = 0.8
    min_gap_percent: float = 0.0010
    min_gap_atr_mult: float = 0.3      # Min gap must be ≥ N×ATR (0 = disabled, use only %)
    max_active_fvgs: int = 5
    lookback_candles: int = 50
    min_volume_ratio: float = 1.2     # Minimum volume ratio on FVG candle
    edge_entry_tolerance: float = 0.0015  # 0.15% — enter when price is within this distance of zone edge
    min_candle_age: int = 1           # FVG must survive N candles before entry
    allowed_direction: str = "BOTH"   # "BOTH", "SHORT", "LONG" — direction filter
    min_strength: float = 0.0         # Min FVG strength to allow entry (0.0 = disabled)
    max_zone_distance: float = 0.015  # 1.5% — expire FVGs farther than this from price
    max_entry_distance: float = 0.015 # 1.5% — sliding window proximity filter
    ifvg_threshold_pct: float = 0.5   # IFVG violation threshold %
    impulse_body_ratio: float = 0.5   # Min body/range ratio for impulse candle strength factor (0.0 = disabled)


@dataclass
class TPSLConfig:
    # ATR-based SL
    sl_buffer_atr_mult: float = 0.3   # Noise buffer beyond zone edge (× ATR)
    sl_min_atr_mult: float = 0.5      # Minimum SL distance (× ATR)
    sl_max_atr_mult: float = 1.5      # Maximum SL distance (× ATR) — cap for tight 5m scalping
    # ATR-based TP
    min_rr: float = 2.0               # Minimum risk:reward ratio
    tp_min_atr_mult: float = 1.0      # TP floor (× ATR)
    min_tp_usd: float = 0.80          # Min TP profit in USD (must cover fees + profit margin)
    min_tp_distance_pct: float = 0.30   # Min TP distance % (lowered: 0.30% achievable, fee drag ~40%)
    max_tp_distance_pct: float = 0.50   # Hard cap on TP distance % (lowered: 0.50% realistic for 15m FVG)
    # Trailing SL — 3-phase system
    trailing_enabled: bool = True
    trailing_breakeven_at_r: float = 1.0   # Phase 2: move SL to BE at 1.0R
    trailing_be_lock_r: float = 0.25       # BE SL = entry + 0.25R
    trailing_runner_at_r: float = 2.0      # Phase 3: activate runner at 2.0R
    trailing_be_trail_offset_r: float = 0.8    # Progressive BE: SL = profit - offset (breathing room)
    trailing_runner_sl_distance_r: float = 2.5  # Runner SL = price - 2.5R
    trailing_runner_tp_distance_r: float = 1.5  # Runner TP = price + 1.5R
    trailing_step_r: float = 0.3           # Min SL move for API call (hysteresis)
    # Trailing tightening — gradually reduce trail distance as profit grows
    trailing_tighten_at_r: float = 5.0     # At 50% TP: trail reaches mid_distance
    trailing_tighten_min_distance_r: float = 1.0  # Trail distance at tighten_at_r
    trailing_tighten_final_distance_r: float = 0.5  # Trail distance at force_close_at_r
    # Dynamic ATR trailing — recalculate ATR on each candle close for adaptive distances
    trailing_dynamic_atr: bool = False          # Use current ATR for trailing distances (vs fixed entry-time risk)
    trailing_atr_clamp: float = 0.5             # Max ATR change from entry (±50%): clamp(current_atr, entry*0.5, entry*1.5)
    # Partial TP
    partial_tp_enabled: bool = True
    partial_tp_percent: float = 0.5        # Close 50% at 1R
    partial_tp_at_r: float = 1.0           # Trigger at 1R profit
    # Adaptive R:R
    adaptive_rr_enabled: bool = True
    trending_rr: float = 3.0               # R:R in strong trend
    ranging_rr: float = 1.5                # R:R in ranging market
    # Force close at extreme profit (failsafe)
    force_close_at_r: float = 3.0   # Force-close if trailing fails at this R multiple
    # ATR period
    atr_period: int = 14            # ATR calculation period for TP/SL
    # ATR fallbacks (when candle data is unavailable)
    atr_fallback_pct: float = 0.0015       # Fallback ATR estimate as % of price (0.15%)
    atr_floor_pct: float = 0.0001          # ATR safety floor as % of price (0.01%)
    fallback_notional_usd: float = 120.0   # Fallback notional for min TP USD calculation
    # SL validation correction
    sl_correction_pct: float = 0.003       # SL adjustment when on wrong side of price (0.3%)
    # Breakeven classification
    breakeven_pnl_threshold: float = 0.50  # USD threshold to classify close as BE ($0.50)
    # Legacy (kept for startup sync fallback)
    sl_multiplier: float = 0.618
    tp_multiplier: float = 1.618


@dataclass
class LeverageConfig:
    low: int = 5
    high: int = 10
    confidence_threshold: float = 0.6  # strength >= 0.6 → high leverage


@dataclass
class PositionConfig:
    risk_percent: float = 0.01
    min_position_usd: float = 10
    max_position_usd: float = 100
    max_balance_percent: float = 1.5
    risk_min_multiplier: float = 0.5     # Floor: risk_percent × this
    risk_max_multiplier: float = 2.0     # Cap: risk_percent × this
    risk_adjustment_rate: float = 0.20   # ±% per trade for dynamic risk sizing
    min_sl_guard_pct: float = 0.0005     # Min SL distance guard (0.05%) — prevents absurd position sizes
    margin_safety_factor: float = 0.95   # Fee/margin safety factor (95% of max capacity)
    default_min_qty: float = 0.001       # Default min quantity when symbol not in min_quantities
    default_step_size: float = 0.0001    # Default quantity step size for rounding
    min_quantities: Dict[str, float] = field(default_factory=dict)


@dataclass
class SessionConfig:
    enabled: bool = True
    # ICT Killzones — list of {name, start_hour, end_hour} (UTC, 24h format)
    # Only trade during these windows. Each zone: start_hour <= hour < end_hour
    killzones: list = field(default_factory=lambda: [
        {"name": "London", "start_hour": 7, "end_hour": 10},
        {"name": "NY", "start_hour": 12, "end_hour": 16},
        {"name": "Late_NY", "start_hour": 20, "end_hour": 23},
    ])
    weekdays_only: bool = True

    def is_killzone_now(self) -> tuple:
        """Check if current UTC hour is within any killzone.
        Returns (is_active, zone_name)."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if self.weekdays_only and now.weekday() >= 5:  # Sat=5, Sun=6
            return False, "weekend"
        hour = now.hour
        for kz in self.killzones:
            start = kz.get("start_hour", 0)
            end = kz.get("end_hour", 24)
            if start <= hour < end:
                return True, kz.get("name", f"{start}-{end}")
        return False, "outside_killzones"

    def get_killzone_leverage_override(self) -> int | None:
        """Return leverage_override for current killzone, or None if not set."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        hour = now.hour
        for kz in self.killzones:
            start = kz.get("start_hour", 0)
            end = kz.get("end_hour", 24)
            if start <= hour < end:
                return kz.get("leverage_override", None)
        return None


@dataclass
class CooldownConfig:
    entry_cooldown_seconds: int = 300
    win_cooldown_seconds: int = 30  # Shorter cooldown after a win
    loss_cooldown_seconds: int = 900  # 15 min cooldown after a loss
    signal_cooldown_seconds: int = 30
    klines_ready_threshold: int = 50
    tpsl_placement_delay: int = 2
    # Global burst limiter — minimum seconds between ANY entry across all symbols
    global_entry_cooldown_seconds: int = 180  # 3 min between entries (prevents burst)
    # Zone cooldown — don't re-enter a zone that just hit SL/BE
    zone_cooldown_seconds: int = 7200  # 2 hours — zone is "spent" after SL/BE
    zone_overlap_threshold: float = 0.50  # 50% overlap = same zone
    # Per-symbol entry limit
    max_entries_per_symbol: int = 2  # Max entries per symbol per window
    max_entries_window_hours: int = 4  # Window size in hours


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5
    recovery_timeout: int = 60


@dataclass
class RetryConfig:
    max_attempts: int = 3
    initial_delay: float = 1.0
    max_delay: float = 60.0
    backoff_factor: float = 2.0


@dataclass
class RandomizationConfig:
    enabled: bool = True
    size_jitter_percent: float = 0.05
    timing_jitter_ms: int = 500


@dataclass
class WebSocketConfig:
    public_url: str = "wss://fapi.bitunix.com/public/"
    private_url: str = "wss://fapi.bitunix.com/private/"
    ping_interval: int = 20
    reconnect_attempts: int = 5
    reconnect_interval: int = 5


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    allowed_users: list = field(default_factory=list)
    notifications_enabled: bool = True
    hourly_stats: bool = False
    daily_summary: bool = False
    quiet_hours: bool = False
    quiet_start: int = 22
    quiet_end: int = 7


@dataclass 
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/goldast_bot.log"
    max_size_mb: int = 10
    backup_count: int = 5


@dataclass
class TrendConfig:
    entry_threshold: float = 0.15   # |combined score| must exceed for SHORT entry
    long_entry_threshold: float = 0.50  # LONG threshold raised — LONGs only in strong uptrend
    ema_fast: int = 8               # Fast EMA period
    ema_slow: int = 21              # Slow EMA period
    weight_15m: float = 0.3         # 15m weight reduced — too noisy, caused false LONGs
    weight_1h: float = 0.7          # 1h weight raised — anchor to hourly trend
    # BTC market leader filter — nerf counter-BTC trades
    btc_leader_enabled: bool = True   # Use BTC trend as market direction leader
    btc_leader_nerf_multiplier: float = 2.0  # Multiply threshold for counter-BTC direction
    btc_leader_boost_divisor: float = 1.5    # Divide threshold for with-BTC direction
    # Trend direction flip — flip FVG direction to match strong 1h trend instead of blocking
    trend_flip_enabled: bool = False
    trend_flip_threshold: float = 0.35  # Score must exceed this for flip
    # RSI extremes filter
    rsi_overbought: float = 75.0    # Block LONG above this RSI
    rsi_oversold: float = 25.0      # Block SHORT below this RSI
    # Candle body volatility confirmation
    min_candle_body_atr_ratio: float = 0.15  # Last candle body must be > this × ATR
    # ATR period used across strategy
    atr_period: int = 14            # ATR calculation period
    # Trend score component weights (must sum to 1.0)
    score_weight_alignment: float = 0.50   # EMA alignment weight
    score_weight_momentum: float = 0.30    # Price momentum weight
    score_weight_slope: float = 0.20       # EMA slope weight
    strong_trend_multiplier: float = 2.0   # Threshold × this = "STRONG" label
    trend_rescan_distance: float = 0.005   # Mid-tick FVG rescan distance (0.5%)
    # === Adaptive Regime: auto-adjust thresholds based on ADX ===
    adaptive_regime_enabled: bool = False   # Enable adaptive ranging/trending regime
    ranging_adx_threshold: float = 20.0    # ADX below this = ranging market
    ranging_trend_multiplier: float = 0.60 # In ranging: thresholds × this
    # === BOS (Break of Structure) Filter ===
    bos_enabled: bool = False         # Require BOS confirmation before entry
    bos_soft_mode: bool = False       # Soft mode: reduce size instead of blocking
    bos_max_age_candles: int = 20     # BOS must be within last N 15m candles
    bos_min_hold_candles: int = 3      # BOS must hold direction for N candles before trusting
    bos_direction_override: bool = False  # BOS determines trade direction (BULLISH→LONG, BEARISH→SHORT)
    # === HTF (1h) FVG Zone Confluence ===
    htf_fvg_enabled: bool = False            # Require 1h FVG zone confluence
    htf_soft_mode: bool = False              # Soft mode: reduce size instead of blocking
    htf_fvg_min_gap_percent: float = 0.001   # Min gap % for 1h FVG detection
    htf_fvg_max_zones: int = 10              # Max 1h FVG zones to track per symbol
    htf_fvg_strength_bonus: float = 0.15     # Bonus to FVG strength when confluent with 1h zone
    # Soft mode sizing
    soft_mode_size_mult: float = 0.5         # Position size mult when BOS/HTF missing
    confluence_bonus_mult: float = 1.25      # Position size mult when both BOS+HTF confirm
    # === Exhaustion Reversal Detection ===
    exhaustion_enabled: bool = True          # Detect 3+ consecutive strong candles for reversal
    exhaustion_min_candles: int = 3           # Min consecutive strong candles to trigger
    exhaustion_body_atr_ratio: float = 0.5   # Each candle body must be > 0.5× ATR
    exhaustion_boost_mult: float = 1.5       # Position size boost for reversal entries
    # === Liquidity Sweep Filter ===
    sweep_enabled: bool = False              # Require recent liquidity sweep before entry
    sweep_soft_mode: bool = True             # Soft mode: reduce size instead of blocking
    sweep_max_age_candles: int = 10          # Sweep must be within last N 15m candles
    sweep_size_mult: float = 0.5             # Position size mult when no sweep (soft mode)
    sweep_bonus_mult: float = 1.25           # Position size bonus when sweep confirmed


@dataclass
class RotationConfig:
    enabled: bool = True            # Enable daily symbol rotation
    interval_hours: int = 24        # Rotate every N hours
    max_symbols: int = 15           # Hard cap: core + proven + trial (accumulative growth)
    rotation_pool_size: int = 6     # Max NEW trial symbols added per rotation cycle
    min_score: float = 55.0         # Minimum scanner score to be eligible
    scan_top_n: int = 30            # Scan top N pairs by volume
    scan_candles: int = 200         # 5m candles per symbol for scoring
    min_24h_volume: float = 10_000_000  # Min 24h USDT volume ($10M) — reject illiquid pairs
    pnl_lookback_hours: int = 72    # Look back N hours for per-symbol PnL
    protect_profitable: bool = True # Never remove symbols with positive PnL
    max_losing_trades: int = 3      # Force-remove after N consecutive losses
    pnl_ban_enabled: bool = True    # Enable 24h PnL-based ban for worst rotation symbol
    pnl_ban_hours: int = 24         # Ban duration for worst-PnL rotation symbol
    pnl_ban_threshold: float = -2.0  # Ban any rotation symbol with PnL below this ($)
    removed_cooldown_hours: float = 3.0  # Removed symbols can't return for N hours
    pnl_penalty_per_dollar: float = 5.0  # Score penalty per $1 negative PnL
    pnl_bonus_per_dollar: float = 3.0   # Score bonus per $1 positive PnL
    pnl_bonus_cap: float = 15.0         # Max bonus points (prevents score inflation)
    # Proven symbol promotion thresholds
    proven_min_pnl: float = 0.50    # Min net PnL ($) to promote symbol to "proven"
    proven_min_trades: int = 2      # Min trades to qualify as proven
    proven_min_wr: float = 40.0     # Min WR% to promote to proven
    # Trial symbol restrictions (non-proven, non-core)
    trial_min_fill: float = 0.30            # Require 30% zone fill for trial (vs 15% for proven)
    trial_require_confluence: bool = True   # Trial symbols need BOS+HTF (no soft mode)
    trial_max_losing_trades: int = 1        # Remove trial after 1st loss — fast fail
    trial_size_multiplier: float = 0.5      # Trial symbols trade at 50% size (unproven = less risk)
    # Signal pipeline ban (fast 2h check)
    signal_ban_min_active_hours: float = 8.0   # Symbol must be active this long before ban
    signal_ban_no_hit_hours: float = 8.0       # Ban if no zone hit for this many hours
    # Proven benching — temporarily free slots from proven symbols far from zones
    proven_bench_min_proximity: float = 30.0   # Bench proven if actionable_proximity < this
    # Cheap coin bonus — prioritize low-price coins (better FVG performance)
    cheap_coin_threshold: float = 0.10         # Coins below this price get bonus score
    cheap_coin_bonus: float = 8.0              # Bonus points for cheap coins
    # Opportunity scanner — fast market-wide proximity check every 15 min
    opportunity_scan_enabled: bool = True       # Enable frequent opportunity scanning
    opportunity_scan_top_n: int = 50            # Scan top N symbols by volume
    opportunity_scan_candles: int = 100         # Fewer candles for faster scan
    opportunity_min_proximity: float = 70.0     # Min actionable proximity to trigger swap
    opportunity_max_swaps: int = 2              # Max symbol swaps per scan cycle
    opportunity_min_score_advantage: float = 10.0  # Min score advantage over worst trial


@dataclass
class RiskConfig:
    max_daily_loss_percent: float = 5.0
    max_drawdown_percent: float = 10.0
    margin_warning_percent: float = 80.0
    # Per-symbol consecutive loss ban
    symbol_max_consecutive_losses: int = 3     # Ban symbol after N consecutive losses
    symbol_loss_ban_seconds: int = 86400       # Ban duration for rotation symbols (24 hours)
    core_symbol_loss_ban_seconds: int = 3600   # Ban duration for core symbols (1 hour)
    # Global consecutive loss pause
    global_max_consecutive_losses: int = 4     # Pause ALL trading after N consecutive losses
    global_loss_pause_seconds: int = 1800      # Pause duration (30 min)
    # Direction nerfing (raise threshold after consecutive directional losses)
    direction_nerf_window: int = 5              # Look at last N trades per direction
    direction_nerf_net_pnl_threshold: float = -2.0  # Block if net PnL < threshold over window
    direction_nerf_duration_seconds: int = 3600  # Block timeout (1 hour)
    direction_nerf_multiplier: float = 3.0     # Threshold multiplier (legacy fallback)


@dataclass
class MultiSymbolConfig:
    max_concurrent_positions: int = 3
    position_per_symbol: int = 1
    max_same_direction: int = 2  # Max positions in same direction
    correlation_guard_enabled: bool = True  # Block correlated symbols in same direction
    max_correlated_same_dir: int = 1       # Max positions in same correlation group + direction


@dataclass
class RiskScalingConfig:
    """Phased risk scaling: automatically increase risk% as balance grows."""
    enabled: bool = False
    # List of (balance_threshold, risk_percent) — applied in order, last match wins
    # Example: [{"balance": 200, "risk": 0.025}, {"balance": 500, "risk": 0.03}]
    phases: List[Dict[str, float]] = field(default_factory=list)


@dataclass
class MeanReversionConfig:
    """Mean reversion strategy for ranging markets (ADX < threshold)."""
    enabled: bool = False
    adx_max: float = 20.0            # Only MR when ADX below this
    bb_period: int = 20              # Bollinger Band SMA period
    bb_std: float = 2.5              # Bollinger Band std dev multiplier
    rsi_entry: float = 30.0          # RSI threshold: LONG when RSI <= this
    sl_pct: float = 0.010            # SL distance as % of price
    tp_pct: float = 0.015            # TP distance as % of price
    max_hold_candles: int = 50       # Max hold time in candles


@dataclass
class Config:
    """Main configuration container"""
    api: APIConfig
    symbols: List[str]
    core_symbols: List[str]
    blacklist: List[str]
    fvg: FVGConfig
    tpsl: TPSLConfig
    leverage: LeverageConfig
    position: PositionConfig
    multi_symbol: MultiSymbolConfig
    session: SessionConfig
    cooldowns: CooldownConfig
    circuit_breaker: CircuitBreakerConfig
    retry: RetryConfig
    randomization: RandomizationConfig
    websocket: WebSocketConfig
    telegram: TelegramConfig
    logging: LoggingConfig
    risk: RiskConfig
    trend: TrendConfig
    rotation: RotationConfig
    mean_reversion: MeanReversionConfig
    risk_scaling: RiskScalingConfig = field(default_factory=RiskScalingConfig)
    database_path: str = "goldast_bot.db"
    dry_run: bool = False


def _expand_env_vars(value: Any) -> Any:
    """Recursively expand environment variables in strings"""
    if isinstance(value, str):
        if value.startswith("${") and value.endswith("}"):
            env_var = value[2:-1]
            return os.environ.get(env_var, "")
        return value
    elif isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    return value


def load_config(config_path: str = "config.yaml") -> Config:
    """Load and validate configuration from YAML file"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(path, 'r') as f:
        raw_config = yaml.safe_load(f)
    
    # Expand environment variables
    raw_config = _expand_env_vars(raw_config)
    
    # Parse sections
    api_cfg = raw_config.get("api", {})
    fvg_cfg = raw_config.get("fvg", {})
    tpsl_cfg = raw_config.get("tpsl", {})
    leverage_cfg = raw_config.get("leverage", {})
    position_cfg = raw_config.get("position", {})
    multi_symbol_cfg = raw_config.get("multi_symbol", {})
    session_cfg = raw_config.get("session", {})
    cooldowns_cfg = raw_config.get("cooldowns", {})
    circuit_breaker_cfg = raw_config.get("circuit_breaker", {})
    retry_cfg = raw_config.get("retry", {})
    randomization_cfg = raw_config.get("randomization", {})
    websocket_cfg = raw_config.get("websocket", {})
    telegram_cfg = raw_config.get("telegram", {})
    logging_cfg = raw_config.get("logging", {})
    risk_cfg = raw_config.get("risk", {})
    trend_cfg = raw_config.get("trend", {})
    rotation_cfg = raw_config.get("rotation", {})
    mr_cfg = raw_config.get("mean_reversion", {})
    rs_cfg = raw_config.get("risk_scaling", {})
    
    # Extract min_quantities from position config
    min_quantities = position_cfg.pop("min_quantities", {})
    
    # Parse core_symbols and blacklist
    core_symbols = raw_config.get("core_symbols", [])
    blacklist = raw_config.get("blacklist", [])
    symbols = raw_config.get("symbols", ["BTCUSDT"])
    
    # Ensure core symbols are always in the symbol list
    for cs in core_symbols:
        if cs not in symbols:
            symbols.append(cs)
    
    # Remove blacklisted symbols
    symbols = [s for s in symbols if s not in blacklist]
    
    config = Config(
        api=APIConfig(**api_cfg),
        symbols=symbols,
        core_symbols=core_symbols,
        blacklist=blacklist,
        fvg=FVGConfig(**fvg_cfg),
        tpsl=TPSLConfig(**tpsl_cfg),
        leverage=LeverageConfig(**leverage_cfg),
        position=PositionConfig(**position_cfg, min_quantities=min_quantities),
        multi_symbol=MultiSymbolConfig(**multi_symbol_cfg),
        session=SessionConfig(**session_cfg),
        cooldowns=CooldownConfig(**cooldowns_cfg),
        circuit_breaker=CircuitBreakerConfig(**circuit_breaker_cfg),
        retry=RetryConfig(**retry_cfg),
        randomization=RandomizationConfig(**randomization_cfg),
        websocket=WebSocketConfig(**websocket_cfg),
        telegram=TelegramConfig(**telegram_cfg),
        logging=LoggingConfig(**logging_cfg),
        risk=RiskConfig(**risk_cfg),
        trend=TrendConfig(**trend_cfg),
        rotation=RotationConfig(**rotation_cfg),
        mean_reversion=MeanReversionConfig(**mr_cfg),
        risk_scaling=RiskScalingConfig(
            enabled=rs_cfg.get("enabled", False),
            phases=rs_cfg.get("phases", []),
        ),
        database_path=raw_config.get("database", {}).get("path", "goldast_bot.db"),
        dry_run=raw_config.get("dry_run", False),
    )
    
    # Validate
    _validate_config(config)
    
    logger.info(f"Configuration loaded from {config_path}")
    logger.info(f"Trading symbols: {config.symbols}")
    logger.info(f"Dry run mode: {config.dry_run}")
    
    return config


def _validate_config(config: Config) -> None:
    """Validate configuration values"""
    errors = []
    
    # API validation
    if not config.api.key:
        errors.append("API key is required")
    if not config.api.secret:
        errors.append("API secret is required")
    
    # FVG validation
    if not (0 <= config.fvg.entry_zone_min < config.fvg.entry_zone_max <= 1):
        errors.append("FVG entry zone must be between 0 and 1, with min < max")
    
    # Leverage validation
    if not (1 <= config.leverage.low <= config.leverage.high <= 125):
        errors.append("Leverage must be between 1 and 125")
    
    # Position validation
    if config.position.risk_percent <= 0 or config.position.risk_percent > 0.5:
        errors.append("Risk percent should be between 0 and 50%")
    
    # Symbols validation
    if not config.symbols:
        errors.append("At least one trading symbol is required")
    
    if errors:
        raise ValueError(f"Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors))
