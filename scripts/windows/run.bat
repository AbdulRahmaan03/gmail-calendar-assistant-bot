@echo off
title Gmail ^& Calendar Executive Assistant Bot
rem Run from the project root (two folders up from this script).
cd /d "%~dp0..\.."
echo Starting Assistant Bot... (it exits straight away if it is already running)
python -m assistant
pause
