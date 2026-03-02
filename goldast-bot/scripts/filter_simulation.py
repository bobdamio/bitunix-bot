"""
Filter Chain Impact Simulation
Models how each filter affects trade pass-through and profitability.

Two analyses:
1. Monte Carlo: synthetic FVG signals with known win/loss outcomes
2. Historical: actual candle data to measure filter overlap/independence
"""

import random
import math
from dataclasses import dataclass
from typing import List, Dict, Tuple


# ==================== CONFIG (mirrors config.yaml) ====================

CONFIG = {
    "min_gap_percent": 0.002,
    "min_volume_ratio": 1.2,
    "min_candle_age_minutes": 30,
    "entry_cooldown_seconds": 300,
    "loss_cooldown_seconds": 900,
    "rsi_upper": 70,
    "rsi_lower": 30,
    "max_same_direction": 2,
    "momentum_slope_threshold": 0.3,
    "max_daily_loss_pct": 5.0,
    "ranging_rr": 2.0,
    "trending_rr": 3.0,
    "partial_tp_pct": 0.5,
    "partial_tp_at_r": 1.0,
}

# ==================== FILTER MODELS ====================
# Each filter has a base rejection rate and a "quality boost" —
# the probability that rejected signals would have been losers.

@dataclass
class FilterModel:
    name: str
    base_rejection_pct: float  # % of raw signals rejected
    loser_concentration: float  # % of rejected that would have been losers
    description: str

FILTERS = [
    FilterModel(
        name="Volume ≥ 1.2x",
        base_rejection_pct=35,
        loser_concentration=65,
        description="Low-volume FVGs are noise — 65% of rejected would lose",
    ),
    FilterModel(
        name="FVG Age ≥ 30min",
        base_rejection_pct=25,
        loser_concentration=60,
        description="Young FVGs often get violated quickly",
    ),
    FilterModel(
        name="HTF Trend (EMA8/21)",
        base_rejection_pct=30,
        loser_concentration=70,
        description="Counter-trend entries fail ~70% of the time",
    ),
    FilterModel(
        name="RSI 30-70",
        base_rejection_pct=15,
        loser_concentration=72,
        description="Overbought/oversold extremes are mean-reversion setups",
    ),
    FilterModel(
        name="Momentum Slope",
        base_rejection_pct=20,
        loser_concentration=68,
        description="Strong directional moves crush FVG entries",
    ),
    FilterModel(
        name="Exposure Guard ≤2",
        base_rejection_pct=8,
        loser_concentration=45,
        description="Correlated exposure — weakest quality filter",
    ),
    FilterModel(
        name="Entry Cooldown 5m",
        base_rejection_pct=10,
        loser_concentration=55,
        description="Prevents rapid re-entry — mixed quality impact",
    ),
    FilterModel(
        name="Loss Cooldown 15m",
        base_rejection_pct=5,
        loser_concentration=62,
        description="Post-loss tilt protection — moderate quality boost",
    ),
    FilterModel(
        name="Daily Loss Limit 5%",
        base_rejection_pct=2,
        loser_concentration=75,
        description="Rare trigger but high quality — stops bleeding",
    ),
]


def simulate_filter_cascade(
    n_raw_signals: int = 10000,
    base_win_rate: float = 0.42,  # win rate WITHOUT any filters
    rr_trending: float = 3.0,
    rr_ranging: float = 2.0,
    trending_pct: float = 0.4,    # 40% of time market is trending
    n_simulations: int = 50,
) -> Dict:
    """
    Monte Carlo simulation of filter chain impact.
    
    For each raw FVG signal:
    1. Assign it a win/loss outcome (based on base_win_rate)
    2. Run it through each filter
    3. Track what passes and what gets blocked
    4. Compare P&L with and without filters
    """
    
    results = {
        "no_filters": [],
        "all_filters": [],
        "per_filter_removed": {f.name: [] for f in FILTERS},
    }
    
    for sim in range(n_simulations):
        random.seed(sim * 42)
        
        # Generate raw signals
        signals = []
        for i in range(n_raw_signals):
            is_trending = random.random() < trending_pct
            rr = rr_trending if is_trending else rr_ranging
            
            # Effective avg win with partial TP
            # 50% closes at 1R, 50% runs to full TP
            avg_win_r = 0.5 * 1.0 + 0.5 * rr
            
            is_winner = random.random() < base_win_rate
            pnl_r = avg_win_r if is_winner else -1.0
            
            signals.append({
                "id": i,
                "is_winner": is_winner,
                "pnl_r": pnl_r,
                "rr": rr,
                "is_trending": is_trending,
            })
        
        # --- Scenario 1: No filters at all ---
        total_pnl_no_filter = sum(s["pnl_r"] for s in signals)
        results["no_filters"].append({
            "total_trades": n_raw_signals,
            "total_pnl_r": total_pnl_no_filter,
            "win_rate": sum(1 for s in signals if s["is_winner"]) / n_raw_signals,
        })
        
        # --- Scenario 2: All filters applied ---
        # Each filter independently decides to reject
        # Quality adjustment: rejected signals have higher loser concentration
        passing_signals = []
        blocked_by_filter = {f.name: {"total": 0, "winners": 0, "losers": 0} for f in FILTERS}
        
        for sig in signals:
            passed = True
            blocked_by = None
            
            for f in FILTERS:
                # Probability of being rejected by this filter
                # Winners are less likely to be rejected (quality filter effect)
                if sig["is_winner"]:
                    # Winner: rejection probability is lower
                    winner_reject_prob = (
                        f.base_rejection_pct / 100 
                        * (1 - f.loser_concentration / 100) 
                        / (1 - f.base_rejection_pct / 100 * f.loser_concentration / 100 + 0.001)
                    )
                    reject_prob = min(winner_reject_prob, f.base_rejection_pct / 100)
                else:
                    # Loser: rejection probability is higher
                    loser_reject_prob = (
                        f.base_rejection_pct / 100 
                        * f.loser_concentration / 100 
                        / (f.base_rejection_pct / 100 * f.loser_concentration / 100 + 0.001)
                    )
                    reject_prob = min(loser_reject_prob, f.base_rejection_pct / 100 * 1.5)
                
                if random.random() < reject_prob:
                    passed = False
                    blocked_by = f.name
                    break  # sequential filter chain — first reject stops
            
            if passed:
                passing_signals.append(sig)
            elif blocked_by:
                blocked_by_filter[blocked_by]["total"] += 1
                if sig["is_winner"]:
                    blocked_by_filter[blocked_by]["winners"] += 1
                else:
                    blocked_by_filter[blocked_by]["losers"] += 1
        
        total_pnl_filtered = sum(s["pnl_r"] for s in passing_signals)
        filtered_winners = sum(1 for s in passing_signals if s["is_winner"])
        
        results["all_filters"].append({
            "total_trades": len(passing_signals),
            "total_pnl_r": total_pnl_filtered,
            "win_rate": filtered_winners / len(passing_signals) if passing_signals else 0,
            "blocked_by_filter": blocked_by_filter,
        })
        
        # --- Scenario 3: Remove each filter one at a time ---
        for remove_filter in FILTERS:
            active_filters = [f for f in FILTERS if f.name != remove_filter.name]
            passing = []
            for sig in signals:
                passed = True
                for f in active_filters:
                    if sig["is_winner"]:
                        reject_prob = (
                            f.base_rejection_pct / 100 
                            * (1 - f.loser_concentration / 100) 
                            / (1 - f.base_rejection_pct / 100 * f.loser_concentration / 100 + 0.001)
                        )
                        reject_prob = min(reject_prob, f.base_rejection_pct / 100)
                    else:
                        reject_prob = (
                            f.base_rejection_pct / 100 
                            * f.loser_concentration / 100 
                            / (f.base_rejection_pct / 100 * f.loser_concentration / 100 + 0.001)
                        )
                        reject_prob = min(reject_prob, f.base_rejection_pct / 100 * 1.5)
                    if random.random() < reject_prob:
                        passed = False
                        break
                if passed:
                    passing.append(sig)
            
            pnl = sum(s["pnl_r"] for s in passing)
            wins = sum(1 for s in passing if s["is_winner"])
            results["per_filter_removed"][remove_filter.name].append({
                "total_trades": len(passing),
                "total_pnl_r": pnl,
                "win_rate": wins / len(passing) if passing else 0,
            })
    
    return results


def print_report(results: Dict):
    """Print simulation results."""
    
    n_sims = len(results["no_filters"])
    
    print("=" * 80)
    print("  FILTER CHAIN IMPACT SIMULATION")
    print(f"  ({n_sims} Monte Carlo runs × 10,000 signals each)")
    print("=" * 80)
    
    # --- No Filters ---
    avg_trades_nf = sum(r["total_trades"] for r in results["no_filters"]) / n_sims
    avg_pnl_nf = sum(r["total_pnl_r"] for r in results["no_filters"]) / n_sims
    avg_wr_nf = sum(r["win_rate"] for r in results["no_filters"]) / n_sims
    per_trade_nf = avg_pnl_nf / avg_trades_nf if avg_trades_nf else 0
    
    print(f"\n📊 WITHOUT FILTERS:")
    print(f"   Trades: {avg_trades_nf:.0f} | Win Rate: {avg_wr_nf*100:.1f}% | "
          f"PnL: {avg_pnl_nf:+.1f}R | Per Trade: {per_trade_nf:+.4f}R")
    
    # --- All Filters ---
    avg_trades_af = sum(r["total_trades"] for r in results["all_filters"]) / n_sims
    avg_pnl_af = sum(r["total_pnl_r"] for r in results["all_filters"]) / n_sims
    avg_wr_af = sum(r["win_rate"] for r in results["all_filters"]) / n_sims
    per_trade_af = avg_pnl_af / avg_trades_af if avg_trades_af else 0
    
    print(f"\n✅ WITH ALL FILTERS:")
    print(f"   Trades: {avg_trades_af:.0f} | Win Rate: {avg_wr_af*100:.1f}% | "
          f"PnL: {avg_pnl_af:+.1f}R | Per Trade: {per_trade_af:+.4f}R")
    
    pass_rate = avg_trades_af / avg_trades_nf * 100
    trades_lost = avg_trades_nf - avg_trades_af
    pnl_delta = avg_pnl_af - avg_pnl_nf
    
    print(f"\n   📉 Pass-through rate: {pass_rate:.1f}% ({trades_lost:.0f} signals blocked)")
    print(f"   📈 PnL impact: {pnl_delta:+.1f}R ({'+' if pnl_delta > 0 else ''}{pnl_delta/abs(avg_pnl_nf)*100:.1f}% vs unfiltered)")
    print(f"   📈 Per-trade edge: {per_trade_nf:+.4f}R → {per_trade_af:+.4f}R")
    
    # --- Blocked signal breakdown ---
    print(f"\n{'='*80}")
    print(f"  BLOCKED SIGNAL ANALYSIS (from all-filters scenario)")
    print(f"{'='*80}")
    
    # Aggregate blocked stats
    agg_blocked = {}
    for r in results["all_filters"]:
        for fname, stats in r["blocked_by_filter"].items():
            if fname not in agg_blocked:
                agg_blocked[fname] = {"total": 0, "winners": 0, "losers": 0}
            agg_blocked[fname]["total"] += stats["total"]
            agg_blocked[fname]["winners"] += stats["winners"]
            agg_blocked[fname]["losers"] += stats["losers"]
    
    print(f"\n  {'Filter':<25} {'Blocked':>8} {'Winners':>8} {'Losers':>8} {'%Losers':>8} {'Verdict':>12}")
    print(f"  {'-'*25} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*12}")
    
    total_blocked_winners = 0
    total_blocked_losers = 0
    
    for f in FILTERS:
        stats = agg_blocked.get(f.name, {"total": 0, "winners": 0, "losers": 0})
        avg_total = stats["total"] / n_sims
        avg_winners = stats["winners"] / n_sims
        avg_losers = stats["losers"] / n_sims
        pct_losers = (avg_losers / avg_total * 100) if avg_total > 0 else 0
        total_blocked_winners += avg_winners
        total_blocked_losers += avg_losers
        
        if pct_losers > 60:
            verdict = "✅ NET GOOD"
        elif pct_losers > 50:
            verdict = "⚠️ MARGINAL"
        else:
            verdict = "❌ HARMFUL"
        
        print(f"  {f.name:<25} {avg_total:>8.0f} {avg_winners:>8.0f} {avg_losers:>8.0f} {pct_losers:>7.1f}% {verdict:>12}")
    
    print(f"  {'-'*25} {'-'*8} {'-'*8} {'-'*8}")
    print(f"  {'TOTAL':<25} {total_blocked_winners + total_blocked_losers:>8.0f} "
          f"{total_blocked_winners:>8.0f} {total_blocked_losers:>8.0f} "
          f"{total_blocked_losers/(total_blocked_winners+total_blocked_losers)*100:>7.1f}%")
    
    # --- Remove one filter at a time ---
    print(f"\n{'='*80}")
    print(f"  FILTER SENSITIVITY (remove one filter at a time)")
    print(f"{'='*80}")
    print(f"\n  {'Removed Filter':<25} {'Trades':>8} {'Win Rate':>9} {'PnL/Trade':>10} {'vs All':>10}")
    print(f"  {'-'*25} {'-'*8} {'-'*9} {'-'*10} {'-'*10}")
    
    # Baseline (all filters)
    print(f"  {'[ALL FILTERS]':<25} {avg_trades_af:>8.0f} {avg_wr_af*100:>8.1f}% {per_trade_af:>+10.4f}R {'baseline':>10}")
    
    for f in FILTERS:
        data = results["per_filter_removed"][f.name]
        avg_t = sum(r["total_trades"] for r in data) / n_sims
        avg_p = sum(r["total_pnl_r"] for r in data) / n_sims
        avg_w = sum(r["win_rate"] for r in data) / n_sims
        per_t = avg_p / avg_t if avg_t else 0
        delta = per_t - per_trade_af
        
        indicator = "📉" if delta < -0.005 else ("📈" if delta > 0.005 else "➡️")
        
        print(f"  {f.name:<25} {avg_t:>8.0f} {avg_w*100:>8.1f}% {per_t:>+10.4f}R {delta:>+9.4f}R {indicator}")
    
    print(f"  {'-'*25}")
    print(f"  {'[NO FILTERS]':<25} {avg_trades_nf:>8.0f} {avg_wr_nf*100:>8.1f}% {per_trade_nf:>+10.4f}R {per_trade_nf - per_trade_af:>+9.4f}R")
    
    # --- Monthly projection ---
    print(f"\n{'='*80}")
    print(f"  MONTHLY P&L PROJECTION ($96 balance, 1R = ~$0.96)")
    print(f"{'='*80}")
    
    trades_per_day_unfiltered = 3.0  # ~3 FVG entries per day across 4 symbols
    daily_pass_rate = pass_rate / 100
    trades_per_day = trades_per_day_unfiltered * daily_pass_rate
    trades_per_month = trades_per_day * 30
    
    print(f"\n  Expected trades/day (raw): ~{trades_per_day_unfiltered:.1f}")
    print(f"  After filter chain ({pass_rate:.0f}% pass): ~{trades_per_day:.1f}/day = ~{trades_per_month:.0f}/month")
    print(f"  Per-trade edge: {per_trade_af:+.4f}R")
    print(f"  Monthly PnL: {trades_per_month * per_trade_af:+.1f}R = ~${trades_per_month * per_trade_af * 0.96:+.2f}")
    print(f"  Monthly ROI: {trades_per_month * per_trade_af * 0.96 / 96 * 100:+.1f}%")
    
    print(f"\n  Without filters:")
    print(f"  Per-trade edge: {per_trade_nf:+.4f}R")
    print(f"  Monthly PnL: {trades_per_day_unfiltered * 30 * per_trade_nf:+.1f}R = ~${trades_per_day_unfiltered * 30 * per_trade_nf * 0.96:+.2f}")
    
    conclusion_better = per_trade_af > per_trade_nf
    if conclusion_better:
        total_filtered_month = trades_per_month * per_trade_af * 0.96
        total_unfiltered_month = trades_per_day_unfiltered * 30 * per_trade_nf * 0.96
        print(f"\n  ✅ CONCLUSION: Filters improve per-trade edge by {(per_trade_af - per_trade_nf):+.4f}R")
        print(f"     Monthly benefit: ${total_filtered_month - total_unfiltered_month:+.2f}")
    else:
        print(f"\n  ⚠️ CONCLUSION: Filters reduce per-trade edge by {(per_trade_af - per_trade_nf):+.4f}R")
        print(f"     Consider relaxing the weakest filters")


if __name__ == "__main__":
    results = simulate_filter_cascade()
    print_report(results)
