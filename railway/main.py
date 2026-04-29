"""
Railway Cloud Server — StructIQ Auth & Billing
Handles: user accounts, sessions, subscription plans
Deployed to Railway.app — no ETABS code here
"""
from fastapi import FastAPI, HTTPException, Request, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
import database
import httpx
import hmac
import hashlib
import os
import asyncio
import importlib.util as _ilu

from bridge_registry import bridge_registry

# ─── Lemon Squeezy config ─────────────────────────────────────────
LS_API_KEY         = os.environ.get("LS_API_KEY", "")
LS_WEBHOOK_SECRET  = os.environ.get("LS_WEBHOOK_SECRET", "")
LS_STORE_ID        = os.environ.get("LS_STORE_ID", "")
LS_VARIANT_MONTHLY = os.environ.get("LS_VARIANT_MONTHLY", "")
LS_VARIANT_YEARLY  = os.environ.get("LS_VARIANT_YEARLY", "")
BASE_URL           = os.environ.get("BASE_URL", "https://structiq-online.up.railway.app")

app = FastAPI(title="StructIQ — Auth Server")

# Initialise DB on startup
database.init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Auth middleware ──────────────────────────────────────────────

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Key-protected endpoints called by the desktop app without a user session
    _KEY_PATHS = ("/api/auth/", "/api/plan", "/api/session/")
    if path.startswith("/api/") and not any(path.startswith(p) for p in _KEY_PATHS):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        token = auth[7:]
        user = database.get_user_by_token(token)
        if user is None:
            return JSONResponse(status_code=401, content={"detail": "Invalid or expired session"})
    return await call_next(request)


# ─── Models ──────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: str
    name: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class UpdatePlanRequest(BaseModel):
    user_id: int
    plan: str
    admin_secret: str

class SetPlanByEmailRequest(BaseModel):
    email: str
    plan: str
    admin_secret: str

class SetExpirationRequest(BaseModel):
    email: str
    expiration_date: str   # "YYYY-MM-DD" or "" to clear
    admin_secret: str

class ToggleActiveRequest(BaseModel):
    email: str
    is_active: bool
    admin_secret: str

class CreateCheckoutRequest(BaseModel):
    email: str
    interval: str   # "monthly" | "yearly"


# ─── Auth routes ─────────────────────────────────────────────────

@app.post("/api/auth/register")
def register(body: RegisterRequest):
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user = database.create_user(body.email, body.name, body.password)
    if user is None:
        raise HTTPException(409, "Email already registered")
    token = database.create_session(user["id"])
    return {"token": token, "user": user}


@app.post("/api/auth/login")
def login(body: LoginRequest):
    row = database.get_user_by_email(body.email)
    if row is None or not database.verify_password(body.password, row["password"], row["salt"]):
        raise HTTPException(401, "Invalid email or password")
    if not row["is_active"]:
        raise HTTPException(403, "Account disabled")
    token = database.create_session(row["id"])
    database.update_last_access(row["id"])
    user  = database.get_user_by_id(row["id"])
    return {"token": token, "user": user}


@app.get("/api/auth/me")
def me(request: Request):
    token = request.headers.get("Authorization", "")[7:]
    user  = database.get_user_by_token(token)
    if not user:
        raise HTTPException(401, "Invalid or expired session")
    return user


@app.post("/api/auth/logout")
def logout(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        database.delete_session(auth[7:])
    return {"ok": True}


# ─── Plan sync + session enforcement ─────────────────────────────

PLAN_SYNC_KEY = os.environ.get("PLAN_SYNC_KEY", "StructIQ-plan-sync-2026")

@app.get("/api/plan")
def get_plan_by_email(email: str, key: str):
    """
    Desktop app calls this to sync plan without needing a Railway session.
    Used for users whose Railway account was auto-created (no password known).
    Protected by PLAN_SYNC_KEY env var.
    """
    if not key or key != PLAN_SYNC_KEY:
        raise HTTPException(403, "Forbidden")
    user = database.get_user_by_email(email)
    if not user:
        return {"plan": "free", "found": False}
    return {"plan": user["plan"], "email": user["email"], "found": True}


# ─── Global session enforcement endpoints ────────────────────────

@app.post("/api/session/register")
def session_register(email: str, session_key: str, key: str):
    """
    Called by the desktop app on every login.
    Registers the session globally — enforces plan session limits.
    Pro/free: kicks oldest session (1 device at a time).
    Enterprise: allows up to 3 simultaneous sessions; rejects 4th.
    """
    if not key or key != PLAN_SYNC_KEY:
        raise HTTPException(403, "Forbidden")
    user = database.get_user_by_email(email)
    if not user:
        raise HTTPException(404, "User not found")
    result = database.register_cloud_session(user["id"], session_key, user["plan"])
    if not result["ok"]:
        raise HTTPException(429, detail=result.get("message", "Session limit reached"))
    return {"ok": True, "plan": user["plan"]}


@app.post("/api/session/validate")
def session_validate(session_key: str, key: str):
    """
    Called periodically by the desktop app to confirm session is still active.
    Returns {"valid": false} if another login has kicked this session out.
    """
    if not key or key != PLAN_SYNC_KEY:
        raise HTTPException(403, "Forbidden")
    valid = database.validate_cloud_session(session_key)
    return {"valid": valid}


@app.post("/api/session/revoke")
def session_revoke(session_key: str, key: str):
    """Called by the desktop app on logout to free up the session slot."""
    if not key or key != PLAN_SYNC_KEY:
        raise HTTPException(403, "Forbidden")
    database.revoke_cloud_session(session_key)
    return {"ok": True}


# ─── License check (called by desktop app on startup) ────────────

@app.get("/api/license/check")
def license_check(request: Request):
    """
    Called by the desktop app every time it starts.
    Returns plan info so the app knows what features to unlock.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    user = database.get_user_by_token(auth[7:])
    if user is None:
        raise HTTPException(401, "Invalid or expired session")
    return {
        "valid":    True,
        "plan":     user["plan"],
        "name":     user["name"],
        "email":    user["email"],
    }


# ─── Admin endpoints (protected by ADMIN_SECRET) ─────────────────

def _check_admin(secret: str):
    """Raise 403 if secret doesn't match the Railway env var."""
    expected = os.environ.get("ADMIN_SECRET", "change-me-in-railway")
    if secret != expected:
        raise HTTPException(403, "Invalid admin secret")

VALID_PLANS = ("free", "pro", "enterprise")


@app.get("/admin/users")
def list_users(secret: str = ""):
    """List all registered users. Pass ?secret=YOUR_ADMIN_SECRET"""
    _check_admin(secret)
    users = database.get_all_users()
    return {"count": len(users), "users": users}


@app.post("/admin/set-plan")
def set_plan_by_email(body: SetPlanByEmailRequest):
    """
    Upgrade or downgrade a user by email.
    Body: { email, plan, admin_secret }
    Plans: free | pro | enterprise
    """
    _check_admin(body.admin_secret)
    if body.plan not in VALID_PLANS:
        raise HTTPException(400, f"Invalid plan. Choose from: {VALID_PLANS}")
    updated = database.update_user_plan_by_email(body.email, body.plan)
    if not updated:
        raise HTTPException(404, f"No user found with email: {body.email}")
    return {"ok": True, "email": body.email, "plan": body.plan}


@app.post("/api/admin/update-plan")
def update_plan(body: UpdatePlanRequest):
    """Legacy: upgrade by user_id. Prefer /admin/set-plan (uses email)."""
    _check_admin(body.admin_secret)
    if body.plan not in VALID_PLANS:
        raise HTTPException(400, "Invalid plan")
    database.update_user_plan(body.user_id, body.plan)
    return {"ok": True, "user_id": body.user_id, "plan": body.plan}


# ─── Lemon Squeezy endpoints ─────────────────────────────────────

@app.post("/stripe/create-checkout")
def create_checkout(body: CreateCheckoutRequest):
    """Create a Lemon Squeezy Checkout session. Returns {checkout_url}."""
    if not LS_API_KEY or not LS_STORE_ID:
        raise HTTPException(503, "Billing not configured")

    # Auto-create a free account if user hasn't synced to Railway yet
    # so the webhook can upgrade them after payment completes.
    user = database.get_user_by_email(body.email)
    if not user:
        user = database.create_free_user_by_email(body.email)
        if not user:
            raise HTTPException(500, "Could not initialise user account")

    variant_id = LS_VARIANT_YEARLY if body.interval == "yearly" else LS_VARIANT_MONTHLY
    if not variant_id:
        raise HTTPException(503, "Billing variant not configured")

    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {
                    "email": body.email,
                    "custom": {
                        "user_email": body.email,
                        "interval":   body.interval,
                    },
                },
                "product_options": {
                    "redirect_url": f"{BASE_URL}/stripe/success",
                },
            },
            "relationships": {
                "store":   {"data": {"type": "stores",   "id": str(LS_STORE_ID)}},
                "variant": {"data": {"type": "variants", "id": str(variant_id)}},
            },
        }
    }

    try:
        r = httpx.post(
            "https://api.lemonsqueezy.com/v1/checkouts",
            headers={
                "Authorization": f"Bearer {LS_API_KEY}",
                "Accept":        "application/vnd.api+json",
                "Content-Type":  "application/vnd.api+json",
            },
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Billing error: {e.response.text}")
    except Exception as e:
        raise HTTPException(503, f"Could not reach billing server: {e}")

    checkout_url = r.json()["data"]["attributes"]["url"]
    return {"checkout_url": checkout_url}


def _verify_ls_signature(payload: bytes, signature: str) -> bool:
    """Verify Lemon Squeezy webhook HMAC-SHA256 signature."""
    if not LS_WEBHOOK_SECRET:
        return True   # skip verification in dev if secret not set
    digest = hmac.new(LS_WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


@app.post("/stripe/webhook")
async def ls_webhook(request: Request):
    """Lemon Squeezy sends payment events here — upgrades/downgrades plans automatically."""
    payload   = await request.body()
    signature = request.headers.get("x-signature", "")

    if not _verify_ls_signature(payload, signature):
        raise HTTPException(400, "Invalid webhook signature")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    event_name  = body.get("meta", {}).get("event_name", "")
    custom_data = body.get("meta", {}).get("custom_data", {})
    attrs       = body.get("data", {}).get("attributes", {})

    # Email from custom_data (set at checkout) or fallback to attributes
    email    = custom_data.get("user_email") or attrs.get("user_email", "")
    interval = custom_data.get("interval", "monthly")
    sub_id   = str(body.get("data", {}).get("id", ""))

    if event_name == "subscription_created" and email:
        database.update_user_plan_by_email(email, "pro")
        database.update_ls_subscription(email, sub_id, interval)

    elif event_name == "subscription_updated" and email:
        status = attrs.get("status", "active")
        if status in ("cancelled", "expired", "unpaid", "paused"):
            database.update_user_plan_by_email(email, "free")

    elif event_name in ("subscription_cancelled", "subscription_expired") and email:
        database.update_user_plan_by_email(email, "free")

    return {"ok": True}


def _fetch_portal_url(sub_id: str) -> str:
    """Fetch customer_portal URL from Lemon Squeezy for a given subscription ID."""
    if not LS_API_KEY:
        raise HTTPException(503, "Billing not configured")
    try:
        r = httpx.get(
            f"https://api.lemonsqueezy.com/v1/subscriptions/{sub_id}",
            headers={
                "Authorization": f"Bearer {LS_API_KEY}",
                "Accept":        "application/vnd.api+json",
            },
            timeout=15,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Billing error: {e.response.text}")
    except Exception as e:
        raise HTTPException(503, f"Could not reach billing server: {e}")

    portal_url = r.json()["data"]["attributes"].get("urls", {}).get("customer_portal", "")
    if not portal_url:
        raise HTTPException(502, "Billing server did not return a portal URL")
    return portal_url


@app.get("/api/billing/portal")
def get_billing_portal(request: Request):
    """Return portal URL — Bearer token path (for users with a Railway session)."""
    auth  = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    user  = database.get_user_by_token(token)
    if not user:
        raise HTTPException(401, "Invalid or expired session")
    sub_id = user.get("ls_subscription_id")
    if not sub_id:
        raise HTTPException(404, "No active subscription found for this account")
    return {"portal_url": _fetch_portal_url(sub_id)}


@app.get("/api/billing/portal-by-email")
def get_billing_portal_by_email(email: str, key: str):
    """
    Return portal URL — email + key path (for users auto-created by webhook
    who have no Railway session token stored in the desktop app).
    """
    if not key or key != PLAN_SYNC_KEY:
        raise HTTPException(403, "Forbidden")
    user = database.get_user_by_email(email)
    if not user:
        raise HTTPException(404, "No account found")
    sub_id = user.get("ls_subscription_id")
    if not sub_id:
        raise HTTPException(404, "No active subscription found for this account")
    return {"portal_url": _fetch_portal_url(sub_id)}


@app.get("/stripe/success", response_class=HTMLResponse)
def stripe_success():
    return """<!DOCTYPE html>
<html><head><title>Payment Successful — StructIQ</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box;}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#0b1827;display:flex;align-items:center;
       justify-content:center;min-height:100vh;}
  .card{background:#102039;border-radius:16px;padding:48px 40px;
        max-width:420px;width:90%;text-align:center;
        border:1px solid rgba(255,255,255,0.07);}
  .icon{font-size:56px;margin-bottom:16px;}
  h1{color:#22c55e;font-size:1.5rem;margin-bottom:12px;}
  p{color:#94a3b8;line-height:1.6;margin-bottom:12px;}
  strong{color:#f1f5f9;}
  .badge{display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);
         color:#fff;font-size:11px;font-weight:800;letter-spacing:.08em;
         padding:4px 14px;border-radius:6px;margin-bottom:20px;}
</style></head>
<body><div class="card">
  <div class="icon">✅</div>
  <div class="badge">PRO</div>
  <h1>Payment Successful!</h1>
  <p>Your <strong>StructIQ PRO</strong> subscription is now active.</p>
  <p>Please <strong>restart StructIQ</strong> on your computer to unlock all PRO features.</p>
</div></body></html>"""


@app.get("/stripe/cancel", response_class=HTMLResponse)
def stripe_cancel():
    return """<!DOCTYPE html>
<html><head><title>Payment Cancelled — StructIQ</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box;}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#0b1827;display:flex;align-items:center;
       justify-content:center;min-height:100vh;}
  .card{background:#102039;border-radius:16px;padding:48px 40px;
        max-width:420px;width:90%;text-align:center;
        border:1px solid rgba(255,255,255,0.07);}
  .icon{font-size:56px;margin-bottom:16px;}
  h1{color:#f97316;font-size:1.5rem;margin-bottom:12px;}
  p{color:#94a3b8;line-height:1.6;}
</style></head>
<body><div class="card">
  <div class="icon">❌</div>
  <h1>Payment Cancelled</h1>
  <p>No charge was made. You can try again anytime from the StructIQ app.</p>
</div></body></html>"""


# ─── License manager admin endpoints ─────────────────────────────

@app.post("/admin/set-expiration")
def set_expiration(body: SetExpirationRequest):
    """Set or clear expiration date for a user. Pass '' to clear (never expires)."""
    _check_admin(body.admin_secret)
    database.set_expiration(body.email, body.expiration_date or None)
    return {"ok": True, "email": body.email, "expiration_date": body.expiration_date or None}


@app.post("/admin/toggle-active")
def toggle_active(body: ToggleActiveRequest):
    """Enable or disable a user account."""
    _check_admin(body.admin_secret)
    database.set_user_active(body.email, 1 if body.is_active else 0)
    return {"ok": True, "email": body.email, "is_active": body.is_active}


@app.get("/admin/export-csv")
def export_csv(secret: str = ""):
    """Download all users as a CSV file."""
    from fastapi.responses import StreamingResponse
    import io, csv
    _check_admin(secret)
    users = database.get_all_users()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "name", "email", "plan", "status",
                     "expiration_date", "registered", "last_access"])
    from datetime import date
    today = date.today().isoformat()
    for u in users:
        exp = u.get("expiration_date") or ""
        if not u["is_active"]:
            status = "DISABLED"
        elif exp and exp <= today:
            status = "EXPIRED"
        else:
            status = "ACTIVE"
        writer.writerow([
            u["id"], u["name"], u["email"], u["plan"].upper(), status,
            exp, (u.get("created_at") or "")[:10], (u.get("last_access") or "")[:16],
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=structiq-licenses.csv"},
    )


@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(secret: str = ""):
    """Full license manager dashboard. Access: /admin/dashboard?secret=YOUR_SECRET"""
    expected = os.environ.get("ADMIN_SECRET", "change-me-in-railway")
    if secret != expected:
        return HTMLResponse("""<!DOCTYPE html>
<html><head><title>StructIQ Admin</title>
<style>*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:-apple-system,sans-serif;background:#05101e;display:flex;
     align-items:center;justify-content:center;min-height:100vh;}
.card{background:#0c1929;border:1px solid rgba(255,255,255,.08);border-radius:14px;
      padding:40px;text-align:center;max-width:360px;}
h2{color:#f1f5f9;margin-bottom:8px;}p{color:#94a3b8;font-size:14px;}
</style></head><body><div class="card">
<h2>Access Denied</h2><p>Invalid or missing admin secret.</p>
</div></body></html>""", status_code=403)

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StructIQ — License Manager</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#05101e;color:#f1f5f9;min-height:100vh;}}
:root{{--blue:#3b82f6;--green:#22c55e;--red:#ef4444;--yellow:#f59e0b;
      --purple:#a78bfa;--bg2:#0c1929;--bg3:#102039;--bdr:rgba(255,255,255,.07);
      --t2:#94a3b8;--t3:#475569;}}

/* ── Header ── */
.header{{background:#0c1929;border-bottom:1px solid var(--bdr);
         padding:0 24px;height:56px;display:flex;align-items:center;gap:16px;}}
.logo{{display:flex;align-items:center;gap:10px;font-weight:700;font-size:15px;}}
.logo svg{{flex-shrink:0;}}
.header-title{{color:var(--t2);font-size:13px;margin-left:4px;}}
.header-right{{margin-left:auto;display:flex;gap:8px;}}
.btn{{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;
      border-radius:8px;border:none;cursor:pointer;font-size:13px;font-weight:500;
      transition:opacity .15s;}}
.btn:hover{{opacity:.85;}}
.btn-ghost{{background:rgba(255,255,255,.06);color:#f1f5f9;}}
.btn-blue{{background:var(--blue);color:#fff;}}
.btn-green{{background:#16a34a;color:#fff;}}
.btn-red{{background:#dc2626;color:#fff;}}
.btn-sm{{padding:5px 10px;font-size:12px;}}

/* ── Stats bar ── */
.stats{{background:#0a1628;border-bottom:1px solid var(--bdr);
        padding:10px 24px;display:flex;gap:24px;align-items:center;font-size:13px;}}
.stat{{display:flex;align-items:center;gap:6px;color:var(--t2);}}
.stat strong{{color:#f1f5f9;}}
.stat-div{{width:1px;height:16px;background:var(--bdr);}}

/* ── Action bar ── */
.action-bar{{padding:10px 24px;background:#0c1929;border-bottom:1px solid var(--bdr);
             display:flex;gap:8px;align-items:center;min-height:48px;}}
.action-bar .hint{{color:var(--t3);font-size:12px;}}
.selected-label{{font-size:12px;color:var(--t2);background:rgba(59,130,246,.12);
                 border:1px solid rgba(59,130,246,.25);border-radius:6px;
                 padding:4px 10px;}}

/* ── Table ── */
.table-wrap{{overflow-x:auto;padding:0 24px 24px;}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:16px;}}
thead th{{background:#0c1929;color:var(--t2);font-weight:600;font-size:11px;
          letter-spacing:.06em;text-transform:uppercase;padding:10px 12px;
          border-bottom:1px solid var(--bdr);text-align:left;white-space:nowrap;}}
tbody tr{{border-bottom:1px solid rgba(255,255,255,.04);cursor:pointer;
          transition:background .1s;}}
tbody tr:hover{{background:rgba(59,130,246,.06);}}
tbody tr.selected{{background:rgba(59,130,246,.12);}}
td{{padding:9px 12px;vertical-align:middle;white-space:nowrap;}}
td.wrap{{white-space:normal;max-width:200px;}}
.num{{color:var(--t3);font-size:11px;}}
.status-chip{{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;
              border-radius:20px;font-size:11px;font-weight:700;letter-spacing:.05em;}}
.status-active{{background:rgba(34,197,94,.15);color:#22c55e;
                border:1px solid rgba(34,197,94,.3);}}
.status-expired{{background:rgba(239,68,68,.15);color:#ef4444;
                 border:1px solid rgba(239,68,68,.3);}}
.status-disabled{{background:rgba(148,163,184,.1);color:#64748b;
                  border:1px solid rgba(148,163,184,.2);}}
.plan-chip{{display:inline-block;padding:2px 8px;border-radius:4px;
            font-size:11px;font-weight:700;letter-spacing:.06em;}}
.plan-free{{background:rgba(148,163,184,.12);color:#94a3b8;}}
.plan-pro{{background:rgba(59,130,246,.18);color:#60a5fa;}}
.plan-enterprise{{background:rgba(124,58,237,.18);color:#a78bfa;}}
.days-ok{{color:#22c55e;}}
.days-warn{{color:#f59e0b;}}
.days-expired{{color:#ef4444;}}
.days-forever{{color:var(--t3);}}
.id-chip{{font-family:monospace;font-size:11px;color:var(--t3);}}
.empty{{text-align:center;padding:48px;color:var(--t3);}}

/* ── Modal ── */
.modal-bg{{position:fixed;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(4px);
           display:flex;align-items:center;justify-content:center;z-index:100;}}
.modal-bg.hidden{{display:none;}}
.modal{{background:#0c1929;border:1px solid var(--bdr);border-radius:14px;
        padding:28px;width:360px;max-width:90vw;}}
.modal h3{{font-size:15px;margin-bottom:16px;}}
.modal-field{{margin-bottom:14px;}}
.modal-field label{{display:block;font-size:11px;color:var(--t2);
                    font-weight:600;margin-bottom:6px;letter-spacing:.06em;}}
.modal-field select,.modal-field input{{width:100%;background:#05101e;
  border:1px solid rgba(255,255,255,.12);border-radius:8px;color:#f1f5f9;
  padding:8px 12px;font-size:13px;outline:none;}}
.modal-field select:focus,.modal-field input:focus{{border-color:var(--blue);}}
.modal-footer{{display:flex;gap:8px;margin-top:20px;justify-content:flex-end;}}
.tag{{font-size:11px;color:var(--t2);background:rgba(255,255,255,.05);
      border-radius:4px;padding:2px 6px;}}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="logo">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
      <rect x="2" y="14" width="4" height="8" rx="1" fill="#3b82f6"/>
      <rect x="8" y="9"  width="4" height="13" rx="1" fill="#60a5fa"/>
      <rect x="14" y="4" width="4" height="18" rx="1" fill="#93c5fd"/>
      <rect x="20" y="11" width="2" height="11" rx="1" fill="#bfdbfe"/>
    </svg>
    StructIQ
  </div>
  <span class="header-title">License Manager</span>
  <div class="header-right">
    <button class="btn btn-ghost" onclick="loadUsers()">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2.5" stroke-linecap="round"><path d="M23 4v6h-6"/>
        <path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
      </svg>Refresh
    </button>
    <button class="btn btn-ghost" onclick="exportCSV()">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2.5" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
        <polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
      </svg>Export CSV
    </button>
  </div>
</div>

<!-- Stats -->
<div class="stats" id="stats-bar">
  <span class="stat">Total: <strong id="s-total">—</strong></span>
  <span class="stat-div"></span>
  <span class="stat">&#10003; Active: <strong id="s-active" style="color:#22c55e">—</strong></span>
  <span class="stat">&#10007; Expired: <strong id="s-expired" style="color:#ef4444">—</strong></span>
  <span class="stat">Disabled: <strong id="s-disabled" style="color:#64748b">—</strong></span>
  <span class="stat-div"></span>
  <span class="stat">Free: <strong id="s-free">—</strong></span>
  <span class="stat">PRO: <strong id="s-pro" style="color:#60a5fa">—</strong></span>
  <span class="stat">Enterprise: <strong id="s-ent" style="color:#a78bfa">—</strong></span>
</div>

<!-- Action bar -->
<div class="action-bar" id="action-bar">
  <span class="hint" id="action-hint">Select a row to perform actions</span>
  <span class="selected-label hidden" id="selected-label"></span>
  <button class="btn btn-ghost btn-sm hidden" id="btn-change-plan" onclick="openPlanModal()">Change Plan</button>
  <button class="btn btn-ghost btn-sm hidden" id="btn-set-expiry" onclick="openExpiryModal()">Set Expiration</button>
  <button class="btn btn-ghost btn-sm hidden" id="btn-toggle-active" onclick="toggleActive()">Disable</button>
</div>

<!-- Table -->
<div class="table-wrap">
  <table id="users-table">
    <thead>
      <tr>
        <th>#</th>
        <th>Status</th>
        <th>Name</th>
        <th>Email</th>
        <th>Plan</th>
        <th>Expiration Date</th>
        <th>Days Left</th>
        <th>Registered</th>
        <th>Last Access</th>
        <th>User ID</th>
      </tr>
    </thead>
    <tbody id="users-tbody">
      <tr><td colspan="10" class="empty">Loading...</td></tr>
    </tbody>
  </table>
</div>

<!-- Change Plan Modal -->
<div class="modal-bg hidden" id="plan-modal">
  <div class="modal">
    <h3>Change Plan</h3>
    <div class="modal-field">
      <label>USER</label>
      <div id="plan-modal-email" style="font-size:13px;color:#f1f5f9;padding:4px 0;"></div>
    </div>
    <div class="modal-field">
      <label>NEW PLAN</label>
      <select id="plan-select">
        <option value="free">Free</option>
        <option value="pro">PRO</option>
        <option value="enterprise">Enterprise</option>
      </select>
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost btn-sm" onclick="closePlanModal()">Cancel</button>
      <button class="btn btn-blue btn-sm" onclick="savePlan()">Save</button>
    </div>
  </div>
</div>

<!-- Set Expiration Modal -->
<div class="modal-bg hidden" id="expiry-modal">
  <div class="modal">
    <h3>Set Expiration Date</h3>
    <div class="modal-field">
      <label>USER</label>
      <div id="expiry-modal-email" style="font-size:13px;color:#f1f5f9;padding:4px 0;"></div>
    </div>
    <div class="modal-field">
      <label>EXPIRATION DATE <span class="tag">leave blank = never expires</span></label>
      <input type="date" id="expiry-input">
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost btn-sm" onclick="closeExpiryModal()">Cancel</button>
      <button class="btn btn-red btn-sm" onclick="clearExpiry()" style="margin-right:auto">Clear</button>
      <button class="btn btn-blue btn-sm" onclick="saveExpiry()">Save</button>
    </div>
  </div>
</div>

<script>
const SECRET = new URLSearchParams(location.search).get('secret') || '';
const BASE    = location.origin;
let users     = [];
let selectedEmail = null;

function fmt(dt) {{
  if (!dt) return '—';
  return dt.slice(0, 16).replace('T', ' ');
}}

function fmtDate(d) {{
  if (!d) return '—';
  return d.slice(0, 10);
}}

function daysLeft(exp) {{
  if (!exp) return null;
  const diff = Math.round((new Date(exp) - new Date()) / 86400000);
  return diff;
}}

function statusOf(u) {{
  if (!u.is_active) return 'DISABLED';
  if (u.expiration_date && u.expiration_date <= new Date().toISOString().slice(0,10))
    return 'EXPIRED';
  return 'ACTIVE';
}}

async function loadUsers() {{
  const res  = await fetch(`${{BASE}}/admin/users?secret=${{encodeURIComponent(SECRET)}}`);
  const data = await res.json();
  users = data.users || [];
  renderTable();
  renderStats();
}}

function renderStats() {{
  const today = new Date().toISOString().slice(0,10);
  let active=0, expired=0, disabled=0, free=0, pro=0, ent=0;
  users.forEach(u => {{
    const st = statusOf(u);
    if (st==='ACTIVE')   active++;
    if (st==='EXPIRED')  expired++;
    if (st==='DISABLED') disabled++;
    if (u.plan==='free')       free++;
    if (u.plan==='pro')        pro++;
    if (u.plan==='enterprise') ent++;
  }});
  document.getElementById('s-total').textContent   = users.length;
  document.getElementById('s-active').textContent  = active;
  document.getElementById('s-expired').textContent = expired;
  document.getElementById('s-disabled').textContent= disabled;
  document.getElementById('s-free').textContent    = free;
  document.getElementById('s-pro').textContent     = pro;
  document.getElementById('s-ent').textContent     = ent;
}}

function renderTable() {{
  const tbody = document.getElementById('users-tbody');
  if (!users.length) {{
    tbody.innerHTML = '<tr><td colspan="10" class="empty">No users found.</td></tr>';
    return;
  }}
  tbody.innerHTML = users.map((u, i) => {{
    const st    = statusOf(u);
    const stCls = st==='ACTIVE' ? 'status-active' : st==='EXPIRED' ? 'status-expired' : 'status-disabled';
    const stIcon= st==='ACTIVE' ? '&#10003;' : '&#10007;';
    const planCls = `plan-${{u.plan}}`;
    const dl    = daysLeft(u.expiration_date);
    let daysHtml = '<span class="days-forever">&#8734;</span>';
    if (dl !== null) {{
      if (dl > 30) daysHtml = `<span class="days-ok">${{dl}}d</span>`;
      else if (dl > 0) daysHtml = `<span class="days-warn">${{dl}}d</span>`;
      else daysHtml = `<span class="days-expired">${{Math.abs(dl)}}d ago</span>`;
    }}
    const sel = selectedEmail === u.email ? ' selected' : '';
    return `<tr class="${{sel}}" onclick="selectRow('${{u.email}}', ${{u.is_active}})">
      <td class="num">${{i+1}}</td>
      <td><span class="status-chip ${{stCls}}">${{stIcon}} ${{st}}</span></td>
      <td>${{u.name}}</td>
      <td class="wrap">${{u.email}}</td>
      <td><span class="plan-chip ${{planCls}}">${{u.plan.toUpperCase()}}</span></td>
      <td>${{fmtDate(u.expiration_date)}}</td>
      <td>${{daysHtml}}</td>
      <td>${{fmtDate(u.created_at)}}</td>
      <td>${{fmt(u.last_access)}}</td>
      <td class="id-chip">#${{u.id}}</td>
    </tr>`;
  }}).join('');
}}

function selectRow(email, isActive) {{
  selectedEmail = email;
  renderTable();
  document.getElementById('action-hint').classList.add('hidden');
  document.getElementById('selected-label').classList.remove('hidden');
  document.getElementById('selected-label').textContent = email;
  ['btn-change-plan','btn-set-expiry','btn-toggle-active'].forEach(id =>
    document.getElementById(id).classList.remove('hidden'));
  const toggleBtn = document.getElementById('btn-toggle-active');
  toggleBtn.textContent = isActive ? 'Disable' : 'Enable';
  toggleBtn.className   = isActive ? 'btn btn-red btn-sm' : 'btn btn-green btn-sm';
}}

// ── Change Plan ──
function openPlanModal() {{
  if (!selectedEmail) return;
  const u = users.find(x => x.email === selectedEmail);
  document.getElementById('plan-modal-email').textContent = selectedEmail;
  document.getElementById('plan-select').value = u ? u.plan : 'free';
  document.getElementById('plan-modal').classList.remove('hidden');
}}
function closePlanModal() {{ document.getElementById('plan-modal').classList.add('hidden'); }}
async function savePlan() {{
  const plan = document.getElementById('plan-select').value;
  const res  = await fetch(`${{BASE}}/admin/set-plan`, {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{email: selectedEmail, plan, admin_secret: SECRET}})
  }});
  if (res.ok) {{ closePlanModal(); await loadUsers(); }}
  else alert('Error: ' + (await res.json()).detail);
}}

// ── Set Expiration ──
function openExpiryModal() {{
  if (!selectedEmail) return;
  const u = users.find(x => x.email === selectedEmail);
  document.getElementById('expiry-modal-email').textContent = selectedEmail;
  document.getElementById('expiry-input').value = u?.expiration_date || '';
  document.getElementById('expiry-modal').classList.remove('hidden');
}}
function closeExpiryModal() {{ document.getElementById('expiry-modal').classList.add('hidden'); }}
async function saveExpiry() {{
  const exp = document.getElementById('expiry-input').value;
  const res = await fetch(`${{BASE}}/admin/set-expiration`, {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{email: selectedEmail, expiration_date: exp, admin_secret: SECRET}})
  }});
  if (res.ok) {{ closeExpiryModal(); await loadUsers(); }}
  else alert('Error: ' + (await res.json()).detail);
}}
async function clearExpiry() {{
  const res = await fetch(`${{BASE}}/admin/set-expiration`, {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{email: selectedEmail, expiration_date: '', admin_secret: SECRET}})
  }});
  if (res.ok) {{ closeExpiryModal(); await loadUsers(); }}
}}

// ── Toggle active ──
async function toggleActive() {{
  const u = users.find(x => x.email === selectedEmail);
  if (!u) return;
  const newActive = !u.is_active;
  const res = await fetch(`${{BASE}}/admin/toggle-active`, {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{email: selectedEmail, is_active: newActive, admin_secret: SECRET}})
  }});
  if (res.ok) await loadUsers();
  else alert('Error: ' + (await res.json()).detail);
}}

// ── Export CSV ──
function exportCSV() {{
  window.open(`${{BASE}}/admin/export-csv?secret=${{encodeURIComponent(SECRET)}}`, '_blank');
}}

// Close modals on background click
document.getElementById('plan-modal').addEventListener('click', e => {{
  if (e.target.id === 'plan-modal') closePlanModal();
}});
document.getElementById('expiry-modal').addEventListener('click', e => {{
  if (e.target.id === 'expiry-modal') closeExpiryModal();
}});

loadUsers();
</script>
</body></html>""")


# ─── Health check ────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "structiq-web"}


# ═══════════════════════════════════════════════════════════════════
#  PMM ENGINE (pure math — no ETABS needed)
# ═══════════════════════════════════════════════════════════════════

_RAILWAY_DIR = os.path.dirname(os.path.abspath(__file__))
_PMM_PATH = os.path.join(_RAILWAY_DIR, '..', 'backend', 'pmm_engine.py')

_HAS_PMM = False
try:
    _spec = _ilu.spec_from_file_location('pmm_engine', _PMM_PATH)
    _pme = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_pme)
    PMMSection   = _pme.PMMSection
    compute_pmm  = _pme.compute_pmm
    check_demands= _pme.check_demands
    rect_coords  = _pme.rect_coords
    rect_bars_grid = _pme.rect_bars_grid
    REBAR_TABLE  = _pme.REBAR_TABLE
    _HAS_PMM = True
except Exception:
    pass

# Unit conversion constants (engine works in kips/inches)
_MM_TO_IN    = 1.0 / 25.4
_MPA_TO_KSI  = 1.0 / 6.89476
_KN_TO_KIPS  = 1.0 / 4.44822
_KNM_TO_KIN  = 1.0 / 0.112985
_IN_TO_MM    = 25.4
_KSI_TO_MPA  = 6.89476
_KIPS_TO_KN  = 4.44822
_KIN_TO_KNM  = 0.112985
_IN2_TO_MM2  = 645.16

REBAR_TABLE_SI = {
    "Ø8":  50.3,  "Ø10":  78.5,  "Ø12": 113.1,
    "Ø16": 201.1, "Ø20": 314.2,  "Ø25": 490.9,
    "Ø28": 615.8, "Ø32": 804.2,  "Ø36": 1017.9,
    "Ø40": 1256.6,
}

# Per-user PMM cache  {user_id: {"alpha_data": ..., "Pmax": ..., "Pmin": ..., "si": bool}}
_pmm_cache: dict = {}


def _get_pmm_user(request: Request) -> dict:
    """Extract user from Bearer token (Railway DB)."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    user = database.get_user_by_token(auth[7:])
    if not user:
        raise HTTPException(401, "Invalid or expired session")
    return user


class PMMRequest(BaseModel):
    b: float
    h: float
    fc: float
    fy: float
    Es: float = 200000.0
    cover: float
    stirrup_dia_mm: float = 10.0
    nbars_b: int = 3
    nbars_h: int = 1
    bar_size: str = "Ø20"
    include_phi: bool = True
    alpha_steps: float = 5.0
    num_points: int = 225
    units: str = "SI"
    demand_P: Optional[float] = None
    demand_Mx: Optional[float] = None
    demand_My: Optional[float] = None


class PMMCheckRequest(BaseModel):
    demands: list


class PMMOptimizeRequest(BaseModel):
    b_mm: float
    h_mm: float
    fc_mpa: float
    fy_mpa: float
    Es_mpa: float = 200000.0
    cover_mm: float
    stirrup_dia_mm: float = 10.0
    include_phi: bool = True
    bar_size: str = "Ø20"
    target_dcr: float = 0.90
    min_rho_pct: float = 1.0
    max_rho_pct: float = 4.0
    alpha_steps: float = 10.0
    num_points: int = 70
    demands: list = []
    sweep_bar_sizes: bool = False
    bar_size_candidates: List[str] = []


@app.post("/api/pmm/calculate")
def pmm_calculate(body: PMMRequest, request: Request):
    if not _HAS_PMM:
        raise HTTPException(503, "PMM engine not available.")
    user = _get_pmm_user(request)
    uid = user["id"]

    _REBAR_DIA_MM = {
        "Ø8": 8.0, "Ø10": 10.0, "Ø12": 12.0, "Ø16": 16.0, "Ø20": 20.0,
        "Ø25": 25.0, "Ø28": 28.0, "Ø32": 32.0, "Ø36": 36.0, "Ø40": 40.0,
    }
    si = body.units.upper() == "SI"
    if si:
        bar_area_mm2 = REBAR_TABLE_SI.get(body.bar_size)
        if bar_area_mm2 is None:
            raise HTTPException(422, f"Unknown bar size '{body.bar_size}'.")
        bar_dia_mm   = _REBAR_DIA_MM.get(body.bar_size, 20.0)
        eff_cover_mm = body.cover + body.stirrup_dia_mm + bar_dia_mm / 2.0
        b_in     = body.b  * _MM_TO_IN;  h_in  = body.h  * _MM_TO_IN
        fc_ksi   = body.fc * _MPA_TO_KSI; fy_ksi = body.fy * _MPA_TO_KSI
        Es_ksi   = body.Es * _MPA_TO_KSI
        cover_in = eff_cover_mm * _MM_TO_IN
        bar_area = bar_area_mm2 * (_MM_TO_IN ** 2)
    else:
        bar_area = REBAR_TABLE.get(body.bar_size)
        if bar_area is None:
            raise HTTPException(422, f"Unknown bar size '{body.bar_size}'.")
        b_in = body.b; h_in = body.h
        fc_ksi = body.fc; fy_ksi = body.fy; Es_ksi = body.Es
        cover_in = body.cover

    if b_in <= 0 or h_in <= 0:
        raise HTTPException(422, "b and h must be positive.")
    nbars_b = max(2, body.nbars_b)
    nbars_h = max(0, body.nbars_h)
    corners = rect_coords(b_in, h_in)
    areas, positions = rect_bars_grid(b_in, h_in, cover_in, nbars_b, nbars_h, bar_area)
    sec = PMMSection(
        corner_coords=corners, fc=fc_ksi, fy=fy_ksi, Es=Es_ksi,
        alpha_steps=body.alpha_steps, num_points=body.num_points,
        include_phi=body.include_phi, bar_areas=areas, bar_positions=positions,
    )
    try:
        result = compute_pmm(sec)
    except Exception as exc:
        raise HTTPException(500, f"PMM computation failed: {exc}")

    if si:
        def _cvt(r):
            r['surface']['P']  = [round(v * _KIPS_TO_KN, 2) for v in r['surface']['P']]
            r['surface']['Mx'] = [round(v * _KIN_TO_KNM, 3) for v in r['surface']['Mx']]
            r['surface']['My'] = [round(v * _KIN_TO_KNM, 3) for v in r['surface']['My']]
            for curve in r.get('alpha_data', {}).values():
                curve['P']  = [round(v * _KIPS_TO_KN, 2)  for v in curve['P']]
                curve['Mx'] = [round(v * _KIN_TO_KNM, 3) for v in curve['Mx']]
                curve['My'] = [round(v * _KIN_TO_KNM, 3) for v in curve['My']]
            for curve in r.get('curves_2d', {}).values():
                curve['P']  = [round(v * _KIPS_TO_KN, 2)  for v in curve['P']]
                curve['Mx'] = [round(v * _KIN_TO_KNM, 3) for v in curve['Mx']]
                curve['My'] = [round(v * _KIN_TO_KNM, 3) for v in curve['My']]
            r['Pmax']     = round(r['Pmax']     * _KIPS_TO_KN, 2)
            r['Pmin']     = round(r['Pmin']     * _KIPS_TO_KN, 2)
            r['Ag']       = round(r['Ag']       * _IN2_TO_MM2, 0)
            r['Ast']      = round(r['Ast']      * _IN2_TO_MM2, 1)
            r['centroid'] = [round(v * _IN_TO_MM, 1) for v in r['centroid']]
            return r
        result = _cvt(result)

    _pmm_cache[uid] = {
        'alpha_data': result.get('alpha_data', {}),
        'Pmax': result.get('Pmax', 0),
        'Pmin': result.get('Pmin', 0),
        'si': si,
    }
    result['units'] = 'SI' if si else 'US'
    result['demand'] = None
    if body.demand_P is not None:
        result['demand'] = {'P': body.demand_P, 'Mx': body.demand_Mx or 0.0, 'My': body.demand_My or 0.0}
    return result


@app.post("/api/pmm/check")
def pmm_check(body: PMMCheckRequest, request: Request):
    if not _HAS_PMM:
        raise HTTPException(503, "PMM engine not available.")
    user = _get_pmm_user(request)
    cache = _pmm_cache.get(user["id"])
    if not cache or cache['alpha_data'] is None:
        raise HTTPException(400, "No PMM surface computed yet. Run Calculate first.")
    demands = [{'label': d.get('label', ''), 'P': float(d.get('P', 0)),
                'Mx': float(d.get('Mx', 0)), 'My': float(d.get('My', 0))}
               for d in body.demands]
    raw = check_demands(cache['alpha_data'], cache['Pmax'], cache['Pmin'], demands)
    return {'results': raw}


@app.post("/api/pmm/optimize")
def pmm_optimize(body: PMMOptimizeRequest, request: Request):
    if not _HAS_PMM:
        raise HTTPException(503, "PMM engine not available.")
    _get_pmm_user(request)  # auth check

    import math as _math
    _REBAR_DIA_MM = {
        "Ø8": 8.0, "Ø10": 10.0, "Ø12": 12.0, "Ø16": 16.0, "Ø20": 20.0,
        "Ø25": 25.0, "Ø28": 28.0, "Ø32": 32.0, "Ø36": 36.0, "Ø40": 40.0,
    }
    candidates = body.bar_size_candidates if (body.sweep_bar_sizes and body.bar_size_candidates) \
        else list(REBAR_TABLE_SI.keys())

    bar_dia_mm   = _REBAR_DIA_MM.get(body.bar_size, 20.0)
    eff_cover_mm = body.cover_mm + body.stirrup_dia_mm + bar_dia_mm / 2.0

    nbars_b = 3; nbars_h = 1  # defaults — override from PMMRequest context if available
    demands = [{'label': d.get('label', ''), 'P': float(d.get('P', 0)),
                'Mx': float(d.get('Mx', 0)), 'My': float(d.get('My', 0))}
               for d in body.demands]

    best_bar = None; best_dcr = None; best_rho = None

    for bar_key in candidates:
        bar_area_mm2 = REBAR_TABLE_SI.get(bar_key)
        if not bar_area_mm2:
            continue
        bdia = _REBAR_DIA_MM.get(bar_key, 20.0)
        eff_cov = body.cover_mm + body.stirrup_dia_mm + bdia / 2.0
        b_in = body.b_mm * _MM_TO_IN; h_in = body.h_mm * _MM_TO_IN
        fc_k = body.fc_mpa * _MPA_TO_KSI; fy_k = body.fy_mpa * _MPA_TO_KSI
        Es_k = body.Es_mpa * _MPA_TO_KSI
        cov  = eff_cov * _MM_TO_IN
        ba   = bar_area_mm2 * (_MM_TO_IN ** 2)
        corners = rect_coords(b_in, h_in)
        areas, positions = rect_bars_grid(b_in, h_in, cov, nbars_b, nbars_h, ba)
        Ag_mm2 = body.b_mm * body.h_mm
        n_bars = 2 * nbars_b + 2 * nbars_h
        Ast_mm2 = n_bars * bar_area_mm2
        rho = Ast_mm2 / Ag_mm2 * 100
        if rho < body.min_rho_pct or rho > body.max_rho_pct:
            continue
        sec = PMMSection(corner_coords=corners, fc=fc_k, fy=fy_k, Es=Es_k,
                         alpha_steps=body.alpha_steps, num_points=body.num_points,
                         include_phi=body.include_phi, bar_areas=areas, bar_positions=positions)
        pmm_raw = compute_pmm(sec)
        ad = pmm_raw.get('alpha_data', {})
        for curve in ad.values():
            curve['P']  = [v * _KIPS_TO_KN  for v in curve['P']]
            curve['Mx'] = [v * _KIN_TO_KNM for v in curve['Mx']]
            curve['My'] = [v * _KIN_TO_KNM for v in curve['My']]
        Pmax = pmm_raw['Pmax'] * _KIPS_TO_KN
        Pmin = pmm_raw['Pmin'] * _KIPS_TO_KN
        if not demands:
            best_bar = bar_key; best_rho = rho; best_dcr = 0.0
            break
        chk = check_demands(ad, Pmax, Pmin, demands)
        max_dcr = max((r.get('dcr', 0) for r in chk), default=0)
        if max_dcr <= body.target_dcr:
            best_bar = bar_key; best_rho = rho; best_dcr = max_dcr
            break

    return {
        "optimized_bar": best_bar,
        "optimized_rho_pct": round(best_rho, 2) if best_rho else None,
        "optimized_dcr": round(best_dcr, 3) if best_dcr is not None else None,
        "status": "OPTIMAL" if best_bar else "NO_SOLUTION",
    }


@app.get("/api/pmm/rebar-table")
def pmm_rebar_table(units: str = "SI"):
    if units.upper() == "SI":
        return {"bars": [{"size": k, "area_mm2": v} for k, v in REBAR_TABLE_SI.items()]}
    if _HAS_PMM:
        return {"bars": [{"size": k, "area_in2": v} for k, v in REBAR_TABLE.items()]}
    return {"bars": []}


@app.get("/api/ping")
def ping():
    return {"source": "railway-web", "bridge_enabled": True}


# ═══════════════════════════════════════════════════════════════════
#  WEBSOCKET BRIDGE — engineers register their local ETABS here
# ═══════════════════════════════════════════════════════════════════

@app.websocket("/ws/bridge")
async def bridge_ws(ws: WebSocket):
    await ws.accept()
    user_id: Optional[int] = None
    try:
        # Step 1 — authenticate
        msg = await asyncio.wait_for(ws.receive_json(), timeout=15)
        if msg.get("type") != "auth":
            await ws.send_json({"type": "auth_fail", "reason": "first message must be auth"})
            return
        token = msg.get("token", "")
        user = database.get_user_by_token(token)
        if not user:
            await ws.send_json({"type": "auth_fail", "reason": "invalid token"})
            return
        user_id = user["id"]
        bridge_registry.register(user_id, ws)
        await ws.send_json({"type": "auth_ok", "user_id": user_id, "name": user.get("name", "")})

        # Step 2 — relay responses from bridge back to waiting HTTP handlers
        async for message in ws.iter_json():
            if message.get("type") == "response":
                bridge_registry.resolve(message["request_id"], message)
            elif message.get("type") == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass
    finally:
        if user_id is not None:
            bridge_registry.unregister(user_id)


# ─── Bridge status ────────────────────────────────────────────────

@app.get("/api/bridge/status")
def bridge_status(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return {"connected": False}
    user = database.get_user_by_token(auth[7:])
    if not user:
        return {"connected": False}
    return {"connected": bridge_registry.is_connected(user["id"])}


# ═══════════════════════════════════════════════════════════════════
#  ETABS PROXY — all routes that require local ETABS forward here
# ═══════════════════════════════════════════════════════════════════

_BRIDGE_OFFLINE_DETAIL = {
    "bridge_required": True,
    "message": (
        "ETABS bridge not connected. "
        "Please run StructIQ Bridge on your local machine."
    ),
}


async def _proxy(request: Request, method: str, path: str,
                 body=None, params: Optional[dict] = None,
                 timeout: float = 60.0) -> dict:
    """Forward an ETABS call to the user's registered bridge WebSocket."""
    auth = request.headers.get("Authorization", "")
    user = database.get_user_by_token(auth[7:] if auth.startswith("Bearer ") else "")
    if not user:
        raise HTTPException(401, "Not authenticated")

    result = await bridge_registry.proxy(user["id"], method, path, body, params, timeout=timeout)

    if result.get("__bridge_offline__"):
        raise HTTPException(503, detail=_BRIDGE_OFFLINE_DETAIL)
    if result.get("__bridge_timeout__"):
        raise HTTPException(504, "Bridge request timed out after 60 s")
    if result.get("__bridge_error__"):
        raise HTTPException(502, f"Bridge error: {result['__bridge_error__']}")

    status = result.get("status", 200)
    body_data = result.get("body", result)
    if status >= 400:
        detail = body_data.get("detail", "Bridge returned an error") if isinstance(body_data, dict) else str(body_data)
        raise HTTPException(status, detail=detail)
    return body_data


# ── ETABS status & load data ──────────────────────────────────────

@app.get("/api/status")
async def proxy_status(request: Request):
    return await _proxy(request, "GET", "/api/status")

@app.get("/api/load-combinations")
async def proxy_load_combinations(request: Request):
    return await _proxy(request, "GET", "/api/load-combinations")

@app.get("/api/load-cases")
async def proxy_load_cases(request: Request):
    return await _proxy(request, "GET", "/api/load-cases")

@app.post("/api/load-combinations/generate")
async def proxy_gen_combos(request: Request):
    params = dict(request.query_params)
    return await _proxy(request, "POST", "/api/load-combinations/generate", params=params)

@app.post("/api/load-combinations/generate-batch")
async def proxy_gen_combos_batch(request: Request):
    body = await request.json()
    return await _proxy(request, "POST", "/api/load-combinations/generate-batch", body=body)

# ── Drift / torsion / reactions ───────────────────────────────────

@app.post("/api/results/drifts-selected")
async def proxy_drifts_selected(request: Request):
    body = await request.json()
    return await _proxy(request, "POST", "/api/results/drifts-selected", body=body)

@app.get("/api/results/drifts")
async def proxy_drifts(request: Request):
    return await _proxy(request, "GET", "/api/results/drifts")

@app.get("/api/results/torsional-irregularity")
async def proxy_torsion(request: Request):
    return await _proxy(request, "GET", "/api/results/torsional-irregularity")

@app.get("/api/results/joint-reactions")
async def proxy_joint_reactions(request: Request):
    params = dict(request.query_params)
    return await _proxy(request, "GET", "/api/results/joint-reactions", params=params)

@app.get("/api/results/reactions")
async def proxy_reactions(request: Request):
    params = dict(request.query_params)
    return await _proxy(request, "GET", "/api/results/reactions", params=params)

# ── Section read/write ────────────────────────────────────────────

@app.get("/api/sections")
async def proxy_sections(request: Request):
    return await _proxy(request, "GET", "/api/sections")

@app.post("/api/sections/modify")
async def proxy_sections_modify(request: Request):
    body = await request.json()
    return await _proxy(request, "POST", "/api/sections/modify", body=body)

@app.post("/api/sections/add")
async def proxy_sections_add(request: Request):
    body = await request.json()
    return await _proxy(request, "POST", "/api/sections/add", body=body)

# ── RC beam/column design ─────────────────────────────────────────

@app.get("/api/rc-beam/materials")
async def proxy_rc_beam_mat(request: Request):
    return await _proxy(request, "GET", "/api/rc-beam/materials")

@app.get("/api/rc-beam/sections")
async def proxy_rc_beam_sec(request: Request):
    return await _proxy(request, "GET", "/api/rc-beam/sections")

@app.post("/api/rc-beam/write")
async def proxy_rc_beam_write(request: Request):
    body = await request.json()
    return await _proxy(request, "POST", "/api/rc-beam/write", body=body)

@app.get("/api/rc-column/materials")
async def proxy_rc_col_mat(request: Request):
    return await _proxy(request, "GET", "/api/rc-column/materials")

@app.get("/api/rc-column/sections")
async def proxy_rc_col_sec(request: Request):
    return await _proxy(request, "GET", "/api/rc-column/sections")

@app.post("/api/rc-column/write")
async def proxy_rc_col_write(request: Request):
    body = await request.json()
    return await _proxy(request, "POST", "/api/rc-column/write", body=body)

@app.get("/api/rc-column/debug/{section_name}")
async def proxy_rc_col_debug(section_name: str, request: Request):
    return await _proxy(request, "GET", f"/api/rc-column/debug/{section_name}")

# ── File cleanup ──────────────────────────────────────────────────

@app.get("/api/clean/browse-folder")
async def proxy_browse(request: Request):
    params = dict(request.query_params)
    return await _proxy(request, "GET", "/api/clean/browse-folder", params=params)

@app.post("/api/clean/run-files")
async def proxy_clean(request: Request):
    body = await request.json()
    return await _proxy(request, "POST", "/api/clean/run-files", body=body)

# ── PMM ETABS routes ──────────────────────────────────────────────

@app.get("/api/pmm/etabs-sections")
async def proxy_pmm_etabs_sections(request: Request):
    return await _proxy(request, "GET", "/api/pmm/etabs-sections")

@app.get("/api/pmm/etabs-combos")
async def proxy_pmm_etabs_combos(request: Request):
    return await _proxy(request, "GET", "/api/pmm/etabs-combos")

@app.post("/api/pmm/etabs-import-forces")
async def proxy_pmm_etabs_import(request: Request):
    body = await request.json()
    return await _proxy(request, "POST", "/api/pmm/etabs-import-forces", body=body)

@app.post("/api/pmm/etabs-section-forces")
async def proxy_pmm_etabs_sec_forces(request: Request):
    body = await request.json()
    return await _proxy(request, "POST", "/api/pmm/etabs-section-forces", body=body)

@app.get("/api/pmm/etabs-batch-diag")
async def proxy_pmm_batch_diag(request: Request):
    return await _proxy(request, "GET", "/api/pmm/etabs-batch-diag")

@app.post("/api/pmm/etabs-batch-check")
async def proxy_pmm_batch_check(request: Request):
    body = await request.json()
    return await _proxy(request, "POST", "/api/pmm/etabs-batch-check", body=body, timeout=120.0)

@app.post("/api/pmm/batch-optimize")
async def proxy_pmm_batch_optimize(request: Request):
    body = await request.json()
    return await _proxy(request, "POST", "/api/pmm/batch-optimize", body=body, timeout=120.0)

# ── ETABS geometry / column axial ────────────────────────────────

@app.post("/api/etabs/column-axial")
async def proxy_col_axial(request: Request):
    body = await request.json()
    return await _proxy(request, "POST", "/api/etabs/column-axial", body=body)

@app.get("/api/etabs/geometry")
async def proxy_geometry(request: Request):
    return await _proxy(request, "GET", "/api/etabs/geometry")


# ═══════════════════════════════════════════════════════════════════
#  SERVE FRONTEND  (mount last so API routes take priority)
# ═══════════════════════════════════════════════════════════════════

_FRONTEND_DIR = os.path.join(_RAILWAY_DIR, '..', 'backend', 'frontend')

if os.path.isdir(_FRONTEND_DIR):
    # Serve individual static assets
    app.mount("/assets", StaticFiles(directory=_FRONTEND_DIR), name="assets")

    @app.get("/")
    def serve_root():
        return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        # Never intercept API / WebSocket / admin / billing paths
        skip = ("api/", "ws/", "admin/", "stripe/", "health")
        if any(full_path.startswith(p) for p in skip):
            raise HTTPException(404)
        idx = os.path.join(_FRONTEND_DIR, "index.html")
        return FileResponse(idx)

