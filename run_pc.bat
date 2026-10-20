@echo off
rem Runs the sites that block GitHub's servers (KEA, J&K, Arunachal) from this PC,
rem then pushes notices_pc.json so the live page shows them too.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python -u monitor.py --pc >> monitor_pc.log 2>&1
git add notices_pc.json
git diff --cached --quiet && exit /b 0
git commit -q -m "update notices (pc sources)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
git pull --rebase -X theirs -q
git push -q
