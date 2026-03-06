"""
database.py — Cloud SQLite store for Railway deployment
Same auth logic as local, but DB path is in /data (Railway persistent volume)
or falls back to the app directory.
"""
import sqlite3
import hashlib
import secrets
import os
from datetime import datetime, timedelta
from typing import Optional

# Railway provides /data as a persistent volume — use it if available
_data_dir  = "/data" if os.path.isdir("/data") else os.path.dirname(__file__)
DB_PATH    = os.path.join(_data_dir, "structiq_cloud.db")

SESSION_DAYS  = 30
PBKDF2_ITERS  = 260_000

# Max simultaneous sessions per plan
SESSION_LIMITS = {"free": 1, "pro": 1, "enterprise": 3}


# ─── Schema ──────────────────────────────────────────────────────

def init_db():
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                email                  TEXT    UNIQUE NOT NULL,
                name                   TEXT    NOT NULL,
                password               TEXT    NOT NULL,
                salt                   TEXT    NOT NULL,
                plan                   TEXT    NOT NULL DEFAULT 'free',
                is_active              INTEGER NOT NULL DEFAULT 1,
                created_at             TEXT    NOT NULL DEFAULT (datetime('now')),
                stripe_customer_id     TEXT,
                stripe_subscription_id TEXT,
                subscription_interval  TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                expires_at TEXT    NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS cloud_sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                session_key TEXT    NOT NULL UNIQUE,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                last_seen   TEXT    NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)
        # Migrate existing DBs — add new columns if missing
        for col_sql in [
            "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT",
            "ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT",
            "ALTER TABLE users ADD COLUMN subscription_interval TEXT",
        ]:
            try:
                c.execute(col_sql)
            except Exception:
                pass


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


# ─── Password helpers ─────────────────────────────────────────────

def _pbkdf2(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PBKDF2_ITERS
    ).hex()

def hash_password(password: str) -> tuple:
    salt = secrets.token_hex(16)
    return _pbkdf2(password, salt), salt

def verify_password(password: str, hashed: str, salt: str) -> bool:
    return secrets.compare_digest(_pbkdf2(password, salt), hashed)


# ─── User CRUD ───────────────────────────────────────────────────

def _safe(row) -> Optional[dict]:
    if row is None: return None
    d = dict(row)
    d.pop("password", None)
    d.pop("salt", None)
    return d

def create_user(email: str, name: str, password: str) -> Optional[dict]:
    pw_hash, salt = hash_password(password)
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO users (email, name, password, salt) VALUES (?,?,?,?)",
                (email.lower().strip(), name.strip(), pw_hash, salt),
            )
            return get_user_by_id(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None

def create_free_user_by_email(email: str) -> Optional[dict]:
    """Auto-create a free user with a random password (used when checkout is initiated
    before the user has synced to Railway). They will never log in via Railway directly."""
    random_pw = secrets.token_urlsafe(24)
    name = email.split("@")[0]   # use the part before @ as a display name
    return create_user(email, name, random_pw)

def get_user_by_email(email: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
        return dict(row) if row else None

def get_user_by_id(user_id: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _safe(row)

def update_user_plan(user_id: int, plan: str):
    with _conn() as c:
        c.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, user_id))

def update_user_plan_by_email(email: str, plan: str) -> bool:
    """Returns True if a user was found and updated, False otherwise."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE users SET plan = ? WHERE email = ?",
            (plan, email.lower().strip()),
        )
        return cur.rowcount > 0

def update_stripe_customer(user_id: int, customer_id: str):
    with _conn() as c:
        c.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?",
                  (customer_id, user_id))

def update_stripe_subscription(email: str, subscription_id: str, interval: str):
    with _conn() as c:
        c.execute(
            "UPDATE users SET stripe_subscription_id = ?, subscription_interval = ? WHERE email = ?",
            (subscription_id, interval, email.lower().strip()),
        )

def get_user_by_stripe_customer(customer_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)
        ).fetchone()
        return _safe(row)

def get_all_users() -> list:
    """Return all users (no passwords/salts) ordered by registration date."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, email, name, plan, is_active, created_at FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Session CRUD ────────────────────────────────────────────────

def create_session(user_id: int) -> str:
    token   = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=SESSION_DAYS)).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, expires),
        )
    return token

def get_user_by_token(token: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("""
            SELECT u.* FROM users u
            JOIN   sessions s ON u.id = s.user_id
            WHERE  s.token      = ?
              AND  s.expires_at > datetime('now')
              AND  u.is_active  = 1
        """, (token,)).fetchone()
        return _safe(row)

def delete_session(token: str):
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ─── Cloud session enforcement ────────────────────────────────────

def register_cloud_session(user_id: int, session_key: str, plan: str) -> dict:
    """
    Register a new active session for the user.
    - free / pro : max 1 → kicks the oldest session to let the new one in
    - enterprise : max 3 → rejects the 4th login (company slot full)
    Returns {"ok": True} or {"ok": False, "reason": "..."}.
    """
    limit = SESSION_LIMITS.get(plan, 1)
    # Expire stale sessions (not seen in 48 h) before checking count
    with _conn() as c:
        c.execute(
            "DELETE FROM cloud_sessions WHERE user_id = ? "
            "AND last_seen < datetime('now', '-48 hours')",
            (user_id,),
        )
        active = c.execute(
            "SELECT COUNT(*) FROM cloud_sessions WHERE user_id = ?", (user_id,)
        ).fetchone()[0]

        if active >= limit:
            if plan == "enterprise":
                return {"ok": False, "reason": "max_sessions",
                        "message": "Maximum of 3 simultaneous users reached for this Enterprise plan."}
            # pro / free: kick the oldest session so new login takes over
            c.execute(
                "DELETE FROM cloud_sessions WHERE id = ("
                "  SELECT id FROM cloud_sessions WHERE user_id = ? "
                "  ORDER BY last_seen ASC LIMIT 1)", (user_id,)
            )

        c.execute(
            "INSERT OR REPLACE INTO cloud_sessions (user_id, session_key, created_at, last_seen) "
            "VALUES (?, ?, datetime('now'), datetime('now'))",
            (user_id, session_key),
        )
    return {"ok": True}


def validate_cloud_session(session_key: str) -> bool:
    """Returns True if the session is still active; updates last_seen."""
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM cloud_sessions WHERE session_key = ?", (session_key,)
        ).fetchone()
        if row:
            c.execute(
                "UPDATE cloud_sessions SET last_seen = datetime('now') WHERE session_key = ?",
                (session_key,),
            )
            return True
    return False


def revoke_cloud_session(session_key: str):
    """Remove the session when user logs out."""
    with _conn() as c:
        c.execute("DELETE FROM cloud_sessions WHERE session_key = ?", (session_key,))
