@echo off
chcp 65001 >nul
cd /d "%~dp0.."
python deploy\worker_start.py
if errorlevel 1 pause
