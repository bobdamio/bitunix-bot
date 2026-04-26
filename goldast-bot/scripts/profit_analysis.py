#!/usr/bin/env python3
"""
Comprehensive profit analysis and parameter optimization for GoldasT Bot.
Analyzes trade history, identifies patterns, and models optimal parameters.
"""

import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean, median, stdev

def load_trades(path="data/trade_history.csv"):
    trades = []
    with open(path, 'r') as f:
        for row in csv.DictReader(f):
            row['pnl'] = float(row['realizedPNL'])
            row['fee_val'] = float(row['fee'])
            row['entry'] = float(row['entryPrice'])
            row['close_price'] = float(row['closePrice'])
            row['lev'] = int(row['leverage'])
            row['qty_val'] = float(row['qty'])
            row['ts'] = int(row['ctime']) / 1000
            row['dt'] = datetime.utcfromtimestamp(row['ts'])
            row['close_ts'] = int(row['mtime']) / 1000
            row['close_dt'] = datetime.utcfromtimestamp(row['close_ts'])
            row['duration_min'] = (row['close_ts'] - row['ts']) / 60
            row['notional'] = row['entry'] * row['qty_val']
            row['gross_pnl'] = row['pnl'] + row['fee_val']  # PnL before fees
            row['pnl_pct'] = row['pnl'] / row['notional'] * 100 if row['notional'] > 0 else 0
            row['is_win'] = row['pnl'] > 0
            trades.append(row)
    return trades

def analyze_by_period(trades, period_days=7):
    """Analyze performance by time periods."""
    if not trades:
        return
    start = trades[0]['dt']
    end = trades[-1]['dt']
    current = start
    print(f"\n{'='*80}")
    print(f"PERIOD ANALYSIS (every {period_days} days)")
    print(f"{'='*80}")
    print(f"{'Period':<20} {'Trades':>6} {'Wins':>5} {'WR%':>6} {'PnL':>10} {'Fees':>8} {'Avg PnL':>8} {'Avg Hold':>8}")
    print("-" * 80)
    while current <= end:
        period_end = current + timedelta(days=period_days)
        period_trades = [t for t in trades if current <= t['dt'] < period_end]
        if period_trades:
            wins = sum(1 for t in period_trades if t['is_win'])
            pnl = sum(t['pnl'] for t in period_trades)
            fees = sum(t['fee_val'] for t in period_trades)
            wr = wins / len(period_trades) * 100
            avg_pnl = pnl / len(period_trades)
            avg_hold = mean(t['duration_min'] for t in period_trades)
            label = current.strftime('%m/%d') + '-' + period_end.strftime('%m/%d')
            print(f"{label:<20} {len(period_trades):>6} {wins:>5} {wr:>5.1f}% {pnl:>+9.2f} {fees:>7.2f} {avg_pnl:>+7.3f} {avg_hold:>7.0f}m")
        current = period_end

def analyze_by_symbol_recent(trades, days=14):
    """Analyze symbol performance over recent period."""
    cutoff = trades[-1]['dt'] - timedelta(days=days) if trades else datetime.now()
    recent = [t for t in trades if t['dt'] >= cutoff]
    
    print(f"\n{'='*80}")
    print(f"SYMBOL PERFORMANCE (last {days} days, {len(recent)} trades)")
    print(f"{'='*80}")
    
    symbol_stats = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0.0, 'fees': 0.0, 
                                         'count': 0, 'durations': [], 'pnls': [], 'gross': 0.0})
    for t in recent:
        s = symbol_stats[t['symbol']]
        s['count'] += 1
        s['pnl'] += t['pnl']
        s['fees'] += t['fee_val']
        s['gross'] += t['gross_pnl']
        s['durations'].append(t['duration_min'])
        s['pnls'].append(t['pnl'])
        if t['is_win']:
            s['wins'] += 1
        else:
            s['losses'] += 1
    
    sorted_syms = sorted(symbol_stats.items(), key=lambda x: x[1]['pnl'], reverse=True)
    print(f"{'Symbol':<14} {'Trades':>6} {'W':>3} {'L':>3} {'WR%':>6} {'PnL':>9} {'Fees':>7} {'Gross':>8} {'AvgHold':>7} {'AvgPnL':>8}")
    print("-" * 85)
    for sym, s in sorted_syms:
        wr = s['wins'] / s['count'] * 100
        avg_hold = mean(s['durations'])
        avg_pnl = s['pnl'] / s['count']
        print(f"{sym:<14} {s['count']:>6} {s['wins']:>3} {s['losses']:>3} {wr:>5.1f}% {s['pnl']:>+8.2f} {s['fees']:>6.2f} {s['gross']:>+7.2f} {avg_hold:>6.0f}m {avg_pnl:>+7.3f}")

def analyze_hold_time(trades):
    """Analyze profitability vs hold time."""
    print(f"\n{'='*80}")
    print(f"HOLD TIME ANALYSIS")
    print(f"{'='*80}")
    
    buckets = [
        ("< 5 min", 0, 5),
        ("5-15 min", 5, 15),
        ("15-30 min", 15, 30),
        ("30-60 min", 30, 60),
        ("1-2 hours", 60, 120),
        ("2-4 hours", 120, 240),
        ("4-8 hours", 240, 480),
        ("8+ hours", 480, 99999),
    ]
    
    print(f"{'Duration':<12} {'Trades':>6} {'Wins':>5} {'WR%':>6} {'PnL':>10} {'AvgPnL':>8} {'Fees':>8}")
    print("-" * 60)
    for label, lo, hi in buckets:
        bucket_trades = [t for t in trades if lo <= t['duration_min'] < hi]
        if bucket_trades:
            wins = sum(1 for t in bucket_trades if t['is_win'])
            pnl = sum(t['pnl'] for t in bucket_trades)
            fees = sum(t['fee_val'] for t in bucket_trades)
            wr = wins / len(bucket_trades) * 100
            avg = pnl / len(bucket_trades)
            print(f"{label:<12} {len(bucket_trades):>6} {wins:>5} {wr:>5.1f}% {pnl:>+9.2f} {avg:>+7.3f} {fees:>7.2f}")

def analyze_rr_distribution(trades):
    """Analyze R:R distribution of wins and losses."""
    print(f"\n{'='*80}")
    print(f"R:R DISTRIBUTION ANALYSIS")
    print(f"{'='*80}")
    
    wins = [t for t in trades if t['is_win']]
    losses = [t for t in trades if not t['is_win']]
    
    if wins:
        win_pnls = [t['pnl'] for t in wins]
        print(f"WINS ({len(wins)}):")
        print(f"  Mean: ${mean(win_pnls):.3f}")
        print(f"  Median: ${median(win_pnls):.3f}")
        print(f"  Max: ${max(win_pnls):.3f}")
        print(f"  Min: ${min(win_pnls):.3f}")
        if len(win_pnls) > 1:
            print(f"  Stdev: ${stdev(win_pnls):.3f}")
        # Distribution
        for threshold in [0.10, 0.25, 0.50, 1.00, 1.50, 2.00, 3.00]:
            count = sum(1 for p in win_pnls if p >= threshold)
            pct = count / len(win_pnls) * 100
            print(f"  >= ${threshold:.2f}: {count} ({pct:.1f}%)")
    
    if losses:
        loss_pnls = [t['pnl'] for t in losses]
        print(f"\nLOSSES ({len(losses)}):")
        print(f"  Mean: ${mean(loss_pnls):.3f}")
        print(f"  Median: ${median(loss_pnls):.3f}")
        print(f"  Worst: ${min(loss_pnls):.3f}")
        print(f"  Best: ${max(loss_pnls):.3f}")
        if len(loss_pnls) > 1:
            print(f"  Stdev: ${stdev(loss_pnls):.3f}")
    
    if wins and losses:
        avg_win = mean(t['pnl'] for t in wins)
        avg_loss = abs(mean(t['pnl'] for t in losses))
        actual_rr = avg_win / avg_loss if avg_loss > 0 else 0
        print(f"\nActual R:R: {actual_rr:.2f}:1 (avg win ${avg_win:.3f} / avg loss ${avg_loss:.3f})")
        
        # Expected value
        wr = len(wins) / len(trades)
        ev = wr * avg_win - (1 - wr) * avg_loss
        print(f"Expected Value per trade: ${ev:.4f}")
        print(f"Win Rate: {wr*100:.1f}%")
        
        # What WR needed to break even at this R:R
        be_wr = 1 / (1 + actual_rr) * 100
        print(f"Break-even WR at R:R {actual_rr:.2f}: {be_wr:.1f}%")

def analyze_fee_impact(trades):
    """Analyze how much fees are eating into profits."""
    print(f"\n{'='*80}")
    print(f"FEE IMPACT ANALYSIS")
    print(f"{'='*80}")
    
    total_pnl = sum(t['pnl'] for t in trades)
    total_fees = sum(t['fee_val'] for t in trades)
    total_gross = sum(t['gross_pnl'] for t in trades)
    
    print(f"Total PnL (net): ${total_pnl:.2f}")
    print(f"Total Fees: ${total_fees:.2f}")
    print(f"Total Gross (PnL + fees): ${total_gross:.2f}")
    print(f"Fee percentage of gross volume: {total_fees / sum(t['notional'] for t in trades) * 100:.4f}%")
    
    # If we had zero fees
    print(f"\nWithout fees: PnL would be ${total_gross:.2f} (diff: ${total_gross - total_pnl:.2f})")
    
    # Trades that were profitable before fees but net negative
    fee_killed = [t for t in trades if t['gross_pnl'] > 0 and t['pnl'] <= 0]
    print(f"Trades killed by fees (gross+ but net-): {len(fee_killed)} ({len(fee_killed)/len(trades)*100:.1f}%)")
    print(f"  PnL lost to fee-killed trades: ${sum(t['pnl'] for t in fee_killed):.2f}")

def analyze_direction_by_trend(trades):
    """Analyze LONG vs SHORT performance over time."""
    print(f"\n{'='*80}")
    print(f"DIRECTION ANALYSIS (LONG vs SHORT)")
    print(f"{'='*80}")
    
    for side in ['BUY', 'SELL']:
        side_trades = [t for t in trades if t['side'] == side]
        if not side_trades:
            continue
        label = "LONG" if side == 'BUY' else "SHORT"
        wins = sum(1 for t in side_trades if t['is_win'])
        pnl = sum(t['pnl'] for t in side_trades)
        fees = sum(t['fee_val'] for t in side_trades)
        wr = wins / len(side_trades) * 100
        avg_pnl = pnl / len(side_trades)
        
        win_trades = [t for t in side_trades if t['is_win']]
        loss_trades = [t for t in side_trades if not t['is_win']]
        avg_win = mean(t['pnl'] for t in win_trades) if win_trades else 0
        avg_loss = abs(mean(t['pnl'] for t in loss_trades)) if loss_trades else 0
        
        print(f"{label}: {len(side_trades)} trades, WR={wr:.1f}%, PnL=${pnl:.2f}, Fees=${fees:.2f}")
        print(f"  Avg win: ${avg_win:.3f}, Avg loss: ${avg_loss:.3f}, R:R: {avg_win/avg_loss:.2f}" if avg_loss > 0 else "")

def simulate_parameter_changes(trades):
    """Simulate different TP/SL/filter parameters on historical data."""
    print(f"\n{'='*80}")
    print(f"PARAMETER OPTIMIZATION SIMULATION")
    print(f"{'='*80}")
    
    # Only use last 30 days of trades for relevance
    if trades:
        cutoff = trades[-1]['dt'] - timedelta(days=30)
        recent = [t for t in trades if t['dt'] >= cutoff]
    else:
        recent = trades
    
    total_pnl = sum(t['pnl'] for t in recent)
    total_count = len(recent)
    wins = sum(1 for t in recent if t['is_win'])
    wr = wins / total_count * 100 if total_count > 0 else 0
    
    print(f"\nBaseline (last 30 days): {total_count} trades, WR={wr:.1f}%, PnL=${total_pnl:.2f}")
    
    # Simulation 1: Filter out short-duration trades (< 5 min = noise)
    short_trades = [t for t in recent if t['duration_min'] < 5]
    if short_trades:
        short_pnl = sum(t['pnl'] for t in short_trades)
        short_wr = sum(1 for t in short_trades if t['is_win']) / len(short_trades) * 100
        remaining_pnl = total_pnl - short_pnl
        print(f"\n1. Remove <5min trades: {len(short_trades)} trades removed (WR={short_wr:.0f}%)")
        print(f"   PnL saved: ${-short_pnl:.2f} → New PnL: ${remaining_pnl:.2f}")
    
    # Simulation 2: Filter out losing symbols
    sym_pnl = defaultdict(float)
    sym_count = defaultdict(int)
    for t in recent:
        sym_pnl[t['symbol']] += t['pnl']
        sym_count[t['symbol']] += 1
    
    losing_syms = {s for s, p in sym_pnl.items() if p < -1.0}
    if losing_syms:
        losing_trades = [t for t in recent if t['symbol'] in losing_syms]
        losing_pnl = sum(t['pnl'] for t in losing_trades)
        remaining_pnl = total_pnl - losing_pnl
        print(f"\n2. Remove consistently losing symbols ({len(losing_syms)}):")
        for s in sorted(losing_syms, key=lambda x: sym_pnl[x]):
            sw = sum(1 for t in recent if t['symbol'] == s and t['is_win'])
            swr = sw / sym_count[s] * 100 if sym_count[s] > 0 else 0
            print(f"   {s}: {sym_count[s]} trades, WR={swr:.0f}%, PnL=${sym_pnl[s]:.2f}")
        print(f"   PnL saved: ${-losing_pnl:.2f} → New PnL: ${remaining_pnl:.2f}")
    
    # Simulation 3: Wider SL (trades killed in <15min are probably noise-stopped)
    noise_stops = [t for t in recent if not t['is_win'] and t['duration_min'] < 15]
    if noise_stops:
        noise_pnl = sum(t['pnl'] for t in noise_stops)
        print(f"\n3. Quick SL hits (<15min): {len(noise_stops)} trades, PnL=${noise_pnl:.2f}")
        print(f"   These are likely noise wicks. Wider SL would save some.")
        # Estimate: if 30% of these would have recovered
        recovery_pct = 0.30
        saved = abs(noise_pnl) * recovery_pct
        print(f"   Est. savings if 30% recover: +${saved:.2f}")
    
    # Simulation 4: Only trade when WR by hour-of-day is > 40%
    hour_stats = defaultdict(lambda: {'wins': 0, 'count': 0, 'pnl': 0.0})
    for t in recent:
        h = t['dt'].hour
        hour_stats[h]['count'] += 1
        hour_stats[h]['pnl'] += t['pnl']
        if t['is_win']:
            hour_stats[h]['wins'] += 1
    
    print(f"\n4. HOUR OF DAY analysis:")
    print(f"   {'Hour':>4} {'Trades':>6} {'WR%':>6} {'PnL':>9}")
    profitable_hours = set()
    for h in sorted(hour_stats.keys()):
        s = hour_stats[h]
        wr_h = s['wins'] / s['count'] * 100 if s['count'] > 0 else 0
        marker = " ✓" if s['pnl'] > 0 else " ✗"
        print(f"   {h:>4} {s['count']:>6} {wr_h:>5.1f}% {s['pnl']:>+8.2f}{marker}")
        if s['pnl'] > 0:
            profitable_hours.add(h)
    
    # Filter to profitable hours only
    good_hour_trades = [t for t in recent if t['dt'].hour in profitable_hours]
    good_hour_pnl = sum(t['pnl'] for t in good_hour_trades)
    good_hour_wr = sum(1 for t in good_hour_trades if t['is_win']) / len(good_hour_trades) * 100 if good_hour_trades else 0
    print(f"   Profitable hours only: {len(good_hour_trades)} trades, WR={good_hour_wr:.1f}%, PnL=${good_hour_pnl:.2f}")
    print(f"   Improvement: ${good_hour_pnl - total_pnl:.2f}")
    
    # Simulation 5: Optimal leverage analysis
    print(f"\n5. LEVERAGE analysis:")
    for lev in sorted(set(t['lev'] for t in recent)):
        lev_trades = [t for t in recent if t['lev'] == lev]
        lev_wins = sum(1 for t in lev_trades if t['is_win'])
        lev_pnl = sum(t['pnl'] for t in lev_trades)
        lev_wr = lev_wins / len(lev_trades) * 100
        avg_win = mean(t['pnl'] for t in lev_trades if t['is_win']) if lev_wins > 0 else 0
        avg_loss = abs(mean(t['pnl'] for t in lev_trades if not t['is_win'])) if len(lev_trades) - lev_wins > 0 else 0
        print(f"   {lev}x: {len(lev_trades)} trades, WR={lev_wr:.1f}%, PnL=${lev_pnl:.2f}, AvgW=${avg_win:.3f} AvgL=${avg_loss:.3f}")

def analyze_consecutive_patterns(trades):
    """Analyze streaks and consecutive trade patterns."""
    print(f"\n{'='*80}")
    print(f"STREAK ANALYSIS")
    print(f"{'='*80}")
    
    max_win_streak = 0
    max_loss_streak = 0
    current_win_streak = 0
    current_loss_streak = 0
    
    # Track all streaks
    loss_streaks = []
    win_streaks = []
    
    for t in trades:
        if t['is_win']:
            current_win_streak += 1
            if current_loss_streak > 0:
                loss_streaks.append(current_loss_streak)
            current_loss_streak = 0
            max_win_streak = max(max_win_streak, current_win_streak)
        else:
            current_loss_streak += 1
            if current_win_streak > 0:
                win_streaks.append(current_win_streak)
            current_win_streak = 0
            max_loss_streak = max(max_loss_streak, current_loss_streak)
    
    # Capture final streak
    if current_win_streak > 0:
        win_streaks.append(current_win_streak)
    if current_loss_streak > 0:
        loss_streaks.append(current_loss_streak)
    
    print(f"Max win streak: {max_win_streak}")
    print(f"Max loss streak: {max_loss_streak}")
    print(f"Avg win streak: {mean(win_streaks):.1f}" if win_streaks else "")
    print(f"Avg loss streak: {mean(loss_streaks):.1f}" if loss_streaks else "")
    
    # PnL after loss streaks of different lengths
    print(f"\nPerformance AFTER loss streaks:")
    for streak_len in [2, 3, 4, 5]:
        # Find trades that follow a streak of streak_len losses
        after_streak_trades = []
        current_losses = 0
        for i, t in enumerate(trades):
            if not t['is_win']:
                current_losses += 1
            else:
                if current_losses >= streak_len:
                    after_streak_trades.append(t)
                current_losses = 0
        if after_streak_trades:
            after_wr = sum(1 for t in after_streak_trades if t['is_win']) / len(after_streak_trades) * 100
            after_pnl = sum(t['pnl'] for t in after_streak_trades)
            print(f"  After {streak_len}+ losses: {len(after_streak_trades)} trades, WR={after_wr:.1f}%, PnL=${after_pnl:.2f}")

def recommend_improvements(trades):
    """Generate specific recommendations based on analysis."""
    print(f"\n{'='*80}")
    print(f"RECOMMENDED IMPROVEMENTS")
    print(f"{'='*80}")
    
    # Only analyze recent trades
    if trades:
        cutoff = trades[-1]['dt'] - timedelta(days=21)
        recent = [t for t in trades if t['dt'] >= cutoff]
    else:
        recent = trades
    
    wins = [t for t in recent if t['is_win']]
    losses = [t for t in recent if not t['is_win']]
    
    if not wins or not losses:
        print("Insufficient data for recommendations")
        return
    
    avg_win = mean(t['pnl'] for t in wins)
    avg_loss = abs(mean(t['pnl'] for t in losses))
    wr = len(wins) / len(recent) * 100
    actual_rr = avg_win / avg_loss if avg_loss > 0 else 0
    total_fees = sum(t['fee_val'] for t in recent)
    
    print(f"\nCurrent (last 21d): WR={wr:.1f}%, R:R={actual_rr:.2f}, Avg Win=${avg_win:.3f}, Avg Loss=${avg_loss:.3f}")
    print(f"Total fees: ${total_fees:.2f} ({total_fees/len(recent):.3f}/trade)")
    
    recommendations = []
    
    # 1. R:R is too low
    if actual_rr < 1.0:
        recommendations.append(
            f"1. CRITICAL: R:R is {actual_rr:.2f} (<1.0). "
            f"Avg win ${avg_win:.3f} < avg loss ${avg_loss:.3f}. "
            f"Need: increase TP target OR tighten SL. "
            f"Suggested: force_close_at_r: 2.0 (from 1.5), min_rr: 2.0 (from 1.5)"
        )
    
    # 2. WR is below break-even for current R:R
    be_wr = 1 / (1 + actual_rr) * 100
    if wr < be_wr:
        diff = be_wr - wr
        recommendations.append(
            f"2. WR {wr:.1f}% is {diff:.1f}% below break-even ({be_wr:.1f}%). "
            f"Either improve WR via stricter filters OR improve R:R."
        )
    
    # 3. Fees eating profits
    fee_pct = total_fees / sum(abs(t['pnl']) + t['fee_val'] for t in recent) * 100
    if fee_pct > 30:
        recommendations.append(
            f"3. Fees are {fee_pct:.0f}% of gross PnL. "
            f"Reduce trade frequency (fewer but better trades). "
            f"Suggested: global_entry_cooldown_seconds: 1800 (from 900)"
        )
    
    # 4. Symbol-specific recommendations
    sym_pnl = defaultdict(lambda: {'pnl': 0.0, 'count': 0, 'wins': 0})
    for t in recent:
        sym_pnl[t['symbol']]['pnl'] += t['pnl']
        sym_pnl[t['symbol']]['count'] += 1
        if t['is_win']:
            sym_pnl[t['symbol']]['wins'] += 1
    
    bad_syms = [(s, d) for s, d in sym_pnl.items() 
                if d['pnl'] < -2.0 and d['count'] >= 3 and d['wins']/d['count'] < 0.40]
    if bad_syms:
        bad_list = ', '.join(f"{s} (PnL=${d['pnl']:.2f}, WR={d['wins']/d['count']*100:.0f}%)" 
                            for s, d in sorted(bad_syms, key=lambda x: x[1]['pnl']))
        recommendations.append(f"4. Remove losing symbols from core: {bad_list}")
    
    # 5. Quick stops
    quick_losses = [t for t in losses if t['duration_min'] < 10]
    if quick_losses and len(quick_losses) / len(losses) > 0.3:
        quick_pnl = sum(t['pnl'] for t in quick_losses)
        recommendations.append(
            f"5. {len(quick_losses)}/{len(losses)} losses (<10min) are noise-killed. "
            f"Lost ${abs(quick_pnl):.2f}. Wider SL buffer needed: "
            f"sl_buffer_atr_mult: 0.25 (from 0.15)"
        )
    
    # 6. Overtrading analysis
    trades_per_day = len(recent) / max(1, (recent[-1]['dt'] - recent[0]['dt']).days) if len(recent) > 1 else 0
    if trades_per_day > 6:
        recommendations.append(
            f"6. Overtrading: {trades_per_day:.1f} trades/day. "
            f"Fees compound losses. Suggested: ≤4 trades/day."
        )
    
    for r in recommendations:
        print(f"\n{r}")


def main():
    trades = load_trades()
    print(f"Loaded {len(trades)} trades from {trades[0]['dt'].strftime('%Y-%m-%d')} to {trades[-1]['dt'].strftime('%Y-%m-%d')}")
    
    analyze_by_period(trades, period_days=7)
    analyze_by_symbol_recent(trades, days=21)
    analyze_hold_time(trades)
    analyze_rr_distribution(trades)
    analyze_fee_impact(trades)
    analyze_direction_by_trend(trades)
    analyze_consecutive_patterns(trades)
    simulate_parameter_changes(trades)
    recommend_improvements(trades)


if __name__ == "__main__":
    main()
