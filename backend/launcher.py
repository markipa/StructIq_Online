"""
launcher.py — StructIQ entry point
Starts the FastAPI server then opens the browser automatically.
Used by both the dev workflow and the packaged .exe
"""
import sys
import os
import time
import threading
import webbrowser
import socket
import subprocess

# ── When packaged with PyInstaller, resources live in sys._MEIPASS ──
if getattr(sys, 'frozen', False):
    # Running as compiled .exe
    BASE_DIR = sys._MEIPASS
    # Also set working dir so relative imports work
    os.chdir(os.path.dirname(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

HOST = "127.0.0.1"
PORT = 8000
URL  = f"http://{HOST}:{PORT}"


def is_port_free(port: int) -> bool:
    """Check if a port is available."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) != 0


def find_free_port(start: int = 8000) -> int:
    """Find the first free port starting from `start`."""
    for p in range(start, start + 20):
        if is_port_free(p):
            return p
    return start


def wait_for_server(host: str, port: int, timeout: int = 30) -> bool:
    """Poll until the server accepts connections, then return True."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def open_browser(url: str):
    """Wait until server is actually ready, then open the browser."""
    host, port_str = url.replace("http://", "").split(":")
    ready = wait_for_server(host, int(port_str), timeout=30)
    if not ready:
        return  # Server never started — nothing to open
    try:
        subprocess.run(f'start "" "{url}"', shell=True)
    except Exception:
        try:
            os.startfile(url)
        except Exception:
            webbrowser.open(url)


def run_server(port: int):
    """Start uvicorn in-process."""
    import uvicorn
    # Add BASE_DIR to path so imports work when frozen
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    uvicorn.run(
        "main:app",
        host=HOST,
        port=port,
        log_level="warning",   # quiet in production
        reload=False,
    )


def main():
    global URL
    port = find_free_port(PORT)
    URL  = f"http://{HOST}:{port}"

    print(f"""
  ╔══════════════════════════════════════════╗
  ║         StructIQ  is starting...         ║
  ║                                          ║
  ║   Opening → {URL:<29}║
  ║   Close this window to stop the app.     ║
  ╚══════════════════════════════════════════╝
""")

    # Open browser in background thread
    t = threading.Thread(target=open_browser, args=(URL,), daemon=True)
    t.start()

    # Start server (blocks until window closed)
    run_server(port)


if __name__ == "__main__":
    main()
