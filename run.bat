@echo off
title Gmail & Calendar Executive Assistant Bot
powershell -NoProfile -Command "$p = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*bot.py*' -and $_.ProcessId -ne $PID }); if ($p.Count -gt 0) { Write-Host '=====================================================' -ForegroundColor Cyan; Write-Host '  ASSISTANT BOT IS ALREADY RUNNING IN THE BACKGROUND! ' -ForegroundColor Green; Write-Host '=====================================================' -ForegroundColor Cyan; Write-Host ('  Process ID (PID) : ' + $p[0].ProcessId); Write-Host '  You do not need to keep this window open.'; Write-Host '  👉 Simply open Telegram and message @Naukr_bot!'; Write-Host '=====================================================' -ForegroundColor Cyan; exit 1 }"
if %ERRORLEVEL% equ 1 (
  echo.
  pause
  exit /b
)

echo Starting Assistant Bot...
python bot.py
pause
