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

# Load pmm_engine.py from the filesystem for the same reason — frozen bytecode
# inside the .exe would otherwise ignore any updated _internal/pmm_engine.py.
_pme = _os.path.join(_here, 'pmm_engine.py')
if _os.path.isfile(_pme):
    _spec2 = _ilu.spec_from_file_location('pmm_engine', _pme)
    _pme_mod = _ilu.module_from_spec(_spec2)
    _sys.modules['pmm_engine'] = _pme_mod
    _spec2.loader.exec_module(_pme_mod)

# Log that the filesystem main.py was loaded (visible in structiq.log)
import sys as _sys2, os as _os2
_LOG_PATH = _os2.path.join(
    _os2.path.dirname(_sys2.executable) if getattr(_sys2, 'frozen', False) else _os2.path.dirname(_os2.path.abspath(__file__)),
    'structiq.log'
)
try:
    with open(_LOG_PATH, 'a', encoding='utf-8') as _lf:
        _eng_ver = getattr(_sys2.modules.get('pmm_engine'), '_ENGINE_VERSION', 'unknown')
        _lf.write(f'[main.py] loaded from filesystem: {__file__}\n')
        _lf.write(f'[main.py] pmm_engine version: {_eng_ver}\n')
except Exception:
    pass

import database
import config
import math
import os
import time as _time
from concurrent.futures import ThreadPoolExecutor as _TPE

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

# Batch PMM surface cache — keyed by section fingerprint, cleared on each new batch run.
# Avoids recomputing the same interaction surface when the user re-runs with the same model.
_PMM_SURFACE_CACHE: dict = {}

# Cached forces + section metadata from the last batch run — used by the rebar optimizer
# so it can work entirely offline (no ETABS calls needed after the batch check).
_last_batch_forces: dict = {}  # {"by_section": {...}, "sections": [...], "combo_names": [...]}

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
    allow_origins=["http://127.0.0.1", "http://localhost"],
    allow_origin_regex=r"http://(127\.0\.0\.1|localhost)(:\d+)?",
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
                # Admin emails always bypass session limits
                _is_admin_login = body.email.lower() in [e.lower() for e in config.ADMIN_EMAILS]
                if not _is_admin_login:
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
    cover:          float              # clear cover (face → stirrup face) — mm or in
    stirrup_dia_mm: float = 10.0      # shear tie / stirrup diameter (mm); ignored if units=US
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

    # Rebar nominal diameters (mm) for effective cover calculation
    _REBAR_DIA_MM = {
        "Ø8": 8.0, "Ø10": 10.0, "Ø12": 12.0, "Ø16": 16.0, "Ø20": 20.0,
        "Ø25": 25.0, "Ø28": 28.0, "Ø32": 32.0, "Ø36": 36.0, "Ø40": 40.0,
    }

    # Resolve bar area
    if si:
        bar_area_mm2 = REBAR_TABLE_SI.get(body.bar_size)
        if bar_area_mm2 is None:
            raise HTTPException(status_code=422,
                                detail=f"Unknown bar size '{body.bar_size}'. "
                                       f"Valid SI sizes: {list(REBAR_TABLE_SI.keys())}")
        # Effective cover = clear cover + stirrup dia + half bar dia (ACI 318)
        bar_dia_mm   = _REBAR_DIA_MM.get(body.bar_size, 20.0)
        eff_cover_mm = body.cover + body.stirrup_dia_mm + bar_dia_mm / 2.0
        # Convert inputs to US customary for the engine (works in kips + inches)
        b_in      = body.b      * _MM_TO_IN
        h_in      = body.h      * _MM_TO_IN
        fc_ksi    = body.fc     * _MPA_TO_KSI
        fy_ksi    = body.fy     * _MPA_TO_KSI
        Es_ksi    = body.Es     * _MPA_TO_KSI
        cover_in  = eff_cover_mm * _MM_TO_IN
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
        import traceback as _tb, datetime as _dt
        try:
            with open(_LOG_PATH, 'a', encoding='utf-8') as _lf:
                _lf.write(
                    f"[{_dt.datetime.now().isoformat()}] [pmm_calculate] compute_pmm failed: {exc}\n"
                    + ''.join(_tb.format_exc().splitlines(keepends=True)[:8])
                )
        except Exception:
            pass
        raise HTTPException(status_code=500,
                            detail=f"PMM computation failed: {exc}")

    # Convert output back to SI if needed
    if si:
        def _cvt_surface(r):
            r['surface']['P']  = [round(v * _KIPS_TO_KN,  2) for v in r['surface']['P']]
            r['surface']['Mx'] = [round(v * _KIN_TO_KNM,  3) for v in r['surface']['Mx']]
            r['surface']['My'] = [round(v * _KIN_TO_KNM,  3) for v in r['surface']['My']]
            # Convert ALL alpha_data curves
            for adeg, curve in r.get('alpha_data', {}).items():
                curve['P']  = [round(v * _KIPS_TO_KN,  2) for v in curve['P']]
                curve['Mx'] = [round(v * _KIN_TO_KNM,  3) for v in curve['Mx']]
                curve['My'] = [round(v * _KIN_TO_KNM,  3) for v in curve['My']]
            # Convert curves_2d explicitly (independent copies — no shared refs)
            for key, curve in r.get('curves_2d', {}).items():
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

    # Embed engine version so the client can verify which pmm_engine is active
    try:
        import pmm_engine as _pme_mod
        result['engine_version'] = getattr(_pme_mod, '_ENGINE_VERSION', 'unknown-frozen')
    except Exception:
        result['engine_version'] = 'error'

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
    # Cover: clear cover from concrete face to stirrup face (mm) — same as UI input
    cover_mm:      float
    stirrup_dia_mm: float = 10.0   # tie/stirrup diameter for eff-cover calculation
    include_phi: bool = True
    # Bar size — fixed when sweep_bar_sizes=False; used as starting hint when True
    bar_size:    str   = "Ø20"
    target_dcr:  float = 0.90
    min_rho_pct: float = 1.0   # ACI §10.6.1.1 minimum ρ (%)
    max_rho_pct: float = 4.0   # user-defined ρ upper limit (%)
    # Surface resolution — should match the UI's current resolution so optimizer DCR = Check DCR
    alpha_steps: float = 10.0
    num_points:  int   = 70
    demands: list = []
    # Bar size sweep — try all standard sizes and return the globally lightest arrangement
    sweep_bar_sizes:     bool      = False
    bar_size_candidates: List[str] = []   # empty = all Ø8 → Ø40


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
    Exact Python mirror of the JS pmmBoundaryAtP parametric-ellipse method.

    At each demand's P level:
      1. Scan all meridians → find Mx_max = max|Mx|, My_max = max|My|
         (identical logic to pmmBoundaryAtP in app.js)
      2. Ellipse ray intersection:
            M_cap = 1 / sqrt( (cosθ/Mx_max)² + (sinθ/My_max)² )
         where θ = atan2(My_demand, Mx_demand)
      3. DCR = Md / M_cap

    This guarantees the optimizer's accept/reject decision matches exactly
    what the frontend will display after applying the design — no false
    "PASS" from the optimizer that appears as FAIL in the UI.

    surface_si  – dict with keys 'P', 'Mx', 'My' (SI, engine-sign positive=compression).
    num_points  – points per meridian (matches alpha sweep resolution).
    demands_si  – list of dicts {'P', 'Mx', 'My'} in SI, engine-sign P.
    """
    allP  = surface_si['P']
    allMx = surface_si['Mx']
    allMy = surface_si['My']
    n_total   = len(allP)
    num_alpha = round(n_total / num_points)

    global_Pmin = min(allP)
    global_Pmax = max(allP)

    max_dcr = 0.0
    for d in demands_si:
        dx, dy = float(d['Mx']), float(d['My'])
        Md = math.sqrt(dx * dx + dy * dy)
        if Md < 1e-9:
            continue
        Ptarget = float(d['P'])   # engine-sign: positive = compression

        if Ptarget < global_Pmin - 1e-6 or Ptarget > global_Pmax + 1e-6:
            max_dcr = max(max_dcr, 999.0)
            continue

        # ── Step 1: find Mx_max and My_max at Ptarget across all meridians ──
        # Mirrors pmmBoundaryAtP in app.js exactly.
        Mx_max = 0.0
        My_max = 0.0
        for a in range(num_alpha):
            base = a * num_points
            mP  = allP [base: base + num_points]
            mMx = allMx[base: base + num_points]
            mMy = allMy[base: base + num_points]
            mPmax = max(mP)
            if Ptarget > mPmax + 1e-6:
                continue   # demand above this meridian's Pmax → skip

            # Find the best (highest-M) interpolated point on this meridian
            mx, my, best_M = 0.0, 0.0, -1.0
            for j in range(num_points - 1):
                p1, p2 = mP[j], mP[j + 1]
                dp = p2 - p1
                if abs(dp) < 1e-12:
                    continue
                t = (Ptarget - p1) / dp
                if t < -1e-9 or t > 1.0 + 1e-9:
                    continue
                tc  = max(0.0, min(1.0, t))
                cMx = mMx[j] + tc * (mMx[j + 1] - mMx[j])
                cMy = mMy[j] + tc * (mMy[j + 1] - mMy[j])
                M   = cMx * cMx + cMy * cMy
                if M > best_M:
                    best_M, mx, my = M, cMx, cMy
            if best_M < 0:
                mPmin = min(mP)
                mx, my = (mMx[0], mMy[0]) if Ptarget <= mPmin else (0.0, 0.0)

            if abs(mx) > Mx_max:
                Mx_max = abs(mx)
            if abs(my) > My_max:
                My_max = abs(my)

        if Mx_max < 1e-9 and My_max < 1e-9:
            max_dcr = max(max_dcr, 999.0)
            continue

        # ── Step 2: ellipse ray intersection → M_cap ────────────────────────
        # Ray direction unit vector components
        cos_t = dx / Md   # = cos(θ)
        sin_t = dy / Md   # = sin(θ)
        inv_cap_sq = 0.0
        if Mx_max > 1e-9:
            inv_cap_sq += (cos_t / Mx_max) ** 2
        if My_max > 1e-9:
            inv_cap_sq += (sin_t / My_max) ** 2
        if inv_cap_sq < 1e-20:
            continue
        M_cap = 1.0 / math.sqrt(inv_cap_sq)

        max_dcr = max(max_dcr, Md / M_cap)

    return max_dcr


def _boundary_dcr_surface(surface_si: dict, num_pts: int,
                          demands_si: list) -> float:
    """
    Python equivalent of the frontend pmmUpdateDCRFromBoundary function.

    Replicates the EXACT algorithm used by the frontend's Check DCR display:
      1. For each demand, slice the 3-D PMM surface at that P level → per-meridian
         (Mx, My) boundary points  [mirrors pmmBoundaryAtP in app.js]
      2. Build a 4-fold symmetric cloud and take its support-function convex hull
         (180 directions) — same convex geometry as the 3-D surface cross-section
      3. Cast a ray from the origin in the demand's moment direction and intersect
         it with the hull boundary  [mirrors pmmRayBoundaryIntersect in app.js]
      4. DCR = |M_demand| / |M_cap at intersection|

    Demand convention note
    ──────────────────────
    The frontend pmmOptimize sends demands with Mx/My swapped to engine convention
    (engine Mx = M22 weak-axis, engine My = M33 strong-axis).
    pmmUpdateDCRFromBoundary casts the ray in TABLE convention (p.Mx=M33, p.My=M22).
    We therefore un-swap here:  ray_x = d['My'] (M33),  ray_y = d['Mx'] (M22).
    This makes the Python result identical to the frontend display.

    surface_si  – dict {'P', 'Mx', 'My'} already in SI (kN / kN·m),
                  organised as nAlpha meridians × num_pts points.
    num_pts     – actual points per meridian (surface_si may contain the key
                  'num_points'; caller should pass that value).
    demands_si  – list of dicts {'P', 'Mx', 'My'} in SI, engine-sign P
                  (+compression), as sent by the pmmOptimize frontend.
    Returns max DCR (float).
    """
    try:
        import numpy as _np
        _use_np = True
    except ImportError:
        _use_np = False

    allP  = surface_si['P']
    allMx = surface_si['Mx']
    allMy = surface_si['My']
    n_total = len(allP)
    if not n_total or num_pts < 2:
        return 0.0

    nPts   = num_pts
    nAlpha = round(n_total / nPts)
    if nAlpha == 0:
        return 0.0

    global_Pmax = max(allP)
    global_Pmin = min(allP)

    # Number of hull directions — 180 gives ~2° resolution, fast enough for optimisation
    N_OUT = 180
    hull_angs = [i / N_OUT * 2.0 * math.pi for i in range(N_OUT + 1)]
    hull_cos  = [math.cos(a) for a in hull_angs]
    hull_sin  = [math.sin(a) for a in hull_angs]

    max_dcr = 0.0

    for d in demands_si:
        Pd = float(d.get('P', 0.0))   # engine-sign: + = compression

        # Un-swap to TABLE convention (Mx=M33, My=M22) to match pmmUpdateDCRFromBoundary
        dx_r = float(d.get('My', 0.0))   # engine My (M33) → table Mx → ray x
        dy_r = float(d.get('Mx', 0.0))   # engine Mx (M22) → table My → ray y
        Md   = math.sqrt(dx_r * dx_r + dy_r * dy_r)

        if Md < 1e-9:
            # Pure-axial demand
            if global_Pmax > 1e-9 and Pd >= 0:
                dcr = Pd / global_Pmax
            elif global_Pmin < -1e-9 and Pd < 0:
                dcr = abs(Pd / global_Pmin)
            else:
                dcr = 0.0
            max_dcr = max(max_dcr, dcr)
            continue

        if Pd > global_Pmax + 1e-6 or Pd < global_Pmin - 1e-6:
            max_dcr = max(max_dcr, 999.0)
            continue

        # ── Step 1: interpolate (Mx, My) at Pd for each meridian ─────────────
        raw_mx = [0.0] * nAlpha
        raw_my = [0.0] * nAlpha

        for a in range(nAlpha):
            base = a * nPts
            mP  = allP [base:base + nPts]
            mMx = allMx[base:base + nPts]
            mMy = allMy[base:base + nPts]
            mP_max = max(mP)
            mP_min = min(mP)

            if Pd > mP_max + 1e-6:
                continue   # stays (0, 0)

            mx, my, bestM2 = 0.0, 0.0, -1.0
            for j in range(nPts - 1):
                p1, p2 = mP[j], mP[j + 1]
                dp = p2 - p1
                if abs(dp) < 1e-12:
                    continue
                t = (Pd - p1) / dp
                if t < -1e-9 or t > 1.0 + 1e-9:
                    continue
                tc  = max(0.0, min(1.0, t))
                cMx = mMx[j] + tc * (mMx[j + 1] - mMx[j])
                cMy = mMy[j] + tc * (mMy[j + 1] - mMy[j])
                M2  = cMx * cMx + cMy * cMy
                if M2 > bestM2:
                    bestM2 = M2; mx = cMx; my = cMy

            if bestM2 < 0:
                if Pd <= mP_min:
                    mx, my = mMx[0], mMy[0]
                # else stays (0, 0)

            raw_mx[a] = mx
            raw_my[a] = my

        # ── Step 2: 4-fold symmetric cloud ───────────────────────────────────
        cloud_x = []
        cloud_y = []
        for a in range(nAlpha):
            mx, my = raw_mx[a], raw_my[a]
            cloud_x.extend([ mx,  mx, -mx, -mx])
            cloud_y.extend([ my, -my,  my, -my])

        N_cloud = len(cloud_x)

        # ── Step 3: support-function convex hull (N_OUT directions) ──────────
        if _use_np:
            cx = _np.array(cloud_x)
            cy = _np.array(cloud_y)
            hc = _np.array(hull_cos)
            hs = _np.array(hull_sin)
            # dots: (N_OUT+1, N_cloud) → argmax per row
            dots  = _np.outer(hc, cx) + _np.outer(hs, cy)
            bidx  = _np.argmax(dots, axis=1)
            bnd_x = cx[bidx].tolist()
            bnd_y = cy[bidx].tolist()
        else:
            bnd_x = []
            bnd_y = []
            for i in range(N_OUT + 1):
                dc, ds = hull_cos[i], hull_sin[i]
                best_dot = -1e30; bx = 0.0; by = 0.0
                for j in range(N_cloud):
                    dot = dc * cloud_x[j] + ds * cloud_y[j]
                    if dot > best_dot:
                        best_dot = dot; bx = cloud_x[j]; by = cloud_y[j]
                bnd_x.append(bx)
                bnd_y.append(by)

        # ── Step 4: ray from origin → (dx_r, dy_r), intersect boundary ───────
        # For ray (t·dx_r, t·dy_r) and segment A→B:
        #   det = dx_r·dBy − dy_r·dBx
        #   t   = (Ax·dBy − Ay·dBx) / det          (scale along ray)
        #   s   = (Ax·dy_r − Ay·dx_r) / det         (scale along segment)
        # Intersection when t > 0, 0 ≤ s ≤ 1.
        # DCR = 1/t  (since |cap| = t·|ray_dir| = t·Md, DCR = Md/(t·Md) = 1/t)
        if _use_np:
            bx_arr = _np.array(bnd_x[:-1]); by_arr = _np.array(bnd_y[:-1])
            bx_nxt = _np.array(bnd_x[1:]);  by_nxt = _np.array(bnd_y[1:])
            dBx = bx_nxt - bx_arr;          dBy = by_nxt - by_arr
            det = dx_r * dBy - dy_r * dBx
            nz  = _np.abs(det) > 1e-10
            t_arr = _np.where(nz, (bx_arr * dBy - by_arr * dBx) / _np.where(nz, det, 1.0), -1.0)
            s_arr = _np.where(nz, (bx_arr * dy_r - by_arr * dx_r) / _np.where(nz, det, 1.0), -1.0)
            valid = nz & (t_arr > 1e-9) & (s_arr >= -1e-9) & (s_arr <= 1.0 + 1e-9)
            if _np.any(valid):
                best_t = float(_np.min(_np.where(valid, t_arr, _np.inf)))
                dcr    = 1.0 / best_t if best_t > 1e-9 else 999.0
            else:
                dcr = 999.0
        else:
            best_t = math.inf
            n_seg  = len(bnd_x) - 1
            for i in range(n_seg):
                Ax, Ay = bnd_x[i],   bnd_y[i]
                Bx, By = bnd_x[i+1], bnd_y[i+1]
                dBx_s, dBy_s = Bx - Ax, By - Ay
                det = dx_r * dBy_s - dy_r * dBx_s
                if abs(det) < 1e-10:
                    continue
                t = (Ax * dBy_s - Ay * dBx_s) / det
                s = (Ax * dy_r  - Ay * dx_r ) / det
                if t > 1e-9 and -1e-9 <= s <= 1.0 + 1e-9 and t < best_t:
                    best_t = t
            dcr = (1.0 / best_t) if best_t < math.inf and best_t > 1e-9 else 999.0

        max_dcr = max(max_dcr, dcr)

    return max_dcr


def _run_pmm_opt(b_in, h_in, fc_ksi, fy_ksi, Es_ksi, cover_in,
                 nb, nh, bar_area_in2, include_phi, demands_si, *,
                 fast=False, ui_alpha=10.0, ui_npts=70):
    """
    Run PMM for optimisation.

    Uses _boundary_dcr_surface() — identical to the frontend pmmUpdateDCRFromBoundary
    (convex-hull ray-intersection on the 2-D slice at each demand's P level).
    This guarantees the optimizer's reported DCR matches the Check DCR table exactly
    after the user clicks "Apply to Section".

    Returns max DCR (float) or None on error.
    """
    # Coarse (bisection) pass: 30° × 20 pts  — ~10× faster, still monotone-correct
    # Fine  (verify)    pass: ui_alpha × ui_npts — full user-selected accuracy
    alpha_steps = 30.0 if fast else ui_alpha
    num_points  = 20   if fast else ui_npts
    try:
        corners = rect_coords(b_in, h_in)
        areas, positions = rect_bars_grid(b_in, h_in, cover_in, nb, nh, bar_area_in2)
        sec = PMMSection(
            corner_coords=corners, fc=fc_ksi, fy=fy_ksi, Es=Es_ksi,
            alpha_steps=alpha_steps, num_points=num_points,
            include_phi=include_phi,
            bar_areas=areas, bar_positions=positions,
        )
        result = compute_pmm(sec)
    except Exception as _exc:
        import traceback as _tb, datetime as _dt
        try:
            with open(_LOG_PATH, 'a', encoding='utf-8') as _lf:
                _lf.write(
                    f"[{_dt.datetime.now().isoformat()}] [_run_pmm_opt] compute_pmm failed "
                    f"b={b_in:.3f}in h={h_in:.3f}in fc={fc_ksi:.3f}ksi fy={fy_ksi:.3f}ksi "
                    f"nb={nb} nh={nh} bar_area={bar_area_in2:.4f}in2 "
                    f"alpha_steps={alpha_steps} num_points={num_points}\n"
                    f"  ERROR: {_exc}\n"
                    + ''.join(_tb.format_exc().splitlines(keepends=True)[:6])
                )
        except Exception:
            pass
        return None

    # Convert surface to SI (engine returns US units: kips, kip·in)
    try:
        surf = result.get('surface', {})
        surf['P']  = [v * _KIPS_TO_KN  for v in surf['P']]
        surf['Mx'] = [v * _KIN_TO_KNM  for v in surf['Mx']]
        surf['My'] = [v * _KIN_TO_KNM  for v in surf['My']]
        # Use the engine-reported pts/meridian (may differ from num_points due to
        # bar-transition clustering — mirrors frontend: surf.num_points || payload.num_points)
        npts_actual = surf.get('num_points', num_points)
        return _boundary_dcr_surface(surf, npts_actual, demands_si)
    except Exception as _exc:
        import traceback as _tb, datetime as _dt
        try:
            with open(_LOG_PATH, 'a', encoding='utf-8') as _lf:
                _lf.write(
                    f"[{_dt.datetime.now().isoformat()}] [_run_pmm_opt] _boundary_dcr_surface failed "
                    f"surf_keys={list(surf.keys())} npts={surf.get('num_points','?')} "
                    f"n_demands={len(demands_si)}\n"
                    f"  ERROR: {_exc}\n"
                    + ''.join(_tb.format_exc().splitlines(keepends=True)[:6])
                )
        except Exception:
            pass
        return None


# ── Bar-size sweep helper — ordered list from pmm_conventions ────────────────
_ALL_BAR_SIZES  = ["Ø8","Ø10","Ø12","Ø16","Ø20","Ø25","Ø28","Ø32","Ø36","Ø40"]
_BAR_AREA_MAP   = {
    "Ø8":  50.3,  "Ø10":  78.5,  "Ø12": 113.1,
    "Ø16": 201.1, "Ø20": 314.2,  "Ø25": 490.9,
    "Ø28": 615.8, "Ø32": 804.2,  "Ø36": 1017.9, "Ø40": 1256.6,
}
_BAR_DIA_MAP    = {
    "Ø8": 8.0,  "Ø10": 10.0, "Ø12": 12.0, "Ø16": 16.0, "Ø20": 20.0,
    "Ø25": 25.0,"Ø28": 28.0, "Ø32": 32.0, "Ø36": 36.0, "Ø40": 40.0,
}
_S_MAX_MM       = 150.0    # ACI §25.7.2.3 max clear (mm)


def _optimize_one_bar_size(
    bar_name: str,
    b_mm: float, h_mm: float, Ag_mm2: float,
    b_in: float, h_in: float,
    fc_ksi: float, fy_ksi: float, Es_ksi: float,
    body,          # PMMOptimizeRequest – cover, stirrup, phi, rho, resolution
    demands: list, # already in engine-sign SI, pre-swapped by caller
    target: float,
    RHO_MIN: float, RHO_MAX: float,
) -> Optional[dict]:
    """
    Run the bisection optimizer for ONE fixed bar size.

    Returns a response dict (same shape as pmm_optimize's return value) or
    None if no feasible candidates exist for this bar size.

    Cover fix (see pmm_optimize docstring):
        eff_cover = clear_cover + stirrup_dia + bar_dia / 2
    Bisection algorithm (O(log N) coarse evals + ±2 fine-verify window).
    """
    area_mm2 = _BAR_AREA_MAP.get(bar_name)
    if area_mm2 is None:
        return None

    db           = _db_mm(area_mm2)
    bar_dia_mm   = _BAR_DIA_MAP.get(bar_name, db)
    bar_area_in2 = area_mm2 * (_MM_TO_IN ** 2)

    eff_cover_mm = body.cover_mm + body.stirrup_dia_mm + bar_dia_mm / 2.0
    cover_in     = eff_cover_mm * _MM_TO_IN
    s_min_req    = round(max(1.5 * db, 40.0), 1)   # ACI §25.8.1

    # ── Dynamic nb / nh range ────────────────────────────────────────────────
    S_MIN  = max(1.5 * db, 40.0)
    cc_min = db + S_MIN
    net_b  = b_mm - 2.0 * eff_cover_mm
    net_h  = h_mm - 2.0 * eff_cover_mm

    max_nb = min(12, max(2, int(net_b / cc_min) + 1))
    min_nb = max(2, math.ceil(net_b / (db + _S_MAX_MM) + 1 - 1e-9))
    max_nh = min(8,  max(0, int(net_h / cc_min) - 1))
    min_nh = max(0,  math.ceil(net_h / (_S_MAX_MM + db) - 1 - 1e-9))

    # ── Build candidate list ─────────────────────────────────────────────────
    candidates: List[dict] = []
    for nb in range(min_nb, max_nb + 1):
        for nh in range(min_nh, max_nh + 1):
            n_total = 2 * nb + 2 * nh
            ast  = n_total * area_mm2
            rho  = ast / Ag_mm2
            if not (RHO_MIN <= rho <= RHO_MAX):
                continue
            ok, min_c, max_c = _check_spacing_aci(
                b_mm, h_mm, eff_cover_mm, nb, nh, db)
            if not ok:
                continue
            candidates.append({
                'ast': ast, 'rho': rho,
                'nb': nb, 'nh': nh, 'n_total': n_total,
                'min_c': min_c, 'max_c': max_c,
                'dcr_coarse': None, 'dcr_fine': None,
            })

    if not candidates:
        return None

    candidates.sort(key=lambda x: x['ast'])

    def _eval(idx: int, fast: bool) -> float:
        cand = candidates[idx]
        key  = 'dcr_coarse' if fast else 'dcr_fine'
        if cand[key] is not None:
            return cand[key]
        dcr = _run_pmm_opt(b_in, h_in, fc_ksi, fy_ksi, Es_ksi,
                           cover_in, cand['nb'], cand['nh'], bar_area_in2,
                           body.include_phi, demands, fast=fast,
                           ui_alpha=body.alpha_steps, ui_npts=body.num_points)
        cand[key] = dcr if dcr is not None else 999.0
        return cand[key]

    # Phase 1: Bisection (O(log N) coarse evals)
    n      = len(candidates)
    dcr_lo = _eval(0,     fast=True)
    dcr_hi = _eval(n - 1, fast=True)

    bisect_winner = None
    if dcr_lo <= target:
        bisect_winner = 0
    elif dcr_hi <= target:
        lo, hi = 0, n - 1
        while hi - lo > 1:
            mid     = (lo + hi) // 2
            dcr_mid = _eval(mid, fast=True)
            if dcr_mid <= target:
                hi = mid
            else:
                lo = mid
        bisect_winner = hi

    # Phase 2: Fine-verify ±2 neighbourhood
    if bisect_winner is not None:
        lo_idx = max(0, bisect_winner - 2)
        hi_idx = min(n - 1, bisect_winner + 2)
        for idx in range(lo_idx, hi_idx + 1):
            _eval(idx, fast=False)
    else:
        _eval(n - 1, fast=False)

    # Pick best passing candidate (lightest Ast)
    best_valid = None
    for cand in candidates:
        fine = cand.get('dcr_fine')
        if fine is None or fine > target:
            continue
        if (best_valid is None
                or cand['ast'] < best_valid['ast']
                or (cand['ast'] == best_valid['ast']
                    and fine > best_valid['dcr_fine'])):
            best_valid = cand

    target_met = best_valid is not None
    best       = best_valid if target_met else candidates[-1]
    final_dcr  = best.get('dcr_fine') or best.get('dcr_coarse') or 0.0

    nb     = best['nb']
    nh     = best['nh']
    ntotal = best['n_total']
    arrangement = (f"{nb} bars/b-face · {ntotal} total" if nh == 0
                   else f"{nb}b + {nh}h bars/face · {ntotal} total")

    return {
        'b_mm':          round(b_mm),
        'h_mm':          round(h_mm),
        'nbars_b':       nb,
        'nbars_h':       nh,
        'n_total':       ntotal,
        'arrangement':   arrangement,
        'bar_size':      bar_name,
        'rho_pct':       round(best['rho'] * 100, 2),
        'achieved_dcr':  round(final_dcr * 100, 1),
        'target_met':    target_met,
        'min_clear_mm':  best['min_c'],
        'max_clear_mm':  best['max_c'],
        'min_clear_req': s_min_req,
        'max_clear_req': _S_MAX_MM,
        '_ast_mm2':      best['ast'],    # internal — used by sweep to pick global best
    }


@app.post("/api/pmm/optimize")
def pmm_optimize(body: PMMOptimizeRequest,
                 current_user: dict = Depends(get_current_user)):
    """
    Bayesian-Bisection optimiser for bar count / arrangement.

    Cover fix
    ─────────
    The UI inputs *clear cover* (face → stirrup face).  The PMM engine needs
    the distance from the concrete face to the *bar centre*:
        eff_cover = clear_cover + stirrup_dia + bar_dia / 2   (ACI convention)
    This matches what pmm_calculate does and ensures the optimizer DCR matches
    the displayed surface after Apply.

    Bisection algorithm (O(log N) evaluations)
    ──────────────────────────────────────────
    DCR is monotonically non-increasing with Ast (more steel → higher capacity
    → lower DCR).  A binary bisection on the sorted candidate list finds the
    lightest passing arrangement in ceil(log2 N) coarse evaluations — typically
    6-8 evals regardless of search space size — rather than scanning all N.

    After bisection, a ±2 neighbourhood around the transition point is
    fine-verified to produce accurate final DCR values.

    Bar size sweep (sweep_bar_sizes=True)
    ──────────────────────────────────────
    Runs the bisection for every bar size in bar_size_candidates (or all Ø8→Ø40)
    and returns the globally lightest passing arrangement measured by total
    steel area (Ast = n_total × area_per_bar).  This finds both the optimal
    diameter and bar count in a single call.

    ACI 318-19 spacing:
      §25.8.1   min clear = max(1.5·db, 40 mm)
      §25.7.2.3 max clear = 150 mm (all faces)
      §10.6.1.1 ρ_min = 1 %,  ρ_max = 8 %
    """
    if not _HAS_PMM:
        raise HTTPException(status_code=503, detail="PMM engine not available.")
    if not body.demands:
        raise HTTPException(status_code=422, detail="At least one demand required.")

    target  = float(body.target_dcr)
    RHO_MIN = max(0.01, min(float(body.min_rho_pct) / 100.0, 0.08))
    RHO_MAX = min(float(body.max_rho_pct) / 100.0, 0.08)
    if RHO_MIN > RHO_MAX:
        raise HTTPException(status_code=422, detail="Min ρ must be less than Max ρ.")

    b_mm   = float(body.b_mm)
    h_mm   = float(body.h_mm)
    Ag_mm2 = b_mm * h_mm
    b_in   = b_mm * _MM_TO_IN
    h_in   = h_mm * _MM_TO_IN
    fc_ksi = body.fc_mpa * _MPA_TO_KSI
    fy_ksi = body.fy_mpa * _MPA_TO_KSI
    Es_ksi = body.Es_mpa * _MPA_TO_KSI

    demands = [{'label': d.get('label', ''), 'P': float(d.get('P', 0)),
                'Mx': float(d.get('Mx', 0)), 'My': float(d.get('My', 0))}
               for d in body.demands]

    # ── Single bar size mode (default) ───────────────────────────────────────
    if not body.sweep_bar_sizes:
        bar_name = body.bar_size
        if bar_name not in _BAR_AREA_MAP:
            raise HTTPException(status_code=422,
                                detail=f"Unknown bar size '{bar_name}'.")
        result = _optimize_one_bar_size(
            bar_name, b_mm, h_mm, Ag_mm2, b_in, h_in,
            fc_ksi, fy_ksi, Es_ksi, body, demands, target, RHO_MIN, RHO_MAX)
        if result is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No feasible arrangement found within ρ [{round(RHO_MIN*100,1)} – "
                    f"{round(RHO_MAX*100,1)} %] and ACI 318-19 spacing limits. "
                    "Try a different bar size or adjust ρ / cover."
                ))
        result.pop('_ast_mm2', None)
        return result

    # ── Bar size sweep mode ───────────────────────────────────────────────────
    # Try every requested bar size; return the globally lightest passing
    # arrangement (lowest total Ast among those meeting target DCR).
    sizes_to_try = [s for s in (body.bar_size_candidates or _ALL_BAR_SIZES)
                    if s in _BAR_AREA_MAP]
    if not sizes_to_try:
        raise HTTPException(status_code=422, detail="No valid bar sizes specified.")

    results_by_size: dict = {}
    for bar_name in sizes_to_try:
        res = _optimize_one_bar_size(
            bar_name, b_mm, h_mm, Ag_mm2, b_in, h_in,
            fc_ksi, fy_ksi, Es_ksi, body, demands, target, RHO_MIN, RHO_MAX)
        if res is not None:
            results_by_size[bar_name] = res

    if not results_by_size:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No feasible arrangement found for any bar size within ρ "
                f"[{round(RHO_MIN*100,1)} – {round(RHO_MAX*100,1)} %] and "
                "ACI 318-19 spacing limits. Adjust ρ / cover or increase section size."
            ))

    # Passing candidates: those where target was met
    passing = {k: v for k, v in results_by_size.items() if v['target_met']}

    if passing:
        # Pick lightest total Ast among passing results
        winner_name = min(passing, key=lambda k: passing[k]['_ast_mm2'])
        winner = passing[winner_name]
    else:
        # No size meets target — return the one with lowest achieved DCR (closest to target)
        winner_name = min(results_by_size,
                          key=lambda k: results_by_size[k]['achieved_dcr'])
        winner = results_by_size[winner_name]

    winner.pop('_ast_mm2', None)
    winner['swept_sizes']  = len(sizes_to_try)
    winner['sizes_tried']  = list(results_by_size.keys())
    return winner


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
            # Normalise field names so the frontend picker always gets consistent keys.
            # actions.get_rc_column_sections() uses prop_name/width/depth/cover but the
            # PMM UI expects name/b_mm/h_mm/cover_mm — add aliases for both.
            entry["name"]     = entry.get("prop_name") or entry.get("name") or ""
            entry["b_mm"]     = entry.get("width")     or entry.get("b_mm")
            entry["h_mm"]     = entry.get("depth")     or entry.get("h_mm")
            entry["cover_mm"] = entry.get("cover")     or entry.get("cover_mm")
            # nbars aliases: ETABS actions returns nbars_3 (bars per top/bottom face,
            # width direction) and nbars_2 (bars per side face, height direction).
            # Map to PMM field names nbars_b / nbars_h, and compute display total.
            # ETABS GetRebarColumn actual field order (confirmed from diagnostic log):
            #   vals[6] = NumBars Along 3-dir Face = bars per TOP/BOTTOM face (b-direction)
            #             face perpendicular to axis-3, spans the b=width dimension
            #             → includes the 2 corner bars
            #   vals[7] = NumBars Along 2-dir Face = bars per SIDE face (h-direction)
            #             face perpendicular to axis-2, spans the h=depth dimension
            #             → includes the 2 corner bars
            #
            # PMM engine convention:
            #   nbars_b = top/bottom face bars INCLUDING corners (≥2)  → vals[6]
            #   nbars_h = side face bars EXCLUDING corners (≥0)        → vals[7] - 2
            nb2 = int(entry.get("nbars_2") or 0)   # vals[6] = Along 3-dir face = top/bot (b)
            nb3 = int(entry.get("nbars_3") or 0)   # vals[7] = Along 2-dir face = side  (h)
            entry["nbars_b"] = nb2                        # PMM: top/bottom bars incl. corners
            entry["nbars_h"] = max(0, nb3 - 2)           # PMM: side intermediate bars only
            if entry.get("nbars") is None and nb2:
                # PMM total = 2*nbars_b + 2*nbars_h
                entry["nbars"] = 2 * nb2 + 2 * max(0, nb3 - 2)
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


class ETABSSectionForcesRequest(BaseModel):
    section_name: str
    combo_names: List[str]
    load_type: str = "combo"


@app.post("/api/pmm/etabs-section-forces")
def pmm_etabs_section_forces(body: ETABSSectionForcesRequest,
                              current_user: dict = Depends(get_current_user)):
    """
    Get frame forces for all columns with a specific section name.
    Returns {results: [{label, P_kN, M3_kNm, M2_kNm}, ...]}
    """
    helper = getattr(actions, "get_etabs_all_column_forces", None)
    if not callable(helper):
        raise HTTPException(status_code=503, detail="get_etabs_all_column_forces not available.")
    all_forces = helper([body.section_name], body.combo_names, body.load_type)
    if "error" in all_forces:
        raise HTTPException(status_code=503, detail=all_forces["error"])
    by_section = all_forces.get("sections", all_forces)  # support both return shapes
    # Try exact match first, then case-insensitive
    rows = by_section.get(body.section_name, [])
    if not rows:
        for key, val in by_section.items():
            if key.lower() == body.section_name.lower():
                rows = val
                break
    results = [
        {"frame":    r["frame"],
         "story":    r.get("story", "?"),
         "combo":    r["combo"],
         "location": r.get("location", ""),
         "P_kN":     r["P_kN"],
         "M3_kNm":   r["M3_kNm"],   # ETABS M33 = user Mx (strong axis)
         "M2_kNm":   r["M2_kNm"]}   # ETABS M22 = user My (weak axis)
        for r in rows
    ]
    return {"results": results}


class ETABSBatchCheckRequest(BaseModel):
    combo_names: list
    load_type: str = "combo"   # "combo" or "case"


def _normalize_bar_size(raw: str) -> str:
    """Map ETABS rebar name to PMM Ø-format key.  e.g. 'D20', '#6', 'R20', 'PHIB20', '32' → 'Ø32'."""
    import re as _re
    _known_dia = [8, 10, 12, 16, 20, 25, 28, 32, 36, 40]

    def _snap(n: int) -> str:
        nearest = min(_known_dia, key=lambda d: abs(d - n))
        return "\u00d8" + str(nearest)  # Ø = U+00D8

    if not raw:
        return "\u00d820"
    s = raw.strip()
    # Already in Ø/ø format  e.g. "Ø20"
    if s[0] in ("\u00d8", "\u00f8"):  # Ø or ø
        rest = s[1:].split()[0]
        nums = _re.findall(r'\d+', rest)
        return _snap(int(nums[0])) if nums else "\u00d820"
    su = s.upper()
    # D-prefix: D16, D20, D-20 …
    m = _re.match(r'^D-?(\d+)', su)
    if m:
        return _snap(int(m.group(1)))
    # R-prefix: R16, R20 …
    m = _re.match(r'^R(\d+)$', su)
    if m:
        return _snap(int(m.group(1)))
    # T-prefix (British): T16, T20 …
    m = _re.match(r'^T(\d+)$', su)
    if m:
        return _snap(int(m.group(1)))
    # US designation #N
    _us_map = {"#3": 10, "#4": 13, "#5": 16, "#6": 19, "#7": 22,
               "#8": 25, "#9": 29, "#10": 32, "#11": 36}
    if s in _us_map:
        return _snap(_us_map[s])
    # Generic fallback: extract all digit runs, pick the one closest to a known diameter
    # Handles "PHIB20" → 20, "SD390D25" → 25, "32" → 32, etc.
    nums = _re.findall(r'\d+', s)
    if nums:
        candidates = [int(n) for n in nums if int(n) >= 6]
        if candidates:
            best = min(candidates, key=lambda n: min(abs(n - d) for d in _known_dia))
            return _snap(best)
    return "\u00d820"  # last-resort fallback


@app.get("/api/pmm/etabs-batch-diag")
def pmm_etabs_batch_diag():
    """
    Diagnostic: returns all frames with their section assignments so we can
    verify that section names match between PropFrame.GetNameList and
    FrameObj.GetSection.
    """
    try:
        SapModel = actions.get_active_etabs()
    except Exception:
        SapModel = None
    if not SapModel:
        raise HTTPException(503, "ETABS is not running.")

    try:
        unit_code = 6
        try:    unit_code = SapModel.GetDatabaseUnits()
        except Exception:
            try: unit_code = SapModel.GetPresentUnits()
            except Exception: pass

        # All frame section definitions
        sec_ret = SapModel.PropFrame.GetNameList()
        all_sec_names = list(sec_ret[1]) if sec_ret and int(sec_ret[-1]) == 0 else []

        # All frame objects + their section assignments
        fr_ret = SapModel.FrameObj.GetNameList()
        all_frames = list(fr_ret[1]) if fr_ret and int(fr_ret[-1]) == 0 else []

        frame_sample = []   # first 30 frames with section info
        errors = []
        for frame in all_frames[:50]:
            try:
                s = SapModel.FrameObj.GetSection(str(frame))
                frame_sample.append({
                    "frame": frame,
                    "raw_return": str(s),
                    "sec_ret0": str(s[0]) if s else None,
                    "retcode": int(s[-1]) if s else None,
                })
            except Exception as e:
                errors.append({"frame": frame, "error": str(e)})

        # Try FrameForce on the first frame to check if analysis results exist
        force_test = {"frame": None, "retcode": None, "n_results": None,
                      "error": None, "raw": None}
        if all_frames:
            test_frame = str(all_frames[0])
            force_test["frame"] = test_frame
            try:
                # Setup: select all combos for output
                SapModel.Results.Setup.DeselectAllCasesAndCombosForOutput()
                try:
                    c_ret = SapModel.RespCombo.GetNameList()
                    if c_ret and int(c_ret[-1]) == 0 and c_ret[1]:
                        for cn in list(c_ret[1])[:3]:   # first 3 combos only
                            SapModel.Results.Setup.SetComboSelectedForOutput(cn, True)
                except Exception:
                    pass
                ret = SapModel.Results.FrameForce(test_frame, 0)
                force_test["raw"] = str(ret)[:300]
                force_test["retcode"] = int(ret[-1]) if ret else None
                force_test["n_results"] = int(ret[0]) if ret else None
            except Exception as e:
                force_test["error"] = str(e)

        return {
            "unit_code": unit_code,
            "n_section_defs": len(all_sec_names),
            "section_defs_sample": all_sec_names[:20],
            "n_frames": len(all_frames),
            "frame_section_sample": frame_sample[:30],
            "force_test": force_test,
            "errors": errors[:10],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


def _perframe_envelope(rows: list) -> list:
    """
    Reduce a flat list of force rows to the 7 worst-case demands per
    (frame, location) pair, keeping:
        max/min P,  max/min M33,  max/min M22,  max √(M33²+M22²)
    ~15× demand reduction on typical models.
    """
    import math as _m
    buckets: dict = {}
    for r in rows:
        key = (r.get("frame"), r.get("location", ""))
        P   = float(r.get("P_kN",   0))
        M3  = float(r.get("M3_kNm", 0))
        M2  = float(r.get("M2_kNm", 0))
        Mv  = _m.sqrt(M3 * M3 + M2 * M2)
        if key not in buckets:
            buckets[key] = {
                "maxP":  (P,  r), "minP":  (P,  r),
                "maxM3": (M3, r), "minM3": (M3, r),
                "maxM2": (M2, r), "minM2": (M2, r),
                "maxMv": (Mv, r),
            }
        else:
            b = buckets[key]
            if P  > b["maxP"][0]:  b["maxP"]  = (P,  r)
            if P  < b["minP"][0]:  b["minP"]  = (P,  r)
            if M3 > b["maxM3"][0]: b["maxM3"] = (M3, r)
            if M3 < b["minM3"][0]: b["minM3"] = (M3, r)
            if M2 > b["maxM2"][0]: b["maxM2"] = (M2, r)
            if M2 < b["minM2"][0]: b["minM2"] = (M2, r)
            if Mv > b["maxMv"][0]: b["maxMv"] = (Mv, r)
    seen: set = set()
    out: list = []
    for b in buckets.values():
        for _, r in b.values():
            rid = id(r)
            if rid not in seen:
                seen.add(rid); out.append(r)
    return out


def _alpha_data_boundary_dcr(alpha_data: dict, Pmax_kN: float,
                              Pmin_kN: float, demands: list) -> list:
    """
    Boundary ray-intersection DCR — mirrors the frontend pmmUpdateDCRFromBoundary.
    Replaces check_demands in the batch endpoint for accuracy on biaxial sections.
    Returns a list of dicts with keys: DCR, M_demand, M_cap, status.
    """
    import math as _m
    allP: list = []; allMx: list = []; allMy: list = []
    for adeg in sorted(alpha_data.keys(), key=float):
        c = alpha_data[adeg]
        allP.extend(c["P"]); allMx.extend(c["Mx"]); allMy.extend(c["My"])
    nAlpha = len(alpha_data)
    nPts   = len(allP) // nAlpha if nAlpha else 1
    gPmax  = max(allP); gPmin = min(allP)
    N = 360
    hcos = [_m.cos(i / N * 2 * _m.pi) for i in range(N + 1)]
    hsin = [_m.sin(i / N * 2 * _m.pi) for i in range(N + 1)]

    results = []
    for d in demands:
        Pd = float(d["P"]); dx = float(d["Mx"]); dy = float(d["My"])
        Md = _m.sqrt(dx * dx + dy * dy)
        if Md < 1e-9:
            dcr = (Pd / Pmax_kN if Pmax_kN > 1e-9 and Pd >= 0
                   else abs(Pd / Pmin_kN) if Pmin_kN < -1e-9 else 0.0)
            results.append({"DCR": round(dcr, 3), "M_demand": 0.0, "M_cap": None,
                             "status": "PASS" if dcr <= 1.0 else "FAIL"})
            continue
        if Pd > gPmax + 1e-6 or Pd < gPmin - 1e-6:
            results.append({"DCR": 999.0, "M_demand": round(Md, 3),
                             "M_cap": 0.0, "status": "FAIL"})
            continue

        raw_mx = [0.0] * nAlpha; raw_my = [0.0] * nAlpha
        for a in range(nAlpha):
            base = a * nPts
            mP = allP[base:base + nPts]
            mMx = allMx[base:base + nPts]
            mMy = allMy[base:base + nPts]
            if Pd > max(mP) + 1e-6:
                continue
            bx = by = bM = -1.0
            for j in range(nPts - 1):
                p1, p2 = mP[j], mP[j + 1]; dp = p2 - p1
                if abs(dp) < 1e-12: continue
                t = (Pd - p1) / dp
                if t < -1e-9 or t > 1 + 1e-9: continue
                tc = max(0.0, min(1.0, t))
                cx2 = mMx[j] + tc * (mMx[j + 1] - mMx[j])
                cy2 = mMy[j] + tc * (mMy[j + 1] - mMy[j])
                M2 = cx2 * cx2 + cy2 * cy2
                if M2 > bM: bM = M2; bx = cx2; by = cy2
            if bM < 0:
                bx, by = (mMx[0], mMy[0]) if Pd <= min(mP) else (0.0, 0.0)
            raw_mx[a] = bx; raw_my[a] = by

        cx_c: list = []; cy_c: list = []
        for a in range(nAlpha):
            mx, my = raw_mx[a], raw_my[a]
            cx_c += [mx, mx, -mx, -mx]; cy_c += [my, -my, my, -my]

        bx_h: list = []; by_h: list = []
        for i in range(N + 1):
            dc, ds = hcos[i], hsin[i]; bd = -1e30; bxv = byv = 0.0
            for j in range(len(cx_c)):
                dot = dc * cx_c[j] + ds * cy_c[j]
                if dot > bd: bd = dot; bxv = cx_c[j]; byv = cy_c[j]
            bx_h.append(bxv); by_h.append(byv)

        best_t = float("inf")
        for i in range(N):
            Ax, Ay = bx_h[i], by_h[i]; Bx, By = bx_h[i + 1], by_h[i + 1]
            dBx, dBy = Bx - Ax, By - Ay; det = dx * dBy - dy * dBx
            if abs(det) < 1e-10: continue
            t = (Ax * dBy - Ay * dBx) / det; s = (Ax * dy - Ay * dx) / det
            if t > 1e-9 and -1e-9 <= s <= 1 + 1e-9 and t < best_t:
                best_t = t

        if best_t == float("inf"):
            results.append({"DCR": 999.0, "M_demand": round(Md, 3),
                             "M_cap": 0.0, "status": "FAIL"})
            continue
        dcr = 1.0 / best_t if best_t > 1e-9 else 999.0
        results.append({"DCR": round(dcr, 3), "M_demand": round(Md, 3),
                         "M_cap": round(best_t * Md, 3),
                         "status": "PASS" if dcr <= 1.0 else "FAIL"})
    return results


@app.post("/api/pmm/etabs-batch-check")
def pmm_etabs_batch_check(body: ETABSBatchCheckRequest,
                           current_user: dict = Depends(get_current_user)):
    """
    Batch PMM DCR check for every RC column section in the ETABS model.
    For each unique section, computes the full P-M-M interaction surface and
    evaluates DCR for every column frame × load combination.

    Returns {columns: [{section, b_mm, h_mm, rho_pct, phi_Pn_max_kN,
                         n_frames, n_checks, n_fail, max_dcr, status,
                         worst: {frame, combo, P_kN, Mx_kNm, My_kNm, M_demand, M_cap}}]}
    """
    if not _HAS_PMM:
        raise HTTPException(503, "PMM engine not available.")

    if not body.combo_names:
        raise HTTPException(400, "combo_names must not be empty.")

    _t_start = _time.perf_counter()

    # ── 1. Get all PMM column sections ────────────────────────────────────────
    helper_sec = getattr(actions, "get_pmm_column_sections", None)
    sec_result = helper_sec() if callable(helper_sec) else _get_pmm_column_sections_fallback()
    if "error" in sec_result:
        raise HTTPException(503, sec_result["error"])
    sections = sec_result.get("sections", [])
    if not sections:
        raise HTTPException(404, "No RC column sections found in ETABS model.")

    # ── 2. Get forces for all column frames ───────────────────────────────────
    helper_forces = getattr(actions, "get_etabs_all_column_forces", None)
    if not callable(helper_forces):
        raise HTTPException(503, "get_etabs_all_column_forces not available in actions.py.")

    sec_names = [s.get("prop_name") or s.get("name", "") for s in sections]
    forces_result = helper_forces(sec_names, body.combo_names, body.load_type)
    if "error" in forces_result:
        raise HTTPException(503, forces_result["error"])
    by_section: dict = forces_result.get("sections", {})

    # Cache forces + section metadata for the rebar optimizer (no ETABS calls needed there)
    _last_batch_forces.update({
        "by_section": by_section,
        "sections": sections,
        "combo_names": body.combo_names,
    })

    _t_forces = _time.perf_counter()
    try:
        with open(_LOG_PATH, 'a', encoding='utf-8') as _lf:
            _lf.write(f"[batch] forces fetch: {_t_forces - _t_start:.2f}s\n")
    except Exception:
        pass

    # ── 3. PMM check per section ──────────────────────────────────────────────
    _REBAR_DIA_MM = {
        "Ø8": 8.0,  "Ø10": 10.0, "Ø12": 12.0, "Ø16": 16.0, "Ø20": 20.0,
        "Ø25": 25.0,"Ø28": 28.0, "Ø32": 32.0, "Ø36": 36.0, "Ø40": 40.0,
    }

    # ── 3a. Pre-compute all PMM surfaces in parallel ──────────────────────────
    # Each section's interaction surface is independent — parallelise using threads.
    # Numba-JIT code releases the GIL so threads genuinely run in parallel.
    def _build_surface(sec):
        sec_name = sec.get("prop_name") or sec.get("name", "?")
        b_mm  = float(sec.get("width") or sec.get("b_mm") or 400)
        h_mm  = float(sec.get("depth") or sec.get("h_mm") or 400)
        fc    = float(sec.get("fc_mpa")  or 28.0)
        fy    = float(sec.get("fy_mpa")  or 420.0)
        Es    = float(sec.get("Es_mpa")  or 200000.0)
        cover = float(sec.get("cover_mm") or sec.get("cover") or 40.0)
        stirrup_dia = 10.0
        nbars_b = max(2, int(sec.get("nbars_b") or sec.get("nbars_2") or 3))
        _nb3_raw = int(sec.get("nbars_3") or 3)
        nbars_h = max(0, int(sec["nbars_h"]) if sec.get("nbars_h") is not None
                       else _nb3_raw - 2)
        raw_bar = str(sec.get("rebar_size") or "Ø20")
        bar_key = _normalize_bar_size(raw_bar)

        # Cache hit — skip recomputation
        _cache_key = (sec_name, b_mm, h_mm, fc, fy, nbars_b, nbars_h, bar_key)
        if _cache_key in _PMM_SURFACE_CACHE:
            return sec_name, _PMM_SURFACE_CACHE[_cache_key]

        try:
            bar_area_mm2 = REBAR_TABLE_SI.get(bar_key) or REBAR_TABLE_SI["Ø20"]
            bar_dia_mm   = _REBAR_DIA_MM.get(bar_key, 20.0)
            eff_cover    = cover + stirrup_dia + bar_dia_mm / 2.0
            b_in   = b_mm  * _MM_TO_IN;  h_in   = h_mm  * _MM_TO_IN
            fc_ksi = fc    * _MPA_TO_KSI; fy_ksi = fy    * _MPA_TO_KSI
            Es_ksi = Es    * _MPA_TO_KSI; cov_in = eff_cover * _MM_TO_IN
            ba_in2 = bar_area_mm2 * (_MM_TO_IN ** 2)
            corners   = rect_coords(b_in, h_in)
            areas, positions = rect_bars_grid(b_in, h_in, cov_in, nbars_b, nbars_h, ba_in2)
            sec_obj = PMMSection(
                corner_coords=corners, fc=fc_ksi, fy=fy_ksi, Es=Es_ksi,
                alpha_steps=30, num_points=30, include_phi=True,
                bar_areas=areas, bar_positions=positions,
            )
            pmm_raw = compute_pmm(sec_obj)
            alpha_data = pmm_raw.get("alpha_data", {})
            for curve in alpha_data.values():
                curve["P"]  = [v * _KIPS_TO_KN  for v in curve["P"]]
                curve["Mx"] = [v * _KIN_TO_KNM for v in curve["Mx"]]
                curve["My"] = [v * _KIN_TO_KNM for v in curve["My"]]
            result = {
                "alpha_data": alpha_data,
                "Pmax_kN": pmm_raw["Pmax"] * _KIPS_TO_KN,
                "Pmin_kN": pmm_raw["Pmin"] * _KIPS_TO_KN,
                "Ag_mm2":  pmm_raw["Ag"]   * _IN2_TO_MM2,
                "Ast_mm2": pmm_raw["Ast"]  * _IN2_TO_MM2,
                "b_mm": b_mm, "h_mm": h_mm, "bar_key": bar_key,
                "nbars_b": nbars_b, "nbars_h": nbars_h, "raw_bar": raw_bar,
                "_nb3_raw": _nb3_raw,
            }
            _PMM_SURFACE_CACHE[_cache_key] = result
            return sec_name, result
        except Exception as exc:
            return sec_name, {"error": str(exc)}

    _n_workers = min(len(sections), 8)
    _surfaces: dict = {}
    try:
        with _TPE(max_workers=_n_workers) as _pool:
            for _sname, _surf in _pool.map(_build_surface, sections):
                _surfaces[_sname] = _surf
    except Exception:
        # Fallback: sequential
        for _sec in sections:
            _sname, _surf = _build_surface(_sec)
            _surfaces[_sname] = _surf

    _t_surfaces = _time.perf_counter()
    try:
        with open(_LOG_PATH, 'a', encoding='utf-8') as _lf:
            _lf.write(f"[batch] PMM surfaces ({len(sections)} sections, {_n_workers} workers): "
                      f"{_t_surfaces - _t_forces:.2f}s\n")
    except Exception:
        pass

    results = []
    story_summary: dict = {}   # story -> aggregated stats
    frame_dcr: dict = {}       # frame_name -> max DCR across all sections/combos
    frame_dims: dict = {}      # frame_name -> {"b_m": float, "h_m": float}
    frame_axial: dict = {}              # frame_name -> max |P_kN| across all combos
    frame_axial_by_combo: dict = {}    # combo -> {frame_name -> max |P_kN|}

    for sec in sections:
        sec_name = sec.get("prop_name") or sec.get("name", "?")
        rows = by_section.get(sec_name, [])

        # ── Retrieve pre-computed surface (built in parallel above) ──────────
        _surf = _surfaces.get(sec_name, {})
        if "error" in _surf:
            results.append({"section": sec_name, "error": _surf["error"]})
            continue

        b_mm    = _surf.get("b_mm", float(sec.get("width") or sec.get("b_mm") or 400))
        h_mm    = _surf.get("h_mm", float(sec.get("depth") or sec.get("h_mm") or 400))
        bar_key = _surf.get("bar_key", "Ø20")
        raw_bar = _surf.get("raw_bar", bar_key)
        nbars_b = _surf.get("nbars_b", 3)
        nbars_h = _surf.get("nbars_h", 1)
        _nb3_raw = _surf.get("_nb3_raw", 3)
        alpha_data = _surf.get("alpha_data", {})
        Pmax_kN    = _surf.get("Pmax_kN", 0.0)
        Pmin_kN    = _surf.get("Pmin_kN", 0.0)
        Ag_mm2     = _surf.get("Ag_mm2", b_mm * h_mm)
        Ast_mm2    = _surf.get("Ast_mm2", 0.0)
        rho_pct    = Ast_mm2 / Ag_mm2 * 100.0 if Ag_mm2 else 0.0

        # Diagnostic log
        try:
            with open(_LOG_PATH, 'a', encoding='utf-8') as _lf:
                _lf.write(
                    f'[batch_check] {sec_name}: raw_bar={raw_bar!r} bar_key={bar_key!r} '
                    f'nbars_2={sec.get("nbars_2")} nbars_3={sec.get("nbars_3")} '
                    f'→ nbars_b={nbars_b} nbars_h={nbars_h} total={2*nbars_b+2*nbars_h}\n')
        except Exception:
            pass

        if not alpha_data:
            results.append({"section": sec_name, "error": "PMM surface not available"})
            continue

        if not rows:
            results.append({
                "section": sec_name, "b_mm": b_mm, "h_mm": h_mm,
                "rho_pct": round(rho_pct, 2),
                "phi_Pn_max_kN": round(Pmax_kN, 1),
                "n_frames": 0, "n_checks": 0, "n_fail": 0,
                "max_dcr": None, "status": "NO DATA",
                "warning": "No column frames assigned to this section were found.",
                "worst": None,
            })
            continue

        try:
            # Reduce to worst-case demands per (frame, location) — ~15× speedup
            rows_check = _perframe_envelope(rows)

            # Build demands (engine sign: compression positive → negate P_kN from ETABS)
            # Engine: Mx = x-arm (b-dir, weak axis) = ETABS M33
            #         My = y-arm (h-dir, strong axis) = ETABS M22
            demands = [
                {"label": f"{r['frame']} / {r['combo']} [{r.get('location','?')}]",
                 "P":  -r["P_kN"],
                 "Mx":  r["M3_kNm"],
                 "My":  r["M2_kNm"]}
                for r in rows_check
            ]

            # Boundary ray-intersection DCR — matches individual check and SpColumn
            dcr_raw = _alpha_data_boundary_dcr(alpha_data, Pmax_kN, Pmin_kN, demands)

            # Attach frame/combo metadata
            dcr_items = []
            for i, dr in enumerate(dcr_raw):
                if i >= len(rows_check):
                    break
                row = rows_check[i]
                # Sanitize DCR: None or NaN → None (avoids float(None) TypeError
                # and JSON serialization failure for out-of-range demands)
                raw_dcr = dr.get("DCR")
                import math as _math
                safe_dcr = None
                if raw_dcr is not None:
                    try:
                        fv = float(raw_dcr)
                        safe_dcr = None if (_math.isnan(fv) or _math.isinf(fv)) else fv
                    except (TypeError, ValueError):
                        safe_dcr = None
                dr_clean = {**dr, "DCR": safe_dcr}
                dcr_items.append({
                    **dr_clean,
                    "frame":    row["frame"],
                    "story":    row.get("story", "?"),
                    "combo":    row["combo"],
                    "location": row.get("location", ""),
                    "P_kN":     row["P_kN"],
                    "Mx_kNm":   row["M3_kNm"],
                    "My_kNm":   row["M2_kNm"],
                })

            # Accumulate per-frame max DCR and section dims for 3D building view
            for _item in dcr_items:
                _fn = _item.get("frame")
                _dv = _item.get("DCR")
                if _fn and _dv is not None:
                    try:
                        _dv_f = float(_dv)
                        if _fn not in frame_dcr or _dv_f > frame_dcr[_fn]:
                            frame_dcr[_fn] = round(_dv_f, 3)
                    except (TypeError, ValueError):
                        pass
                # Store section dims (mm→m) keyed by frame — reliable source
                if _fn and _fn not in frame_dims:
                    frame_dims[_fn] = {"b_m": round(b_mm / 1000.0, 4),
                                       "h_m": round(h_mm / 1000.0, 4)}
                # Max absolute axial load per frame — global max and per-combo
                _pv = _item.get("P_kN")
                _cb = _item.get("combo", "")
                if _fn and _pv is not None:
                    try:
                        _pa = abs(float(_pv))
                        if _fn not in frame_axial or _pa > frame_axial[_fn]:
                            frame_axial[_fn] = round(_pa, 1)
                        if _cb:
                            if _cb not in frame_axial_by_combo:
                                frame_axial_by_combo[_cb] = {}
                            if _fn not in frame_axial_by_combo[_cb] or _pa > frame_axial_by_combo[_cb][_fn]:
                                frame_axial_by_combo[_cb][_fn] = round(_pa, 1)
                    except (TypeError, ValueError):
                        pass

            # worst = item with highest valid DCR; items with DCR=None sort to 0
            worst = max(dcr_items, key=lambda r: float(r.get("DCR") or 0)) if dcr_items else None
            # If worst has no valid DCR (all None), treat as no data
            if worst and worst.get("DCR") is None:
                worst = None

            n_fail   = sum(1 for r in dcr_items if r.get("status") == "FAIL")
            n_frames = len({r["frame"] for r in rows})
            n_bars   = 2 * nbars_b + 2 * nbars_h

            # ── Story-level aggregation ───────────────────────────────────────
            for item in dcr_items:
                st = item.get("story") or "?"
                if st not in story_summary:
                    story_summary[st] = {
                        "story": st, "n_checks": 0, "n_pass": 0, "n_fail": 0,
                        "max_dcr": None, "critical_section": None,
                        "critical_frame": None, "critical_combo": None,
                        "critical_location": None,
                    }
                ss = story_summary[st]
                ss["n_checks"] += 1
                status = item.get("status", "")
                if status == "FAIL":
                    ss["n_fail"] += 1
                elif status == "PASS":
                    ss["n_pass"] += 1
                dcr_val = item.get("DCR")
                if dcr_val is not None:
                    try:
                        dcr_f = float(dcr_val)
                        if ss["max_dcr"] is None or dcr_f > ss["max_dcr"]:
                            ss["max_dcr"]            = round(dcr_f, 3)
                            ss["critical_section"]   = sec_name
                            ss["critical_frame"]     = item.get("frame")
                            ss["critical_combo"]     = item.get("combo")
                            ss["critical_location"]  = item.get("location")
                    except (TypeError, ValueError):
                        pass

            results.append({
                "section":        sec_name,
                "b_mm":           b_mm,
                "h_mm":           h_mm,
                "nbars":          n_bars,
                "rebar_size":     bar_key,
                "_raw_rebar":     raw_bar,
                "_raw_nb2":       sec.get("nbars_2"),
                "_raw_nb3":       sec.get("nbars_3"),
                "rho_pct":        round(rho_pct, 2),
                "phi_Pn_max_kN":  round(Pmax_kN, 1),
                "n_frames":       n_frames,
                "n_checks":       len(dcr_items),
                "n_fail":         n_fail,
                "max_dcr":        round(float(worst["DCR"]), 3) if worst else None,
                "status":         ("FAIL" if float(worst["DCR"]) > 1.0 else "PASS")
                                  if worst else "NO DATA",
                "worst": {
                    "frame":    worst["frame"],
                    "story":    worst.get("story", "?"),
                    "combo":    worst["combo"],
                    "location": worst.get("location", ""),
                    "P_kN":     worst["P_kN"],
                    "Mx_kNm":   worst["Mx_kNm"],
                    "My_kNm":   worst["My_kNm"],
                    "M_demand": round(float(worst.get("M_demand") or 0), 2),
                    "M_cap":    round(float(worst.get("M_cap")    or 0), 2),
                } if worst else None,
            })

        except Exception as exc:
            import traceback as _tb, datetime as _dt
            try:
                with open(_LOG_PATH, 'a', encoding='utf-8') as _lf:
                    _lf.write(
                        f"[{_dt.datetime.now().isoformat()}] [batch_check] DCR loop failed "
                        f"sec={sec_name} n_rows={len(rows)}\n"
                        f"  ERROR: {exc}\n"
                        + ''.join(_tb.format_exc().splitlines(keepends=True)[:8])
                    )
            except Exception:
                pass
            results.append({"section": sec_name, "error": f"DCR evaluation failed: {exc}"})

    # Sort sections: FAIL first, then by max_dcr descending
    results.sort(key=lambda r: (
        0 if r.get("status") == "FAIL" else (1 if r.get("status") == "PASS" else 2),
        -(r.get("max_dcr") or 0),
    ))

    # Sort stories: FAIL first, then by max_dcr descending
    story_list = sorted(
        story_summary.values(),
        key=lambda s: (
            0 if s["n_fail"] > 0 else 1,
            -(s["max_dcr"] or 0),
        ),
    )

    # Invalidate cached geometry when a fresh batch run completes
    _geometry_cache.clear()

    _t_end = _time.perf_counter()
    try:
        with open(_LOG_PATH, 'a', encoding='utf-8') as _lf:
            _lf.write(
                f"[batch] TOTAL: {_t_end - _t_start:.2f}s "
                f"({len(sections)} sections, {len(body.combo_names)} combos)\n"
            )
    except Exception:
        pass

    return {"columns": results, "story_summary": story_list,
            "frame_dcr": frame_dcr, "frame_dims": frame_dims,
            "frame_axial": frame_axial,
            "frame_axial_by_combo": frame_axial_by_combo}


# ── Rebar Optimization endpoint ───────────────────────────────────────────────

class BatchOptimizeRequest(BaseModel):
    target_dcr:  float = 0.90   # max allowed DCR (e.g. 0.90 = 90 %)
    min_rho_pct: float = 1.0    # minimum steel ratio in %


@app.post("/api/pmm/batch-optimize")
def pmm_batch_optimize(body: BatchOptimizeRequest,
                        current_user: dict = Depends(get_current_user)):
    """
    Rebar optimizer — runs entirely offline (no ETABS calls).
    Uses forces cached from the last batch check and the PMM surface cache.
    For each section it tries standard bar diameters Ø12→Ø40, finds the
    SMALLEST bar where:
      • max DCR across all demand points ≤ target_dcr
      • steel ratio ρ ≥ min_rho_pct
    Returns per-section: current bar, optimized bar, ρ comparison, DCR, status.
    """
    if not _HAS_PMM:
        raise HTTPException(503, "PMM engine not available.")
    if not _last_batch_forces.get("sections"):
        raise HTTPException(400, "No batch data cached. Run the Batch Check first.")

    sections   = _last_batch_forces["sections"]
    by_section = _last_batch_forces["by_section"]

    STANDARD_BARS = ["Ø12", "Ø16", "Ø20", "Ø25", "Ø28", "Ø32", "Ø36", "Ø40"]
    _REBAR_DIA_MM_OPT = {
        "Ø8": 8.0, "Ø10": 10.0, "Ø12": 12.0, "Ø16": 16.0, "Ø20": 20.0,
        "Ø25": 25.0, "Ø28": 28.0, "Ø32": 32.0, "Ø36": 36.0, "Ø40": 40.0,
    }
    _t0_opt = _time.perf_counter()

    # ── Helper: build PMM surface for a given bar key (uses shared cache) ───
    def _surface_for_bar(sec, bar_key):
        b_mm    = float(sec.get("width")    or sec.get("b_mm")   or 400)
        h_mm    = float(sec.get("depth")    or sec.get("h_mm")   or 400)
        fc      = float(sec.get("fc_mpa")   or 28.0)
        fy      = float(sec.get("fy_mpa")   or 420.0)
        Es      = float(sec.get("Es_mpa")   or 200000.0)
        cover   = float(sec.get("cover_mm") or sec.get("cover")  or 40.0)
        stirrup = 10.0
        nbars_b = max(2, int(sec.get("nbars_b") or sec.get("nbars_2") or 3))
        _nb3    = int(sec.get("nbars_3") or 3)
        nbars_h = max(0, int(sec["nbars_h"]) if sec.get("nbars_h") is not None
                       else _nb3 - 2)
        sec_name = sec.get("prop_name") or sec.get("name", "?")

        _ck = (sec_name, b_mm, h_mm, fc, fy, nbars_b, nbars_h, bar_key)
        if _ck in _PMM_SURFACE_CACHE:
            return _PMM_SURFACE_CACHE[_ck]

        bar_area_mm2 = REBAR_TABLE_SI.get(bar_key, 314.2)
        bar_dia_mm   = _REBAR_DIA_MM_OPT.get(bar_key, 20.0)
        eff_cover    = cover + stirrup + bar_dia_mm / 2.0
        b_in  = b_mm * _MM_TO_IN;  h_in  = h_mm * _MM_TO_IN
        fc_k  = fc   * _MPA_TO_KSI; fy_k = fy   * _MPA_TO_KSI
        Es_k  = Es   * _MPA_TO_KSI; cov  = eff_cover * _MM_TO_IN
        ba    = bar_area_mm2 * (_MM_TO_IN ** 2)
        corners = rect_coords(b_in, h_in)
        areas, positions = rect_bars_grid(b_in, h_in, cov, nbars_b, nbars_h, ba)
        sec_obj = PMMSection(
            corner_coords=corners, fc=fc_k, fy=fy_k, Es=Es_k,
            alpha_steps=30, num_points=30, include_phi=True,
            bar_areas=areas, bar_positions=positions,
        )
        pmm_raw = compute_pmm(sec_obj)
        alpha   = pmm_raw.get("alpha_data", {})
        for curve in alpha.values():
            curve["P"]  = [v * _KIPS_TO_KN  for v in curve["P"]]
            curve["Mx"] = [v * _KIN_TO_KNM for v in curve["Mx"]]
            curve["My"] = [v * _KIN_TO_KNM for v in curve["My"]]
        result = {
            "alpha_data": alpha,
            "Pmax_kN": pmm_raw["Pmax"] * _KIPS_TO_KN,
            "Pmin_kN": pmm_raw["Pmin"] * _KIPS_TO_KN,
            "Ag_mm2":  pmm_raw["Ag"]   * _IN2_TO_MM2,
            "Ast_mm2": pmm_raw["Ast"]  * _IN2_TO_MM2,
        }
        _PMM_SURFACE_CACHE[_ck] = result
        return result

    # ── Pre-warm cache in parallel: all (section × bar) combos ──────────────
    def _warm(args):
        sec, bk = args
        try: _surface_for_bar(sec, bk)
        except Exception: pass

    tasks = [(sec, bk) for sec in sections for bk in STANDARD_BARS]
    try:
        with _TPE(max_workers=8) as pool:
            list(pool.map(_warm, tasks))
    except Exception:
        for t in tasks:
            _warm(t)

    # ── Per-section optimization ─────────────────────────────────────────────
    import math as _mth
    results = []

    for sec in sections:
        sec_name    = sec.get("prop_name") or sec.get("name", "?")
        rows        = by_section.get(sec_name, [])
        b_mm        = float(sec.get("width") or sec.get("b_mm") or 400)
        h_mm        = float(sec.get("depth") or sec.get("h_mm") or 400)
        Ag_mm2      = b_mm * h_mm
        nbars_b     = max(2, int(sec.get("nbars_b") or sec.get("nbars_2") or 3))
        _nb3        = int(sec.get("nbars_3") or 3)
        nbars_h     = max(0, int(sec["nbars_h"]) if sec.get("nbars_h") is not None
                          else _nb3 - 2)
        n_bars_total = 2 * nbars_b + 2 * nbars_h
        current_bar  = _normalize_bar_size(str(sec.get("rebar_size") or "Ø20"))

        # Current ρ
        cur_area_mm2  = REBAR_TABLE_SI.get(current_bar, 314.2)
        cur_Ast_mm2   = n_bars_total * cur_area_mm2
        cur_rho_pct   = round(cur_Ast_mm2 / Ag_mm2 * 100.0, 2)

        if not rows:
            results.append({
                "section": sec_name, "b_mm": b_mm, "h_mm": h_mm,
                "n_bars": n_bars_total,
                "current_bar": current_bar, "current_rho_pct": cur_rho_pct,
                "optimized_bar": current_bar, "optimized_rho_pct": cur_rho_pct,
                "optimized_dcr": None, "current_dcr": None,
                "status": "NO DATA", "note": "No force data from batch check.",
            })
            continue

        # Build enveloped demands
        rows_env = _perframe_envelope(rows)
        demands  = [
            {"label": f"{r['frame']}/{r['combo']}[{r.get('location','?')}]",
             "P": -r["P_kN"], "Mx": r["M3_kNm"], "My": r["M2_kNm"]}
            for r in rows_env
        ]

        def _max_dcr_for_bar(bk):
            try:
                surf = _surface_for_bar(sec, bk)
                dcrs = _alpha_data_boundary_dcr(
                    surf["alpha_data"], surf["Pmax_kN"], surf["Pmin_kN"], demands)
                vals = [float(d["DCR"]) for d in dcrs
                        if d.get("DCR") is not None
                        and not _mth.isnan(float(d["DCR"]))
                        and not _mth.isinf(float(d["DCR"]))]
                return max(vals) if vals else None
            except Exception:
                return None

        # Current DCR
        cur_dcr = _max_dcr_for_bar(current_bar)

        # Find minimum adequate bar
        opt_bar = None; opt_rho = None; opt_dcr = None
        for bk in STANDARD_BARS:
            Ast = n_bars_total * REBAR_TABLE_SI.get(bk, 314.2)
            rho = Ast / Ag_mm2 * 100.0
            if rho < body.min_rho_pct:
                continue
            dcr = _max_dcr_for_bar(bk)
            if dcr is None:
                continue
            if dcr <= body.target_dcr:
                opt_bar = bk
                opt_rho = round(rho, 2)
                opt_dcr = round(dcr, 3)
                break

        if opt_bar is None:
            # Even Ø40 doesn't satisfy target → flag overstressed
            bk  = STANDARD_BARS[-1]
            Ast = n_bars_total * REBAR_TABLE_SI.get(bk, 314.2)
            opt_bar = bk
            opt_rho = round(Ast / Ag_mm2 * 100.0, 2)
            opt_dcr = _max_dcr_for_bar(bk)
            if opt_dcr: opt_dcr = round(opt_dcr, 3)
            status = "OVERSTRESSED"
            note   = f"Even {bk} exceeds target DCR {body.target_dcr:.0%}"
        elif opt_bar == current_bar:
            status = "OPTIMAL"
            note   = "Current bar is already the minimum adequate size."
        elif STANDARD_BARS.index(opt_bar) < STANDARD_BARS.index(current_bar):
            status = "DOWNSIZE"
            note   = f"Can reduce from {current_bar} → {opt_bar}"
        else:
            status = "UPSIZE"
            note   = f"Must increase from {current_bar} → {opt_bar}"

        results.append({
            "section":          sec_name,
            "b_mm":             b_mm,
            "h_mm":             h_mm,
            "n_bars":           n_bars_total,
            "current_bar":      current_bar,
            "current_rho_pct":  cur_rho_pct,
            "current_dcr":      round(cur_dcr, 3) if cur_dcr is not None else None,
            "optimized_bar":    opt_bar,
            "optimized_rho_pct": opt_rho,
            "optimized_dcr":    opt_dcr,
            "status":           status,
            "note":             note,
        })

    _t1_opt = _time.perf_counter()
    try:
        with open(_LOG_PATH, 'a', encoding='utf-8') as _lf:
            _lf.write(f"[optimize] {len(sections)} sections in {_t1_opt - _t0_opt:.2f}s\n")
    except Exception:
        pass

    # Sort: OVERSTRESSED first, then UPSIZE, then DOWNSIZE, then OPTIMAL
    _order = {"OVERSTRESSED": 0, "UPSIZE": 1, "DOWNSIZE": 2, "OPTIMAL": 3, "NO DATA": 4}
    results.sort(key=lambda r: _order.get(r.get("status", "NO DATA"), 5))

    return {
        "results": results,
        "target_dcr":  body.target_dcr,
        "min_rho_pct": body.min_rho_pct,
        "elapsed_s":   round(_t1_opt - _t0_opt, 2),
    }


# ── Column Axial Force endpoint ───────────────────────────────────────────────

class _AxialReq(BaseModel):
    combo:      str
    col_frames: list        # frame names from geometry cache (already column-classified)
    load_type:  str = "combo"

@app.post("/api/etabs/column-axial")
def etabs_column_axial(req: _AxialReq,
                       current_user: dict = Depends(get_current_user)):
    """
    Extract axial force at station-0 (bottom) of each supplied column frame.
    Returns {axial: {frame_name: abs_P_kN}, combo: str}.
    """
    helper = getattr(actions, "get_column_axial_for_combo", None)
    if not callable(helper):
        raise HTTPException(503, "get_column_axial_for_combo not available.")
    result = helper(req.combo, req.col_frames, req.load_type)
    if "error" in result:
        raise HTTPException(503, result["error"])
    return result


# ── 3D Building Geometry endpoint ─────────────────────────────────────────────
_geometry_cache: dict = {}   # {"data": {...}} — invalidated on each batch run


@app.get("/api/etabs/geometry")
def etabs_geometry(current_user: dict = Depends(get_current_user)):
    """
    Returns all frame (column/beam) and wall geometry for the 3D building view.
    Result is cached until the next batch check run.
    """
    if _geometry_cache.get("data"):
        return _geometry_cache["data"]

    helper = getattr(actions, "get_building_geometry", None)
    if not callable(helper):
        raise HTTPException(503, "get_building_geometry not available in actions.py.")

    result = helper()
    if "error" in result:
        raise HTTPException(503, result["error"])

    _geometry_cache["data"] = result
    return result


# ── 2D FEM endpoints ──────────────────────────────────────────────────────────
try:
    _fem2d_path = _os.path.join(_here, 'fem2d_engine.py')
    # Bypass all import machinery: read source and exec directly.
    # This works in both dev and PyInstaller frozen environments.
    with open(_fem2d_path, encoding='utf-8') as _f:
        _fem2d_src = _f.read()
    _fem2d_ns: dict = {}
    exec(compile(_fem2d_src, _fem2d_path, 'exec'), _fem2d_ns)
    generate_mesh = _fem2d_ns['generate_mesh']
    solve_fem2d   = _fem2d_ns['solve_fem2d']
    _HAS_FEM2D = True
except Exception as _e:
    import traceback as _tb
    _HAS_FEM2D = False
    print(f'[fem2d] FAILED: {_e}\n{_tb.format_exc()}')


@app.post('/api/fem2d/mesh')
async def fem2d_mesh(req: dict):
    if not _HAS_FEM2D:
        raise HTTPException(503, 'FEM2D engine not available')
    try:
        return generate_mesh(
            geometry  = req.get('geometry', {}),
            mesh_size = float(req.get('mesh_size', 50)),
            mesh_type = req.get('mesh_type', 'quad'),
        )
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post('/api/fem2d/solve')
async def fem2d_solve(req: dict):
    if not _HAS_FEM2D:
        raise HTTPException(503, 'FEM2D engine not available')
    try:
        return solve_fem2d(
            nodes      = req['nodes'],
            elements   = req['elements'],
            thickness  = float(req.get('thickness', 10)),
            E          = float(req.get('E', 200000)),
            nu         = float(req.get('nu', 0.3)),
            mode       = int(req.get('mode', 1)),
            unit_wt    = float(req.get('unit_wt', 0)),
            supports   = req.get('supports', []),
            loads      = req.get('loads', []),
            edge_loads = req.get('edge_loads', []),
        )
    except Exception as e:
        raise HTTPException(400, str(e))
