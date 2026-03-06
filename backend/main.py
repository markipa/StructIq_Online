from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Dict, List, Optional
from datetime import datetime
from etabs_api import actions
import database
import config
import os

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

app = FastAPI(title="StructIQ API")

# Initialise DB on startup
database.init_db()


# ─── Cloud (Railway) helpers ──────────────────────────────────────────────────

def _cloud_post(path: str, payload: dict, timeout: int = 8):
    """POST to Railway cloud server. Returns parsed JSON dict or None on failure."""
    if not config.CLOUD_URL or not _HAS_REQUESTS:
        return None
    try:
        r = _requests.post(
            f"{config.CLOUD_URL.rstrip('/')}{path}",
            json=payload,
            timeout=timeout,
        )
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def _cloud_get(path: str, token: str, timeout: int = 8):
    """GET from Railway cloud server with Bearer token. Returns dict or None."""
    if not config.CLOUD_URL or not _HAS_REQUESTS:
        return None
    try:
        r = _requests.get(
            f"{config.CLOUD_URL.rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


# ─── Auth helpers ────────────────────────────────────────────────────────────

def get_current_user(request: Request) -> dict:
    """Dependency: extracts Bearer token, returns public user dict or 401."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth[7:]
    user = database.get_user_by_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    # Developer override: admin emails always get enterprise plan + skip grace period
    if user.get("email", "").lower() in [e.lower() for e in config.ADMIN_EMAILS]:
        user = dict(user)
        user["plan"] = "enterprise"
        user["last_cloud_sync"] = None   # never triggers grace period check
    return user


# ─── Plan enforcement ─────────────────────────────────────────────────────────

PLAN_LEVEL = {"free": 0, "pro": 1, "enterprise": 2}


def require_plan(min_plan: str = "free"):
    """
    Dependency factory: checks offline grace period + plan level.
    Usage:  Depends(require_plan("pro"))
    Returns 402 if grace period expired, 403 if plan too low.
    """
    def _dep(current_user: dict = Depends(get_current_user)):
        # ── Offline grace period ──────────────────────────────────────────
        last_sync_str = current_user.get("last_cloud_sync")
        if last_sync_str:
            try:
                last_sync_dt = datetime.fromisoformat(last_sync_str)
                days_offline = (datetime.utcnow() - last_sync_dt).days
                if days_offline > config.OFFLINE_GRACE_DAYS:
                    raise HTTPException(
                        status_code=402,
                        detail={
                            "code": "grace_expired",
                            "days": days_offline,
                            "message": (
                                f"License verification overdue "
                                f"({days_offline} days offline). "
                                "Connect to the internet and restart the app."
                            ),
                        },
                    )
            except HTTPException:
                raise
            except Exception:
                pass  # Malformed date — be lenient

        # ── Plan level check ─────────────────────────────────────────────
        user_plan  = current_user.get("plan", "free")
        user_level = PLAN_LEVEL.get(user_plan, 0)
        need_level = PLAN_LEVEL.get(min_plan, 0)
        if user_level < need_level:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "plan_required",
                    "required": min_plan,
                    "current": user_plan,
                    "message": (
                        f"This feature requires a {min_plan.upper()} plan. "
                        f"You are on the {user_plan.upper()} plan."
                    ),
                },
            )
        return current_user
    return _dep


# ─── Auth request/response models ────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: str
    name: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Protect all /api/* routes except /api/auth/*."""
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        token = auth[7:]
        user = database.get_user_by_token(token)
        if user is None:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "Invalid or expired session"})
    return await call_next(request)

# ─── Auth routes ─────────────────────────────────────────────────────────────

@app.post("/api/auth/register")
def register(body: RegisterRequest):
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    user = database.create_user(body.email, body.name, body.password)
    if user is None:
        raise HTTPException(status_code=409, detail="Email already registered")
    token = database.create_session(user["id"])

    # Best-effort cloud registration — don't block if Railway is unreachable
    cloud = _cloud_post("/api/auth/register", {
        "email": body.email, "name": body.name, "password": body.password
    })
    if cloud and "token" in cloud:
        database.update_cloud_token(user["id"], cloud["token"])
        cloud_plan = (cloud.get("user") or {}).get("plan")
        if cloud_plan:
            database.update_last_cloud_sync(user["id"], cloud_plan)

    return {"token": token, "user": database.get_user_by_id(user["id"])}


@app.post("/api/auth/login")
def login(body: LoginRequest):
    row = database.get_user_by_email(body.email)
    if row is None or not database.verify_password(body.password, row["password"], row["salt"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="Account disabled")
    token = database.create_session(row["id"])

    # Best-effort cloud login — syncs plan from Railway on every sign-in
    cloud = _cloud_post("/api/auth/login", {
        "email": body.email, "password": body.password
    })
    if cloud and "token" in cloud:
        database.update_cloud_token(row["id"], cloud["token"])
        cloud_plan = (cloud.get("user") or {}).get("plan")
        if cloud_plan:
            database.update_last_cloud_sync(row["id"], cloud_plan)

    user = database.get_user_by_id(row["id"])
    return {"token": token, "user": user}


@app.get("/api/auth/me")
def me(current_user: dict = Depends(get_current_user)):
    return current_user


@app.post("/api/auth/logout")
def logout(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        database.delete_session(auth[7:])
    return {"ok": True}


# ─── Cloud sync ───────────────────────────────────────────────────────────────

@app.get("/api/cloud/sync")
def cloud_sync(current_user: dict = Depends(get_current_user)):
    """
    Sync subscription plan from Railway cloud server.
    Called by the frontend on every boot — best-effort, never blocks the app.
    Returns: {plan, source: 'cloud'|'local', synced: bool}
    """
    cloud_token = database.get_cloud_token(current_user["id"])

    if cloud_token and config.CLOUD_URL:
        cloud = _cloud_get("/api/license/check", cloud_token)
        if cloud and cloud.get("valid"):
            plan = cloud["plan"]
            database.update_last_cloud_sync(current_user["id"], plan)
            return {"plan": plan, "source": "cloud", "synced": True,
                    "name": cloud.get("name"), "email": cloud.get("email")}

    # Fallback: email-based plan lookup (works for auto-created Railway users
    # who never had a cloud session token).
    email = current_user.get("email", "")
    sync_key = getattr(config, "PLAN_SYNC_KEY", "")
    if email and sync_key and config.CLOUD_URL and _HAS_REQUESTS:
        try:
            import urllib.parse
            params = urllib.parse.urlencode({"email": email, "key": sync_key})
            r = _requests.get(
                f"{config.CLOUD_URL.rstrip('/')}/api/plan?{params}",
                timeout=8,
            )
            if r.ok:
                data = r.json()
                plan = data.get("plan", "free")
                database.update_last_cloud_sync(current_user["id"], plan)
                return {"plan": plan, "source": "cloud", "synced": True}
        except Exception:
            pass

    # Cloud unreachable or no cloud token → return local plan
    return {"plan": current_user["plan"], "source": "local", "synced": False}


# ─── Stripe checkout proxy ────────────────────────────────────────────────────

class StripeCheckoutRequest(BaseModel):
    interval: str   # "monthly" | "yearly"

@app.post("/api/stripe/checkout")
def stripe_checkout(
    body: StripeCheckoutRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Proxy Stripe checkout through the Railway cloud server.
    Authenticated locally — passes the user's email to Railway.
    Uses a direct requests call (not _cloud_post) so errors are surfaced.
    """
    if not config.CLOUD_URL:
        raise HTTPException(503, detail="Cloud billing is not configured")
    if not _HAS_REQUESTS:
        raise HTTPException(503, detail="requests library unavailable")

    url = f"{config.CLOUD_URL.rstrip('/')}/stripe/create-checkout"
    try:
        r = _requests.post(
            url,
            json={"email": current_user["email"], "interval": body.interval},
            timeout=15,
        )
        try:
            data = r.json()
        except Exception:
            raise HTTPException(502, detail=f"Bad response from billing server (HTTP {r.status_code})")

        if not r.ok:
            detail = data.get("detail", f"Billing server error (HTTP {r.status_code})")
            raise HTTPException(r.status_code, detail=detail)

        if "checkout_url" not in data:
            raise HTTPException(500, detail="Billing server did not return a checkout URL")

        return {"checkout_url": data["checkout_url"]}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, detail=f"Could not reach billing server: {e}")


# ─── Static frontend ─────────────────────────────────────────────────────────

# Create frontend directory if it doesn't exist
os.makedirs("frontend", exist_ok=True)
app.mount("/app", StaticFiles(directory="frontend"), name="frontend")

@app.get("/")
def serve_index():
    if os.path.exists("frontend/index.html"):
        return FileResponse(
            "frontend/index.html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma":        "no-cache",
                "Expires":       "0",
            },
        )
    return {"message": "ETABS Backend is alive, but frontend/index.html is missing"}

@app.get("/api/status")
def get_status():
    status = actions.check_connection()
    return {"connected": status}

@app.get("/api/load-combinations")
def get_load_combinations():
    """Returns all load combination names available in the active ETABS model."""
    res = actions.get_load_combinations()
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res

@app.get("/api/load-cases")
def get_load_cases():
    """Returns all load case names available in the active ETABS model."""
    try:
        from etabs_api.connection import get_active_etabs
        SapModel = get_active_etabs()
        if not SapModel:
            raise HTTPException(status_code=503, detail="ETABS is not connected.")
        ret = SapModel.LoadCases.GetNameList()
        # Filter out internal/modal cases that start with ~ or 'Modal'
        cases = [c for c in ret[1] if not c.startswith("~")]
        return {"status": "success", "cases": cases}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/load-combinations/generate")
def generate_load_combinations(
    dead_case: str = Query(default="DL", description="Name of dead load case in ETABS"),
    live_case: str = Query(default="LL", description="Name of live load case in ETABS"),
    comb1_name: str = Query(default="Web_Comb_1"),
    comb2_name: str = Query(default="Web_Comb_2"),
    _u: dict = Depends(require_plan("pro")),
):
    res = actions.generate_load_combinations(
        dead_case=dead_case,
        live_case=live_case,
        comb1_name=comb1_name,
        comb2_name=comb2_name,
    )
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])

    return res

class DriftRequest(BaseModel):
    names: List[str]
    load_type: str = "combo"  # "combo" or "case"

@app.post("/api/results/drifts-selected")
def get_drifts_selected(request: DriftRequest):
    """Returns story drift data with elevation for specified load cases/combinations."""
    res = actions.get_story_drifts_selected(request.names, request.load_type)
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res


class ComboDefinition(BaseModel):
    name: str
    combo_type: int = 0  # 0=Linear Add, 1=Envelope, 2=Absolute Add, 3=SRSS
    factors: Dict[str, float]

class BatchGenerateRequest(BaseModel):
    combinations: List[ComboDefinition]

@app.post("/api/load-combinations/generate-batch")
def generate_combinations_batch(
    request: BatchGenerateRequest,
    _u: dict = Depends(require_plan("pro")),
):
    """Generate multiple load combinations from a grid-style definition."""
    res = actions.generate_combinations_batch(
        [c.model_dump() for c in request.combinations]
    )
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res


@app.get("/api/results/drifts")
def get_drifts():
    """Returns real story drift results from ETABS."""
    res = actions.get_story_drifts()
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res

@app.get("/api/results/torsional-irregularity")
def check_torsion():
    """Returns torsional irregularity check based on real ETABS drift results."""
    res = actions.check_torsional_irregularity()
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res

@app.get("/api/results/joint-reactions")
def get_joint_reactions(
    names: str = Query(default=None, description="Comma-separated list of combo/case names to fetch (omit for all)"),
    load_type: str = Query(default="combo", description="Source type: 'combo' (default) or 'case'"),
    _u: dict = Depends(require_plan("pro")),
):
    """
    Returns individual joint/support/spring reactions from ETABS with XYZ coordinates.
    Pass ?names=COMB1,COMB2 to restrict output to specific combinations/cases (faster).
    """
    name_list = [n.strip() for n in names.split(",")] if names else None
    res = actions.get_joint_reactions(names=name_list, load_type=load_type)
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res


@app.get("/api/results/reactions")
def get_reactions(
    combo: str = Query(default=None, description="Filter by a specific load combination or case name"),
    load_type: str = Query(default="combo", description="Source type: 'combo' (default) or 'case'"),
):
    """
    Returns real base reactions from ETABS.
    ?load_type=combo|case  selects whether to query load combinations or load cases.
    ?combo=<name>          optionally filters to a single named item of that type.
    """
    res = actions.get_base_reactions(combo_name=combo, load_type=load_type)
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res
