@echo off
title Assistant Bot Status
echo Checking Assistant Bot status...
echo.
powershell -NoProfile -Command ^
  "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*bot.py*' -and $_.ProcessId -ne $PID }; " ^
  "if ($p) { " ^
  "  Write-Host '=====================================================' -ForegroundColor Green; " ^
  "  Write-Host '  [STATUS: ACTIVE] BOT IS RUNNING IN BACKGROUND      ' -ForegroundColor Green; " ^
  "  Write-Host '=====================================================' -ForegroundColor Green; " ^
  "  Write-Host ('  Process ID (PID) : ' + $p.ProcessId); " ^
  "  Write-Host '  Ready to use     : Send a message to @Naukr_bot on Telegram'; " ^
  "  Write-Host '=====================================================' -ForegroundColor Green; " ^
  "} else { " ^
  "  Write-Host '=====================================================' -ForegroundColor Red; " ^
  "  Write-Host '  [STATUS: STOPPED] BOT IS NOT CURRENTLY RUNNING     ' -ForegroundColor Red; " ^
  "  Write-Host '=====================================================' -ForegroundColor Red; " ^
  "  Write-Host '  To start it, double-click run.bat'; " ^
  "  Write-Host '=====================================================' -ForegroundColor Red; " ^
  "}"
echo.
pause
