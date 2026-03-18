@echo off
chcp 65001 >nul 2>&1
title Exness Bot - Full Installer
color 0A

echo ============================================================
echo   Exness Bot v1.0 - Full Windows Installer
echo   FVG/IFVG + Supply/Demand + MTF Strategy
echo ============================================================
echo.

REM ============================================================
REM  STEP 1: Check/Install Python
REM ============================================================
echo [1/5] Checking Python...

python --version >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
    echo       Python found: %PYVER%
    goto :python_ok
)

echo       Python not found. Downloading Python 3.12.9...
echo.

REM Download Python installer
powershell -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe' -OutFile '%TEMP%\python-installer.exe' -UseBasicParsing"

if not exist "%TEMP%\python-installer.exe" (
    echo       [ERROR] Failed to download Python. Install manually from python.org
    pause
    exit /b 1
)

echo       Installing Python 3.12.9 (silent)...
"%TEMP%\python-installer.exe" /quiet InstallAllUsers=1 PrependPath=1 Include_pip=1 Include_test=0
del "%TEMP%\python-installer.exe" >nul 2>&1

REM Refresh PATH
set "PATH=%ProgramFiles%\Python312;%ProgramFiles%\Python312\Scripts;%PATH%"

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo       [ERROR] Python installation failed. Please install manually.
    pause
    exit /b 1
)
echo       Python installed successfully!

:python_ok
echo       [OK]
echo.

REM ============================================================
REM  STEP 2: Create venv and install packages
REM ============================================================
echo [2/5] Setting up virtual environment...

cd /d "%~dp0"

if not exist venv (
    python -m venv venv
)
call venv\Scripts\activate.bat

python -m pip install --upgrade pip --quiet 2>nul
pip install -r requirements.txt --quiet 2>nul

if not exist data mkdir data
if not exist logs mkdir logs

echo       [OK]
echo.

REM ============================================================
REM  STEP 3: Configure MT5 credentials
REM ============================================================
echo [3/5] MT5 Credentials...

if exist .env (
    findstr /C:"MT5_PASSWORD=" .env >nul 2>&1
    if %errorlevel% equ 0 (
        echo       .env already configured
        goto :env_ok
    )
)

echo.
echo       Enter your Exness MT5 credentials:
echo.
set /p MT5_SRV="       MT5 Server [Exness-MT5Trial15]: "
set /p MT5_LOG="       MT5 Login (account number): "
set /p MT5_PWD="       MT5 Password: "

if "%MT5_SRV%"=="" set MT5_SRV=Exness-MT5Trial15

(
echo MT5_SERVER=%MT5_SRV%
echo MT5_LOGIN=%MT5_LOG%
echo MT5_PASSWORD=%MT5_PWD%
) > .env

echo       [OK] Saved to .env

:env_ok
echo.

REM ============================================================
REM  STEP 4: Check/Install MetaTrader 5
REM ============================================================
echo [4/5] Checking MetaTrader 5...

set MT5_FOUND=0

if exist "%ProgramFiles%\MetaTrader 5\terminal64.exe" (
    set MT5_FOUND=1
    set "MT5_EXE=%ProgramFiles%\MetaTrader 5\terminal64.exe"
)
if exist "%ProgramFiles%\MetaTrader 5 EXNESS\terminal64.exe" (
    set MT5_FOUND=1
    set "MT5_EXE=%ProgramFiles%\MetaTrader 5 EXNESS\terminal64.exe"
)
if exist "%ProgramFiles(x86)%\MetaTrader 5\terminal64.exe" (
    set MT5_FOUND=1
    set "MT5_EXE=%ProgramFiles(x86)%\MetaTrader 5\terminal64.exe"
)

if %MT5_FOUND% equ 1 (
    echo       MT5 found: %MT5_EXE%
    goto :mt5_ok
)

echo       MT5 not found. Downloading Exness MT5 installer...
powershell -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://download.exness.com/MT5/exness5setup.exe' -OutFile '%TEMP%\mt5setup.exe' -UseBasicParsing"

if not exist "%TEMP%\mt5setup.exe" (
    echo       [ERROR] Failed to download Exness MT5.
    echo       Please download manually from your Exness Personal Area:
    echo       Login at https://my.exness.com ^> your MT5 account ^> Download MT5
    pause
    exit /b 1
)

echo       Installing Exness MetaTrader 5...
start /wait "" "%TEMP%\mt5setup.exe" /auto
del "%TEMP%\mt5setup.exe" ^>nul 2^>^&1

REM Re-check after install
if exist "%ProgramFiles%\MetaTrader 5\terminal64.exe" (
    set MT5_FOUND=1
    set "MT5_EXE=%ProgramFiles%\MetaTrader 5\terminal64.exe"
)

if %MT5_FOUND% equ 1 (
    echo       MT5 installed: %MT5_EXE%
) else (
    echo       [!] MT5 may have installed to a custom location.
    echo           Please open MT5 manually and log in.
)

:mt5_ok
echo       [OK]
echo.
echo       *** IMPORTANT: Open MT5 and log in with your Exness credentials. ***
echo       *** Server, login and password are in your Exness Personal Area. ***
echo.

REM ============================================================
REM  STEP 5: Setup auto-start
REM ============================================================
echo [5/5] Setting up auto-start...

REM Create auto-start batch file
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

REM Bot auto-start shortcut
(
echo @echo off
echo cd /d "%~dp0"
echo timeout /t 15 /nobreak ^>nul
echo call venv\Scripts\activate.bat
echo python main.py --config config.yaml
) > "%STARTUP%\ExnessBot.bat"
echo       Bot auto-start: created

REM MT5 auto-start shortcut
if %MT5_FOUND% equ 1 (
    if not exist "%STARTUP%\MetaTrader5.lnk" (
        powershell -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut('%STARTUP%\MetaTrader5.lnk');$s.TargetPath='%MT5_EXE%';$s.Save()"
        echo       MT5 auto-start: created
    ) else (
        echo       MT5 auto-start: already exists
    )
)

echo       [OK]
echo.

REM ============================================================
REM  DONE
REM ============================================================
echo ============================================================
echo   Installation Complete!
echo ============================================================
echo.
echo   Location:    %~dp0
echo   Config:      config.yaml
echo   Credentials: .env
echo   Logs:        logs\exness_bot.log
echo.
echo   TO START NOW:
echo     1. Make sure MT5 is running and logged in
echo     2. Double-click:  run_bot.bat
echo.
echo   Auto-start on reboot: ENABLED
echo     Bot and MT5 will start automatically.
echo.

set /p START_NOW="   Start the bot now? (Y/n): "
if /i "%START_NOW%"=="n" goto :done

echo.
echo   Starting Exness Bot...
echo   Press Ctrl+C to stop.
echo.
call venv\Scripts\activate.bat
python main.py --config config.yaml

:done
pause
