"""Windowless runner for the PC-only sources (KEA, J&K, Arunachal).

Meant for `pythonw.exe run_pc.py` from Task Scheduler: no console window, no PowerShell,
no crash dialog. Fetches the sources, then pushes notices_pc.json to GitHub.
"""
import ctypes
import faulthandler
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
NO_WINDOW = 0x08000000

try:  # never show a "python.exe has stopped working" box
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
except Exception:  # noqa: BLE001
    pass

log = open(os.path.join(HERE, "monitor_pc.log"), "a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = log
faulthandler.enable(log)

sys.argv = ["monitor.py", "--pc"]
import monitor  # noqa: E402  (PC_MODE is read from sys.argv at import)

monitor.run([])


def git(*args):
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True,
                          creationflags=NO_WINDOW, timeout=120)


git("add", "notices_pc.json")
if git("diff", "--cached", "--quiet").returncode != 0:
    git("commit", "-q", "-m", "update notices (pc sources)", "-m",
        "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>")
    git("pull", "--rebase", "-X", "theirs", "-q")
    r = git("push", "-q")
    print("push:", r.returncode, r.stderr.strip()[:200])
log.flush()
