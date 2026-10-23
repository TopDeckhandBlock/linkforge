"""LinkForge — SQLite storage layer."""

from __future__ import annotations

import sqlite3
import threading


class Store:
    """Thread-safe SQLite store for hunt results."""

    def __init__(self, path: str = "results.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS hunts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    activation_id TEXT,
                    otp TEXT,
                    logged_in INTEGER DEFAULT 0,
                    link TEXT,
                    error TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )""")
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(hunts)")}
            if "error" not in cols:
                self.conn.execute("ALTER TABLE hunts ADD COLUMN error TEXT")

    def save(
        self,
        phone: str,
        act_id: str | None,
        otp: str | None,
        logged_in: bool,
        link: str | None,
        error: str | None = None,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO hunts (phone, activation_id, otp, logged_in, link, error) "
                "VALUES (?,?,?,?,?,?)",
                (phone, act_id, otp, int(logged_in), link, error),
            )

    def all_links(self) -> list[str]:
        cur = self.conn.execute(
            "SELECT link FROM hunts WHERE link IS NOT NULL AND link != '' "
            "ORDER BY id DESC"
        )
        return [r["link"] for r in cur.fetchall()]

    def hunt_links(self) -> list[dict]:
        """Ссылки из hunts с метаданными (телефон, время) — для команды check."""
        cur = self.conn.execute(
            "SELECT phone, link, created_at FROM hunts "
            "WHERE link IS NOT NULL AND link != '' ORDER BY id DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def stats(self) -> dict:
        cur = self.conn.execute(
            "SELECT COUNT(*) n, SUM(logged_in) logins, "
            "SUM(CASE WHEN link IS NOT NULL AND link != '' THEN 1 ELSE 0 END) links "
            "FROM hunts"
        )
        row = cur.fetchone()
        err_cur = self.conn.execute(
            "SELECT error, COUNT(*) n FROM hunts "
            "WHERE error IS NOT NULL AND error != '' "
            "GROUP BY error ORDER BY n DESC"
        )
        errors = {r["error"]: r["n"] for r in err_cur.fetchall()}
        return {
            "hunts": row["n"] or 0,
            "logins": row["logins"] or 0,
            "links": row["links"] or 0,
            "errors": errors,
        }