@echo off
REM ============================================
REM  Exness Bot - Run Bot
REM ============================================

REM Activate virtual environment
call venv\Scripts\activate.bat

REM Run the bot
echo Starting Exness Bot...
echo Press Ctrl+C to stop
echo.
python main.py --config config.yaml
