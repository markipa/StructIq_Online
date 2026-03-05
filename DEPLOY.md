# StructIQ — Deployment Guide

## Project Structure

```
Try/
├── backend/              ← Local desktop app (user installs this)
│   ├── launcher.py       ← Entry point: starts server + opens browser
│   ├── main.py           ← FastAPI (all routes)
│   ├── database.py       ← Local SQLite auth
│   ├── config.py         ← Cloud URL config
│   ├── structiq.spec     ← PyInstaller build spec
│   ├── build.bat         ← One-click build script
│   ├── requirements.txt
│   ├── etabs_api/        ← ETABS COM bridge
│   └── frontend/         ← HTML / CSS / JS
│
└── railway/              ← Cloud auth server (deploy this to Railway)
    ├── main.py           ← FastAPI auth-only routes
    ├── database.py       ← Cloud SQLite
    ├── requirements.txt
    ├── Procfile
    └── railway.json
```

---

## Step 1 — Deploy Auth Server to Railway

1. Go to https://railway.app → Sign up (free)
2. Click **New Project** → **Deploy from GitHub**
3. Push the `railway/` folder to a GitHub repo
4. Connect that repo to Railway
5. Set environment variable in Railway dashboard:
   ```
   ADMIN_SECRET = your-secret-password-here
   ```
6. Railway gives you a URL like:
   ```
   https://structiq-production.up.railway.app
   ```
7. Test it: open `https://your-url.railway.app/health`
   → Should return `{"status": "ok"}`

---

## Step 2 — Update Local App Config

Edit `backend/config.py`:
```python
CLOUD_URL = "https://your-url.railway.app"
```

---

## Step 3 — Build the .exe

From the `backend/` folder, double-click `build.bat`

Or run manually:
```bash
cd backend
..\venv\Scripts\activate
pyinstaller structiq.spec --noconfirm
```

Output: `backend/dist/StructIQ/StructIQ.exe`

---

## Step 4 — Test the .exe

1. Double-click `StructIQ.exe`
2. Terminal window opens showing:
   ```
   StructIQ is starting...
   Opening → http://127.0.0.1:8000
   ```
3. Browser opens automatically
4. Login screen appears

---

## Step 5 — Distribute to Users

Send engineers the `dist/StructIQ/` folder (zip it up)

They:
1. Unzip anywhere on their PC
2. Double-click `StructIQ.exe`
3. Browser opens → login → use the app

---

## Managing Users

### Manually upgrade a user to Pro:
```bash
curl -X POST https://your-url.railway.app/api/admin/update-plan \
  -H "Content-Type: application/json" \
  -d '{"user_id": 1, "plan": "pro", "admin_secret": "your-secret"}'
```

### Check all users (Railway SQLite viewer):
Railway Dashboard → your project → Volume → browse `structiq_cloud.db`

---

## Environment Variables (Railway)

| Variable | Value | Description |
|---|---|---|
| `ADMIN_SECRET` | your-secret | Protects admin endpoints |
| `PORT` | (auto) | Railway sets this automatically |

---

## Dev Workflow (no .exe needed)

```bash
cd backend
python launcher.py
# Opens browser at http://localhost:8000
```
