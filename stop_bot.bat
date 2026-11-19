@echo off
title Stop Assistant Bot
echo Stopping Assistant Bot...
powershell -NoProfile -Command ^
  "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*bot.py*' }; " ^
  "if ($procs) { " ^
  "  $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; " ^
  "  Write-Host '[SUCCESS] Assistant Bot stopped successfully.' -ForegroundColor Green; " ^
  "} else { " ^
  "  Write-Host 'No running Assistant Bot instance found.' -ForegroundColor Yellow; " ^
  "}"
pause
