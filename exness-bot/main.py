#!/usr/bin/env python3
"""
Exness Bot v1.0 - Main Entry Point

Trading Strategy: FVG/IFVG + Supply/Demand Zones + Multi-Timeframe Analysis
Platform: Exness MT5

Usage:
    python main.py                    # Use default config.yaml
    python main.py -c custom.yaml     # Use custom config
    python main.py --help             # Show help
"""

import sys
import argparse

sys.path.insert(0, '.')

from src.bot import main


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Exness Bot v1.0 - MT5 Trading Bot")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config file")
    args = parser.parse_args()

    main(config_path=args.config)
