# StructIQ — Deployment Guide

## Project Structure

```
StructIQ/
├── backend/                   ← Local desktop app (distributed to engineers)
│   ├── launcher.py            ← Entry point: starts FastAPI + opens browser
│   ├── main.py                ← All local API routes
│   ├── database.py            ← Local SQLite (user sessions, offline cache)
│   ├── config.py              ← Cloud URL, version, admin emails
│   ├── structiq.spec          ← PyInstaller build spec
│   ├── build.bat              ← One-click build script
│   ├── make_icon.py           ← Generates icon.ico + favicon files (Pillow)
│   ├── icon.ico               ← App icon embedded in StructIQ.exe
│   ├── requirements.txt
│   ├── etabs_api/             ← ETABS COM bridge (Windows only)
│   └── frontend/              ← Single-page app (HTML / CSS / JS)
│       ├── index.html
│       ├── styles.css
│       ├── app.js
│       ├── favicon.ico        ← Browser favicon (48/32/16 px)
│       └── favicon.svg        ← Vector favicon (sharp on Retina/4K)
│
└── railway/                   ← Cloud auth + billing server (Railway.app)
    ├── main.py                ← FastAPI: auth, plans, sessions, Stripe
    ├── database.py            ← Cloud SQLite (users, subscriptions, sessions)
    ├── requirements.txt
    ├── Procfile
    ├── railway.json
    └── nixpacks.toml
```

---

## Step 1 — Deploy Auth Server to Railway

1. Push the `railway/` folder to a GitHub repo
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**
3. Connect that repo; Railway auto-detects Python via nixpacks
4. Add a **Volume** at `/app/data` so the SQLite DB persists across deploys
5. Set these **Environment Variables** in the Railway dashboard:

| Variable | Required | Description |
|---|---|---|
| `ADMIN_SECRET` | Yes | Protects `/admin/*` endpoints |
| `PLAN_SYNC_KEY` | Yes | Shared secret between desktop app and Railway |
| `STRIPE_SECRET_KEY` | Yes (for payments) | Stripe secret key (`sk_live_...`) |
| `STRIPE_PUBLISHABLE_KEY` | Yes (for payments) | Stripe publishable key (`pk_live_...`) |
| `STRIPE_WEBHOOK_SECRET` | Yes (for payments) | Stripe webhook signing secret (`whsec_...`) |
| `STRIPE_PRICE_MONTHLY` | Optional | Monthly price ID (default: `price_1T7x...`) |
| `STRIPE_PRICE_YEARLY` | Optional | Yearly price ID (default: `price_1T7x...`) |
| `PORT` | Auto | Set by Railway automatically |

6. Railway gives you a URL like:
   ```
   https://structiq-production.up.railway.app
   ```
7. Verify: open `https://your-url.railway.app/health`
   → Should return `{"status": "ok", "service": "structiq-auth"}`

---

## Step 2 — Configure Stripe Webhooks

1. In Stripe Dashboard → **Webhooks** → **Add endpoint**
2. URL: `https://your-url.railway.app/stripe/webhook`
3. Events to listen for:
   - `checkout.session.completed`
   - `customer.subscription.deleted`
   - `customer.subscription.paused`
4. Copy the **Signing Secret** → add as `STRIPE_WEBHOOK_SECRET` in Railway

---

## Step 3 — Update Local App Config

Edit `backend/config.py`:
```python
CLOUD_URL     = "https://your-url.railway.app"
PLAN_SYNC_KEY = "your-shared-secret"   # must match Railway env var
ADMIN_EMAILS  = ["your@email.com"]     # always gets ENTERPRISE plan
```

Or set via environment variable:
```
STRUCTIQ_CLOUD_URL=https://your-url.railway.app
PLAN_SYNC_KEY=your-shared-secret
```

---

## Step 4 — Regenerate Icons (if brand changes)

```bash
cd backend
pip install Pillow
python make_icon.py
```

Produces:
- `icon.ico` — 256/128/64/48/32/16 px (embedded in .exe by PyInstaller)
- `frontend/favicon.ico` — 48/32/16 px (served by the app)
- `frontend/favicon.svg` — vector (sharp on Retina/4K screens)

---

## Step 5 — Build the .exe

From the `backend/` folder:

```bash
# Activate venv first
..\\.venv\\Scripts\\activate

# Build
pyinstaller structiq.spec --noconfirm
```

Or double-click `build.bat`.

Output: `backend/dist/StructIQ/StructIQ.exe`

The exe embeds the brand icon (`icon='icon.ico'` in structiq.spec).

---

## Step 6 — Test the .exe

1. Double-click `dist/StructIQ/StructIQ.exe`
2. Console shows:
   ```
   StructIQ is starting...
   Opening → http://127.0.0.1:8000
   ```
3. Browser opens automatically to the login screen
4. Register → login → connect to ETABS

---

## Step 7 — Distribute to Engineers

Zip the entire `dist/StructIQ/` folder and send it.

Engineers:
1. Unzip anywhere on their PC
2. Double-click `StructIQ.exe`
3. Browser opens → register / login → use the app

No Python or ETABS Python API installation needed.

---

## Managing Users

### List all users:
```bash
curl "https://your-url.railway.app/admin/users?secret=YOUR_ADMIN_SECRET"
```

### Upgrade a user by email (preferred):
```bash
curl -X POST https://your-url.railway.app/admin/set-plan \
  -H "Content-Type: application/json" \
  -d '{"email": "engineer@firm.com", "plan": "pro", "admin_secret": "YOUR_ADMIN_SECRET"}'
```

Plans: `free` | `pro` | `enterprise`

### Upgrade by user ID (legacy):
```bash
curl -X POST https://your-url.railway.app/api/admin/update-plan \
  -H "Content-Type: application/json" \
  -d '{"user_id": 1, "plan": "pro", "admin_secret": "YOUR_ADMIN_SECRET"}'
```

### Browse the database:
Railway Dashboard → your project → Volume → open `structiq_cloud.db`

---

## Plan Features

| Feature | Free | PRO | Enterprise |
|---|---|---|---|
| ETABS connection | Yes | Yes | Yes |
| Story drifts | Yes | Yes | Yes |
| Load combinations | 5 combos max | Unlimited | Unlimited |
| Torsion check | View only | Full | Full |
| Joint reactions | View only | Full | Full |
| Simultaneous devices | 1 | 1 | 3 |
| Support | Community | Email | Priority |
| Price | Free | $29/mo or $299/yr | $699/yr |

---

## Session Enforcement

- **Free / PRO**: 1 active session per account. Logging in on a second device kicks the first.
- **Enterprise**: up to 3 simultaneous sessions. 4th login is rejected.
- Sessions are tracked in the Railway `cloud_sessions` table.
- The desktop app calls `/api/cloud/sync` every 5 minutes; if `session_valid: false` is returned, the app shows a toast and auto-logs out.

---

## Dev Workflow (no .exe needed)

```bash
cd backend
python launcher.py
# Opens browser at http://localhost:8000
```

Run Railway server locally:
```bash
cd railway
pip install -r requirements.txt
uvicorn main:app --reload --port 9000
```

Then point `config.py` → `CLOUD_URL = "http://localhost:9000"`.

---

## Environment Variables Summary

### Railway (cloud server)
| Variable | Notes |
|---|---|
| `ADMIN_SECRET` | Keep this secret; used for all `/admin/*` calls |
| `PLAN_SYNC_KEY` | Must match `PLAN_SYNC_KEY` in `config.py` |
| `STRIPE_SECRET_KEY` | From Stripe Dashboard |
| `STRIPE_PUBLISHABLE_KEY` | From Stripe Dashboard |
| `STRIPE_WEBHOOK_SECRET` | From Stripe Webhook settings |

### Local app (`config.py` or environment)
| Variable | Notes |
|---|---|
| `STRUCTIQ_CLOUD_URL` | Override `CLOUD_URL` without editing config.py |
| `PLAN_SYNC_KEY` | Override plan sync key |
