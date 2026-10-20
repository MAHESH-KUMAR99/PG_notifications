@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python monitor.py >> monitor.log 2>&1
