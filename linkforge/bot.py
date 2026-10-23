"""Telegram control bot.

Two jobs: push every freshly mined redemption link to the operator, and answer
a handful of commands from the same chat. Only ``TG_CHAT_ID`` is listened to --
messages from any other chat are dropped, so a leaked token cannot be used to
pull links out of the farm.

No dependency on ``python-telegram-bot``: the Bot API is four HTTP calls.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import requests

API = "https://api.telegram.org/bot{token}/{method}"
HELP = "/status — баланс, попытки, найдено\n/links — последние ссылки\n/balance — баланс провайдера\n/help — эта справка"


class Bot:
    def __init__(self, token: str = "", chat_id: str = "", log: Callable[..., Any] | None = None):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.log = log or (lambda *_args: None)
        self._stop = threading.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, http_timeout: int = 30, **payload: Any) -> dict | None:
        if not self.enabled:
            return None
        try:
            res = requests.post(API.format(token=self.token, method=method), json=payload, timeout=http_timeout)
            return res.json()
        except Exception as exc:
            self.log("bot error", method, type(exc).__name__)
            return None

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        res = self._call("sendMessage", chat_id=self.chat_id, text=text, disable_web_page_preview=True)
        return bool(res and res.get("ok"))

    def stop(self) -> None:
        self._stop.set()

    def _dispatch(self, handlers: dict[str, Callable[[], str | None]]) -> None:
        offset = 0
        while not self._stop.is_set() and self.enabled:
            res = self._call("getUpdates", http_timeout=40, offset=offset, timeout=25, allowed_updates=["message"])
            if not res or not res.get("ok"):
                time.sleep(3)
                continue
            for update in res.get("result") or []:
                offset = int(update.get("update_id", 0)) + 1
                message = update.get("message") or {}
                if str((message.get("chat") or {}).get("id", "")) != self.chat_id:
                    continue
                text = (message.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                name = text.split()[0][1:].split("@")[0].lower()
                handler = handlers.get(name)
                if handler is None:
                    self.send(HELP)
                    continue
                try:
                    answer = handler()
                except Exception as exc:
                    answer = "error: %s" % type(exc).__name__
                if answer:
                    self.send(str(answer))

    def start_commands(self, handlers: dict[str, Callable[[], str | None]]) -> threading.Thread | None:
        if not self.enabled:
            return None
        thread = threading.Thread(target=self._dispatch, args=(handlers,), name="bot-poll", daemon=True)
        thread.start()
        return thread
