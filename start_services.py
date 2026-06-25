"""
Starts Flask and bot as fully detached Windows processes.
Saves PIDs to .flask.pid and .bot.pid for clean shutdown.
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(BASE, ".venv", "Scripts", "python.exe")

DETACHED = 0x00000008
NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200
FLAGS = DETACHED | NO_WINDOW | NEW_GROUP


def launch(script, log_name, pid_file):
    pid_path = os.path.join(BASE, pid_file)
    # kill old instance if PID file exists
    if os.path.exists(pid_path):
        try:
            old = int(open(pid_path).read().strip())
            subprocess.call(
                ["taskkill", "/F", "/PID", str(old)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        os.remove(pid_path)

    log_path = os.path.join(BASE, log_name)
    proc = subprocess.Popen(
        [PYTHON, os.path.join(BASE, script)],
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        cwd=BASE,
        creationflags=FLAGS,
        close_fds=True,
    )
    with open(pid_path, "w") as f:
        f.write(str(proc.pid))
    return proc.pid


def wait_http(url, timeout=20, label=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            pass
        time.sleep(1)
    return False


if __name__ == "__main__":
    print("[1/3] Starting Flask (app.py)...")
    flask_pid = launch("app.py", "flask.log", ".flask.pid")
    print(f"      PID {flask_pid}")

    print("      Waiting for Flask on :5000...")
    if wait_http("http://127.0.0.1:5000/", timeout=20, label="Flask"):
        print("      Flask OK")
    else:
        print("      Flask FAILED - check flask.log")
        sys.exit(1)

    print("[2/3] Starting Bot (bot.py)...")
    bot_pid = launch("bot.py", "bot.log", ".bot.pid")
    print(f"      PID {bot_pid}")

    print("[3/3] Starting ngrok watchdog...")
    wd_pid = launch("ngrok_watchdog.py", "ngrok_watchdog.log", ".watchdog.pid")
    print(f"      PID {wd_pid}")

    # Give watchdog time to bring ngrok up
    print("      Waiting for ngrok tunnel...")
    time.sleep(12)

    import urllib.error
    import urllib.request

    def check(url, label):
        req = urllib.request.Request(
            url, headers={"ngrok-skip-browser-warning": "true"}
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                code = r.getcode()
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:
            code = str(e)
        ok = "OK" if str(code) == "200" else "FAIL"
        print(f"      [{ok}] {label} -> HTTP {code}")

    base = "https://appendage-uptake-aflutter.ngrok-free.dev"
    check(f"{base}/", "v1 dashboard")
    check(f"{base}/v2/", "v2 dashboard")

    print()
    print("Flask PID:", flask_pid, " Bot PID:", bot_pid, " Watchdog PID:", wd_pid)
    print("Logs: flask.log  bot.log  ngrok.log  ngrok_watchdog.log")
