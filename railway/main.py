"""
Railway Cloud Server — StructIQ Auth & Billing
Handles: user accounts, sessions, subscription plans
Deployed to Railway.app — no ETABS code here
"""
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional
import database
import stripe
import os

# ─── Stripe config ────────────────────────────────────────────────
stripe.api_key            = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET     = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_MONTHLY      = os.environ.get("STRIPE_PRICE_MONTHLY", "price_1T7xXDGjto3S8juRtyrzuMhp")
STRIPE_PRICE_YEARLY       = os.environ.get("STRIPE_PRICE_YEARLY",  "price_1T7xXDGjto3S8juRw30FfIeQ")
STRIPE_PUBLISHABLE_KEY    = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
BASE_URL                  = "https://structiq-production.up.railway.app"

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
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
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


# ─── Stripe endpoints ────────────────────────────────────────────

@app.post("/stripe/create-checkout")
def create_checkout(body: CreateCheckoutRequest):
    """Create a Stripe Checkout session. Returns {checkout_url}."""
    if not stripe.api_key:
        raise HTTPException(503, "Stripe not configured")
    user = database.get_user_by_email(body.email)
    if not user:
        # User hasn't synced to Railway yet — auto-create a free account
        # so the webhook can upgrade them after payment completes.
        user = database.create_free_user_by_email(body.email)
        if not user:
            raise HTTPException(500, "Could not initialise user account")

    price_id = STRIPE_PRICE_YEARLY if body.interval == "yearly" else STRIPE_PRICE_MONTHLY

    # Create or reuse Stripe customer
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        customer    = stripe.Customer.create(
            email    = body.email,
            name     = user["name"],
            metadata = {"user_id": str(user["id"])},
        )
        customer_id = customer.id
        database.update_stripe_customer(user["id"], customer_id)

    session = stripe.checkout.Session.create(
        customer             = customer_id,
        payment_method_types = ["card"],
        line_items           = [{"price": price_id, "quantity": 1}],
        mode                 = "subscription",
        success_url          = f"{BASE_URL}/stripe/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url           = f"{BASE_URL}/stripe/cancel",
        metadata             = {"user_email": body.email, "interval": body.interval},
    )
    return {"checkout_url": session.url}


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe sends payment events here — upgrades/downgrades plans automatically."""
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid Stripe signature")

    etype = event["type"]
    obj   = event["data"]["object"]

    if etype == "checkout.session.completed":
        email    = obj.get("metadata", {}).get("user_email")
        interval = obj.get("metadata", {}).get("interval", "monthly")
        sub_id   = obj.get("subscription")
        if email:
            database.update_user_plan_by_email(email, "pro")
            if sub_id:
                database.update_stripe_subscription(email, sub_id, interval)

    elif etype in ("customer.subscription.deleted", "customer.subscription.paused"):
        customer_id = obj.get("customer")
        if customer_id:
            user = database.get_user_by_stripe_customer(customer_id)
            if user:
                database.update_user_plan(user["id"], "free")

    return {"ok": True}


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


# ─── Health check ────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "structiq-auth"}
