"""
SkillSync — Start All Services.

Launches Flask dashboard, Discord bot, and ngrok tunnel as
detached Windows processes. Use stop.bat to shut everything down.

Usage:
    python start_services.py
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(BASE, ".venv", "Scripts", "python.exe")
NGROK = os.path.join(BASE, ".venv", "Scripts", "ngrok.exe")

DETACHED = 0x00000008
NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200
FLAGS = DETACHED | NO_WINDOW | NEW_GROUP


def _kill_old(pid_file: str):
    """Kill a process by its PID file, if it exists."""
    pid_path = os.path.join(BASE, pid_file)
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
        try:
            os.remove(pid_path)
        except Exception:
            pass


def launch(script: str, log_name: str, pid_file: str) -> int:
    """Launch a Python script as a detached process. Returns PID."""
    _kill_old(pid_file)
    log_path = os.path.join(BASE, log_name)
    proc = subprocess.Popen(
        [PYTHON, os.path.join(BASE, script)],
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        cwd=BASE,
        creationflags=FLAGS,
        close_fds=True,
    )
    with open(os.path.join(BASE, pid_file), "w") as f:
        f.write(str(proc.pid))
    return proc.pid


def launch_ngrok() -> int:
    """Launch ngrok as a detached process. Returns PID."""
    _kill_old(".ngrok.pid")
    log_path = os.path.join(BASE, "ngrok.log")
    proc = subprocess.Popen(
        [NGROK, "http", "--log=stdout", "--log-level=warn", "5000"],
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        cwd=BASE,
        creationflags=FLAGS,
        close_fds=True,
    )
    with open(os.path.join(BASE, ".ngrok.pid"), "w") as f:
        f.write(str(proc.pid))
    return proc.pid


def wait_http(url: str, timeout: int = 20) -> bool:
    """Wait until a URL returns HTTP 200, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                url, headers={"ngrok-skip-browser-warning": "true"}
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.getcode() == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def get_ngrok_url() -> str:
    """Fetch the public ngrok URL from the local API. Returns empty string if not ready."""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:4040/api/tunnels",
            headers={"ngrok-skip-browser-warning": "true"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            import json

            data = json.loads(r.read())
            tunnels = data.get("tunnels", [])
            for t in tunnels:
                if t.get("proto") == "https":
                    return t["public_url"]
    except Exception:
        pass
    return ""


def print_status(label: str, ok: bool):
    status = "OK" if ok else "FAIL"
    print(f"      [{status}] {label}")


if __name__ == "__main__":
    print("=" * 50)
    print("  SkillSync — Starting All Services")
    print("=" * 50)
    print()

    # ── 1. Flask Dashboard ──
    print("[1/3] Starting Flask dashboard (run_dashboard.py)...")
    flask_pid = launch("run_dashboard.py", "flask.log", ".flask.pid")
    print(f"      PID {flask_pid}")
    print("      Waiting for Flask on :5000...")
    if wait_http("http://127.0.0.1:5000/health", timeout=20):
        print_status("Flask dashboard", True)
    else:
        print_status("Flask dashboard", False)
        print("      Check flask.log for details.")
        sys.exit(1)

    # ── 2. Discord Bot ──
    print()
    print("[2/3] Starting Discord bot (run_bot.py)...")
    bot_pid = launch("run_bot.py", "bot.log", ".bot.pid")
    print(f"      PID {bot_pid}")
    print_status("Discord bot", True)

    # ── 3. ngrok Tunnel ──
    print()
    print("[3/3] Starting ngrok tunnel...")
    ngrok_pid = launch_ngrok()
    print(f"      PID {ngrok_pid}")
    print("      Waiting for tunnel...")
    time.sleep(8)
    ngrok_url = get_ngrok_url()
    if ngrok_url:
        print_status("ngrok tunnel", True)
        print(f"      Public URL: {ngrok_url}")
        print(f"      Inspector:  http://127.0.0.1:4040")
    else:
        print_status("ngrok tunnel", False)
        print("      Check ngrok.log. Tunnel may need a moment.")
        print("      Run: curl http://127.0.0.1:4040/api/tunnels")

    print()
    print("=" * 50)
    print("  All services launched")
    print(f"  Flask PID: {flask_pid}")
    print(f"  Bot PID:   {bot_pid}")
    print(f"  ngrok PID: {ngrok_pid}")
    print()
    print("  Logs:")
    print("    flask.log       — Flask server output")
    print("    bot.log         — Discord bot output")
    print("    ngrok.log       — ngrok tunnel output")
    print()
    print("  Dashboard: http://localhost:5000")
    if ngrok_url:
        print(f"  Public:    {ngrok_url}")
    print("=" * 50)
