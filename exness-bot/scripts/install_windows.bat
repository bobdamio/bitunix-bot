@echo off
REM ============================================
REM  Exness Bot - Windows VPS Setup Script
REM  Run this as Administrator on your Windows VPS
REM ============================================

echo ============================================
echo  Exness Bot - Windows VPS Setup
echo ============================================
echo.

REM --- Check Python ---
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.10+ from python.org
    echo   Download: https://www.python.org/downloads/
    echo   IMPORTANT: Check "Add Python to PATH" during installation!
    pause
    exit /b 1
)
echo [OK] Python found

REM --- Check pip ---
pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] pip not found. Reinstall Python with pip included.
    pause
    exit /b 1
)
echo [OK] pip found

REM --- Create virtual environment ---
echo.
echo Creating virtual environment...
if not exist venv (
    python -m venv venv
    echo [OK] Virtual environment created
) else (
    echo [OK] Virtual environment already exists
)

REM --- Activate venv ---
call venv\Scripts\activate.bat

REM --- Install dependencies ---
echo.
echo Installing dependencies...
pip install --upgrade pip
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)
echo [OK] All dependencies installed

REM --- Create directories ---
if not exist data mkdir data
if not exist logs mkdir logs
echo [OK] Data and log directories ready

REM --- Check .env ---
if not exist .env (
    echo.
    echo [!] .env file not found. Creating from template...
    copy .env.example .env
    echo [!] EDIT .env with your MT5 credentials before running the bot!
)

echo.
echo ============================================
echo  Setup Complete!
echo ============================================
echo.
echo  Next steps:
echo   1. Install MetaTrader 5 from Exness
echo   2. Login to MT5 at least once manually
echo   3. Edit .env with your MT5 credentials
echo   4. Run: run_bot.bat
echo.
pause
