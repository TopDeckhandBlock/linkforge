"""LinkForge — Rich console helpers."""

from __future__ import annotations

import sys

from rich.console import Console
from rich.theme import Theme

# Force UTF-8 on Windows so emoji don't crash with cp1252
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

THEME = Theme(
    {
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "info": "cyan",
        "highlight": "magenta",
        "link": "bold green on black",
        "phase": "bold cyan",
        "dim": "dim",
        "counter": "bold white",
    }
)

# No force_terminal: Rich must detect the real stream, otherwise piping the
# output to a file (or into another command) fills it with ANSI escape codes
# and every `links --out` style redirect becomes unreadable.
console = Console(theme=THEME, highlight=False)


def tg_send(msg: str, bot_token: str = "", chat_id: str = "") -> None:
    """Send a Telegram alert (non-blocking, best-effort)."""
    if not bot_token or not chat_id:
        return
    try:
        import requests

        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=15,
        )
    except Exception:
        pass