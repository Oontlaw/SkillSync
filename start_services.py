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
VENV_PYTHON = os.path.join(BASE, ".venv", "Scripts", "python.exe")
PYTHON = VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable
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


def _kill_processes_by_name_and_cmd(name: str, cmd_filters: list[str]):
    """Kill processes matching *name* whose command line contains any of
    *cmd_filters* AND includes the project BASE path.

    Uses PowerShell / Get-CimInstance on Windows (more reliable than WMIC,
    which can fail with "PROCESS - Alias not found").
    Kills only SkillSync-owned processes by requiring the project BASE path in
    the command line, so it never touches unrelated python/ngrok processes.
    """
    for filt in cmd_filters:
        try:
            if filt:
                ps_script = (
                    f"Get-CimInstance Win32_Process -Filter \"name='{name}'\" | "
                    f"Where-Object {{ ($_.CommandLine -like '*{BASE}*') -and "
                    f"($_.CommandLine -like '*{filt}*') }} | "
                    f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
                )
            else:
                # Empty filter = match all with project path
                ps_script = (
                    f"Get-CimInstance Win32_Process -Filter \"name='{name}'\" | "
                    f"Where-Object {{ $_.CommandLine -like '*{BASE}*' }} | "
                    f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"
                )
            subprocess.call(
                ["powershell", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except Exception:
            pass


def _kill_all_skill_sync_processes():
    """Aggressively kill all existing SkillSync processes before starting new ones.

    Strategy:
      1. Kill processes tracked by stale PID files (first pass).
      2. Enumerate all ``python.exe`` processes and kill those whose command
         line contains the project path AND one of the SkillSync script names.
      3. Enumerate all ``ngrok.exe`` processes that are SkillSync-owned
         (project path in command line).
    """
    # First pass — PID files
    for pid_file in (".flask.pid", ".bot.pid", ".ngrok.pid"):
        _kill_old(pid_file)

    # Second pass — scan running python.exe processes (SkillSync-owned only)
    _kill_processes_by_name_and_cmd(
        "python.exe",
        ["run_dashboard.py", "run_bot.py", "app.py", "bot.py", "bot_watchdog.py"],
    )

    # Third pass — kill only SkillSync-owned ngrok.exe instances
    _kill_processes_by_name_and_cmd("ngrok.exe", ["http 5000"])


def launch(script: str, log_name: str, pid_file: str) -> int:
    """Launch a Python script as a detached process with unbuffered output. Returns PID."""
    log_path = os.path.join(BASE, log_name)
    proc = subprocess.Popen(
        [PYTHON, "-u", os.path.join(BASE, script)],
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

    # ── 0. Aggressive cleanup of any lingering processes ──
    print("[0/3] Cleaning up lingering SkillSync processes...")
    _kill_all_skill_sync_processes()
    time.sleep(1)  # brief settling time for killed processes
    print_status("Process cleanup", True)
    print()

    # ── 1. Flask Dashboard ──
    print("[1/3] Starting Flask dashboard (run_dashboard.py)...")
    flask_pid = launch("run_dashboard.py", "flask.log", ".flask.pid")
    print(f"      PID {flask_pid}")
    print("      Waiting for Flask on :5000...")
    if wait_http("http://127.0.0.1:5000/health", timeout=45):
        print_status("Flask dashboard", True)
    else:
        print_status("Flask dashboard", False)
        print("      Check flask.log for details.")
        sys.exit(1)

    # ── 2. Discord Bot (with watchdog) ──
    print()
    print("[2/3] Starting Discord bot via watchdog (bot_watchdog.py)...")
    bot_pid = launch("bot_watchdog.py", "bot.log", ".bot.pid")
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
