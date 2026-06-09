"""
database.py — Zuhal Teyze kullanıcı ve credit yönetimi
"""

import sqlite3
import os
import secrets
import datetime

DB_PATH = os.environ.get("DB_PATH", "zuhal_teyze.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            credits INTEGER DEFAULT 3
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS magic_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            used INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_token TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS question_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            question TEXT,
            asked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── LAB TABLOLARI ──────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS lab_sessions (
            token TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS lab_feedback (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT,
            question      TEXT,
            chart_data    TEXT,
            system_prompt TEXT,
            output        TEXT,
            rating        INTEGER DEFAULT 0,
            tags          TEXT DEFAULT '[]',
            note          TEXT DEFAULT ''
        )
    """)

    # ── KULLANICI REVIEW QUEUE ─────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS review_requests (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            submitted_at     TEXT,
            user_id          INTEGER,
            question         TEXT,
            output           TEXT,
            chart_data       TEXT DEFAULT '',
            status           TEXT DEFAULT 'pending',
            astrologer_note  TEXT DEFAULT ''
        )
    """)

    conn.commit()
    conn.close()
    print("[DB] Tablolar hazır.")


def get_or_create_user(email: str) -> dict:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE email = ?", (email,))
    user = c.fetchone()
    if not user:
        c.execute("INSERT INTO users (email, credits) VALUES (?, 3)", (email,))
        conn.commit()
        c.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = c.fetchone()
    conn.close()
    return dict(user)


def create_magic_token(email: str) -> str:
    conn = get_db()
    c = conn.cursor()
    # Eski tokenları temizle
    c.execute("DELETE FROM magic_tokens WHERE email = ? OR created_at < datetime('now', '-1 hour')", (email,))
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO magic_tokens (email, token) VALUES (?, ?)", (email, token))
    conn.commit()
    conn.close()
    return token


def verify_magic_token(token: str) -> str | None:
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT email FROM magic_tokens
        WHERE token = ?
        AND used = 0
        AND created_at > datetime('now', '-1 hour')
    """, (token,))
    row = c.fetchone()
    if row:
        c.execute("UPDATE magic_tokens SET used = 1 WHERE token = ?", (token,))
        conn.commit()
        conn.close()
        return row["email"]
    conn.close()
    return None


def create_session(user_id: int) -> str:
    conn = get_db()
    c = conn.cursor()
    session_token = secrets.token_urlsafe(48)
    expires_at = (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat()
    c.execute("""
        INSERT INTO sessions (user_id, session_token, expires_at)
        VALUES (?, ?, ?)
    """, (user_id, session_token, expires_at))
    conn.commit()
    conn.close()
    return session_token


def get_user_by_session(session_token: str) -> dict | None:
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT u.* FROM users u
        JOIN sessions s ON s.user_id = u.id
        WHERE s.session_token = ?
        AND s.expires_at > datetime('now')
    """, (session_token,))
    user = c.fetchone()
    conn.close()
    return dict(user) if user else None


def use_credit(user_id: int) -> bool:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    if not row or row["credits"] <= 0:
        conn.close()
        return False
    c.execute("UPDATE users SET credits = credits - 1 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True


def log_question(user_id: int, question: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO question_log (user_id, question) VALUES (?, ?)", (user_id, question))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("DB başlatıldı.")
