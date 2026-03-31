# StructIQ

**ETABS automation and RC column design for structural engineers — desktop app with cloud licensing.**

[![CI](https://github.com/markipa/StructIQ/actions/workflows/ci.yml/badge.svg)](https://github.com/markipa/StructIQ/actions/workflows/ci.yml)

StructIQ connects to a running ETABS model and automates common post-processing tasks. It also includes a standalone ACI 318-19 P-M-M Column Designer that works without ETABS. Runs as a local web app (FastAPI + browser UI) packaged as a Windows `.exe`.

---

## Features

| Feature | Description |
|---|---|
| **P-M-M Column Designer** | ACI 318-19 biaxial interaction surface, optimizer, batch ETABS check, Save/Open sessions |
| **Story Drifts** | Reads ETABS drift results and plots them interactively per load case |
| **Torsion Check** | ASCE 7 torsional irregularity (all stories, all load combos) |
| **Joint Reactions** | Extracts base reactions for any load combination |
| **Load Combinations** | Spreadsheet editor — generate ASCE 7 / custom combos with one click |

### Plan Tiers

| | Free | PRO | Enterprise |
|---|---|---|---|
| Story drifts | Yes | Yes | Yes |
| Load combinations | 5 max | Unlimited | Unlimited |
| Torsion check | View only | Full | Full |
| Joint reactions | View only | Full | Full |
| P-M-M Column Designer | — | Full | Full |
| Simultaneous devices | 1 | 1 | 3 |
| Price | Free | $29/mo · $299/yr | $699/yr |

---

## P-M-M Column Designer

Full ACI 318-19 biaxial interaction diagram for rectangular RC columns.

| Capability | Detail |
|---|---|
| **Generate** | 3D P-M-M surface, P–Mx, P–My, and Mx–My interaction charts |
| **Check DCR** | Evaluates demand/capacity ratio for any number of load combinations |
| **Optimize** | Bisection search for minimum steel area meeting a target DCR |
| **Bar size sweep** | Tries Ø8→Ø40 and returns the globally lightest passing arrangement |
| **Batch check** | Checks all RC column sections in the ETABS model at once |
| **Save / Open** | Saves the full session (inputs + loads + surface) to a `.siq` file |
| **Export** | Word report with embedded charts; CSV of interaction curve |
| **Accuracy** | Benchmarked against ACI 318-19 hand-calculations: 0.0% error on φPn,max and φPn,min |

### Axis Convention

```
ETABS          Engine (pmm_engine.py)    Display table
------         ----------------------    -------------
M33 (strong)   My  (h-face)              Mx column
M22 (weak)     Mx  (b-face)              My column
P (neg = comp) P   (pos = comp)          P (neg = comp)
```

See `backend/pmm_conventions.py` for full documentation with line citations.

---

## Architecture

```
┌────────────────────────────────────┐      ┌─────────────────────────────┐
│  Engineer's PC                     │      │  Railway.app (cloud)        │
│                                    │      │                             │
│  StructIQ.exe                      │      │  Auth & billing server      │
│  ├─ FastAPI (localhost:8000)       │◄────►│  ├─ User accounts           │
│  ├─ Browser UI (HTML/CSS/JS)       │      │  ├─ Plan management         │
│  ├─ ETABS COM bridge               │      │  ├─ Stripe subscriptions    │
│  └─ Local SQLite (sessions)        │      │  └─ Session enforcement     │
│                                    │      │                             │
│  ETABS (separate process)          │      │  SQLite (persistent volume) │
└────────────────────────────────────┘      └─────────────────────────────┘
```

- **Local FastAPI** handles all ETABS communication and serves the frontend.
- **Railway** handles authentication, subscriptions (Stripe), and global session limits.
- The app works offline for up to 3 days if Railway is unreachable.

---

## Quick Start (Development)

### Prerequisites

- Python 3.12+
- ETABS installed and licensed (optional — P-M-M works without ETABS)
- Windows (ETABS COM API is Windows-only)

### Setup

```bash
git clone <repo-url>
cd StructIQ

# Create and activate venv
python -m venv .venv
.venv\Scripts\activate

# Install backend dependencies
cd backend
pip install -r requirements.txt

# Run the app
python start_server.py
```

Browser opens automatically at `http://localhost:8000`.

### Run the cloud server locally

```bash
cd railway
pip install -r requirements.txt
uvicorn main:app --reload --port 9000
```

Point the local app at it by editing `backend/config.py`:
```python
CLOUD_URL = "http://localhost:9000"
```

---

## Testing

72 tests across three suites — run from the `backend/` directory:

```bash
# All at once
python -m unittest tests.test_pmm_helpers tests.test_integration_pmm tests.test_spcolumn_benchmark -v

# Individual suites
python -m unittest tests.test_pmm_helpers -v        # 28 unit tests
python -m unittest tests.test_integration_pmm -v    # 28 integration tests
python -m unittest tests.test_spcolumn_benchmark -v # 16 ACI 318-19 benchmark tests
```

Tests run automatically on every push via **GitHub Actions** (see `.github/workflows/ci.yml`).

### What is GitHub Actions CI?

Every time you push code to GitHub, GitHub's cloud servers automatically run all 72 tests and report pass ✅ or fail ❌ on the commit. You never have to remember to run tests manually — if a push silently breaks the PMM engine, DCR calculation, or optimizer, you'll see it immediately before the `.exe` gets rebuilt.

### ACI 318-19 Benchmark (RC-01 Reference Column)

| Property | Hand-calc | Engine | Diff |
|---|---|---|---|
| φPn,max | 3 284.3 kN | 3 284.3 kN | 0.0% |
| φPn,min | −1 484.5 kN | −1 484.5 kN | 0.0% |
| ρ | 1.963% | 1.963% | 0.0% |

Reference column: 400×500 mm, f'c = 28 MPa, fy = 420 MPa, 8×Ø25.

---

## Deploying Updates (no rebuild needed)

After editing source files, sync them to the running `.exe` with:

```bash
python deploy.py
```

This copies changed files from `backend/` to `backend/dist/StructIQ/_internal/`. Restart the `.exe` to pick up the changes — no PyInstaller rebuild required.

```bash
python deploy.py --dry-run   # preview what would be copied
python deploy.py --force     # copy all files regardless of timestamp
```

---

## Building the .exe

```bash
cd backend

# Generate brand icons (only needed if make_icon.py was changed)
pip install Pillow
python make_icon.py

# Build
pip install pyinstaller
pyinstaller structiq.spec --noconfirm
```

Output: `backend/dist/StructIQ/` — zip this folder and distribute.

---

## Deployment

See [DEPLOY.md](DEPLOY.md) for full instructions including:
- Deploying the auth server to Railway
- Configuring Stripe webhooks
- Setting environment variables
- Managing users and plans

---

## Project Layout

```
StructIQ/
├── README.md
├── DEPLOY.md
├── deploy.py                  ← Sync source → dist without rebuilding
├── .github/
│   └── workflows/
│       └── ci.yml             ← GitHub Actions: runs 72 tests on every push
├── backend/
│   ├── main.py                ← All local API routes (~2 500 lines)
│   ├── pmm_engine.py          ← ACI 318-19 P-M-M computation engine
│   ├── pmm_conventions.py     ← Axis convention documentation + constants
│   ├── database.py            ← Local SQLite (sessions, offline cache)
│   ├── config.py              ← Cloud URL, plan sync key, admin emails
│   ├── start_server.py        ← Dev server entry point
│   ├── launcher.py            ← Starts FastAPI, opens browser (production)
│   ├── structiq.spec          ← PyInstaller spec
│   ├── requirements.txt
│   ├── tests/
│   │   ├── test_pmm_helpers.py        ← 28 unit tests
│   │   ├── test_integration_pmm.py    ← 28 integration tests
│   │   └── test_spcolumn_benchmark.py ← 16 ACI 318-19 benchmark tests
│   ├── etabs_api/
│   │   ├── __init__.py
│   │   ├── actions.py         ← All ETABS COM calls
│   │   └── connection.py      ← ETABS COM bridge
│   └── frontend/
│       ├── index.html
│       ├── styles.css
│       ├── app.js
│       ├── favicon.ico
│       └── favicon.svg
└── railway/
    ├── main.py                ← Auth, billing, session enforcement
    ├── database.py            ← Cloud SQLite schema + queries
    ├── requirements.txt
    ├── Procfile
    ├── railway.json
    └── nixpacks.toml
```

---

## Key API Endpoints

### Local app (`localhost:8000`)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/auth/login` | Login with email + password |
| `POST` | `/api/auth/register` | Register new account |
| `GET` | `/api/cloud/sync` | Sync plan from cloud, validate session |
| `POST` | `/api/pmm/calculate` | Compute P-M-M interaction surface |
| `POST` | `/api/pmm/check` | Check DCR for demand loads |
| `POST` | `/api/pmm/optimize` | Find minimum steel area for target DCR |
| `POST` | `/api/pmm/etabs-batch-check` | Batch DCR check for all ETABS columns |
| `GET` | `/api/pmm/etabs-sections` | Import column sections from ETABS |
| `POST` | `/api/pmm/export-report` | Export Word report |
| `GET` | `/api/drift` | Get story drift data |
| `GET` | `/api/torsion` | Run torsion irregularity check |
| `GET` | `/api/reactions` | Get joint reactions |
| `POST` | `/api/load-combinations/generate` | Generate load combinations |

### Cloud server (Railway)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/auth/register` | Create account |
| `POST` | `/api/auth/login` | Login |
| `GET` | `/api/plan` | Sync plan by email (key-protected) |
| `POST` | `/api/session/register` | Register session (enforce limits) |
| `POST` | `/api/session/validate` | Check if session is still valid |
| `POST` | `/stripe/create-checkout` | Create Stripe checkout session |
| `POST` | `/stripe/webhook` | Stripe payment events |
| `GET` | `/admin/users` | List all users (admin) |
| `POST` | `/admin/set-plan` | Set plan by email (admin) |

---

## Session Enforcement

When a user logs in:
1. The local app registers the session with Railway (`/api/session/register`)
2. For Free/PRO: if another session exists, it is kicked out
3. For Enterprise: up to 3 simultaneous sessions are allowed
4. Every 5 minutes, `/api/cloud/sync` validates the session
5. If `session_valid: false` is returned, the app auto-logs out with a toast notification

---

## Contact

Enterprise inquiries: mmi.structural@gmail.com
