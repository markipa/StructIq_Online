# StructIQ

**ETABS automation for structural engineers — desktop app with cloud licensing.**

StructIQ connects to a running ETABS model and automates common post-processing tasks: story drifts, torsion irregularity checks, joint reactions, and load combination generation. It runs as a local web app (FastAPI + browser UI) packaged as a Windows `.exe`.

---

## Features

| Feature | Description |
|---|---|
| **Story Drifts** | Reads ETABS drift results and plots them interactively per load case |
| **Torsion Check** | ASCE 7 torsional irregularity (checks all stories, all load combos) |
| **Joint Reactions** | Extracts base reactions for any load combination |
| **Load Combinations** | Spreadsheet editor — generate ASCE 7 / custom combos with one click |

### Plan Tiers

| | Free | PRO | Enterprise |
|---|---|---|---|
| Story drifts | Yes | Yes | Yes |
| Load combinations | 5 max | Unlimited | Unlimited |
| Torsion check | View only | Full | Full |
| Joint reactions | View only | Full | Full |
| Simultaneous devices | 1 | 1 | 3 |
| Price | Free | $29/mo · $299/yr | $699/yr |

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

- Python 3.11+
- ETABS installed and licensed
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
python launcher.py
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
├── backend/
│   ├── launcher.py        ← Starts FastAPI, opens browser
│   ├── main.py            ← All local API routes
│   ├── database.py        ← Local SQLite (sessions, offline cache)
│   ├── config.py          ← Cloud URL, plan sync key, admin emails
│   ├── make_icon.py       ← Generates .ico + favicon files
│   ├── icon.ico           ← App icon (embedded in exe)
│   ├── structiq.spec      ← PyInstaller spec
│   ├── build.bat          ← One-click build
│   ├── requirements.txt
│   ├── etabs_api/
│   │   ├── __init__.py
│   │   └── connection.py  ← ETABS COM bridge
│   └── frontend/
│       ├── index.html
│       ├── styles.css
│       ├── app.js
│       ├── favicon.ico
│       └── favicon.svg
└── railway/
    ├── main.py            ← Auth, billing, session enforcement
    ├── database.py        ← Cloud SQLite schema + queries
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
| `POST` | `/api/auth/logout` | Logout (revokes local + cloud session) |
| `GET` | `/api/cloud/sync` | Sync plan from cloud, validate session |
| `GET` | `/api/etabs/connect` | Connect to running ETABS instance |
| `GET` | `/api/drift` | Get story drift data |
| `GET` | `/api/torsion` | Run torsion irregularity check |
| `GET` | `/api/reactions` | Get joint reactions |
| `POST` | `/api/loadcombos/generate` | Generate load combinations |

### Cloud server (Railway)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/auth/register` | Create account |
| `POST` | `/api/auth/login` | Login |
| `GET` | `/api/plan` | Sync plan by email (key-protected) |
| `POST` | `/api/session/register` | Register session (enforce limits) |
| `POST` | `/api/session/validate` | Check if session is still valid |
| `POST` | `/api/session/revoke` | Revoke session on logout |
| `POST` | `/stripe/create-checkout` | Create Stripe checkout session |
| `POST` | `/stripe/webhook` | Stripe payment events |
| `GET` | `/admin/users` | List all users (admin) |
| `POST` | `/admin/set-plan` | Set plan by email (admin) |
| `GET` | `/health` | Health check |

---

## Session Enforcement

When a user logs in:
1. The local app registers the session with Railway (`/api/session/register`)
2. For Free/PRO: if another session exists, it is kicked out
3. For Enterprise: up to 3 simultaneous sessions are allowed
4. Every 5 minutes, `/api/cloud/sync` validates the session
5. If `session_valid: false` is returned, the app auto-logs out with a toast notification

---

## Regenerating Brand Icons

If you change brand colors, edit `backend/make_icon.py` and run:

```bash
cd backend
python make_icon.py
```

This regenerates:
- `icon.ico` — multi-resolution (256→16 px) for the Windows exe
- `frontend/favicon.ico` — multi-resolution (48→16 px) for the browser
- `frontend/favicon.svg` — vector SVG (sharp at any DPI)

Then rebuild the exe to embed the new icon.

---

## Contact

Enterprise inquiries: mmi.structural@gmail.com
