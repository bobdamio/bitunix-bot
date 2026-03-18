#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Exness Bot - One-Click Windows Installer
.DESCRIPTION
    Downloads Python, installs dependencies, configures credentials,
    and sets up auto-start for both MT5 terminal and the trading bot.
.NOTES
    Run as Administrator:
      Right-click PowerShell -> "Run as Administrator"
      Then: Set-ExecutionPolicy Bypass -Scope Process -Force; .\install.ps1
#>

$ErrorActionPreference = "Stop"
$BOT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$PYTHON_VERSION = "3.12.9"
$PYTHON_URL = "https://www.python.org/ftp/python/$PYTHON_VERSION/python-$PYTHON_VERSION-amd64.exe"
$PYTHON_INSTALLER = "$env:TEMP\python-$PYTHON_VERSION-amd64.exe"

function Write-Step($msg) {
    Write-Host "`n=== $msg ===" -ForegroundColor Cyan
}

function Write-Ok($msg) {
    Write-Host "  [OK] $msg" -ForegroundColor Green
}

function Write-Warn($msg) {
    Write-Host "  [!] $msg" -ForegroundColor Yellow
}

function Write-Err($msg) {
    Write-Host "  [ERROR] $msg" -ForegroundColor Red
}

# ============================================================
# HEADER
# ============================================================
Clear-Host
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Exness Bot v1.0 - Windows Installer" -ForegroundColor Cyan
Write-Host "  FVG/IFVG + Supply/Demand + MTF Strategy" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# ============================================================
# STEP 1: Install Python
# ============================================================
Write-Step "Step 1/6: Python $PYTHON_VERSION"

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
$needInstall = $true

if ($pythonCmd) {
    $ver = & python --version 2>&1
    if ($ver -match "3\.(10|11|12)\.") {
        Write-Ok "Python already installed: $ver"
        $needInstall = $false
    } else {
        Write-Warn "Found $ver but need 3.10-3.12. Installing $PYTHON_VERSION..."
    }
}

if ($needInstall) {
    Write-Host "  Downloading Python $PYTHON_VERSION..." -NoNewline
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $PYTHON_URL -OutFile $PYTHON_INSTALLER -UseBasicParsing
    Write-Host " done" -ForegroundColor Green

    Write-Host "  Installing Python (silent)..." -NoNewline
    Start-Process -FilePath $PYTHON_INSTALLER -ArgumentList `
        "/quiet", "InstallAllUsers=1", "PrependPath=1", `
        "Include_pip=1", "Include_test=0" -Wait -NoNewWindow
    Write-Host " done" -ForegroundColor Green

    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + `
                [System.Environment]::GetEnvironmentVariable("Path", "User")

    Remove-Item $PYTHON_INSTALLER -Force -ErrorAction SilentlyContinue
    Write-Ok "Python $PYTHON_VERSION installed"
}

# Verify
$pyVer = & python --version 2>&1
if ($pyVer -notmatch "3\.(10|11|12)\.") {
    Write-Err "Python 3.10-3.12 required. Got: $pyVer"
    Write-Host "  Please install manually from https://python.org/downloads/" -ForegroundColor Yellow
    pause
    exit 1
}

# ============================================================
# STEP 2: Create venv and install dependencies
# ============================================================
Write-Step "Step 2/6: Virtual Environment & Dependencies"

Set-Location $BOT_DIR

if (-not (Test-Path "venv")) {
    Write-Host "  Creating virtual environment..."
    & python -m venv venv
    Write-Ok "Virtual environment created"
} else {
    Write-Ok "Virtual environment already exists"
}

# Activate and install
& "$BOT_DIR\venv\Scripts\python.exe" -m pip install --upgrade pip --quiet 2>$null
& "$BOT_DIR\venv\Scripts\pip.exe" install -r requirements.txt --quiet
Write-Ok "All dependencies installed"

# Create directories
New-Item -ItemType Directory -Path "data" -Force | Out-Null
New-Item -ItemType Directory -Path "logs" -Force | Out-Null
Write-Ok "Data and log directories ready"

# ============================================================
# STEP 3: Configure MT5 credentials
# ============================================================
Write-Step "Step 3/6: MT5 Credentials"

if (Test-Path ".env") {
    $content = Get-Content ".env" -Raw
    if ($content -match "MT5_PASSWORD=.+") {
        Write-Ok ".env already configured"
        $skipEnv = $true
    } else {
        $skipEnv = $false
    }
} else {
    $skipEnv = $false
}

if (-not $skipEnv) {
    Write-Host ""
    Write-Host "  Enter your Exness MT5 credentials:" -ForegroundColor Yellow
    Write-Host ""
    $server   = Read-Host "  MT5 Server (e.g. Exness-MT5Trial15)"
    $login    = Read-Host "  MT5 Login (account number)"
    $password = Read-Host "  MT5 Password"

    if (-not $server)   { $server   = "Exness-MT5Trial15" }
    if (-not $login)    { $login    = "0" }
    if (-not $password) { $password = "" }

    @"
MT5_SERVER=$server
MT5_LOGIN=$login
MT5_PASSWORD=$password
"@ | Set-Content ".env" -Encoding UTF8

    Write-Ok "Credentials saved to .env"
}

# ============================================================
# STEP 4: Check MetaTrader 5 installation
# ============================================================
Write-Step "Step 4/6: MetaTrader 5 Terminal"

$mt5Paths = @(
    "${env:ProgramFiles}\MetaTrader 5\terminal64.exe",
    "${env:ProgramFiles}\MetaTrader 5 EXNESS\terminal64.exe",
    "${env:ProgramFiles(x86)}\MetaTrader 5\terminal64.exe",
    "${env:ProgramFiles(x86)}\MetaTrader 5 EXNESS\terminal64.exe",
    "$env:APPDATA\MetaQuotes\Terminal\*\terminal64.exe"
)

$mt5Found = $false
foreach ($p in $mt5Paths) {
    $resolved = Resolve-Path $p -ErrorAction SilentlyContinue
    if ($resolved) {
        $mt5Exe = $resolved.Path
        $mt5Found = $true
        Write-Ok "MT5 found: $mt5Exe"
        break
    }
}

if (-not $mt5Found) {
    Write-Warn "MetaTrader 5 not found!"
    Write-Host "  Please install MT5 from your Exness Personal Area" -ForegroundColor Yellow
    Write-Host "  Then run this installer again, or start MT5 manually" -ForegroundColor Yellow
    $mt5Exe = $null
}

# ============================================================
# STEP 5: Auto-Start Setup (Task Scheduler)
# ============================================================
Write-Step "Step 5/6: Auto-Start Configuration"

$setupAutostart = Read-Host "  Set up auto-start on reboot? (Y/n)"
if ($setupAutostart -ne "n" -and $setupAutostart -ne "N") {

    # Bot task
    $botTaskName = "ExnessBot"
    $existingTask = Get-ScheduledTask -TaskName $botTaskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Unregister-ScheduledTask -TaskName $botTaskName -Confirm:$false
    }

    $batPath = "$BOT_DIR\scripts\run_bot.bat"
    $action  = New-ScheduledTaskAction -Execute $batPath -WorkingDirectory $BOT_DIR
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = "PT30S"  # 30 second delay
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType ServiceAccount -RunLevel Highest

    Register-ScheduledTask -TaskName $botTaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description "Exness FVG/IFVG Trading Bot" | Out-Null
    Write-Ok "Bot auto-start task created: $botTaskName"

    # MT5 auto-start via Startup folder
    if ($mt5Exe) {
        $startupFolder = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
        $shortcutPath  = "$startupFolder\MetaTrader5.lnk"
        if (-not (Test-Path $shortcutPath)) {
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($shortcutPath)
            $shortcut.TargetPath = $mt5Exe
            $shortcut.WorkingDirectory = Split-Path $mt5Exe
            $shortcut.Save()
            Write-Ok "MT5 auto-start shortcut created"
        } else {
            Write-Ok "MT5 auto-start shortcut already exists"
        }
    }
} else {
    Write-Warn "Auto-start skipped. Start manually with: scripts\run_bot.bat"
}

# ============================================================
# STEP 6: Summary
# ============================================================
Write-Step "Step 6/6: Installation Complete"

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "  Exness Bot installed successfully!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Location:  $BOT_DIR" -ForegroundColor White
Write-Host "  Python:    $pyVer" -ForegroundColor White
if ($mt5Exe) {
    Write-Host "  MT5:       $mt5Exe" -ForegroundColor White
}
Write-Host ""
Write-Host "  TO START:" -ForegroundColor Yellow
Write-Host "    1. Make sure MT5 is running and logged in" -ForegroundColor White
Write-Host "    2. Run:  scripts\run_bot.bat" -ForegroundColor White
Write-Host ""
Write-Host "  LOGS:      logs\exness_bot.log" -ForegroundColor White
Write-Host "  CONFIG:    config.yaml" -ForegroundColor White
Write-Host "  CREDS:     .env" -ForegroundColor White
Write-Host ""

pause
