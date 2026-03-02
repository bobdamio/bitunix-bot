"""
Telegram Bot Control Module for GoldasT Trading Bot
Provides user interface for bot control, monitoring, and configuration
"""

import logging
import asyncio
from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    """Telegram bot configuration"""
    token: str
    allowed_users: list
    notifications_enabled: bool = True
    log_level: str = "INFO"


class TelegramBotController:
    """Main Telegram bot controller for GoldasT trading bot"""

    def __init__(self, config: TelegramConfig, bot=None):
        """
        Initialize Telegram bot controller

        Args:
            config: TelegramConfig object
            bot: Reference to main GoldasBot for control and data access
        """
        self.config = config
        self.bot = bot
        self.app: Optional[Application] = None
        self.running = False

        # Statistics tracking
        self.stats = {
            'total_commands': 0,
            'total_trades': 0,
            'total_profit': 0.0,
        }

        logger.info("✅ TelegramBotController initialized")

    async def initialize(self):
        """Initialize Telegram bot application"""
        try:
            self.app = Application.builder().token(self.config.token).build()
            self._register_handlers()
            logger.info("✅ Telegram bot initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Telegram bot: {e}")
            raise

    def _register_handlers(self):
        """Register all command and callback handlers"""
        # Command handlers
        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("help", self._handle_help))
        self.app.add_handler(CommandHandler("balance", self._handle_balance))
        self.app.add_handler(CommandHandler("pnl", self._handle_pnl))
        self.app.add_handler(CommandHandler("stats", self._handle_stats))
        self.app.add_handler(CommandHandler("status", self._handle_status))
        self.app.add_handler(CommandHandler("trades", self._handle_trades))
        self.app.add_handler(CommandHandler("positions", self._handle_positions))
        self.app.add_handler(CommandHandler("start_trading", self._handle_start_trading))
        self.app.add_handler(CommandHandler("stop_trading", self._handle_stop_trading))
        self.app.add_handler(CommandHandler("logs", self._handle_logs))

        # Callback handlers for inline buttons
        self.app.add_handler(CallbackQueryHandler(
            self._handle_callback,
            pattern=r"^(main|profile|settings|alert|trade)_.*"
        ))

        # Error handler
        self.app.add_error_handler(self._error_handler)

    def _check_authorization(self, user_id: int) -> bool:
        """Check if user is authorized to use bot"""
        return user_id in self.config.allowed_users

    def _get_main_keyboard(self) -> InlineKeyboardMarkup:
        """Get main menu keyboard"""
        keyboard = [
            [
                InlineKeyboardButton("💰 Balance", callback_data="main_balance"),
                InlineKeyboardButton("📊 PnL", callback_data="main_pnl"),
            ],
            [
                InlineKeyboardButton("📈 Stats", callback_data="main_stats"),
                InlineKeyboardButton("📋 Logs", callback_data="main_logs"),
            ],
        ]
        return InlineKeyboardMarkup(keyboard)

    # ============================================================
    # COMMAND HANDLERS
    # ============================================================

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id

        if not self._check_authorization(user_id):
            await update.message.reply_text(
                "❌ Unauthorized access. Your user ID is not in the allowed list."
            )
            logger.warning(f"Unauthorized access attempt from user {user_id}")
            return

        welcome_message = f"""
🤖 **GoldasT Trading Bot Control Panel**

Welcome, {update.effective_user.first_name}! 👋

I can help you:
• 💰 Check your balance and holdings
• 📊 View trading statistics
• 📈 Monitor bot status in real-time
• 📋 Check bot logs
• 🔔 Manage notifications

Use /help to see all available commands.
        """

        await update.message.reply_text(
            welcome_message,
            reply_markup=self._get_main_keyboard(),
            parse_mode="Markdown"
        )

    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        user_id = update.effective_user.id
        if not self._check_authorization(user_id):
            return

        help_text = """
*Available Commands:*

💰 *Balance & PnL:*
/balance - Account overview + open positions
/pnl - Daily PnL summary

📊 *Statistics:*
/stats - Trading statistics
/trades - Show recent trades
/positions - View open positions

🎯 *Trading Control:*
/start\_trading - Start the bot
/stop\_trading - Stop the bot
/status - Check bot status

📋 *Other:*
/logs - View recent logs
/help - Show this message
        """

        await update.message.reply_text(
            help_text,
            parse_mode="Markdown"
        )

    async def _handle_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /balance command — shows equity, balance, margin, uPnL & open positions"""
        user_id = update.effective_user.id
        if not self._check_authorization(user_id):
            return

        try:
            if not (self.bot and self.bot.exchange):
                await update.message.reply_text("❌ Bot not connected")
                return

            balance = await self.bot.exchange.get_balance()
            if balance is None:
                await update.message.reply_text("⚠️ API unreachable")
                return

            positions = await self.bot.exchange.get_positions() or []

            # Equity emoji
            upnl_emoji = "🟢" if balance.unrealized_pnl >= 0 else "🔴"

            msg = (
                f"💰 *Account Overview*\n\n"
                f"Equity:  `${balance.equity:.2f}`\n"
                f"Balance: `${balance.total:.2f}`\n"
                f"Available: `${balance.available:.2f}`\n"
                f"Margin:  `${balance.margin:.2f}`\n"
                f"{upnl_emoji} Unrealized PnL: `${balance.unrealized_pnl:+.2f}`\n"
            )

            # Daily PnL from bot state
            if self.bot and hasattr(self.bot, 'state'):
                daily = self.bot.state.daily_pnl
                daily_emoji = "📈" if daily >= 0 else "📉"
                msg += f"{daily_emoji} Daily PnL: `${daily:+.2f}`\n"

            if positions:
                total_upnl = sum(float(p.get('unrealizedPNL', p.get('unrealized_pnl', 0))) for p in positions)
                msg += f"\n📊 *Open Positions* ({len(positions)})\n"
                msg += f"Total uPnL: `${total_upnl:+.2f}`\n"
                msg += "━━━━━━━━━━━━━━━━━━━━\n"

                for pos in positions:
                    sym = pos.get('symbol', '?')
                    side = pos.get('side', '?').upper()
                    qty = float(pos.get('qty', pos.get('amount', 0)))
                    entry = float(pos.get('avgOpenPrice', pos.get('entry_price', 0)))
                    upnl = float(pos.get('unrealizedPNL', pos.get('unrealized_pnl', 0)))
                    lev = pos.get('leverage', '?')
                    margin_val = float(pos.get('margin', 0))

                    # Mark price: WS cache → compute from uPnL → API field → 0
                    mark = 0.0
                    if self.bot and hasattr(self.bot, 'symbol_states'):
                        ss = self.bot.symbol_states.get(sym)
                        if ss and ss.last_price and ss.last_price > 0:
                            mark = ss.last_price
                    if mark == 0.0 and entry > 0 and qty > 0:
                        if side in ("BUY", "LONG"):
                            mark = entry + (upnl / qty)
                        else:
                            mark = entry - (upnl / qty)
                    if mark <= 0:
                        mark = float(pos.get('markPrice', pos.get('mark_price', entry)))

                    side_emoji = "🟢" if side in ("BUY", "LONG") else "🔴"
                    pnl_emoji = "✅" if upnl >= 0 else "❌"

                    # Calculate PnL %
                    pnl_pct = (upnl / margin_val * 100) if margin_val > 0 else 0

                    # Price formatting: more decimals for small-price assets
                    price_fmt = ".2f" if entry > 10 else ".4f"

                    msg += (
                        f"\n{side_emoji} *{sym}* `{side}` {lev}x\n"
                        f"  Entry: `${entry:{price_fmt}}`  →  Mark: `${mark:{price_fmt}}`\n"
                        f"  Size: `{qty}` | Margin: `${margin_val:.2f}`\n"
                        f"  {pnl_emoji} PnL: `${upnl:+.2f}` ({pnl_pct:+.1f}%)\n"
                    )
            else:
                msg += "\n📭 No open positions"

            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def _handle_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /pnl command — daily PnL summary"""
        user_id = update.effective_user.id
        if not self._check_authorization(user_id):
            return

        try:
            if not self.bot:
                await update.message.reply_text("❌ Bot not available")
                return

            state = self.bot.state
            daily = state.daily_pnl
            total = state.total_pnl
            wins = state.winning_trades
            losses = state.losing_trades
            total_trades = state.total_trades
            wr = (wins / total_trades * 100) if total_trades > 0 else 0

            daily_emoji = "📈" if daily >= 0 else "📉"
            total_emoji = "📈" if total >= 0 else "📉"

            # Uptime
            uptime_str = "N/A"
            if state.start_time:
                delta = datetime.now() - state.start_time
                total_sec = int(delta.total_seconds())
                h = total_sec // 3600
                m = (total_sec % 3600) // 60
                uptime_str = f"{h}h {m}m"

            # Open position count
            open_count = state.get_open_positions_count()

            msg = (
                f"📊 *Daily PnL Report*\n\n"
                f"{daily_emoji} Daily PnL: `${daily:+.2f}`\n"
                f"{total_emoji} Session PnL: `${total:+.2f}`\n\n"
                f"Trades today: `{total_trades}`\n"
                f"Wins: `{wins}` | Losses: `{losses}`\n"
                f"Win Rate: `{wr:.1f}%`\n\n"
                f"Open positions: `{open_count}`\n"
                f"Uptime: `{uptime_str}`\n"
            )

            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Error in /pnl: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def _handle_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stats command"""
        user_id = update.effective_user.id
        if not self._check_authorization(user_id):
            return

        try:
            if self.bot:
                state = self.bot.state
                wins = state.winning_trades
                losses = state.losing_trades
                total_trades = state.total_trades
                wr = (wins / total_trades * 100) if total_trades > 0 else 0
                open_count = state.get_open_positions_count()

                # Count per-direction from symbol states
                long_count = sum(
                    1 for s in state.symbols.values()
                    if s.has_position and s.current_order
                    and str(getattr(s.current_order.get('direction', ''), 'value', s.current_order.get('direction', ''))).upper() in ("BUY", "LONG")
                )
                short_count = open_count - long_count

                daily_emoji = "📈" if state.daily_pnl >= 0 else "📉"

                msg = (
                    f"📊 *Trading Statistics*\n\n"
                    f"Total Trades: `{total_trades}`\n"
                    f"Wins: `{wins}` | Losses: `{losses}`\n"
                    f"Win Rate: `{wr:.1f}%`\n\n"
                    f"{daily_emoji} Daily PnL: `${state.daily_pnl:+.2f}`\n"
                    f"Session PnL: `${state.total_pnl:+.2f}`\n\n"
                    f"Open: `{open_count}` (🟢 {long_count}L / 🔴 {short_count}S)\n"
                    f"Active Symbols: `{len(self.bot.config.symbols)}`\n"
                )

                await update.message.reply_text(msg, parse_mode="Markdown")
            else:
                await update.message.reply_text("❌ Bot not available")

        except Exception as e:
            logger.error(f"Error fetching stats: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        user_id = update.effective_user.id
        if not self._check_authorization(user_id):
            return

        try:
            if self.bot:
                state = self.bot.state
                running = self.bot._running
                status_emoji = "🟢" if running else "🔴"

                uptime = "0:00"
                if state.start_time:
                    delta = datetime.now() - state.start_time
                    total_seconds = int(delta.total_seconds())
                    hours = total_seconds // 3600
                    minutes = (total_seconds % 3600) // 60
                    uptime = f"{hours}:{minutes:02d}"

                message = f"""
{status_emoji} **Bot Status**

Running: `{running}`
Uptime: `{uptime}`
Symbols: `{len(self.bot.config.symbols)}`

Total Trades: `{state.total_trades}`
Daily P&L: `${state.daily_pnl:.2f}`
                """

                await update.message.reply_text(
                    message,
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("❌ Bot not available")

        except Exception as e:
            logger.error(f"Error fetching status: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def _handle_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /trades command"""
        user_id = update.effective_user.id
        if not self._check_authorization(user_id):
            return

        try:
            # Would need to integrate with TradeHistory
            await update.message.reply_text("📋 Trade history - coming soon")

        except Exception as e:
            logger.error(f"Error fetching trades: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def _handle_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /positions command"""
        user_id = update.effective_user.id
        if not self._check_authorization(user_id):
            return

        try:
            if self.bot and self.bot.exchange:
                positions = await self.bot.exchange.get_positions()

                if not positions:
                    await update.message.reply_text("No open positions")
                    return

                message = "📈 **Open Positions**\n\n"

                for pos in positions:
                    sym = pos.get('symbol', '?')
                    side = pos.get('side', '?').upper()
                    qty = float(pos.get('qty', pos.get('amount', 0)))
                    entry = float(pos.get('avgOpenPrice', pos.get('entry_price', 0)))
                    upnl = float(pos.get('unrealizedPNL', pos.get('unrealized_pnl', 0)))

                    # Mark price: WS cache → compute from uPnL → API field → entry
                    mark = 0.0
                    if self.bot and hasattr(self.bot, 'symbol_states'):
                        ss = self.bot.symbol_states.get(sym)
                        if ss and ss.last_price and ss.last_price > 0:
                            mark = ss.last_price
                    if mark == 0.0 and entry > 0 and qty > 0:
                        if side in ("BUY", "LONG"):
                            mark = entry + (upnl / qty)
                        else:
                            mark = entry - (upnl / qty)
                    if mark <= 0:
                        mark = float(pos.get('markPrice', pos.get('mark_price', entry)))

                    price_fmt = ".2f" if entry > 10 else ".4f"
                    message += f"""
**{sym}**
Side: `{side}`
Amount: `{qty:.4f}`
Entry: `${entry:.2f}`
Current: `${mark:.2f}`
P&L: `${upnl:.2f}`
---
"""

                await update.message.reply_text(
                    message,
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text("❌ Bot not connected")

        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def _handle_start_trading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start_trading command"""
        user_id = update.effective_user.id
        if not self._check_authorization(user_id):
            return

        try:
            if self.bot:
                self.bot.state.is_running = True
                await update.message.reply_text("✅ Trading started!")
                logger.info("Trading started via Telegram")
            else:
                await update.message.reply_text("❌ Bot not available")

        except Exception as e:
            logger.error(f"Error starting trading: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def _handle_stop_trading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop_trading command"""
        user_id = update.effective_user.id
        if not self._check_authorization(user_id):
            return

        try:
            if self.bot:
                self.bot.state.is_running = False
                await update.message.reply_text("⏹️ Trading stopped!")
                logger.info("Trading stopped via Telegram")
            else:
                await update.message.reply_text("❌ Bot not available")

        except Exception as e:
            logger.error(f"Error stopping trading: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def _handle_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /logs command"""
        user_id = update.effective_user.id
        if not self._check_authorization(user_id):
            return

        try:
            from pathlib import Path
            log_file = Path(self.bot.config.logging.file if self.bot else "logs/goldast_bot.log")

            log_lines = []
            if log_file.exists():
                with open(log_file, "r") as f:
                    lines = f.readlines()
                    log_lines = lines[-20:] if len(lines) >= 20 else lines
            else:
                log_lines = ["Log file not found"]

            message = "📋 **Recent Logs**\n\n```\n"
            for line in log_lines:
                line = line.strip()
                if len(line) > 100:
                    line = line[:97] + "..."
                message += line + "\n"
            message += "```"

            await update.message.reply_text(
                message,
                parse_mode="Markdown"
            )

        except Exception as e:
            logger.error(f"Error fetching logs: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    # ============================================================
    # CALLBACK HANDLERS
    # ============================================================

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline button callbacks"""
        query = update.callback_query
        user_id = query.from_user.id
        callback_data = query.data

        logger.info(f"Callback received: user={user_id}, data={callback_data}")

        # Check authorization
        if not self._check_authorization(user_id):
            await query.answer("❌ Unauthorized", show_alert=True)
            return

        await query.answer()

        # Main menu callbacks
        if callback_data.startswith("main_"):
            await self._handle_main_menu(query, callback_data)

    async def _handle_main_menu(self, query, action: str):
        """Handle main menu buttons"""
        action_map = {
            "main_balance": self._handle_balance,
            "main_pnl": self._handle_pnl,
            "main_stats": self._handle_stats,
            "main_status": self._handle_status,
            "main_logs": self._handle_logs,
        }

        handler = action_map.get(action)
        if handler:
            # Create a mock update for handler reuse
            class MockUpdate:
                def __init__(self, query):
                    self.message = query.message
                    self.effective_user = query.from_user
                    self.effective_chat = query.message.chat

            mock_update = MockUpdate(query)
            mock_context = None
            await handler(mock_update, mock_context)

    async def _error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle Telegram bot errors"""
        logger.error(f"Telegram bot error: {context.error}")

    # ============================================================
    # NOTIFICATION METHODS
    # ============================================================

    async def send_notification(self, message: str, parse_mode: str = "Markdown"):
        """Send notification to all allowed users"""
        if not self.config.notifications_enabled:
            return

        if not self.app:
            logger.warning("Telegram app not initialized")
            return

        for user_id in self.config.allowed_users:
            try:
                await self.app.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode=parse_mode
                )
            except Exception as e:
                logger.error(f"Failed to send notification to user {user_id}: {e}")

    async def notify_trade(self, symbol: str, side: str, entry_price: float, amount: float,
                           tp_price: float = 0, sl_price: float = 0, leverage: int = 0,
                           position_usd: float = 0, rr_ratio: float = 0):
        """Send rich trade execution notification"""
        side_emoji = "🟢" if side.upper() in ("BUY", "LONG") else "🔴"
        
        # Price formatting
        price_fmt = ".2f" if entry_price > 10 else ".4f"
        
        msg = (
            f"{side_emoji} *NEW TRADE*\n\n"
            f"Symbol: `{symbol}`\n"
            f"Side: `{side.upper()}`  |  Leverage: `{leverage}x`\n"
            f"Entry: `${entry_price:{price_fmt}}`\n"
        )
        
        if tp_price > 0 and sl_price > 0:
            tp_distance_pct = abs(tp_price - entry_price) / entry_price * 100
            sl_distance_pct = abs(sl_price - entry_price) / entry_price * 100
            msg += (
                f"🎯 TP: `${tp_price:{price_fmt}}` ({tp_distance_pct:.2f}%)\n"
                f"🛑 SL: `${sl_price:{price_fmt}}` ({sl_distance_pct:.2f}%)\n"
            )
        
        if rr_ratio > 0:
            msg += f"📐 R:R: `{rr_ratio:.1f}:1`\n"
        
        msg += f"Size: `{amount:.4f}` (~`${position_usd:.0f}`)\n"
        
        await self.send_notification(msg)

    async def notify_position_close(self, symbol: str, pnl: float, close_price: float,
                                    entry_price: float = 0, side: str = "",
                                    leverage: int = 0, close_type: str = "",
                                    hold_time_str: str = "", daily_pnl: float = 0,
                                    r_achieved: float = 0):
        """Send rich position close notification"""
        is_win = pnl > 0
        emoji = "🎯✅" if is_win else "🛑❌"
        close_label = {
            "tp": "Take Profit",
            "sl": "Stop Loss",
            "trailing_sl": "Trailing SL",
            "manual": "Manual Close",
        }.get(close_type, close_type.upper() if close_type else "Closed")

        price_fmt = ".2f" if entry_price > 10 else ".4f"

        msg = f"{emoji} *POSITION CLOSED — {close_label}*\n\n"
        msg += f"Symbol: `{symbol}`\n"

        if side:
            side_emoji = "🟢" if side.upper() in ("BUY", "LONG") else "🔴"
            msg += f"Side: {side_emoji} `{side.upper()}`"
            if leverage > 0:
                msg += f"  |  `{leverage}x`"
            msg += "\n"

        if entry_price > 0 and close_price > 0:
            msg += f"Entry: `${entry_price:{price_fmt}}`  →  Close: `${close_price:{price_fmt}}`\n"
        elif close_price > 0:
            msg += f"Close: `${close_price:{price_fmt}}`\n"

        # PnL
        pnl_pct = ""
        if entry_price > 0 and leverage > 0:
            pnl_pct_val = (pnl / (entry_price / leverage)) * 100 if entry_price > 0 else 0
            pnl_pct = f" ({pnl_pct_val:+.1f}%)"
        msg += f"💵 PnL: `${pnl:+.2f}`{pnl_pct}\n"

        if r_achieved != 0:
            msg += f"📐 R achieved: `{r_achieved:+.1f}R`\n"

        if hold_time_str:
            msg += f"⏱ Hold time: `{hold_time_str}`\n"

        # Running daily PnL
        if daily_pnl != 0 or pnl != 0:
            daily_emoji = "📈" if daily_pnl >= 0 else "📉"
            msg += f"\n{daily_emoji} Daily PnL: `${daily_pnl:+.2f}`"

        await self.send_notification(msg)

    async def notify_error(self, error_message: str):
        """Send error notification"""
        message = f"""
❌ **Error Alert**

{error_message}
        """
        await self.send_notification(message)

    # ============================================================
    # RUN METHOD
    # ============================================================

    async def run(self):
        """Run the Telegram bot (non-blocking, compatible with existing event loop).
        
        Uses initialize() + start() + updater.start_polling() instead of
        run_polling() which would take over the event loop and block the trading bot.
        """
        if not self.app:
            await self.initialize()

        self.running = True
        try:
            await self.app.initialize()
            await self.app.start()
            await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            logger.info("🚀 Telegram bot polling started")
        except Exception as e:
            logger.error(f"❌ Telegram bot polling failed: {e}")
            self.running = False

    async def stop(self):
        """Stop the Telegram bot gracefully"""
        self.running = False
        if self.app:
            try:
                if self.app.updater and self.app.updater.running:
                    await self.app.updater.stop()
                if self.app.running:
                    await self.app.stop()
                await self.app.shutdown()
                logger.info("✅ Telegram bot stopped")
            except Exception as e:
                logger.error(f"Error stopping Telegram bot: {e}")
