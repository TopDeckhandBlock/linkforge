"""LinkForge — persistent Jio login sessions.

A hunt used to throw the Jio session away once the link was pulled, so an
expired link meant renting and logging in all over again. Keeping the cookies
lets us ask Jio for a fresh link later without paying for a new number.

Cookies are the whole login: ``jio_validate_otp`` writes the auth state into
the jar and nothing else carries a token, so a JSON dump of the jar is enough
to restore the session.
"""

from __future__ import annotations

import json
import sqlite3
import threading

ACTIVE = "active"
DEAD = "dead"


class SessionStore:
    """Thread-safe SQLite store of Jio login cookies, keyed by phone."""

    def __init__(self, path: str = "results.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    phone TEXT PRIMARY KEY,
                    cookies TEXT NOT NULL,
                    link TEXT,
                    link_at TEXT,
                    status TEXT DEFAULT 'active',
                    created_at TEXT DEFAULT (datetime('now')),
                    last_used_at TEXT
                )""")
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(sessions)")}
            if "link_at" not in cols:
                self.conn.execute("ALTER TABLE sessions ADD COLUMN link_at TEXT")

    def save(self, phone: str, cookies: dict, link: str | None = None) -> None:
        """Insert or replace a live login for *phone*.

        ``link_at`` is stamped only when *link* is given: a bare login has no
        link age yet, and a zeroed timestamp would read as "instantly expired".
        """
        if not phone or not cookies:
            return
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO sessions (phone, cookies, link, link_at, status, last_used_at) "
                "VALUES (?,?,?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END, "
                "'active', datetime('now')) "
                "ON CONFLICT(phone) DO UPDATE SET "
                "cookies=excluded.cookies, link=excluded.link, "
                "link_at=excluded.link_at, "
                "status='active', last_used_at=excluded.last_used_at",
                (phone, json.dumps(cookies), link, link),
            )

    def save_cookies(self, phone: str, cookies: dict) -> None:
        """Persist a rotated cookie jar and mark the session alive.

        Jio can reissue auth cookies on any call while the session is valid
        (``Set-Cookie`` alongside the response). Dropping them means the next
        restore starts from an older token than the server last handed out, so
        whoever proved the session alive owns the duty of storing the fresh jar.
        """
        if not phone or not cookies:
            return
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE sessions SET cookies=?, status='active', "
                "last_used_at=datetime('now') WHERE phone=?",
                (json.dumps(cookies), phone),
            )

    def set_link(self, phone: str, link: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE sessions SET link=?, link_at=datetime('now'), "
                "last_used_at=datetime('now') WHERE phone=?",
                (link, phone),
            )

    def mark_dead(self, phone: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE sessions SET status=?, last_used_at=datetime('now') WHERE phone=?",
                (DEAD, phone),
            )

    def touch(self, phone: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE sessions SET last_used_at=datetime('now') WHERE phone=?",
                (phone,),
            )

    def get_cookies(self, phone: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT cookies FROM sessions WHERE phone=?", (phone,)
        )
        row = cur.fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["cookies"])
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def get_link(self, phone: str) -> str | None:
        cur = self.conn.execute(
            "SELECT link FROM sessions WHERE phone=?", (phone,)
        )
        row = cur.fetchone()
        return (row["link"] or None) if row else None

    def get_link_at(self, phone: str) -> str | None:
        cur = self.conn.execute(
            "SELECT link_at FROM sessions WHERE phone=?", (phone,)
        )
        row = cur.fetchone()
        return (row["link_at"] or None) if row else None

    def get_status(self, phone: str) -> str | None:
        cur = self.conn.execute(
            "SELECT status FROM sessions WHERE phone=?", (phone,)
        )
        row = cur.fetchone()
        return row["status"] if row else None

    def phones(self, only_active: bool = True) -> list[str]:
        sql = "SELECT phone FROM sessions"
        if only_active:
            sql += " WHERE status='active'"
        sql += " ORDER BY created_at"
        return [r["phone"] for r in self.conn.execute(sql).fetchall()]

    def list(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT phone, status, link, link_at, created_at, last_used_at "
            "FROM sessions ORDER BY created_at"
        )
        return [dict(r) for r in cur.fetchall()]

    def count(self) -> dict:
        cur = self.conn.execute(
            "SELECT COUNT(*) total, "
            "SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) active, "
            "SUM(CASE WHEN link IS NOT NULL AND link != '' THEN 1 ELSE 0 END) links "
            "FROM sessions"
        )
        row = cur.fetchone()
        return {
            "total": row["total"] or 0,
            "active": row["active"] or 0,
            "links": row["links"] or 0,
        }
