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


# ─── Schema ──────────────────────────────────────────────────────

def init_db():
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT    UNIQUE NOT NULL,
                name       TEXT    NOT NULL,
                password   TEXT    NOT NULL,
                salt       TEXT    NOT NULL,
                plan       TEXT    NOT NULL DEFAULT 'free',
                is_active  INTEGER NOT NULL DEFAULT 1,
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                expires_at TEXT    NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)


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
