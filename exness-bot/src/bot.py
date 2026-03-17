"""
Exness Bot - Main Bot Class
Manages the bot lifecycle: startup, main loop, shutdown.
"""

import logging
import logging.handlers
import time
import signal
import sys
from datetime import datetime
from pathlib import Path

from .config import Config, load_config
from .mt5_client import MT5Client
from .strategy_engine import StrategyEngine

logger = logging.getLogger(__name__)


class ExnessBot:
    """
    Main bot class. Handles:
    - Configuration loading
    - Logging setup
    - MT5 connection lifecycle
    - Main trading loop (poll-based, runs every ~5 seconds)
    - Graceful shutdown
    """

    def __init__(self, config: Config):
        self.config = config
        self.mt5_client = MT5Client(config.mt5, config.account)
        self.strategy = StrategyEngine(config, self.mt5_client)
        self._running = False

    def start(self) -> None:
        """Start the bot."""
        logger.info("=" * 60)
        logger.info("🤖 Exness Bot v1.0 Starting...")
        logger.info(f"   Symbols: {', '.join(self.config.symbols)}")
        logger.info(f"   Server: {self.config.mt5.server}")
        logger.info(f"   MTF: {self.config.mtf.htf_timeframe} → {self.config.mtf.mtf_timeframe} → {self.config.mtf.ltf_timeframe}")
        logger.info(f"   Risk: {self.config.position.risk_percent*100:.1f}%")
        logger.info("=" * 60)

        if not self.strategy.initialize():
            logger.error("Failed to initialize strategy — exiting")
            sys.exit(1)

        self._running = True
        self._run_loop()

    def stop(self) -> None:
        """Stop the bot gracefully."""
        logger.info("🛑 Stopping bot...")
        self._running = False
        self.mt5_client.disconnect()
        logger.info("Bot stopped")

    def _run_loop(self) -> None:
        """Main trading loop."""
        cycle_interval = 5  # seconds between cycles

        while self._running:
            try:
                cycle_start = time.time()

                # Ensure MT5 connection
                if not self.mt5_client.is_connected():
                    if not self.mt5_client.reconnect():
                        logger.error("MT5 reconnect failed — waiting 30s")
                        time.sleep(30)
                        continue

                # Run strategy cycle
                self.strategy.run_cycle()

                # Cleanup closed positions
                self.strategy.cleanup_closed_positions()

                # Sleep for remainder of cycle
                elapsed = time.time() - cycle_start
                sleep_time = max(0, cycle_interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received")
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(10)

        self.stop()


def setup_logging(config: Config) -> None:
    """Configure logging with file rotation."""
    log_dir = Path(config.logging.file).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )
    console.setFormatter(console_fmt)
    root_logger.addHandler(console)

    # File handler with rotation
    file_handler = logging.handlers.RotatingFileHandler(
        config.logging.file,
        maxBytes=config.logging.max_size_mb * 1024 * 1024,
        backupCount=config.logging.backup_count,
    )
    file_handler.setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))
    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_fmt)
    root_logger.addHandler(file_handler)


def main(config_path: str = "config.yaml") -> None:
    """Main entry point."""
    # Load config
    config = load_config(config_path)

    # Setup logging
    setup_logging(config)

    # Create and start bot
    bot = ExnessBot(config)

    # Handle SIGTERM for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Signal {signum} received")
        bot.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    bot.start()
