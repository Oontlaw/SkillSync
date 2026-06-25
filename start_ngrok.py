"""
Persistent ngrok launcher for Windows.
Starts ngrok as a fully detached process (survives terminal close).
Writes the PID to .ngrok.pid so stop.bat / this script can kill it cleanly.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

NGROK_EXE = os.path.join(os.path.dirname(__file__), ".venv", "Scripts", "ngrok.exe")
NGROK_URL = "appendage-uptake-aflutter.ngrok-free.dev"
FLASK_PORT = 5000
PID_FILE = os.path.join(os.path.dirname(__file__), ".ngrok.pid")
API_BASE = "http://127.0.0.1:4040"

# Windows process-creation flags
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROC_GRP = 0x00000200


def kill_existing():
    """Kill any ngrok process recorded in .ngrok.pid, plus stray ngrok.exe processes."""
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
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
    # Belt-and-suspenders: kill any stray ngrok.exe
    subprocess.call(
        ["taskkill", "/F", "/IM", "ngrok.exe"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)


def start_ngrok():
    """Launch ngrok as a detached Windows process."""
    if not os.path.exists(NGROK_EXE):
        print(f"[ERROR] ngrok not found at {NGROK_EXE}")
        sys.exit(1)

    cmd = [
        NGROK_EXE,
        "http",
        f"--url={NGROK_URL}",
        f"--log=stdout",
        "--log-level=warn",
        str(FLASK_PORT),
    ]

    flags = DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROC_GRP
    proc = subprocess.Popen(
        cmd,
        stdout=open(os.path.join(os.path.dirname(__file__), "ngrok.log"), "w"),
        stderr=subprocess.STDOUT,
        creationflags=flags,
        close_fds=True,
    )
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))
    return proc.pid


def wait_for_tunnel(timeout=15):
    """Poll ngrok's local API until the tunnel is up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{API_BASE}/api/tunnels", timeout=2) as r:
                data = json.loads(r.read())
                tunnels = data.get("tunnels", [])
                if tunnels:
                    return tunnels[0]["public_url"]
        except Exception:
            pass
        time.sleep(1)
    return None


def check_dashboard(url, path="/", label="Dashboard"):
    """Return HTTP status code for a URL path via ngrok."""
    full = url.rstrip("/") + path
    req = urllib.request.Request(full, headers={"ngrok-skip-browser-warning": "true"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.getcode()
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        code = f"ERROR: {e}"
    ok = "✅" if str(code) == "200" else "❌"
    print(f"  {ok}  {label:<20} {full}  →  HTTP {code}")
    return code


if __name__ == "__main__":
    print("[1/4] Killing any existing ngrok...")
    kill_existing()

    print("[2/4] Starting ngrok (detached)...")
    pid = start_ngrok()
    print(f"      PID: {pid}  →  log: ngrok.log")

    print("[3/4] Waiting for tunnel to come up...")
    public_url = wait_for_tunnel(timeout=20)
    if not public_url:
        print("[ERROR] Tunnel did not start within 20 s. Check ngrok.log.")
        sys.exit(1)
    print(f"      Tunnel: {public_url}")

    print("[4/4] Verifying dashboards...")
    check_dashboard(public_url, "/", "Dashboard v1 (/)")
    check_dashboard(public_url, "/v2/", "Dashboard v2 (/v2/)")

    print()
    print("=" * 56)
    print(f"  ngrok live  →  {public_url}")
    print(f"  v1  →  {public_url}/")
    print(f"  v2  →  {public_url}/v2/")
    print(f"  Auth callback  →  {public_url}/auth/callback")
    print("=" * 56)
