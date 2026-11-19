@echo off
title Assistant Bot Status
cd /d "%~dp0..\.."
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$pidFile = Join-Path (Get-Location) 'runtime\bot.pid'; $p = $null; " ^
  "if (Test-Path $pidFile) { $id = [int](Get-Content $pidFile -Raw); " ^
  "  $p = Get-CimInstance Win32_Process -Filter \"ProcessId=$id\" | Where-Object { $_.CommandLine -like '*-m assistant*' } }; " ^
  "if ($p) { Write-Host \"[RUNNING] Assistant bot is running (PID $($p.ProcessId)).\" -ForegroundColor Green } " ^
  "else { Write-Host '[STOPPED] Assistant bot is not running. Start it with run.bat or start_silent.vbs.' -ForegroundColor Red }"
echo.
pause
