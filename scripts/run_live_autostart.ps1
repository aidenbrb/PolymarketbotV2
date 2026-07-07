# Autostart wrapper for the Polymarket US live-trading bot.
# Registered via Windows Task Scheduler (task name: PolymarketBotLiveAutostart).
# See src/polymarket_bot/live/RUNBOOK.md section 8 before changing this.
#
# Sets LIVE_UNATTENDED_MODE=true ONLY in this script's own process
# environment -- never in the shared .env -- so a manual, interactive run of
# `live-start` from a terminal still requires the typed confirmation phrase
# every time. This script is the one deliberate exception, requested and
# confirmed explicitly by the user, understanding it removes that safety net
# for autostart launches.

$ErrorActionPreference = "Stop"
$ProjectRoot = "c:\Users\dougf\Polymarket Bot"
Set-Location $ProjectRoot

$env:LIVE_UNATTENDED_MODE = "true"

$logDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$stdoutLog = Join-Path $logDir "autostart_stdout.log"

"[$(Get-Date -Format o)] Autostart wrapper launching live-start" | Out-File -Append -FilePath $stdoutLog -Encoding utf8

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
& $python -m polymarket_bot.main live-start *>> $stdoutLog

"[$(Get-Date -Format o)] live-start process exited with code $LASTEXITCODE" | Out-File -Append -FilePath $stdoutLog -Encoding utf8
