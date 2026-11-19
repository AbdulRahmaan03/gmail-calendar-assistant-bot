@echo off
title Stop Assistant Bot
cd /d "%~dp0..\.."
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$pidFile = Join-Path (Get-Location) 'runtime\bot.pid'; " ^
  "if (-not (Test-Path $pidFile)) { Write-Host 'Assistant bot is not running (no runtime\bot.pid).' -ForegroundColor Yellow; exit 0 }; " ^
  "$id = [int](Get-Content $pidFile -Raw); " ^
  "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=$id\" | Where-Object { $_.CommandLine -like '*-m assistant*' }; " ^
  "if ($p) { Stop-Process -Id $id -Force; Remove-Item $pidFile -ErrorAction SilentlyContinue; " ^
  "  Write-Host \"[STOPPED] Assistant bot (PID $id) stopped.\" -ForegroundColor Green } " ^
  "else { Remove-Item $pidFile -ErrorAction SilentlyContinue; Write-Host 'Assistant bot was not running (stale PID file removed).' -ForegroundColor Yellow }"
pause
