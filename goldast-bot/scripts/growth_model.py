#!/usr/bin/env python3
"""
GoldasT Bot — Growth Model: $94 → $8,000 in 90 days
Based on FVG-era data (122 trades, 10 core symbols, Feb 17 – Mar 23)
"""
import random
import statistics

# ═══════════════════════════════════════════════════
#  BASELINE DATA (FVG-era, broken trailing, 10 core)
# ═══════════════════════════════════════════════════
STARTING_BALANCE = 94.08
TARGET = 8000
DAYS = 90
RISK_PCT = 0.02          # 2% risk per trade
LEVERAGE = 5
MAX_BALANCE_PCT = 0.80   # 80% of balance usable as margin

# FVG-era observed (with broken trailing)
OBSERVED_WR = 0.607      # 60.7%
OBSERVED_RR = 1.10       # avg_win / avg_loss
OBSERVED_TRADES_DAY = 5.1
OBSERVED_DAILY_RETURN = 1.57 / 130  # ~1.21% daily on $130 balance

# Fees: avg $0.12/trade (entry+exit) on ~$130 notional
FEE_PER_TRADE_PCT = 0.0009  # ~0.09% round-trip (maker 0.02% + taker 0.05%)

# ═══════════════════════════════════════════════════
#  TRAILING FIX IMPACT ESTIMATE
# ═══════════════════════════════════════════════════
# Old: BE disabled (at 99R) — trades that hit 1R+ then reversed = full loss
# New: BE at 1R locks 0.5R — those trades now save 0.5R instead of losing 1R
# ~25% of FVG trades reached 1R before reversing (from quick trade analysis)
# Conservative: converts 10% of losses to 0.5R saves
# This shifts: some -1R → +0.5R

print("=" * 70)
print("  GoldasT Bot — Growth Model: $94.08 → $8,000")
print("  Based on 122 FVG-era trades (10 core symbols)")
print("=" * 70)

# ═══════════════════════════════════════════════════
#  SCENARIO DEFINITIONS
# ═══════════════════════════════════════════════════
scenarios = {
    "CONSERVATIVE (as-is FVG data)": {
        "wr": 0.607,
        "avg_win_r": 1.10,   # win = 1.10R (historical)
        "avg_loss_r": 1.00,  # loss = 1R
        "trades_day": 5,
        "be_rate": 0.00,     # no BE improvement
        "be_save_r": 0.0,
        "desc": "Raw FVG-era stats, no trailing improvement"
    },
    "MODERATE (trailing fix working)": {
        "wr": 0.607,
        "avg_win_r": 1.20,   # slightly better with runner catching some 2R+
        "avg_loss_r": 1.00,
        "trades_day": 5,
        "be_rate": 0.10,     # 10% of losses → BE saves (0.5R)
        "be_save_r": 0.50,
        "desc": "Trailing BE converts 10% of losses to +0.5R"
    },
    "OPTIMISTIC (trailing + rotation finds gems)": {
        "wr": 0.607,
        "avg_win_r": 1.40,   # runner at 2R catches extended moves
        "avg_loss_r": 1.00,
        "trades_day": 6,
        "be_rate": 0.15,     # 15% of losses → BE saves
        "be_save_r": 0.50,
        "desc": "Trailing + some 2R runners + rotation adds good symbols"
    },
    "AGGRESSIVE (scale risk at milestones)": {
        "wr": 0.607,
        "avg_win_r": 1.30,
        "avg_loss_r": 1.00,
        "trades_day": 5,
        "be_rate": 0.10,
        "be_save_r": 0.50,
        "desc": "Moderate stats + increase risk% at balance milestones",
        "risk_scaling": True  # 2%→3%→4% at $200/$500/$1000
    },
}


def simulate_once(scenario, seed=None):
    """Simulate 90 days of trading with compound growth."""
    rng = random.Random(seed)
    
    wr = scenario["wr"]
    avg_win_r = scenario["avg_win_r"]
    avg_loss_r = scenario["avg_loss_r"]
    trades_day = scenario["trades_day"]
    be_rate = scenario["be_rate"]
    be_save_r = scenario["be_save_r"]
    risk_scaling = scenario.get("risk_scaling", False)
    
    balance = STARTING_BALANCE
    peak = balance
    max_dd = 0
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_be = 0
    daily_balances = [balance]
    
    # Risk management limits
    max_daily_loss_pct = 0.02   # -2% daily loss limit
    max_drawdown_pct = 0.10     # -10% max drawdown
    
    for day in range(1, DAYS + 1):
        day_start = balance
        day_pnl = 0
        
        # Risk scaling at milestones
        risk = RISK_PCT
        if risk_scaling:
            if balance >= 1000:
                risk = 0.04   # 4% at $1000+
            elif balance >= 500:
                risk = 0.03   # 3% at $500+
            elif balance >= 200:
                risk = 0.025  # 2.5% at $200+
        
        # Randomize trades/day (±2)
        n_trades = max(2, int(rng.gauss(trades_day, 1.5)))
        
        for _ in range(n_trades):
            # Daily loss limit check
            if day_pnl < 0 and abs(day_pnl) >= day_start * max_daily_loss_pct:
                break
            
            # Max drawdown check
            if balance < peak * (1 - max_drawdown_pct):
                break
            
            # Position sizing
            risk_usd = balance * risk
            
            # Margin check: position = risk_usd / sl_distance
            # With 2% risk and ~1% SL, position ≈ 2× balance
            # Max position = balance × leverage × 0.80 = balance × 4
            # So we're always within margin at small balances
            
            # Determine outcome
            roll = rng.random()
            if roll < wr:
                # WIN
                # Some wins are bigger (runner catches 2R+)
                win_r = rng.gauss(avg_win_r, avg_win_r * 0.3)  # variance
                win_r = max(0.3, win_r)  # min 0.3R win
                pnl = risk_usd * win_r
                total_wins += 1
            elif roll < wr + (1 - wr) * be_rate:
                # BREAKEVEN SAVE (trailing locked 0.5R)
                pnl = risk_usd * be_save_r
                total_be += 1
            else:
                # LOSS
                loss_r = rng.gauss(avg_loss_r, avg_loss_r * 0.15)
                loss_r = max(0.5, min(1.5, loss_r))
                pnl = -risk_usd * loss_r
                total_losses += 1
            
            # Subtract fees
            # Fee is on the notional (position size), not the risk
            # position_usd ≈ risk_usd / ~0.01 (1% SL) = risk_usd × 100
            # But actually fee is variable. Approximate:
            fee = risk_usd * 0.06  # ~6% of risk amount as fees (observed: $0.12 fee on $1.88 risk)
            pnl -= fee
            
            balance += pnl
            day_pnl += pnl
            total_trades += 1
            
            # Floor at $5 (can't trade below min)
            if balance < 5:
                balance = 5
                break
        
        # Track peak and drawdown
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak
        if dd > max_dd:
            max_dd = dd
        
        daily_balances.append(balance)
    
    return {
        "final": balance,
        "peak": peak,
        "max_dd": max_dd,
        "total_trades": total_trades,
        "wins": total_wins,
        "losses": total_losses,
        "be": total_be,
        "daily_balances": daily_balances,
    }


def run_monte_carlo(scenario, n_sims=10000):
    """Run Monte Carlo simulation."""
    results = []
    for i in range(n_sims):
        r = simulate_once(scenario, seed=i)
        results.append(r)
    
    finals = [r["final"] for r in results]
    dds = [r["max_dd"] for r in results]
    
    finals.sort()
    
    return {
        "median": statistics.median(finals),
        "mean": statistics.mean(finals),
        "p10": finals[int(n_sims * 0.10)],
        "p25": finals[int(n_sims * 0.25)],
        "p50": finals[int(n_sims * 0.50)],
        "p75": finals[int(n_sims * 0.75)],
        "p90": finals[int(n_sims * 0.90)],
        "p95": finals[int(n_sims * 0.95)],
        "min": min(finals),
        "max": max(finals),
        "hit_target": sum(1 for f in finals if f >= TARGET) / n_sims * 100,
        "avg_max_dd": statistics.mean(dds),
        "worst_dd": max(dds),
        "ruin_rate": sum(1 for f in finals if f < 20) / n_sims * 100,
    }


# ═══════════════════════════════════════════════════
#  RUN ALL SCENARIOS
# ═══════════════════════════════════════════════════
N_SIMS = 10000

for name, scenario in scenarios.items():
    print(f"\n{'─' * 70}")
    print(f"  📊 {name}")
    print(f"  {scenario['desc']}")
    print(f"{'─' * 70}")
    
    # Calculate theoretical expectancy
    wr = scenario["wr"]
    be_rate = scenario["be_rate"]
    loss_rate = (1 - wr) * (1 - be_rate)
    be_adj_rate = (1 - wr) * be_rate
    
    exp_per_trade_r = (
        wr * scenario["avg_win_r"]
        + be_adj_rate * scenario["be_save_r"]
        - loss_rate * scenario["avg_loss_r"]
    )
    
    risk = 0.02
    if scenario.get("risk_scaling"):
        risk_note = "2%→2.5%→3%→4% (scaling)"
    else:
        risk_note = "2% fixed"
    
    exp_per_trade_pct = exp_per_trade_r * risk * 100
    trades_per_day = scenario["trades_day"]
    exp_daily_pct = exp_per_trade_pct * trades_per_day
    
    print(f"\n  Parameters:")
    print(f"    WR: {wr*100:.1f}%  |  Avg Win: {scenario['avg_win_r']:.2f}R  |  Avg Loss: {scenario['avg_loss_r']:.2f}R")
    print(f"    BE save rate: {be_rate*100:.0f}%  |  Trades/day: {trades_per_day}")
    print(f"    Risk: {risk_note}  |  Leverage: {LEVERAGE}x")
    print(f"\n  Theoretical Expectancy:")
    print(f"    Per trade: {exp_per_trade_r:+.4f}R = {exp_per_trade_pct:+.3f}% of balance")
    print(f"    Per day:   ~{exp_daily_pct:+.2f}% of balance ({trades_per_day} trades)")
    
    # Simple compound projection (no variance)
    simple_90d = STARTING_BALANCE * (1 + exp_daily_pct / 100) ** DAYS
    print(f"    Simple compound 90d: ${simple_90d:,.0f}")
    
    # Days to $8000 (simple compound)
    if exp_daily_pct > 0:
        import math
        days_to_target = math.log(TARGET / STARTING_BALANCE) / math.log(1 + exp_daily_pct / 100)
        print(f"    Days to ${TARGET:,} (simple): {days_to_target:.0f} days")
    
    # Monte Carlo
    mc = run_monte_carlo(scenario, N_SIMS)
    
    print(f"\n  Monte Carlo ({N_SIMS:,} simulations, 90 days):")
    print(f"    ┌──────────────┬─────────────┐")
    print(f"    │ Percentile   │ Balance     │")
    print(f"    ├──────────────┼─────────────┤")
    print(f"    │ Worst case   │ ${mc['min']:>9,.0f}  │")
    print(f"    │ 10th (bad)   │ ${mc['p10']:>9,.0f}  │")
    print(f"    │ 25th         │ ${mc['p25']:>9,.0f}  │")
    print(f"    │ MEDIAN (50%) │ ${mc['p50']:>9,.0f}  │")
    print(f"    │ 75th         │ ${mc['p75']:>9,.0f}  │")
    print(f"    │ 90th (good)  │ ${mc['p90']:>9,.0f}  │")
    print(f"    │ 95th         │ ${mc['p95']:>9,.0f}  │")
    print(f"    │ Best case    │ ${mc['max']:>9,.0f}  │")
    print(f"    └──────────────┴─────────────┘")
    print(f"    Hit ${TARGET:,}+:  {mc['hit_target']:.1f}% chance")
    print(f"    Ruin (<$20):   {mc['ruin_rate']:.1f}% chance")
    print(f"    Avg max DD:    {mc['avg_max_dd']*100:.1f}%")
    print(f"    Worst DD:      {mc['worst_dd']*100:.1f}%")


# ═══════════════════════════════════════════════════
#  WHAT RISK % IS NEEDED TO HIT $8000 IN 90 DAYS?
# ═══════════════════════════════════════════════════
print(f"\n{'═' * 70}")
print(f"  🎯 WHAT'S NEEDED TO HIT ${TARGET:,} IN 90 DAYS?")
print(f"{'═' * 70}")

# Required daily return for simple compound
import math
required_daily = (TARGET / STARTING_BALANCE) ** (1 / DAYS) - 1
print(f"\n  Required daily return: {required_daily*100:.2f}%")
print(f"  That's {required_daily*100/OBSERVED_DAILY_RETURN:.1f}× the FVG-era daily return")

# With moderate scenario expectancy per trade
mod = scenarios["MODERATE (trailing fix working)"]
mod_exp_r = (
    mod["wr"] * mod["avg_win_r"]
    + (1 - mod["wr"]) * mod["be_rate"] * mod["be_save_r"]
    - (1 - mod["wr"]) * (1 - mod["be_rate"]) * mod["avg_loss_r"]
)

# risk_pct needed: required_daily = mod_exp_r * risk_pct * trades/day
needed_risk = required_daily / (mod_exp_r * mod["trades_day"])
print(f"\n  With MODERATE stats (WR={mod['wr']*100:.0f}%, exp={mod_exp_r:.4f}R/trade):")
print(f"    Risk% needed: {needed_risk*100:.1f}% per trade")
print(f"    Current risk: {RISK_PCT*100:.0f}% → need {needed_risk/RISK_PCT:.1f}× more risk")

# Phased plan
print(f"\n  ═══ PHASED GROWTH PLAN ═══")
phases = [
    {"name": "Phase 1: Survival", "start": 94, "end": 200, "risk": 0.02, "desc": "Prove strategy works, 2% risk"},
    {"name": "Phase 2: Growth",   "start": 200, "end": 500, "risk": 0.03, "desc": "Validated, scale to 3% risk"},
    {"name": "Phase 3: Scale",    "start": 500, "end": 2000, "risk": 0.04, "desc": "Consistent profits, 4% risk"},
    {"name": "Phase 4: Push",     "start": 2000, "end": 8000, "risk": 0.05, "desc": "Aggressive but controlled, 5% risk"},
]

total_days = 0
for phase in phases:
    # Daily return estimate with moderate expectancy
    daily_ret = mod_exp_r * phase["risk"] * mod["trades_day"]
    days_needed = math.log(phase["end"] / phase["start"]) / math.log(1 + daily_ret) if daily_ret > 0 else float('inf')
    total_days += days_needed
    
    print(f"\n  {phase['name']}: ${phase['start']} → ${phase['end']}")
    print(f"    Risk: {phase['risk']*100:.0f}%  |  Daily return: ~{daily_ret*100:.2f}%")
    print(f"    Est. days: {days_needed:.0f}  |  Cumulative: {total_days:.0f} days")
    print(f"    {phase['desc']}")

print(f"\n  ═══ TOTAL ESTIMATED: {total_days:.0f} days to reach ${TARGET:,} ═══")
print(f"  (vs target: {DAYS} days)")

# Final honest assessment
print(f"\n{'═' * 70}")
print(f"  💡 HONEST ASSESSMENT")
print(f"{'═' * 70}")
print(f"""
  Starting at $94, reaching $8,000 = 85× growth.
  
  MATH REALITY:
  • At 2% fixed risk: ~{DAYS} day median is ${scenarios['CONSERVATIVE (as-is FVG data)']['wr']*100:.0f}% WR → modest growth
  • Compound growth is EXPONENTIAL — small daily % compounds fast
  • But variance is HIGH with small balance (one bad day = big % hit)
  
  KEY RISKS:
  1. Slippage/fees eat more at small sizes
  2. Daily loss limit (-2%) can pause trading for days
  3. 5 trades/day is realistic, not guaranteed (FVG zones may not fill)
  4. Market regime change can drop WR below 50%
  
  WHAT MAKES IT POSSIBLE:
  1. Compound position sizing (positions grow with balance)
  2. FVG strategy has proven edge on selected symbols
  3. Trailing fix recovers some losing trades
  4. Gradual risk scaling as confidence grows
  
  REALISTIC EXPECTATIONS:
  • 90 days at 2% fixed risk: ${simple_90d:,.0f} (median)
  • With risk scaling: significantly more but higher variance
  • $8,000 requires either higher risk% OR more time
""")
