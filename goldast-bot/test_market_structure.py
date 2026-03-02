
import logging
import sys
from datetime import datetime

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

from src.models import Candle
from src.market_structure import MarketStructure, TrendDirection

def create_candle(ts, close, high, low):
    return Candle(
        timestamp=ts,
        open=close, # simplify
        close=close,
        high=high,
        low=low,
        volume=100
    )

def test_uptrend_bos():
    print("\n--- Testing Uptrend BOS ---")
    ms = MarketStructure("BTCUSDT")
    candles = []
    
    # 1. Create a Swing High at 100
    # Pattern: 90, 95, 100 (High), 95, 90
    candles.append(create_candle(1, 90, 92, 88))
    candles.append(create_candle(2, 95, 97, 93))
    candles.append(create_candle(3, 98, 100, 96)) # High
    candles.append(create_candle(4, 95, 97, 93))
    candles.append(create_candle(5, 90, 92, 88))
    
    for c in candles:
        ms.update([c]) # Feed one by one (simulating live) - actually update needs list
    
    # Feed the whole history so far
    ms.update(candles)
    
    print(f"Swing Highs: {len(ms.swing_highs)}")
    if ms.swing_highs:
        print(f"Last High: {ms.swing_highs[-1].price}")
    
    # 2. Break the High (BOS)
    print("Breaking structure...")
    break_candle = create_candle(6, 105, 106, 101) # Close 105 > 100
    candles.append(break_candle)
    ms.update(candles)
    
    print(f"New Trend: {ms.trend}")
    print(f"Last BOS Price: {ms.last_bos_price}")
    
    if ms.trend == TrendDirection.BULLISH:
        print("✅ PASSED: Trend is BULLISH after breaking High")
    else:
        print("❌ FAILED: Trend should be BULLISH")

def test_premium_discount():
    print("\n--- Testing Premium/Discount ---")
    ms = MarketStructure("ETHUSDT", lookback=10)
    candles = []
    
    # Create range 100 - 200
    candles.append(create_candle(1, 150, 200, 150)) # High 200
    candles.append(create_candle(2, 100, 150, 100)) # Low 100
    
    # Fill buffer
    for i in range(8):
        candles.append(create_candle(3+i, 150, 160, 140))
        
    ms.update(candles)
    
    print(f"Range: {ms.range_low} - {ms.range_high}")
    
    # Test 110 (Discount)
    zone, pct = ms.get_premium_discount(110)
    print(f"Price 110: {zone} ({pct:.2f})")
    if zone == "DISCOUNT":
        print("✅ PASSED: 110 is Discount")
    else:
        print("❌ FAILED: 110 should be Discount")

    # Test 190 (Premium)
    zone, pct = ms.get_premium_discount(190)
    print(f"Price 190: {zone} ({pct:.2f})")
    if zone == "PREMIUM":
        print("✅ PASSED: 190 is Premium")
    else:
        print("❌ FAILED: 190 should be Premium")

if __name__ == "__main__":
    test_uptrend_bos()
    test_premium_discount()
