#!/usr/bin/env python3
"""
GoldasT Bot v2 - Main Entry Point

Usage:
    python main.py                    # Use default config.yaml
    python main.py -c custom.yaml     # Use custom config
    python main.py --help             # Show help
"""

import asyncio
import sys
import logging

# Ensure src is in path
sys.path.insert(0, '.')

from src.bot import main


if __name__ == "__main__":
    asyncio.run(main())
