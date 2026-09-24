"""Account identities, password recovery, and encrypted exchange credentials."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet

from auth import hash_password, verify_password


class Accounts:
    def __init__(self, path: Path, master_key: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cipher = Fernet(master_key.encode())
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS verification (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    code_hash TEXT NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS recovery (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    code_hash TEXT NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    encrypted BLOB NOT NULL
                );
            """)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def register(self, username: str, email: str, password: str, invitation_code: str, expected_code: str):
        if not all(isinstance(value, str) for value in (username, email, password, invitation_code, expected_code)):
            raise ValueError("Invalid registration request")
        if not expected_code or not hmac.compare_digest(invitation_code, expected_code):
            raise PermissionError("Invalid invitation code")
        username = username.strip()
        email = email.strip().lower()
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", username):
            raise ValueError("Username must be 3–40 letters, digits, underscores or hyphens")
        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", email) or len(email) > 254:
            raise ValueError("Invalid email address")
        encoded = hash_password(password)
        user_id = secrets.token_hex(16)
        try:
            with self.connect() as conn:
                conn.execute("DELETE FROM users WHERE verified=0 AND id IN "
                             "(SELECT user_id FROM verification WHERE expires_at < ?)", (time.time(),))
                conn.execute("DELETE FROM users WHERE verified=0 AND username=? AND email=?",
                             (username, email))
                conn.execute("INSERT INTO users(id,username,email,password_hash) VALUES(?,?,?,?)",
                             (user_id, username, email, encoded))
        except sqlite3.IntegrityError as exc:
            raise ValueError("Username or email already registered") from exc
        return {"id": user_id, "username": username, "email": email}

    def issue_verification(self, user_id: str):
        code = f"{secrets.randbelow(100000000):08d}"
        with self.connect() as conn:
            conn.execute("INSERT INTO verification(user_id,code_hash,expires_at,attempts) VALUES(?,?,?,0) "
                         "ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash,"
                         "expires_at=excluded.expires_at,attempts=0",
                         (user_id, hashlib.sha256(code.encode()).hexdigest(), time.time() + 600))
        return code

    def verify_email(self, email: str, code: str):
        user = self.by_email(email)
        if not user or not isinstance(code, str) or not re.fullmatch(r"\d{8}", code):
            return False
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT code_hash,expires_at,attempts FROM verification WHERE user_id=?",
                               (user["id"],)).fetchone()
            if not row or row[1] < time.time() or row[2] >= 5:
                return False
            conn.execute("UPDATE verification SET attempts=attempts+1 WHERE user_id=?", (user["id"],))
            if not hmac.compare_digest(row[0], hashlib.sha256(code.encode()).hexdigest()):
                return False
            conn.execute("UPDATE users SET verified=1 WHERE id=?", (user["id"],))
            conn.execute("DELETE FROM verification WHERE user_id=?", (user["id"],))
        return True

    def login(self, username: str, password: str):
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT id,username,email,password_hash,verified FROM users WHERE username=?", (username,)).fetchone()
        # Spend comparable work for unknown accounts.
        encoded = row[3] if row else _DUMMY_HASH
        valid = verify_password(password, encoded)
        if not row or not valid or not row[4]:
            return None
        return {"id": row[0], "username": row[1], "email": row[2]}

    def by_email(self, email: str):
        if not isinstance(email, str):
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT id,username,email,verified FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        return {"id": row[0], "username": row[1], "email": row[2], "verified": bool(row[3])} if row else None

    def by_username(self, username: str):
        with self.connect() as conn:
            row = conn.execute("SELECT id,username,email,verified FROM users WHERE username=?", (username,)).fetchone()
        return {"id": row[0], "username": row[1], "email": row[2], "verified": bool(row[3])} if row else None

    def issue_recovery(self, user_id: str):
        code = f"{secrets.randbelow(100000000):08d}"
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.connect() as conn:
            conn.execute("INSERT INTO recovery(user_id,code_hash,expires_at,attempts) VALUES(?,?,?,0) "
                         "ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash,"
                         "expires_at=excluded.expires_at,attempts=0", (user_id, digest, time.time() + 600))
        return code

    def reset_password(self, email: str, code: str, password: str):
        user = self.by_email(email)
        if not user or not user["verified"] or not isinstance(code, str) or not re.fullmatch(r"\d{8}", code):
            return False
        encoded = hash_password(password)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT code_hash,expires_at,attempts FROM recovery WHERE user_id=?", (user["id"],)).fetchone()
            if not row or row[1] < time.time() or row[2] >= 5:
                return False
            conn.execute("UPDATE recovery SET attempts=attempts+1 WHERE user_id=?", (user["id"],))
            if not hmac.compare_digest(row[0], hashlib.sha256(code.encode()).hexdigest()):
                return False
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (encoded, user["id"]))
            conn.execute("DELETE FROM recovery WHERE user_id=?", (user["id"],))
        return True

    def credentials(self, user_id: str):
        with self.connect() as conn:
            row = conn.execute("SELECT encrypted FROM credentials WHERE user_id=?", (user_id,)).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else {}

    def set_credentials(self, user_id: str, values: dict):
        encrypted = self.cipher.encrypt(json.dumps(values).encode())
        with self.connect() as conn:
            conn.execute("INSERT INTO credentials(user_id,encrypted) VALUES(?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET encrypted=excluded.encrypted", (user_id, encrypted))


_DUMMY_HASH = "scrypt$16384$8$1$00000000000000000000000000000000$" + "0" * 64
