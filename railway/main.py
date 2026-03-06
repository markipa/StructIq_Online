"""
Railway Cloud Server — StructIQ Auth & Billing
Handles: user accounts, sessions, subscription plans
Deployed to Railway.app — no ETABS code here
"""
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import database
import os

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


# ─── Health check ────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "structiq-auth"}
