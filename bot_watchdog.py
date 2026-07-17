#!/usr/bin/env python3
"""
SkillSync Bot Watchdog — Auto-restart on crash.

Usage (manual first start after boot):
    python bot_watchdog.py

What it does:
    - Launches run_bot.py as a subprocess.
    - If the bot crashes (non-zero exit), waits 10 seconds and restarts.
    - If you press Ctrl+C, the bot is stopped cleanly and watchdog exits
      without restarting.
    - If stop.bat kills this process, the bot dies and stays dead.

Logs to: bot_watchdog.log
"""

import os
import signal
import subprocess
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE, "bot_watchdog.log")
VENV_PYTHON = os.path.join(BASE, ".venv", "Scripts", "python.exe")
MAX_RESTARTS = 5
RESTART_WAIT = 10

_shutting_down = False


def _resolve_python():
    for candidate in [VENV_PYTHON, sys.executable]:
        if os.path.exists(candidate):
            return candidate
    return None


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def handle_signal(signum, frame):
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    log(f"Received signal {signum}, shutting down bot...")
    # The child process will get the signal too via the process group
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log("=" * 50)
    log("Bot watchdog started")
    log("=" * 50)

    bot_script = os.path.join(BASE, "run_bot.py")
    restart_count = 0
    healthy_run = False

    while not _shutting_down:
        python_bin = _resolve_python()
        if not python_bin:
            log("ERROR: No Python binary found. Retrying in 30s...")
            for _ in range(30):
                if _shutting_down:
                    break
                time.sleep(1)
            continue

        log(f"Python: {python_bin}")
        log(f"Starting bot... (restart #{restart_count})")
        try:
            proc = subprocess.Popen(
                [python_bin, "-u", bot_script],
                cwd=BASE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            log(f"ERROR: Could not start bot process: {e}")
            restart_count += 1
            log(f"Retrying in {RESTART_WAIT}s...")
            for _ in range(RESTART_WAIT):
                if _shutting_down:
                    break
                time.sleep(1)
            continue

        # Stream output to console + log until process ends
        try:
            for line in proc.stdout:
                decoded = line.decode("utf-8", errors="replace").rstrip()
                print(decoded)
                try:
                    with open(LOG_FILE, "a") as f:
                        f.write(decoded + "\n")
                except Exception:
                    pass
        except Exception:
            pass

        proc.wait()
        returncode = proc.returncode

        if _shutting_down:
            log("Watchdog exiting (clean shutdown requested).")
            break

        if returncode == 0:
            log("Bot exited cleanly (code 0). Restarting in 5s...")
            healthy_run = True
            restart_count = 0
            for _ in range(5):
                if _shutting_down:
                    break
                time.sleep(1)
            continue

        restart_count += 1
        if healthy_run:
            restart_count = 0
            healthy_run = False

        if restart_count >= MAX_RESTARTS:
            log(f"FATAL: Bot crashed {MAX_RESTARTS} times in a row. Giving up.")
            log("Fix the issue and restart the watchdog manually.")
            break

        log(
            f"Bot crashed with code {returncode}. Restarting in {RESTART_WAIT}s..."
            f" (restart #{restart_count}/{MAX_RESTARTS})"
        )
        for _ in range(RESTART_WAIT):
            if _shutting_down:
                break
            time.sleep(1)

    log("Watchdog stopped.")


if __name__ == "__main__":
    main()
