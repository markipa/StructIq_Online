"""
StructIQ Bridge — launcher
Starts the local ETABS FastAPI server, then connects to Railway and
proxies ETABS requests back to localhost.  Runs silently in the system tray.
"""
import sys
import os
import json
import threading
import asyncio
import time
import signal

# ── Paths ─────────────────────────────────────────────────────────
_HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
_CONFIG_DIR  = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "StructIQ")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "bridge_config.json")
_LOG_FILE    = os.path.join(_CONFIG_DIR, "bridge.log")

BRIDGE_WS_URL   = os.environ.get("BRIDGE_WS_URL", "wss://structiqonline-production.up.railway.app/ws/bridge")
AUTH_API_URL    = os.environ.get("AUTH_API_URL",  "https://structiqonline-production.up.railway.app/api/auth/login")
LOCAL_PORT      = int(os.environ.get("BRIDGE_LOCAL_PORT", "19999"))

os.makedirs(_CONFIG_DIR, exist_ok=True)

# ── Windows self-registration ─────────────────────────────────────
def _register_windows():
    """
    Run once on first launch:
      1. Register structiq:// URI scheme so the web app can launch the bridge.
      2. Add the bridge to Windows startup so it auto-runs on login.
    """
    if sys.platform != "win32":
        return
    try:
        import winreg

        exe = sys.executable if not getattr(sys, 'frozen', False) else os.path.abspath(sys.argv[0])

        # ── URI scheme: structiq:// ────────────────────────────────
        key_path = r"Software\Classes\structiq"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.SetValue(k, "", winreg.REG_SZ, "StructIQ Bridge")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\shell\open\command") as k:
            winreg.SetValue(k, "", winreg.REG_SZ, f'"{exe}" "%1"')

        # ── Startup entry ──────────────────────────────────────────
        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "StructIQ Bridge", 0, winreg.REG_SZ, f'"{exe}"')

        _log("Registered structiq:// URI scheme and Windows startup entry.")
    except Exception as e:
        _log(f"Windows registration skipped: {e}")


# ── Logging ───────────────────────────────────────────────────────
def _log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Config storage ────────────────────────────────────────────────
def _load_token() -> str:
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f).get("token", "")
    except Exception:
        return ""


def _save_token(token: str, name: str = ""):
    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"token": token, "name": name}, f)
    except Exception:
        pass


def _clear_token():
    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    except Exception:
        pass


# ── Disconnect signal ─────────────────────────────────────────────
_disconnect_requested = False

# ── Tray state ────────────────────────────────────────────────────
_tray_icon = None
_tray_status = "starting"   # "starting" | "connected" | "disconnected" | "no_token"

def _make_icon_image(color: str):
    """Create a simple coloured square icon for the system tray."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill=color)
        # Draw 'S' for StructIQ
        draw.text((20, 16), "S", fill="white")
        return img
    except Exception:
        from PIL import Image
        return Image.new("RGB", (16, 16), color)


_STATUS_COLORS = {
    "starting":     "#f59e0b",   # amber
    "connected":    "#22c55e",   # green
    "disconnected": "#ef4444",   # red
    "no_token":     "#6366f1",   # purple
}

_STATUS_LABELS = {
    "starting":     "StructIQ Bridge — starting…",
    "connected":    "StructIQ Bridge — connected ✓",
    "disconnected": "StructIQ Bridge — reconnecting…",
    "no_token":     "StructIQ Bridge — sign in required",
}


def _update_tray(status: str):
    global _tray_status
    _tray_status = status
    if _tray_icon is None:
        return
    try:
        color = _STATUS_COLORS.get(status, "#94a3b8")
        _tray_icon.icon  = _make_icon_image(color)
        _tray_icon.title = _STATUS_LABELS.get(status, "StructIQ Bridge")
    except Exception:
        pass


def _open_setup():
    """Open the local setup page so the user can log in."""
    import webbrowser
    webbrowser.open(f"http://localhost:{LOCAL_PORT}/bridge-setup")


def _quit_bridge(icon=None, item=None):
    _log("User requested quit.")
    if _tray_icon:
        try:
            _tray_icon.stop()
        except Exception:
            pass
    os.kill(os.getpid(), signal.SIGTERM)


def _start_tray():
    global _tray_icon
    try:
        import pystray
        icon_img = _make_icon_image(_STATUS_COLORS["starting"])
        menu = pystray.Menu(
            pystray.MenuItem("StructIQ Bridge", lambda: None, enabled=False),
            pystray.MenuItem("Setup / Sign In", lambda icon, item: _open_setup()),
            pystray.MenuItem("Quit", _quit_bridge),
        )
        _tray_icon = pystray.Icon("StructIQ Bridge", icon_img, "StructIQ Bridge — starting…", menu)
        _tray_icon.run()   # blocking
    except Exception as e:
        _log(f"Tray unavailable: {e} — running without tray icon")
        # Keep process alive
        try:
            signal.pause()
        except AttributeError:
            while True:
                time.sleep(60)


# ── Local server ──────────────────────────────────────────────────
def _start_local_server():
    """Start bridge/server.py FastAPI on localhost:LOCAL_PORT."""
    try:
        import uvicorn

        # Add the bridge dir to path so server.py can be imported
        sys.path.insert(0, _HERE)
        from server import app as bridge_app

        _log(f"Starting local ETABS server on localhost:{LOCAL_PORT}")
        uvicorn.run(
            bridge_app,
            host="127.0.0.1",
            port=LOCAL_PORT,
            log_level="error",
            log_config=None,     # prevents "Unable to configure formatter" in frozen .exe
            access_log=False,
        )
    except Exception as e:
        _log(f"Local server error: {e}")


# ── Login helper (via Railway API) ────────────────────────────────
def _login(email: str, password: str):
    """Authenticate with Railway and store token. Returns (token, name) or raises."""
    import urllib.request
    payload = json.dumps({"email": email, "password": password}).encode()
    req = urllib.request.Request(
        AUTH_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    token = data["token"]
    name  = data.get("user", {}).get("name", email)
    _save_token(token, name)
    return token, name


# ── Setup endpoint (served via local FastAPI) ─────────────────────
def _add_setup_routes(port: int):
    """Add /bridge-setup and /bridge-login routes to the local server after it starts."""
    pass   # Handled inline in server.py via a startup hook if needed.
    # The standalone setup page is opened directly if no token is found.


# ── WebSocket bridge client ───────────────────────────────────────
async def _bridge_client(token: str):
    """
    Connects to Railway WebSocket, authenticates, then forwards every
    ETABS request to the local backend (localhost:LOCAL_PORT) and sends
    the response back through the WebSocket.
    """
    import websockets
    import httpx

    _log(f"Connecting to Railway bridge at {BRIDGE_WS_URL}")
    reconnect_delay = 5

    while True:
        try:
            async with websockets.connect(
                BRIDGE_WS_URL,
                ping_interval=30,
                ping_timeout=10,
                open_timeout=15,
            ) as ws:
                # Authenticate
                await ws.send(json.dumps({"type": "auth", "token": token}))
                auth_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

                if auth_resp.get("type") != "auth_ok":
                    _log(f"Auth rejected: {auth_resp.get('reason', '?')}")
                    _update_tray("no_token")
                    _clear_token()
                    await asyncio.sleep(30)
                    return   # caller will re-prompt for login

                user_name = auth_resp.get("name", "")
                _log(f"Bridge connected as {user_name}")
                _update_tray("connected")
                reconnect_delay = 5

                async with httpx.AsyncClient(timeout=90.0) as http:
                    async for raw in ws:
                        global _disconnect_requested
                        if _disconnect_requested:
                            _disconnect_requested = False
                            _log("Disconnect requested — closing bridge connection.")
                            _update_tray("no_token")
                            await ws.close()
                            return
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        if msg.get("type") == "pong":
                            continue
                        if msg.get("type") != "request":
                            continue

                        asyncio.create_task(_forward(ws, http, msg))

        except Exception as e:
            _log(f"Bridge connection lost: {e} — retrying in {reconnect_delay}s")
            _update_tray("disconnected")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)


async def _forward(ws, http, msg: dict):
    """Forward one ETABS request from Railway to localhost and send back the response."""
    rid    = msg.get("request_id", "?")
    method = msg.get("method", "GET").upper()
    path   = msg.get("path", "/")
    body   = msg.get("body")
    params = msg.get("params") or {}

    url = f"http://localhost:{LOCAL_PORT}{path}"
    try:
        if method == "GET":
            r = await http.get(url, params=params)
        elif method == "POST":
            r = await http.post(url, json=body, params=params)
        else:
            r = await http.request(method, url, json=body, params=params)

        try:
            resp_body = r.json()
        except Exception:
            resp_body = {"detail": r.text}

        await ws.send(json.dumps({
            "type":       "response",
            "request_id": rid,
            "status":     r.status_code,
            "body":       resp_body,
        }))
    except Exception as e:
        _log(f"Forward error [{method} {path}]: {e}")
        await ws.send(json.dumps({
            "type":       "response",
            "request_id": rid,
            "status":     502,
            "body":       {"detail": str(e)},
        }))


# ── Simple login setup page served locally ────────────────────────
_SETUP_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StructIQ Bridge — Sign In</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0b1827;display:flex;align-items:center;justify-content:center;min-height:100vh;}
.card{background:#102039;border-radius:16px;padding:40px;width:360px;
      border:1px solid rgba(255,255,255,.07);}
h2{color:#f1f5f9;font-size:1.2rem;margin-bottom:20px;text-align:center;}
label{display:block;color:#94a3b8;font-size:12px;font-weight:600;
      letter-spacing:.06em;margin-bottom:6px;}
input{width:100%;background:#05101e;border:1px solid rgba(255,255,255,.12);
      border-radius:8px;color:#f1f5f9;padding:10px 14px;font-size:14px;
      outline:none;margin-bottom:14px;}
input:focus{border-color:#3b82f6;}
button{width:100%;background:#3b82f6;color:#fff;border:none;border-radius:8px;
       padding:12px;font-size:14px;font-weight:600;cursor:pointer;margin-top:4px;}
button:hover{opacity:.9;}
#msg{color:#ef4444;font-size:13px;margin-top:12px;text-align:center;min-height:20px;}
#ok{color:#22c55e;font-size:13px;margin-top:12px;text-align:center;display:none;}
</style></head><body>
<div class="card">
  <h2>StructIQ Bridge — Sign In</h2>
  <label>EMAIL</label>
  <input id="email" type="email" placeholder="you@firm.com" autocomplete="email">
  <label>PASSWORD</label>
  <input id="password" type="password" placeholder="••••••••" autocomplete="current-password">
  <button onclick="doLogin()">Connect Bridge</button>
  <div id="msg"></div>
  <div id="ok">Bridge connected! You can close this window.</div>
</div>
<script>
async function doLogin() {
  const email    = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value;
  document.getElementById('msg').textContent = '';
  if (!email || !password) { document.getElementById('msg').textContent='Enter email and password.'; return; }
  try {
    const res = await fetch('/bridge-login', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({email, password})
    });
    const data = await res.json();
    if (res.ok) {
      document.getElementById('ok').style.display='block';
    } else {
      document.getElementById('msg').textContent = data.detail || 'Login failed.';
    }
  } catch(e) {
    document.getElementById('msg').textContent = 'Connection error.';
  }
}
document.addEventListener('keydown', e => { if (e.key==='Enter') doLogin(); });
</script></body></html>"""


def _add_setup_to_server(app, token_holder: list):
    """Inject /bridge-setup and /bridge-login into the local FastAPI app."""
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel

    class _LoginBody(BaseModel):
        email: str
        password: str

    @app.get("/bridge-setup", response_class=HTMLResponse)
    def setup_page():
        return _SETUP_HTML

    @app.post("/bridge-login")
    def bridge_login(body: _LoginBody):
        try:
            token, name = _login(body.email, body.password)
            token_holder.append(token)
            _log(f"Logged in as {name}")
            return {"ok": True, "name": name}
        except Exception as e:
            from fastapi import HTTPException
            raise HTTPException(401, f"Login failed: {e}")

    @app.post("/bridge-disconnect")
    def bridge_disconnect():
        global _disconnect_requested
        _clear_token()
        _disconnect_requested = True
        _update_tray("no_token")
        _log("Bridge disconnected by user.")
        return {"ok": True}


# ── Main entry point ──────────────────────────────────────────────
def main():
    _log("StructIQ Bridge starting…")
    _register_windows()
    _update_tray("starting")

    token_holder: list = []

    # ── Start local ETABS server in background thread ─────────────
    server_thread = threading.Thread(target=_start_local_server, daemon=True)
    server_thread.start()

    # Wait for local server to be ready
    import urllib.request
    for _ in range(20):
        try:
            urllib.request.urlopen(f"http://localhost:{LOCAL_PORT}/api/status", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    # Inject setup routes into server (patch after import)
    try:
        sys.path.insert(0, _HERE)
        from server import app as bridge_app
        _add_setup_to_server(bridge_app, token_holder)
    except Exception as e:
        _log(f"Could not add setup routes: {e}")

    # ── Start system tray in background thread ─────────────────────
    tray_thread = threading.Thread(target=_start_tray, daemon=True)
    tray_thread.start()

    # ── Bridge WebSocket client loop ──────────────────────────────
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        while True:
            token = _load_token()
            if not token:
                if token_holder:
                    token = token_holder[-1]
                else:
                    _log("No token — waiting for login via web app (localhost:{}/bridge-login)".format(LOCAL_PORT))
                    _update_tray("no_token")
                    # Wait silently — web app will POST to /bridge-login when user signs in
                    while not _load_token() and not token_holder:
                        await asyncio.sleep(2)
                    continue

            await _bridge_client(token)
            # If _bridge_client returns (auth failure), clear token and re-prompt
            await asyncio.sleep(2)

    try:
        loop.run_until_complete(_run())
    except (KeyboardInterrupt, SystemExit):
        _log("Bridge shutting down.")


if __name__ == "__main__":
    main()
