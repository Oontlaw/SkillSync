#!/usr/bin/env python3
"""
SkillSync Flask Watchdog — auto-restart on crash or health-check failure.

Usage (launched by start_services.py, or manually):
    python flask_watchdog.py

What it does:
    - Launches run_dashboard.py as a subprocess.
    - Polls http://127.0.0.1:5000/health every HEALTH_INTERVAL seconds.
    - HEALTH_FAILS consecutive failures (or a dead child) => kill + restart.
    - Logs to flask_watchdog.log; stops cleanly on Ctrl+C.

The bot has bot_watchdog.py; this covers the dashboard the same way.
"""

import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE, "flask_watchdog.log")
VENV_PYTHON = os.path.join(BASE, ".venv", "Scripts", "python.exe")

HEALTH_URL = "http://127.0.0.1:5000/health"
HEALTH_INTERVAL = 30
HEALTH_FAILS = 3          # consecutive failures before restart
RESTART_WAIT = 5
MAX_CONSECUTIVE_CRASHES = 5  # circuit breaker, mirrors bot_watchdog

_shutting_down = False
proc = None


def _log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _start_flask():
    python = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable
    return subprocess.Popen(
        [python, "-u", "run_dashboard.py"],
        cwd=BASE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )


def _stop_flask():
    global proc
    if proc is not None and proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    proc = None


def _health_ok():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def main():
    global proc, _shutting_down
    crashes = 0
    _log("=" * 50)
    _log("Flask watchdog started")
    _log(f"Health checks every {HEALTH_INTERVAL}s, restart after {HEALTH_FAILS} failures")
    _log("=" * 50)

    proc = _start_flask()
    started_at = time.time()
    fails = 0

    while not _shutting_down:
        time.sleep(HEALTH_INTERVAL)
        if proc.poll() is not None:
            ran = time.time() - started_at
            crashes = 0 if ran >= 120 else crashes + 1
            _log(f"Flask exited (rc={proc.returncode}) after {ran:.0f}s (crash #{crashes})")
            if crashes >= MAX_CONSECUTIVE_CRASHES:
                _log("Circuit breaker: too many consecutive crashes, backing off 10 min")
                time.sleep(600)
            time.sleep(RESTART_WAIT)
            proc = _start_flask()
            started_at = time.time()
            fails = 0
            continue
        if _health_ok():
            fails = 0
            continue
        fails += 1
        _log(f"Health check failed ({fails}/{HEALTH_FAILS})")
        if fails >= HEALTH_FAILS:
            ran = time.time() - started_at
            crashes = 0 if ran >= 120 else crashes + 1
            _log(f"Flask unhealthy for {fails} checks — restarting (crash #{crashes})")
            _stop_flask()
            if crashes >= MAX_CONSECUTIVE_CRASHES:
                _log("Circuit breaker: too many consecutive crashes, backing off 10 min")
                time.sleep(600)
            time.sleep(RESTART_WAIT)
            proc = _start_flask()
            started_at = time.time()
            fails = 0


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _shutting_down = True
        _stop_flask()
        _log("Watchdog stopped (Ctrl+C) — Flask stopped with it")
