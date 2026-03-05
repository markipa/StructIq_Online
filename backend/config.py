"""
config.py — StructIQ local app configuration
Update CLOUD_URL once you deploy to Railway.
"""
import os

# ── Cloud auth server URL ─────────────────────────────────────────
# Set this to your Railway URL after deploying.
# Example: "https://structiq-production.up.railway.app"
# Leave as empty string to use LOCAL auth only (offline / dev mode)
CLOUD_URL = os.environ.get("STRUCTIQ_CLOUD_URL", "https://structiq-production.up.railway.app")

# ── App info ──────────────────────────────────────────────────────
APP_NAME    = "StructIQ"
APP_VERSION = "1.0.0"

# ── Offline grace period ──────────────────────────────────────────
# How many days the app works without reaching the cloud server
# (in case engineer has no internet temporarily)
OFFLINE_GRACE_DAYS = 3
