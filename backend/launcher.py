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
    # Running as compiled .exe — all files extracted to _MEIPASS (_internal/)
    BASE_DIR = sys._MEIPASS
    os.chdir(BASE_DIR)  # chdir to _MEIPASS so frontend/, etabs_api/ etc. are found
    # Point requests at the bundled CA certificate so HTTPS works in the .exe
    _cert = os.path.join(BASE_DIR, 'certifi', 'cacert.pem')
    if os.path.exists(_cert):
        os.environ.setdefault('SSL_CERT_FILE',      _cert)
        os.environ.setdefault('REQUESTS_CA_BUNDLE', _cert)
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


def log(msg: str):
    """Write to console AND a crash log file next to the exe."""
    print(msg)
    try:
        log_path = os.path.join(os.path.dirname(sys.executable)
                                if getattr(sys, 'frozen', False)
                                else BASE_DIR, "structiq.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def run_server(port: int):
    """Start uvicorn in-process."""
    # Add BASE_DIR to path so imports work when frozen
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    log(f"[launcher] sys.path = {sys.path[:3]}")
    log(f"[launcher] BASE_DIR = {BASE_DIR}")
    log(f"[launcher] cwd      = {os.getcwd()}")
    try:
        import uvicorn
        from main import app   # direct import — string ref fails in frozen .exe
        uvicorn.run(
            app,
            host=HOST,
            port=port,
            log_level="info",
            reload=False,
        )
    except Exception as e:
        log(f"[ERROR] Server failed to start: {e}")
        import traceback
        log(traceback.format_exc())
        input("Press Enter to exit...")   # keep window open on crash


def main():
    global URL
    port = find_free_port(PORT)
    URL  = f"http://{HOST}:{port}"

    log(f"""
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
    try:
        main()
    except Exception as e:
        log(f"[FATAL] {e}")
        import traceback
        log(traceback.format_exc())
        input("Press Enter to exit...")
