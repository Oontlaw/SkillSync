"""
ngrok watchdog — runs forever, restarts ngrok whenever it dies.
Launch once with start_services.py; stays alive as a detached process.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
NGROK = os.path.join(BASE, ".venv", "Scripts", "ngrok.exe")
URL = "appendage-uptake-aflutter.ngrok-free.dev"
PORT = 5000
PID_FILE = os.path.join(BASE, ".ngrok.pid")
LOG_FILE = os.path.join(BASE, "ngrok.log")
API = "http://127.0.0.1:4040/api/tunnels"

DETACHED = 0x00000008
NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200
FLAGS = DETACHED | NO_WINDOW | NEW_GROUP


def is_tunnel_alive():
    try:
        with urllib.request.urlopen(API, timeout=3) as r:
            d = json.loads(r.read())
            return bool(d.get("tunnels"))
    except Exception:
        return False


def kill_old():
    if os.path.exists(PID_FILE):
        try:
            pid = int(open(PID_FILE).read().strip())
            subprocess.call(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    subprocess.call(
        ["taskkill", "/F", "/IM", "ngrok.exe"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)


def start_ngrok():
    kill_old()
    proc = subprocess.Popen(
        [NGROK, "http", f"--url={URL}", "--log=stdout", "--log-level=warn", str(PORT)],
        stdout=open(LOG_FILE, "w"),
        stderr=subprocess.STDOUT,
        cwd=BASE,
        creationflags=FLAGS,
        close_fds=True,
    )
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))
    # wait up to 15s for tunnel
    for _ in range(15):
        time.sleep(1)
        if is_tunnel_alive():
            return True
    return False


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] WATCHDOG: {msg}"
    # Only write to file (stdout is already redirected to the log file when detached)
    try:
        with open(os.path.join(BASE, "ngrok_watchdog.log"), "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    log("Watchdog started")
    while True:
        if not is_tunnel_alive():
            log("Tunnel down — restarting ngrok...")
            ok = start_ngrok()
            log("Tunnel up" if ok else "Failed to start tunnel — will retry in 30s")
        time.sleep(15)
