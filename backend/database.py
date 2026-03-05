"""
database.py — SQLite user store for StructIQ
No external dependencies: uses only Python stdlib (sqlite3, hashlib, secrets).
"""
import sqlite3
import hashlib
import secrets
import os
from datetime import datetime, timedelta
from typing import Optional

DB_PATH       = os.path.join(os.path.dirname(__file__), "structiq.db")
SESSION_DAYS  = 30
PBKDF2_ITERS  = 260_000   # OWASP 2023 recommendation for PBKDF2-HMAC-SHA256


# ─────────────────────────────────────────────────────────────────────
#  Schema
# ─────────────────────────────────────────────────────────────────────

def init_db():
    """Create tables if they don't exist (safe to call on every startup)."""
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                email            TEXT    UNIQUE NOT NULL,
                name             TEXT    NOT NULL,
                password         TEXT    NOT NULL,
                salt             TEXT    NOT NULL,
                plan             TEXT    NOT NULL DEFAULT 'free',
                is_active        INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
                cloud_token      TEXT,
                last_cloud_sync  TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                expires_at TEXT    NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)
        # Migrate existing DBs — add new columns if they don't exist yet
        for col_sql in [
            "ALTER TABLE users ADD COLUMN cloud_token TEXT",
            "ALTER TABLE users ADD COLUMN last_cloud_sync TEXT",
        ]:
            try:
                c.execute(col_sql)
            except Exception:
                pass  # Column already exists — safe to ignore


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


# ─────────────────────────────────────────────────────────────────────
#  Password helpers
# ─────────────────────────────────────────────────────────────────────

def _pbkdf2(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERS
    ).hex()


def hash_password(password: str) -> tuple:
    """Returns (hashed_hex, salt_hex)."""
    salt = secrets.token_hex(16)
    return _pbkdf2(password, salt), salt


def verify_password(password: str, hashed: str, salt: str) -> bool:
    return secrets.compare_digest(_pbkdf2(password, salt), hashed)


# ─────────────────────────────────────────────────────────────────────
#  User CRUD
# ─────────────────────────────────────────────────────────────────────

def _safe(row) -> Optional[dict]:
    """Return row as dict with sensitive fields removed, or None."""
    if row is None:
        return None
    d = dict(row)
    d.pop("password", None)
    d.pop("salt", None)
    return d


def create_user(email: str, name: str, password: str) -> Optional[dict]:
    """
    Create a new user.  Returns the public user dict, or None if the
    email is already taken.
    """
    pw_hash, salt = hash_password(password)
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO users (email, name, password, salt) VALUES (?, ?, ?, ?)",
                (email.lower().strip(), name.strip(), pw_hash, salt),
            )
            return get_user_by_id(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def get_user_by_email(email: str) -> Optional[dict]:
    """Returns full row (including password/salt) for auth checks."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    """Returns public user dict (no password/salt)."""
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _safe(row)


# ─────────────────────────────────────────────────────────────────────
#  Session CRUD
# ─────────────────────────────────────────────────────────────────────

def create_session(user_id: int) -> str:
    token    = secrets.token_urlsafe(32)
    expires  = (datetime.utcnow() + timedelta(days=SESSION_DAYS)).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires),
        )
    return token


def get_user_by_token(token: str) -> Optional[dict]:
    """Returns public user dict for a valid, unexpired token; None otherwise."""
    with _conn() as c:
        row = c.execute(
            """
            SELECT u.*
            FROM   users u
            JOIN   sessions s ON u.id = s.user_id
            WHERE  s.token      = ?
              AND  s.expires_at > datetime('now')
              AND  u.is_active  = 1
            """,
            (token,),
        ).fetchone()
        return _safe(row)


def delete_session(token: str):
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions():
    """Optional cleanup — call periodically if needed."""
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")


# ─────────────────────────────────────────────────────────────────────
#  Cloud sync helpers
# ─────────────────────────────────────────────────────────────────────

def update_user_plan(user_id: int, plan: str):
    """Update a user's subscription plan (used by cloud sync)."""
    with _conn() as c:
        c.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, user_id))


def update_cloud_token(user_id: int, cloud_token: str):
    """Store the Railway cloud session token for later syncing."""
    with _conn() as c:
        c.execute(
            "UPDATE users SET cloud_token = ? WHERE id = ?",
            (cloud_token, user_id),
        )


def update_last_cloud_sync(user_id: int, plan: str):
    """Update plan from cloud and record the sync timestamp."""
    with _conn() as c:
        c.execute(
            "UPDATE users SET plan = ?, last_cloud_sync = datetime('now') WHERE id = ?",
            (plan, user_id),
        )


def get_cloud_token(user_id: int) -> Optional[str]:
    """Return the stored Railway cloud token for this user, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT cloud_token FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row["cloud_token"] if row else None
