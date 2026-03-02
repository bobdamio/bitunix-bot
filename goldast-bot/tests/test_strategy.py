"""
Basic tests for GoldasT Bot v2

Run: python -m pytest tests/ -v
"""

import pytest
from datetime import datetime

import sys
sys.path.insert(0, '.')

from src.models import Candle, FVG, FVGType, TradeDirection
from src.fvg_detector import FVGDetector
from src.tpsl_calculator import TPSLCalculator
from src.config import FVGConfig, TPSLConfig


class TestFVGDetector:
    """Test FVG detection logic"""
    
    def setup_method(self):
        """Setup test fixtures"""
        self.config = FVGConfig(
            timeframe="5m",
            min_gap_percent=0.001,
            min_strength_score=0.3,
            entry_zone_min=0.5,
            entry_zone_max=0.8,
        )
        self.detector = FVGDetector(self.config)
    
    def test_detect_bullish_fvg(self):
        """Test bullish FVG detection"""
        # Bullish FVG: C1.high < C3.low (gap up)
        c1 = Candle(
            timestamp=datetime.now(),
            open=100, high=101, low=99, close=100.5, volume=1000
        )
        c2 = Candle(
            timestamp=datetime.now(),
            open=101, high=105, low=100.5, close=104, volume=2000
        )
        c3 = Candle(
            timestamp=datetime.now(),
            open=104, high=106, low=103, close=105, volume=1500
        )
        
        fvg = self.detector.detect_fvg(c1, c2, c3)
        
        assert fvg is not None
        assert fvg.type == FVGType.BULLISH
        assert fvg.gap_high == 103  # C3.low
        assert fvg.gap_low == 101   # C1.high
    
    def test_detect_bearish_fvg(self):
        """Test bearish FVG detection"""
        # Bearish FVG: C1.low > C3.high (gap down)
        c1 = Candle(
            timestamp=datetime.now(),
            open=105, high=106, low=104, close=104.5, volume=1000
        )
        c2 = Candle(
            timestamp=datetime.now(),
            open=104, high=104.5, low=100, close=100.5, volume=2000
        )
        c3 = Candle(
            timestamp=datetime.now(),
            open=100, high=102, low=99, close=101, volume=1500
        )
        
        fvg = self.detector.detect_fvg(c1, c2, c3)
        
        assert fvg is not None
        assert fvg.type == FVGType.BEARISH
        assert fvg.gap_high == 104  # C1.low
        assert fvg.gap_low == 102   # C3.high
    
    def test_no_fvg_when_no_gap(self):
        """Test no FVG when candles overlap"""
        c1 = Candle(
            timestamp=datetime.now(),
            open=100, high=102, low=99, close=101, volume=1000
        )
        c2 = Candle(
            timestamp=datetime.now(),
            open=101, high=103, low=100, close=102, volume=1000
        )
        c3 = Candle(
            timestamp=datetime.now(),
            open=102, high=104, low=101, close=103, volume=1000
        )
        
        fvg = self.detector.detect_fvg(c1, c2, c3)
        
        assert fvg is None
    
    def test_entry_conditions_bullish(self):
        """Test entry condition check for bullish FVG"""
        fvg = FVG(
            type=FVGType.BULLISH,
            gap_high=105.0,
            gap_low=100.0,
            midpoint=102.5,
            formed_at=datetime.now(),
            formation_price=103.0,
            candle_index=0,
        )
        
        # Price at 50% fill (in zone)
        signal = self.detector.check_entry_conditions(fvg, 102.5)
        assert signal is not None
        assert signal.direction == TradeDirection.LONG
        
        # Price at 80% fill (in zone)
        signal = self.detector.check_entry_conditions(fvg, 101.0)
        assert signal is not None
        
        # Price below zone (too deep)
        signal = self.detector.check_entry_conditions(fvg, 100.5)
        assert signal is None
        
        # Price above zone (not filled enough)
        signal = self.detector.check_entry_conditions(fvg, 104.0)
        assert signal is None


class TestTPSLCalculator:
    """Test TP/SL calculation"""
    
    def setup_method(self):
        """Setup test fixtures"""
        self.config = TPSLConfig(
            sl_multiplier=0.618,
            tp_multiplier=1.618,
            use_dynamic_tiers=True,
        )
        self.calculator = TPSLCalculator(self.config)
    
    def test_calculate_long_tpsl(self):
        """Test TP/SL for long position"""
        fvg = FVG(
            type=FVGType.BULLISH,
            gap_high=105.0,
            gap_low=100.0,
            midpoint=102.5,
            formed_at=datetime.now(),
            formation_price=103.0,
            candle_index=0,
        )
        
        entry_price = 102.0
        levels = self.calculator.calculate(fvg, entry_price, TradeDirection.LONG)
        
        # SL below entry (for long)
        assert levels.sl_price < entry_price
        
        # TP above entry (for long)
        assert levels.tp_price > entry_price
        
        # TP should be further than SL (positive R:R)
        tp_distance = levels.tp_price - entry_price
        sl_distance = entry_price - levels.sl_price
        assert tp_distance > sl_distance
    
    def test_calculate_short_tpsl(self):
        """Test TP/SL for short position"""
        fvg = FVG(
            type=FVGType.BEARISH,
            gap_high=105.0,
            gap_low=100.0,
            midpoint=102.5,
            formed_at=datetime.now(),
            formation_price=102.0,
            candle_index=0,
        )
        
        entry_price = 103.0
        levels = self.calculator.calculate(fvg, entry_price, TradeDirection.SHORT)
        
        # SL above entry (for short)
        assert levels.sl_price > entry_price
        
        # TP below entry (for short)
        assert levels.tp_price < entry_price


class TestPositionSizer:
    """Test position sizing"""
    
    def test_risk_based_sizing(self):
        """Test that position is sized based on risk"""
        from src.position_sizer import PositionSizer
        from src.config import PositionConfig
        
        config = PositionConfig(
            risk_percent=0.01,  # 1% risk
            max_position_usd=1000,
            min_position_usd=10,
            max_balance_percent=0.5,
        )
        sizer = PositionSizer(config)
        
        # With 1% risk and 1% SL distance
        size = sizer.calculate(
            balance=1000,
            entry_price=50000,
            sl_distance_percent=0.01,
            leverage=10,
            symbol="BTCUSDT",
        )
        
        # Risk = 1000 * 0.01 = $10
        # Position = 10 / 0.01 = $1000 (capped by max)
        assert size.quantity_usd <= config.max_position_usd


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
