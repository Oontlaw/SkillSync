#!/usr/bin/env python3
"""
SkillSync Bot Watchdog — Auto-restart on crash or freeze.

Usage (manual first start after boot):
    python bot_watchdog.py

What it does:
    - Launches run_bot.py as a subprocess.
    - If the bot crashes (non-zero exit), waits and restarts.
    - If the bot freezes (no log file activity for STALE_TIMEOUT seconds), kills and restarts.
    - If you press Ctrl+C, the bot is stopped cleanly and watchdog exits.
    - Circuit breaker: after MAX_CRASH_CONSECUTIVE crashes (not zombie kills), backs off
      exponentially. If the bot ran for >= MIN_HEALTHY_SECONDS before being killed for
      zombie, it counts as a healthy run and resets the crash counter.

Logs to: bot_watchdog.log
"""

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE, "bot_watchdog.log")
BOT_LOG = os.path.join(BASE, "skillsync_bot.log")
VENV_PYTHON = os.path.join(BASE, ".venv", "Scripts", "python.exe")
HEARTBEAT_FILE = os.path.join(BASE, ".bot_heartbeat")

MAX_CRASH_CONSECUTIVE = 5   # consecutive *crashes* before backing off
RESTART_WAIT = 10
STALE_TIMEOUT = 300          # 5 min with no log file activity = zombie
MIN_HEALTHY_SECONDS = 120    # if bot ran 2+ min, counts as healthy run
BACKOFF_BASE = 30            # backoff base seconds after consecutive crashes

_shutting_down = False
_last_log_write_time = 0.0
_log_lock = threading.Lock()


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
    sys.exit(0)


def _check_bot_log_staleness():
    """Check if the bot's own log file has been written to recently."""
    try:
        mtime = os.path.getmtime(BOT_LOG)
        age = time.time() - mtime
        return age
    except OSError:
        return 0


def _check_heartbeat_file():
    """Check the heartbeat file the bot writes periodically."""
    try:
        mtime = os.path.getmtime(HEARTBEAT_FILE)
        age = time.time() - mtime
        return age
    except OSError:
        return 999999


def _is_bot_alive():
    """Determine if the bot is truly alive using multiple signals.
    
    Returns (is_alive: bool, age_seconds: float).
    Uses the best (most recent) signal among:
    - Bot log file mtime (bot writes via log() → skillsync_bot.log)
    - Heartbeat file mtime (bot writes .bot_heartbeat every 30s)
    """
    log_age = _check_bot_log_staleness()
    hb_age = _check_heartbeat_file()
    
    # Use the best (most recent) signal
    best_age = min(log_age, hb_age)
    is_alive = best_age <= STALE_TIMEOUT
    return is_alive, best_age


def main():
    global _shutting_down

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log("=" * 50)
    log("Bot watchdog started")
    log(f"Stale timeout: {STALE_TIMEOUT}s")
    log(f"Min healthy run: {MIN_HEALTHY_SECONDS}s")
    log(f"Max consecutive crashes before backoff: {MAX_CRASH_CONSECUTIVE}")
    log("=" * 50)

    bot_script = os.path.join(BASE, "run_bot.py")
    crash_count = 0

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
        log(f"Starting bot... (crash #{crash_count})")
        try:
            proc = subprocess.Popen(
                [python_bin, "-u", bot_script],
                cwd=BASE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            log(f"ERROR: Could not start bot process: {e}")
            crash_count += 1
            wait = min(RESTART_WAIT + crash_count * BACKOFF_BASE, 300)
            log(f"Retrying in {wait}s...")
            for _ in range(wait):
                if _shutting_down:
                    break
                time.sleep(1)
            continue

        start_time = time.time()
        killed = False

        # Monitor loop: check every 30s
        while proc.poll() is None:
            if _shutting_down:
                proc.terminate()
                killed = True
                break

            time.sleep(30)

            if proc.poll() is not None:
                break

            # Check bot liveness via log file + heartbeat file
            is_alive, age = _is_bot_alive()

            if not is_alive:
                log(f"WARN: Bot appears frozen (no activity for {int(age)}s). Killing...")
                try:
                    proc.kill()
                except Exception:
                    pass
                killed = True
                break

        if _shutting_down:
            break

        uptime = time.time() - start_time
        healthy_run = uptime >= MIN_HEALTHY_SECONDS and not killed

        if killed:
            if healthy_run:
                # Bot ran long enough — this was a slow zombie, not a crash
                # Don't count toward crash counter
                log(f"Bot was healthy for {int(uptime)}s before zombie detected. Not counting as crash.")
                crash_count = 0
            else:
                crash_count += 1

            # Exponential backoff but never give up permanently
            if crash_count >= MAX_CRASH_CONSECUTIVE:
                wait = min(60 + crash_count * BACKOFF_BASE, 600)
                log(f"Bot frozen {crash_count} times consecutively. Backing off {wait}s (but will retry)...")
            else:
                wait = RESTART_WAIT
                log(f"Restarting in {wait}s... (consecutive crashes #{crash_count}/{MAX_CRASH_CONSECUTIVE})")

            for _ in range(wait):
                if _shutting_down:
                    break
                time.sleep(1)
            continue

        # Process exited on its own (crash)
        returncode = proc.returncode

        if returncode == 0:
            log(f"Bot exited cleanly (code 0). Restarting in 5s...")
            crash_count = 0
            for _ in range(5):
                if _shutting_down:
                    break
                time.sleep(1)
            continue

        crash_count += 1

        if crash_count >= MAX_CRASH_CONSECUTIVE:
            wait = min(60 + crash_count * BACKOFF_BASE, 600)
            log(f"Bot crashed {crash_count} times consecutively. Backing off {wait}s (but will retry)...")
        else:
            wait = RESTART_WAIT

        log(
            f"Bot crashed with code {returncode}. Restarting in {wait}s..."
            f" (consecutive crashes #{crash_count}/{MAX_CRASH_CONSECUTIVE})"
        )
        for _ in range(wait):
            if _shutting_down:
                break
            time.sleep(1)

    log("Watchdog stopped.")


if __name__ == "__main__":
    main()
