from fastapi import FastAPI, HTTPException, Query, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Dict, List, Optional
from datetime import datetime
# Load actions.py from the filesystem so updates take effect without a full EXE rebuild.
# In a frozen PyInstaller build the frozen module cache would otherwise shadow the file.
import importlib.util as _ilu
import sys as _sys, os as _os
_here = getattr(_sys, '_MEIPASS', _os.path.dirname(_os.path.abspath(__file__)))
_ap   = _os.path.join(_here, 'etabs_api', 'actions.py')
if _os.path.isfile(_ap):
    _spec = _ilu.spec_from_file_location('etabs_api.actions', _ap)
    actions = _ilu.module_from_spec(_spec)
    _sys.modules['etabs_api.actions'] = actions
    _spec.loader.exec_module(actions)
else:
    from etabs_api import actions  # fallback for non-frozen dev environment
import database
import config
import math
import os

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    from pmm_engine import (PMMSection, compute_pmm, check_demands,
                             rect_coords, perimeter_bars, rect_bars_grid, REBAR_TABLE)
    _HAS_PMM = True
except ImportError:
    _HAS_PMM = False

# Cache for last computed PMM surface (used by /api/pmm/check)
_pmm_cache: dict = {'alpha_data': None, 'Pmax': None, 'Pmin': None, 'si': True}

# Unit conversion constants (engine works in kips / kip·in)
_KN_TO_KIPS  = 1.0 / 4.44822
_KNM_TO_KININ = 1.0 / 0.112985

# SI rebar table: nominal diameter → area in mm²
REBAR_TABLE_SI = {
    "Ø8":  50.3,  "Ø10":  78.5,  "Ø12": 113.1,
    "Ø16": 201.1, "Ø20": 314.2,  "Ø25": 490.9,
    "Ø28": 615.8, "Ø32": 804.2,  "Ø36": 1017.9,
    "Ø40": 1256.6,
}

# Unit conversion constants (SI → US customary, for PMM engine)
_MM_TO_IN   = 1.0 / 25.4         # mm  → in
_MPA_TO_KSI = 1.0 / 6.89476      # MPa → ksi
_KN_TO_KIPS = 1.0 / 4.44822      # kN  → kips
_KNM_TO_KIN = 1.0 / 0.112985     # kN·m → k·in  (8.8507 k·in = 1 kN·m)
_IN_TO_MM   = 25.4
_KSI_TO_MPA = 6.89476
_KIPS_TO_KN = 4.44822
_KIN_TO_KNM = 0.112985
_IN2_TO_MM2 = 645.16

app = FastAPI(title="StructIQ API")

# Initialise DB on startup
database.init_db()

@app.get("/api/ping")
def ping():
    """Diagnostic: confirms this main.py (filesystem copy) is running."""
    return {"source": "filesystem", "browse_available": True}


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


def _cloud_session_post(path: str, params: dict, timeout: int = 8):
    """POST to Railway session endpoint with PLAN_SYNC_KEY auth. Returns dict or None."""
    if not config.CLOUD_URL or not _HAS_REQUESTS:
        return None
    sync_key = getattr(config, "PLAN_SYNC_KEY", "")
    try:
        import urllib.parse
        qs = urllib.parse.urlencode({**params, "key": sync_key})
        r = _requests.post(
            f"{config.CLOUD_URL.rstrip('/')}{path}?{qs}",
            timeout=timeout,
        )
        return r.json() if r.ok else None
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
        _is_admin = body.email.lower() in [e.lower() for e in config.ADMIN_EMAILS]
        if cloud_plan and not _is_admin:
            database.update_last_cloud_sync(user["id"], cloud_plan)

    u = dict(database.get_user_by_id(user["id"]))
    if u.get("email", "").lower() in [e.lower() for e in config.ADMIN_EMAILS]:
        u["plan"] = "enterprise"
    return {"token": token, "user": u}


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
    }, timeout=4)
    if cloud and "token" in cloud:
        database.update_cloud_token(row["id"], cloud["token"])
        cloud_plan = (cloud.get("user") or {}).get("plan")
        _is_admin = body.email.lower() in [e.lower() for e in config.ADMIN_EMAILS]
        if cloud_plan and not _is_admin:
            database.update_last_cloud_sync(row["id"], cloud_plan)

    # Register session globally on Railway — enforces single-session (pro)
    # and 3-session limit (enterprise). Uses the local token as the session key.
    if config.CLOUD_URL and _HAS_REQUESTS:
        sync_key = getattr(config, "PLAN_SYNC_KEY", "")
        try:
            import urllib.parse
            qs = urllib.parse.urlencode({
                "email": body.email, "session_key": token, "key": sync_key
            })
            reg_r = _requests.post(
                f"{config.CLOUD_URL.rstrip('/')}/api/session/register?{qs}",
                timeout=4,
            )
            if reg_r.status_code == 429:
                # Enterprise plan is at full capacity — block this login
                database.delete_session(token)
                detail = reg_r.json().get("detail", "Maximum simultaneous users reached for this plan.")
                raise HTTPException(429, detail=detail)
        except HTTPException:
            raise
        except Exception:
            pass  # Railway unreachable — allow offline login gracefully

    user = dict(database.get_user_by_id(row["id"]))
    if user.get("email", "").lower() in [e.lower() for e in config.ADMIN_EMAILS]:
        user["plan"] = "enterprise"
    return {"token": token, "user": user}


@app.get("/api/auth/me")
def me(current_user: dict = Depends(get_current_user)):
    return current_user


@app.post("/api/auth/logout")
def logout(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        database.delete_session(token)
        # Best-effort: free up the Railway cloud session slot on logout
        _cloud_session_post("/api/session/revoke", {"session_key": token})
    return {"ok": True}


# ─── Cloud sync ───────────────────────────────────────────────────────────────

@app.get("/api/cloud/sync")
def cloud_sync(request: Request, current_user: dict = Depends(get_current_user)):
    """
    Sync subscription plan from Railway cloud server AND validate the global session.
    Called by the frontend on every boot — best-effort, never blocks the app.
    Returns: {plan, source: 'cloud'|'local', synced: bool, session_valid: bool}
    """
    import urllib.parse

    # Extract the local token — it is also used as the Railway session key
    token    = request.headers.get("Authorization", "")[7:]
    sync_key = getattr(config, "PLAN_SYNC_KEY", "")

    plan   = current_user["plan"]
    synced = False
    source = "local"

    # Admin accounts always get enterprise — never let Railway override that.
    _email = current_user.get("email", "").lower()
    _is_admin = _email in [e.lower() for e in config.ADMIN_EMAILS]
    if _is_admin:
        return {"plan": "enterprise", "source": "local",
                "synced": False, "session_valid": True}

    # ── 1. Plan sync (cloud token path) ──────────────────────────────
    cloud_token = database.get_cloud_token(current_user["id"])
    if cloud_token and config.CLOUD_URL:
        cloud = _cloud_get("/api/license/check", cloud_token)
        if cloud and cloud.get("valid"):
            plan = cloud["plan"]
            database.update_last_cloud_sync(current_user["id"], plan)
            synced = True
            source = "cloud"

    # ── 2. Email-based plan fallback ──────────────────────────────────
    if not synced:
        email = current_user.get("email", "")
        if email and sync_key and config.CLOUD_URL and _HAS_REQUESTS:
            try:
                params = urllib.parse.urlencode({"email": email, "key": sync_key})
                r = _requests.get(
                    f"{config.CLOUD_URL.rstrip('/')}/api/plan?{params}",
                    timeout=8,
                )
                if r.ok:
                    data = r.json()
                    plan = data.get("plan", "free")
                    database.update_last_cloud_sync(current_user["id"], plan)
                    synced = True
                    source = "cloud"
            except Exception:
                pass

    # ── 3. Session validation (kicked by another login?) ──────────────
    session_valid = True  # optimistic default — keeps app usable offline
    if token and sync_key and config.CLOUD_URL and _HAS_REQUESTS:
        try:
            qs = urllib.parse.urlencode({"session_key": token, "key": sync_key})
            r  = _requests.post(
                f"{config.CLOUD_URL.rstrip('/')}/api/session/validate?{qs}",
                timeout=5,
            )
            if r.ok:
                session_valid = r.json().get("valid", True)
        except Exception:
            pass  # Railway unreachable — stay optimistic

    return {
        "plan":          plan,
        "source":        source,
        "synced":        synced,
        "session_valid": session_valid,
    }


# ─── Billing checkout proxy ───────────────────────────────────────────────────

class StripeCheckoutRequest(BaseModel):
    interval: str   # "monthly" | "yearly"

@app.post("/api/stripe/checkout")
def stripe_checkout(
    body: StripeCheckoutRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Proxy billing checkout through the Railway cloud server (Lemon Squeezy).
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


@app.get("/api/billing/portal")
def billing_portal(current_user: dict = Depends(get_current_user)):
    """
    Proxy to Railway: returns the Lemon Squeezy customer portal URL.
    Only available for users who have an active subscription (ls_subscription_id set).
    """
    if not config.CLOUD_URL:
        raise HTTPException(503, detail="Cloud billing is not configured")
    if not _HAS_REQUESTS:
        raise HTTPException(503, detail="requests library unavailable")

    import urllib.parse
    sync_key    = getattr(config, "PLAN_SYNC_KEY", "")
    cloud_token = database.get_cloud_token(current_user["id"])

    try:
        if cloud_token:
            # Path 1: Bearer token (user registered directly on Railway)
            r = _requests.get(
                f"{config.CLOUD_URL.rstrip('/')}/api/billing/portal",
                headers={"Authorization": f"Bearer {cloud_token}"},
                timeout=15,
            )
        else:
            # Path 2: email + key (user auto-created by Lemon Squeezy webhook)
            params = urllib.parse.urlencode({
                "email": current_user["email"],
                "key":   sync_key,
            })
            r = _requests.get(
                f"{config.CLOUD_URL.rstrip('/')}/api/billing/portal-by-email?{params}",
                timeout=15,
            )

        try:
            data = r.json()
        except Exception:
            raise HTTPException(502, detail=f"Bad response from billing server (HTTP {r.status_code})")

        if not r.ok:
            detail = data.get("detail", f"Billing server error (HTTP {r.status_code})")
            raise HTTPException(r.status_code, detail=detail)

        return {"portal_url": data["portal_url"]}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, detail=f"Could not reach billing server: {e}")


# ─── Static frontend ─────────────────────────────────────────────────────────

# Create frontend directory if it doesn't exist
os.makedirs("frontend", exist_ok=True)
app.mount("/app", StaticFiles(directory="frontend"), name="frontend")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Serve the StructIQ brand icon as the browser tab favicon."""
    ico = os.path.join("frontend", "favicon.ico")
    if os.path.exists(ico):
        return FileResponse(ico, media_type="image/x-icon")
    from fastapi.responses import Response
    return Response(status_code=204)


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
def check_torsion(
    _u: dict = Depends(require_plan("pro")),
):
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


class SectionModifyRequest(BaseModel):
    name: str
    b: float
    h: float

class SectionAddRequest(BaseModel):
    name: str
    material: str
    b: float
    h: float

@app.get("/api/sections")
def get_sections(_u: dict = Depends(require_plan("pro"))):
    """Returns all frame sections from ETABS with dimensions and computed properties."""
    res = actions.get_frame_sections()
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res

@app.post("/api/sections/modify")
def modify_section(
    request: SectionModifyRequest,
    _u: dict = Depends(require_plan("pro")),
):
    """Modify width and depth of an existing rectangular frame section."""
    res = actions.set_rectangular_section_dims(request.name, request.b, request.h)
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res

@app.post("/api/sections/add")
def add_section(
    request: SectionAddRequest,
    _u: dict = Depends(require_plan("pro")),
):
    """Create a new rectangular frame section in ETABS."""
    res = actions.add_rectangular_section(request.name, request.material, request.b, request.h)
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


# ─── RC Beam Section Generator endpoints ────────────────────────────────────

class WriteBeamSectionsRequest(BaseModel):
    sections: List[dict]

@app.get("/api/rc-beam/materials")
def rc_beam_get_materials(
    _u: dict = Depends(require_plan("pro")),
):
    """Return all material names defined in the active ETABS model."""
    res = actions.get_frame_materials()
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res


@app.get("/api/rc-beam/sections")
def rc_beam_import_sections(
    _u: dict = Depends(require_plan("pro")),
):
    """Import all rectangular frame sections from the active ETABS model."""
    res = actions.get_rc_beam_sections()
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res


@app.post("/api/rc-beam/write")
def rc_beam_write_sections(
    request: WriteBeamSectionsRequest,
    _u: dict = Depends(require_plan("pro")),
):
    """Create or update rectangular RC beam sections in ETABS."""
    if not request.sections:
        raise HTTPException(status_code=400, detail="No sections provided.")
    res = actions.write_rc_beam_sections(request.sections)
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res


# ─── RC Column Section Generator endpoints ───────────────────────────────────

class WriteColumnSectionsRequest(BaseModel):
    sections: List[dict]

@app.get("/api/rc-column/materials")
def rc_column_get_materials(
    _u: dict = Depends(require_plan("pro")),
):
    """Return all material names defined in the active ETABS model."""
    res = actions.get_frame_materials()
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res


@app.get("/api/rc-column/sections")
def rc_column_import_sections(
    _u: dict = Depends(require_plan("pro")),
):
    """Import all rectangular frame sections from the active ETABS model as columns."""
    res = actions.get_rc_column_sections()
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res


@app.post("/api/rc-column/write")
def rc_column_write_sections(
    request: WriteColumnSectionsRequest,
    _u: dict = Depends(require_plan("pro")),
):
    """Create or update rectangular RC column sections in ETABS."""
    if not request.sections:
        raise HTTPException(status_code=400, detail="No sections provided.")
    res = actions.write_rc_column_sections(request.sections)
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res




@app.get("/api/rc-column/debug/{section_name}")
def rc_column_debug_raw(
    section_name: str,
    _u: dict = Depends(require_plan("pro")),
):
    """
    Return raw comtypes output for GetRectangle + GetRebarColumn on one section.
    Shows positional values, types, unit_code, and converted mm values.
    Use this to diagnose unit/parsing issues after import.
    """
    res = actions.debug_rc_column_raw(section_name)
    if "error" in res:
        raise HTTPException(status_code=500, detail=res["error"])
    return res


# ─── Run File Cleaner ────────────────────────────────────────────────────────

# File extensions produced by ETABS / SAFE during analysis runs
# Wildcard patterns mirroring the VBA DeleteEtabsRunFiles macro exactly.
# Each pattern uses shell-style wildcards: * = any sequence of characters.
# Matching is performed on the full filename (case-insensitive), not just the
# extension, so files without a dot (e.g. a bare "LOG" file) are also caught.
import fnmatch as _fnmatch

_CLEAN_PATTERNS = [
    "*ebk",  "*ico",  "*K_0",  "*K_E",  "*K_G",  "*K_I",  "*K_J",  "*K_M",
    "*LOG",  "*msh",  "*OUT",
    "*Y",    "*Y$$",
    "*Y00",  "*Y01",  "*Y02",  "*Y03",  "*Y04",  "*Y05",  "*Y06",  "*Y07",
    "*Y08",  "*Y09",  "*Y0A",  "*Y0B",  "*Y0C",  "*Y0D",  "*Y0E",
    "*Y_",   "*Y_1",
    "*xsdm", "*fbk",  "*K_L",  "*CSJ",  "*CSP",  "*K_1",
]

def _clean_matches(filename: str) -> bool:
    """Return True if filename matches any ETABS/SAFE run-file pattern (case-insensitive)."""
    fname = filename.lower()
    return any(_fnmatch.fnmatch(fname, pat.lower()) for pat in _CLEAN_PATTERNS)


class CleanRequest(BaseModel):
    directory: str
    dry_run: bool = True   # True = preview only, False = actually delete


@app.post("/api/clean/run-files")
def clean_run_files(
    body: CleanRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Scan a directory (recursively) for ETABS / SAFE run files and
    optionally delete them.  Free feature — available to all users.
    """
    import pathlib

    import os as _os

    # Normalise: strip surrounding whitespace, quotes, and control characters
    raw = body.directory.strip().strip('"\'').rstrip('\\/').strip()
    # Expand env vars (e.g. %USERPROFILE%) and resolve any symlinks
    raw = _os.path.abspath(_os.path.expandvars(_os.path.expanduser(raw)))
    p = pathlib.Path(raw)

    # Use os.path — more reliable than pathlib on OneDrive / network paths
    if not _os.path.exists(raw):
        # Give the user the parent chain so they can spot the bad segment
        parts = pathlib.PurePath(raw).parts
        found_up_to = ""
        for i in range(len(parts)):
            candidate = str(pathlib.Path(*parts[:i+1]))
            if _os.path.exists(candidate):
                found_up_to = candidate
            else:
                break
        detail = (
            f"Folder not found: {raw}\n"
            f"Last existing segment: {found_up_to or '(none)'}"
        )
        raise HTTPException(status_code=400, detail=detail)
    if not _os.path.isdir(raw):
        raise HTTPException(status_code=400, detail=f"Path is a file, not a folder: {raw}")

    # Recursively collect matching files using os.walk — mirrors VBA
    # DeleteFilesRecursive and works reliably on OneDrive / UNC paths.
    found: list[str] = []
    for dirpath, _dirs, filenames in _os.walk(raw):
        for fname in filenames:
            if _clean_matches(fname):
                found.append(_os.path.join(dirpath, fname))

    found.sort()

    # Measure sizes before any deletion
    def _safe_size(path: str) -> int:
        try:
            return _os.path.getsize(path)
        except Exception:
            return 0

    total_bytes = sum(_safe_size(fp) for fp in found)

    if body.dry_run:
        return {"dry_run": True, "count": len(found), "files": found, "total_bytes": total_bytes}

    # Actually delete (mirrors VBA fso.DeleteFile with error skip)
    deleted, errors = [], []
    deleted_bytes = 0
    for fp in found:
        size = _safe_size(fp)
        try:
            _os.remove(fp)
            deleted.append(fp)
            deleted_bytes += size
        except Exception as exc:
            errors.append({"file": fp, "error": str(exc)})

    return {
        "dry_run":       False,
        "count":         len(found),
        "deleted":       len(deleted),
        "errors":        errors,
        "files":         deleted,
        "total_bytes":   deleted_bytes,
    }


@app.get("/api/clean/browse-folder")
def browse_folder(current_user: dict = Depends(get_current_user)):
    """
    Open a native Windows folder-picker dialog and return the selected path.
    Uses ctypes + Shell32 (SHBrowseForFolderW) in a dedicated STA thread —
    no subprocess, no PowerShell, always appears in the foreground.
    """
    import ctypes
    import ctypes.wintypes as _wt
    import threading as _threading
    import queue    as _queue

    result_q: _queue.Queue = _queue.Queue()

    def _picker_thread() -> None:
        ole32   = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32
        user32  = ctypes.windll.user32

        # COM must be initialised as Single-Threaded Apartment for UI dialogs.
        COINIT_APARTMENTTHREADED = 0x2
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        try:
            # Snap the foreground window NOW (browser window) so the dialog
            # is owned by it and therefore guaranteed to appear in front.
            hwnd = user32.GetForegroundWindow()

            # ── BROWSEINFO structure ──────────────────────────────────────
            class BROWSEINFOW(ctypes.Structure):
                _fields_ = [
                    ("hwndOwner",      _wt.HWND),
                    ("pidlRoot",       ctypes.c_void_p),
                    ("pszDisplayName", _wt.LPWSTR),
                    ("lpszTitle",      _wt.LPCWSTR),
                    ("ulFlags",        _wt.UINT),
                    ("lpfn",           ctypes.c_void_p),
                    ("lParam",         ctypes.c_void_p),
                    ("iImage",         ctypes.c_int),
                ]

            BIF_RETURNONLYFSDIRS = 0x0001  # folders only
            BIF_NEWDIALOGSTYLE   = 0x0040  # resizable modern dialog
            BIF_EDITBOX          = 0x0010  # editable path field

            disp_buf = ctypes.create_unicode_buffer(260)
            bi = BROWSEINFOW()
            bi.hwndOwner      = hwnd
            bi.pszDisplayName = ctypes.cast(disp_buf, ctypes.c_wchar_p)
            bi.lpszTitle      = "Select project folder to clean"
            bi.ulFlags        = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX

            shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
            pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))

            if not pidl:
                result_q.put(None)   # user cancelled
                return

            path_buf = ctypes.create_unicode_buffer(32768)
            shell32.SHGetPathFromIDListW(ctypes.c_void_p(pidl), path_buf)
            ole32.CoTaskMemFree(ctypes.c_void_p(pidl))

            result_q.put(path_buf.value.strip() or None)

        except Exception as _exc:
            result_q.put(_exc)
        finally:
            ole32.CoUninitialize()

    t = _threading.Thread(target=_picker_thread, daemon=True)
    t.start()
    t.join(timeout=120)

    if result_q.empty():
        raise HTTPException(status_code=408, detail="Folder picker timed out.")

    val = result_q.get_nowait()
    if isinstance(val, Exception):
        raise HTTPException(status_code=500, detail=f"Folder picker error: {val}")
    if not val:
        return {"path": None, "cancelled": True}
    return {"path": val, "cancelled": False}

# ============================================================
#  P-M-M INTERACTION DIAGRAM  (ACI 318-19)
# ============================================================

class PMMRequest(BaseModel):
    # Section geometry (rectangular)
    b: float                           # width  — mm (SI) or in (US)
    h: float                           # depth  — mm (SI) or in (US)
    # Materials
    fc: float                          # f'c — MPa (SI) or ksi (US)
    fy: float                          # fy  — MPa (SI) or ksi (US)
    Es: float = 200000.0               # Es  — MPa (SI) or ksi (US)
    # Reinforcement
    cover:    float                    # clear cover to bar centre — mm or in
    nbars_b:  int   = 3               # bars on bottom/top face incl. corners (≥2)
    nbars_h:  int   = 1               # bars on each side face excl. corners (≥0)
    bar_size: str = "Ø20"             # rebar designation e.g. "Ø20" or "#8"
    # Analysis options
    include_phi:  bool  = True
    alpha_steps:  float = 5.0
    num_points:   int   = 225
    # Unit system
    units: str = "SI"                  # "SI" or "US"
    # Optional demand point (in the same unit system as above)
    demand_P:  Optional[float] = None
    demand_Mx: Optional[float] = None
    demand_My: Optional[float] = None


@app.post("/api/pmm/calculate")
def pmm_calculate(body: PMMRequest,
                  current_user: dict = Depends(get_current_user)):
    if not _HAS_PMM:
        raise HTTPException(status_code=503,
                            detail="PMM engine not available (shapely missing).")

    si = body.units.upper() == "SI"

    # Resolve bar area
    if si:
        bar_area_mm2 = REBAR_TABLE_SI.get(body.bar_size)
        if bar_area_mm2 is None:
            raise HTTPException(status_code=422,
                                detail=f"Unknown bar size '{body.bar_size}'. "
                                       f"Valid SI sizes: {list(REBAR_TABLE_SI.keys())}")
        # Convert inputs to US customary for the engine (works in kips + inches)
        b_in      = body.b      * _MM_TO_IN
        h_in      = body.h      * _MM_TO_IN
        fc_ksi    = body.fc     * _MPA_TO_KSI
        fy_ksi    = body.fy     * _MPA_TO_KSI
        Es_ksi    = body.Es     * _MPA_TO_KSI
        cover_in  = body.cover  * _MM_TO_IN
        bar_area  = bar_area_mm2 * (_MM_TO_IN ** 2)   # mm² → in²
    else:
        bar_area = REBAR_TABLE.get(body.bar_size)
        if bar_area is None:
            raise HTTPException(status_code=422,
                                detail=f"Unknown bar size '{body.bar_size}'. "
                                       f"Valid US sizes: {list(REBAR_TABLE.keys())}")
        b_in = body.b; h_in = body.h
        fc_ksi = body.fc; fy_ksi = body.fy; Es_ksi = body.Es
        cover_in = body.cover; bar_area = bar_area

    if b_in <= 0 or h_in <= 0:
        raise HTTPException(status_code=422, detail="b and h must be positive.")
    if fc_ksi <= 0 or fy_ksi <= 0:
        raise HTTPException(status_code=422, detail="Material strengths must be positive.")
    if cover_in < 1.5 / 25.4:
        raise HTTPException(status_code=422, detail="Cover too small.")
    nbars_b = max(2, body.nbars_b)
    nbars_h = max(0, body.nbars_h)
    if 2 * nbars_b + 2 * nbars_h < 4:
        raise HTTPException(status_code=422, detail="Minimum 4 bars required.")

    corners  = rect_coords(b_in, h_in)
    areas, positions = rect_bars_grid(b_in, h_in, cover_in, nbars_b, nbars_h, bar_area)

    sec = PMMSection(
        corner_coords = corners,
        fc            = fc_ksi,
        fy            = fy_ksi,
        Es            = Es_ksi,
        alpha_steps   = body.alpha_steps,
        num_points    = body.num_points,
        include_phi   = body.include_phi,
        bar_areas     = areas,
        bar_positions = positions,
    )

    try:
        result = compute_pmm(sec)
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"PMM computation failed: {exc}")

    # Convert output back to SI if needed
    if si:
        def _cvt_surface(r):
            r['surface']['P']  = [round(v * _KIPS_TO_KN,  2) for v in r['surface']['P']]
            r['surface']['Mx'] = [round(v * _KIN_TO_KNM,  3) for v in r['surface']['Mx']]
            r['surface']['My'] = [round(v * _KIN_TO_KNM,  3) for v in r['surface']['My']]
            # Convert ALL alpha_data curves (curves_2d shares the same dict references,
            # so they are converted automatically here — no separate loop needed)
            for adeg, curve in r.get('alpha_data', {}).items():
                curve['P']  = [round(v * _KIPS_TO_KN,  2) for v in curve['P']]
                curve['Mx'] = [round(v * _KIN_TO_KNM,  3) for v in curve['Mx']]
                curve['My'] = [round(v * _KIN_TO_KNM,  3) for v in curve['My']]
            r['Pmax']     = round(r['Pmax']     * _KIPS_TO_KN, 2)
            r['Pmin']     = round(r['Pmin']     * _KIPS_TO_KN, 2)
            r['Ag']       = round(r['Ag']       * _IN2_TO_MM2, 0)
            r['Ast']      = round(r['Ast']      * _IN2_TO_MM2, 1)
            r['centroid'] = [round(v * _IN_TO_MM, 1) for v in r['centroid']]
            return r
        result = _cvt_surface(result)

    # Cache results AFTER conversion so units are consistent (SI when si=True)
    _pmm_cache['alpha_data'] = result.get('alpha_data', {})
    _pmm_cache['Pmax']       = result.get('Pmax', 0)
    _pmm_cache['Pmin']       = result.get('Pmin', 0)
    _pmm_cache['si']         = si

    if si:
        result['units'] = 'SI'
        # Convert demand point
        if body.demand_P is not None:
            result['demand'] = {
                'P':  body.demand_P,
                'Mx': body.demand_Mx or 0.0,
                'My': body.demand_My or 0.0,
            }
        else:
            result['demand'] = None
    else:
        result['units'] = 'US'
        result['demand'] = None
        if body.demand_P is not None:
            result['demand'] = {
                'P':  body.demand_P,
                'Mx': body.demand_Mx or 0.0,
                'My': body.demand_My or 0.0,
            }

    return result


class PMMCheckRequest(BaseModel):
    demands: list  # [{label, P (kN), Mx (kN·m), My (kN·m)}]

@app.post("/api/pmm/check")
def pmm_check(body: PMMCheckRequest,
              current_user: dict = Depends(get_current_user)):
    if not _HAS_PMM:
        raise HTTPException(status_code=503, detail="PMM engine not available.")
    if _pmm_cache['alpha_data'] is None:
        raise HTTPException(status_code=400,
                            detail="No PMM surface computed yet. Run Calculate first.")
    ad   = _pmm_cache['alpha_data']
    Pmax = _pmm_cache['Pmax']
    Pmin = _pmm_cache['Pmin']

    # alpha_data is already in the same units as the frontend (SI when si=True,
    # engine units when si=False). No conversion needed — just pass demands through.
    demands = [{'label': d.get('label', ''), 'P': float(d.get('P', 0)),
                'Mx': float(d.get('Mx', 0)), 'My': float(d.get('My', 0))}
               for d in body.demands]

    raw = check_demands(ad, Pmax, Pmin, demands)
    return {'results': raw}


class PMMOptimizeRequest(BaseModel):
    # Section geometry (fixed — no section size changes)
    b_mm:     float
    h_mm:     float
    fc_mpa:   float
    fy_mpa:   float
    Es_mpa:   float = 200000.0
    cover_mm: float          # distance from face to bar centre (mm)
    include_phi: bool = True
    # Bar diameter is FIXED — optimizer only adjusts bar count / arrangement
    bar_size:    str   = "Ø20"
    target_dcr:  float = 0.90
    max_rho_pct: float = 4.0   # user-defined ρ upper limit (%)
    demands: list = []


# ---------------------------------------------------------------------------
# Optimizer helpers
# ---------------------------------------------------------------------------

def _db_mm(area_mm2: float) -> float:
    """Nominal bar diameter (mm) from cross-sectional area."""
    return math.sqrt(4.0 * area_mm2 / math.pi)


def _check_spacing_aci(b_mm: float, h_mm: float, cover_mm: float,
                       nb: int, nh: int, db: float):
    """
    ACI 318-19 column longitudinal bar spacing checks.

    §25.8.1   min clear spacing = max(1.5·db, 40 mm)  — ALL adjacent bar pairs
    §25.7.2.3 max clear spacing = 150 mm               — ALL faces (incl. corner-only)

    cover_mm = face → bar CENTRE distance.
    Returns (ok, min_clear_mm, max_clear_mm)
      min_clear_mm : smallest clear spacing across all bar pairs (governs §25.8.1)
      max_clear_mm : largest clear spacing across all faces      (governs §25.7.2.3)
    """
    S_MIN = max(1.5 * db, 40.0)   # §25.8.1
    S_MAX = 150.0                  # §25.7.2.3 — applies to ALL faces

    all_sc: List[float] = []

    # ── b-face (both corner-only nb=2 and with intermediates nb>2) ───────────
    if nb >= 2:
        cc_b = (b_mm - 2.0 * cover_mm) / (nb - 1)
        sc_b = cc_b - db
        if cc_b <= db or sc_b < S_MIN:           # bars overlap or min violated
            return False, sc_b, sc_b
        if sc_b > S_MAX:                         # max violated on ANY b-face spacing
            return False, sc_b, sc_b
        all_sc.append(sc_b)

    # ── h-face ──────────────────────────────────────────────────────────────
    if nh >= 1:                                  # intermediate bars on h-face
        cc_h = (h_mm - 2.0 * cover_mm) / (nh + 1)
        sc_h = cc_h - db
        if cc_h <= db or sc_h < S_MIN:
            return False, sc_h, sc_h
        if sc_h > S_MAX:
            return False, sc_h, sc_h
        all_sc.append(sc_h)
    else:                                        # corner bars only on h-face
        sc_h = (h_mm - 2.0 * cover_mm) - db     # corner-to-corner clear
        if sc_h <= 0:
            return False, sc_h, sc_h
        if sc_h > S_MAX:                         # max violated on corner-only h-face
            return False, sc_h, sc_h
        all_sc.append(sc_h)

    min_c = min(all_sc) if all_sc else 0.0
    max_c = max(all_sc) if all_sc else 0.0
    return True, round(min_c, 1), round(max_c, 1)


def _boundary_dcr_py(surface_si: dict, num_points: int,
                     demands_si: list) -> float:
    """
    Python mirror of the JS pmmBoundaryAtP + pmmRayBoundaryIntersect logic.

    Slices the 3D surface at each demand's P level to build the Mx-My
    boundary polygon, then casts a ray in the demand direction and returns
    the maximum DCR = M_demand / M_cap_boundary across all demands.

    surface_si  – dict with keys 'P', 'Mx', 'My' (all SI, engine-sign:
                  positive P = compression, same ordering as JS allP).
    num_points  – points per meridian (== alpha sweep resolution).
    demands_si  – list of dicts {'P', 'Mx', 'My'} in SI, engine-sign P.
    """
    allP  = surface_si['P']
    allMx = surface_si['Mx']
    allMy = surface_si['My']
    n_total   = len(allP)
    num_alpha = round(n_total / num_points)

    max_dcr = 0.0
    for d in demands_si:
        dx, dy = float(d['Mx']), float(d['My'])
        Md = math.sqrt(dx * dx + dy * dy)
        if Md < 1e-9:
            continue
        Ptarget = float(d['P'])   # engine-sign: positive = compression

        # ── Build boundary polygon at Ptarget ──────────────────────────
        bx: list = []
        by: list = []
        for a in range(num_alpha):
            base = a * num_points
            mP  = allP [base: base + num_points]
            mMx = allMx[base: base + num_points]
            mMy = allMy[base: base + num_points]
            if Ptarget <= mP[0]:
                mx, my = mMx[0], mMy[0]
            elif Ptarget >= mP[-1]:
                mx, my = mMx[-1], mMy[-1]
            else:
                lo, hi = 0, num_points - 1
                while hi - lo > 1:
                    mid = (lo + hi) >> 1
                    if mP[mid] <= Ptarget:
                        lo = mid
                    else:
                        hi = mid
                t  = (Ptarget - mP[lo]) / (mP[hi] - mP[lo])
                mx = mMx[lo] + t * (mMx[hi] - mMx[lo])
                my = mMy[lo] + t * (mMy[hi] - mMy[lo])
            bx.append(mx)
            by.append(my)
        bx.append(bx[0])   # close polygon
        by.append(by[0])

        # ── Ray-polygon intersection ─────────────────────────────────
        n_seg  = len(bx) - 1
        best_t = float('inf')
        cap_x  = cap_y = None
        for i in range(n_seg):
            Ax, Ay   = bx[i],     by[i]
            dBx, dBy = bx[i+1]-Ax, by[i+1]-Ay
            det = dx * dBy - dy * dBx
            if abs(det) < 1e-10:
                continue
            t = (Ax * dBy - Ay * dBx) / det
            s = (Ax * dy  - Ay * dx ) / det
            if t > 1e-9 and -1e-9 <= s <= 1.0 + 1e-9 and t < best_t:
                best_t = t
                cap_x  = t * dx
                cap_y  = t * dy

        if cap_x is None:
            continue
        M_geo = math.sqrt(cap_x * cap_x + cap_y * cap_y)
        if M_geo < 1e-9:
            continue
        max_dcr = max(max_dcr, Md / M_geo)

    return max_dcr


def _run_pmm_opt(b_in, h_in, fc_ksi, fy_ksi, Es_ksi, cover_in,
                 nb, nh, bar_area_in2, include_phi, demands_si):
    """
    Run PMM for optimisation with same accuracy as the 'Fast (10°)' preset
    (alpha_steps=10, num_points=120).

    Uses boundary-based DCR (ray-polygon intersection on the Mx-My slice at
    each demand's P level) — the same metric shown in the frontend — so the
    optimiser and the display are always consistent.
    Returns max DCR (float) or None on error.
    """
    try:
        corners = rect_coords(b_in, h_in)
        areas, positions = rect_bars_grid(b_in, h_in, cover_in, nb, nh, bar_area_in2)
        sec = PMMSection(
            corner_coords=corners, fc=fc_ksi, fy=fy_ksi, Es=Es_ksi,
            alpha_steps=10.0, num_points=120,   # matches UI "Fast (10°)" preset
            include_phi=include_phi,
            bar_areas=areas, bar_positions=positions,
        )
        result = compute_pmm(sec)
    except Exception:
        return None

    # Convert surface to SI (engine returns US units)
    surf = result.get('surface', {})
    if surf and 'P' in surf and 'Mx' in surf and 'My' in surf:
        surf_si = {
            'P':  [v * _KIPS_TO_KN  for v in surf['P']],
            'Mx': [v * _KIN_TO_KNM  for v in surf['Mx']],
            'My': [v * _KIN_TO_KNM  for v in surf['My']],
        }
        return _boundary_dcr_py(surf_si, 120, demands_si)

    # Fallback: engine DCR (less conservative but avoids silent failure)
    for curve in result.get('alpha_data', {}).values():
        curve['P']  = [v * _KIPS_TO_KN  for v in curve['P']]
        curve['Mx'] = [v * _KIN_TO_KNM  for v in curve['Mx']]
        curve['My'] = [v * _KIN_TO_KNM  for v in curve['My']]
    Pmax = result['Pmax'] * _KIPS_TO_KN
    Pmin = result['Pmin'] * _KIPS_TO_KN
    checks = check_demands(result['alpha_data'], Pmax, Pmin, demands_si)
    return max((c['DCR'] for c in checks), default=0.0)


@app.post("/api/pmm/optimize")
def pmm_optimize(body: PMMOptimizeRequest,
                 current_user: dict = Depends(get_current_user)):
    """
    Optimise bar count/arrangement for a FIXED bar diameter and FIXED section size.

    Strategy:
      • Sort all ACI-spacing-valid (nb, nh) arrangements by ascending Ast (ρ_min → ρ_max).
      • Compute DCR for each, starting from the lightest arrangement.
      • Return the arrangement whose DCR is closest to target from below (DCR ≤ target).
        – If even the maximum-steel arrangement gives DCR > target, return that maximum
          arrangement with a warning flag so the caller knows the target was not met.
      • Never modifies the section size or bar diameter.
    """
    if not _HAS_PMM:
        raise HTTPException(status_code=503, detail="PMM engine not available.")
    if not body.demands:
        raise HTTPException(status_code=422, detail="At least one demand required.")

    target  = float(body.target_dcr)
    RHO_MIN = 0.01
    RHO_MAX = min(float(body.max_rho_pct) / 100.0, 0.08)   # cap at ACI max 8 %

    BAR_AREA_MAP = {
        "Ø8":  50.3,  "Ø10":  78.5,  "Ø12": 113.1,
        "Ø16": 201.1, "Ø20": 314.2,  "Ø25": 490.9,
        "Ø28": 615.8, "Ø32": 804.2,  "Ø36": 1017.9, "Ø40": 1256.6,
    }
    bar_name = body.bar_size
    area_mm2 = BAR_AREA_MAP.get(bar_name)
    if area_mm2 is None:
        raise HTTPException(status_code=422, detail=f"Unknown bar size '{bar_name}'.")

    db           = _db_mm(area_mm2)
    bar_area_in2 = area_mm2 * (_MM_TO_IN ** 2)
    s_min_req    = round(max(1.5 * db, 40.0), 1)   # ACI §25.8.1

    b_mm     = float(body.b_mm)
    h_mm     = float(body.h_mm)
    Ag_mm2   = b_mm * h_mm
    b_in     = b_mm * _MM_TO_IN
    h_in     = h_mm * _MM_TO_IN
    fc_ksi   = body.fc_mpa  * _MPA_TO_KSI
    fy_ksi   = body.fy_mpa  * _MPA_TO_KSI
    Es_ksi   = body.Es_mpa  * _MPA_TO_KSI
    cover_in = body.cover_mm * _MM_TO_IN

    demands = [{'label': d.get('label', ''), 'P': float(d.get('P', 0)),
                'Mx': float(d.get('Mx', 0)), 'My': float(d.get('My', 0))}
               for d in body.demands]

    # ── Build candidate arrangements ────────────────────────────────────────
    # nb = bars per b-face (incl. corners): 2 … 6
    # nh = intermediate bars per h-face:    0 … 4
    candidates: List[tuple] = []
    for nb in range(2, 7):
        for nh in range(0, 5):
            n_total = 2 * nb + 2 * nh
            ast = n_total * area_mm2
            rho = ast / Ag_mm2
            if not (RHO_MIN <= rho <= RHO_MAX):
                continue
            ok, min_c, max_c = _check_spacing_aci(
                b_mm, h_mm, body.cover_mm, nb, nh, db)
            if not ok:
                continue
            candidates.append((ast, rho, nb, nh, n_total, min_c, max_c))

    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="No feasible arrangement found within the ρ and spacing limits. "
                   "Try a different bar size or adjust the ρ limit.")

    candidates.sort(key=lambda x: x[0])   # ascending Ast (ρ_min → ρ_max)

    # ── Evaluate each candidate, stop as soon as we find the lightest valid ─
    best_valid    = None   # lightest arrangement with DCR ≤ target
    best_valid_ast = None
    heaviest      = None   # fallback: heaviest arrangement (max capacity)

    for ast, rho, nb, nh, n_total, min_c, max_c in candidates:
        # Once we have a valid design, only keep checking arrangements that are
        # within 8 % more steel (catches equivalent arrangements at same Ast level).
        if best_valid is not None and ast > best_valid_ast * 1.08:
            break

        max_dcr = _run_pmm_opt(b_in, h_in, fc_ksi, fy_ksi, Es_ksi,
                               cover_in, nb, nh, bar_area_in2,
                               body.include_phi, demands)
        if max_dcr is None:
            continue

        entry = {
            'ast': ast, 'rho': rho,
            'nbars_b': nb, 'nbars_h': nh, 'n_total': n_total,
            'bar_size': bar_name, 'area_mm2': area_mm2,
            'min_clear_mm': min_c, 'max_clear_mm': max_c,
            'max_dcr': max_dcr,
        }
        # Track heaviest evaluated (most capacity) as fallback
        if heaviest is None or ast > heaviest['ast']:
            heaviest = entry

        if max_dcr <= target:
            # Keep the arrangement closest to target (highest DCR ≤ target)
            if best_valid is None or max_dcr > best_valid['max_dcr']:
                best_valid = entry
                best_valid_ast = ast

    # If target not achieved, use the heaviest arrangement (best capacity available)
    target_met = best_valid is not None
    best = best_valid if target_met else heaviest

    if best is None:
        raise HTTPException(status_code=400,
                            detail="Optimisation failed — no valid PMM result.")

    nb     = best['nbars_b']
    nh     = best['nbars_h']
    ntotal = best['n_total']
    arrangement = (f"{nb} bars/face · {ntotal} total" if nh == 0
                   else f"{nb}+{nh} bars/face · {ntotal} total")

    return {
        'b_mm':               round(b_mm),
        'h_mm':               round(h_mm),
        'nbars_b':            nb,
        'nbars_h':            nh,
        'n_total':            ntotal,
        'arrangement':        arrangement,
        'bar_size':           best['bar_size'],
        'rho_pct':            round(best['rho'] * 100, 2),
        'achieved_dcr':       round(best['max_dcr'] * 100, 1),
        'target_met':         target_met,
        'min_clear_mm':  best['min_clear_mm'],
        'max_clear_mm':  best['max_clear_mm'],
        'min_clear_req': s_min_req,
        'max_clear_req': 150.0,
    }


@app.get("/api/pmm/rebar-table")
def pmm_rebar_table(units: str = "SI",
                    current_user: dict = Depends(get_current_user)):
    if not _HAS_PMM:
        raise HTTPException(status_code=503, detail="PMM engine not available.")
    return REBAR_TABLE_SI if units.upper() == "SI" else REBAR_TABLE


def _get_pmm_column_sections_fallback() -> dict:
    """Build PMM-ready ETABS column sections if actions helper is unavailable."""
    try:
        SapModel = actions.get_active_etabs()
    except Exception:
        SapModel = None
    if not SapModel:
        return {"error": "ETABS is not currently running."}

    try:
        try:
            unit_code = SapModel.GetDatabaseUnits()
        except Exception:
            try:
                unit_code = SapModel.GetPresentUnits()
            except Exception:
                unit_code = 6

        base = actions.get_rc_column_sections()
        if "error" in base:
            return base
        sections = base.get("sections", [])

        mat_cache = {}

        def _stress_to_mpa_local(v: float) -> float:
            if hasattr(actions, "_stress_to_mpa"):
                return float(actions._stress_to_mpa(v, unit_code))
            if unit_code in (1, 2):
                return v * 6.89476e-3
            if unit_code in (3, 4):
                return v * 6.89476
            if unit_code in (5, 9):
                return v
            if unit_code == 6:
                return v * 1e-3
            if unit_code == 10:
                return v * 1e-6
            if unit_code == 7:
                return v * 9.80665
            if unit_code == 8:
                return v * 9.80665e-3
            return v

        def _lookup_mat(mat_name: str):
            if mat_name in mat_cache:
                return mat_cache[mat_name]
            result = {"fc_mpa": None, "fy_mpa": None, "Es_mpa": None}
            try:
                cr = SapModel.PropMaterial.GetOConcrete(mat_name)
                if cr and int(cr[-1]) == 0:
                    result["fc_mpa"] = round(_stress_to_mpa_local(float(cr[0])), 1)
            except Exception:
                pass
            try:
                rr = SapModel.PropMaterial.GetRebar(mat_name)
                if rr and int(rr[-1]) == 0:
                    result["fy_mpa"] = round(_stress_to_mpa_local(float(rr[0])), 1)
                    result["Es_mpa"] = round(_stress_to_mpa_local(float(rr[2])), 0)
            except Exception:
                pass
            mat_cache[mat_name] = result
            return result

        enriched = []
        for sec in sections:
            entry = dict(sec)
            conc_props = _lookup_mat(sec.get("material", ""))
            entry["fc_mpa"] = conc_props["fc_mpa"]
            rebar_props = _lookup_mat(sec.get("fy_main", ""))
            entry["fy_mpa"] = rebar_props["fy_mpa"]
            entry["Es_mpa"] = rebar_props["Es_mpa"]
            enriched.append(entry)

        return {"status": "success", "sections": enriched}
    except Exception as e:
        return {"error": f"Failed to get PMM column sections: {str(e)}"}


@app.get("/api/pmm/etabs-sections")
def pmm_etabs_sections(current_user: dict = Depends(get_current_user)):
    """Return all RC column sections from the active ETABS model for PMM use."""
    helper = getattr(actions, "get_pmm_column_sections", None)
    result = helper() if callable(helper) else _get_pmm_column_sections_fallback()
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])
    return result


@app.get("/api/pmm/etabs-combos")
def pmm_etabs_combos(current_user: dict = Depends(get_current_user)):
    """Return all load combinations and load cases from the active ETABS model."""
    helper = getattr(actions, "get_etabs_combos", None)
    if not callable(helper):
        raise HTTPException(status_code=503, detail="get_etabs_combos not available.")
    result = helper()
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])
    return result


class ETABSImportForcesRequest(BaseModel):
    combo_names: list
    load_type: str = "combo"   # "combo" or "case"


@app.post("/api/pmm/etabs-import-forces")
def pmm_etabs_import_forces(body: ETABSImportForcesRequest,
                             current_user: dict = Depends(get_current_user)):
    """
    Get frame forces for currently selected columns in ETABS,
    for the chosen load combinations / cases.
    Returns {results: [{label, P_kN, M3_kNm, M2_kNm}, ...]}
    """
    helper = getattr(actions, "get_etabs_frame_forces", None)
    if not callable(helper):
        raise HTTPException(status_code=503, detail="get_etabs_frame_forces not available.")
    result = helper(body.combo_names, body.load_type)
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])
    return result
