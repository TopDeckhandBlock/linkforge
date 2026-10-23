"""Telegram transport: messages, inline keyboards, commands, callbacks.

Only ``TG_CHAT_ID`` is served -- messages and button presses from any other
chat are dropped, so a leaked token cannot be used to drive the farm.

No ``python-telegram-bot`` dependency: the Bot API is six HTTP calls.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

import requests

API = "https://api.telegram.org/bot{token}/{method}"
Row = list[dict]
Keyboard = list[Row]

Reply = str | tuple[str, Keyboard | None]


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
            self.log("bot:", method, type(exc).__name__)
            return None

    def send(self, text: str, keyboard: Keyboard | None = None) -> int | None:
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        res = self._call("sendMessage", **payload)
        if res and res.get("ok"):
            return int(res["result"]["message_id"])
        return None

    def edit(self, message_id: int, text: str, keyboard: Keyboard | None = None) -> bool:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        res = self._call("editMessageText", **payload)
        if res and res.get("ok"):
            return True
        # "message is not modified" means the view is already current.
        if res and "not modified" in str(res.get("description", "")):
            return True
        self.send(text, keyboard)
        return False

    def answer(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def stop(self) -> None:
        self._stop.set()

    # --- dispatch -------------------------------------------------------

    @staticmethod
    def _unpack(reply: Reply | None) -> tuple[str | None, Keyboard | None]:
        if isinstance(reply, tuple):
            return reply[0], reply[1]
        return reply, None

    def _update(self, update: dict, commands: dict, actions: dict) -> None:
        if "callback_query" in update:
            query = update["callback_query"] or {}
            message = query.get("message") or {}
            if str((message.get("chat") or {}).get("id", "")) != self.chat_id:
                return
            self.answer(query.get("id", ""))
            action, _, arg = (query.get("data") or "").partition(":")
            handler = actions.get(action)
            text, keyboard = self._unpack(handler(arg) if handler else "кнопка устарела")
            if text:
                self.edit(int(message.get("message_id", 0)), text, keyboard)
            return

        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id", "")) != self.chat_id:
            return
        raw = (message.get("text") or "").strip()
        if not raw.startswith("/"):
            return
        name = raw.split()[0][1:].split("@")[0].lower()
        handler = commands.get(name)
        if handler is None:
            self.send("Неизвестная команда. /menu — панель.")
            return
        try:
            reply = handler()
        except Exception as exc:
            reply = "ошибка: %s" % type(exc).__name__
        text, keyboard = self._unpack(reply)
        if text:
            self.send(text, keyboard)

    def serve(self, commands: dict[str, Callable[[], Reply | None]], actions: dict[str, Callable[[str], Reply | None]]) -> None:
        """Long-poll until ``stop()``. Safe to run in a thread."""
        offset = 0
        while not self._stop.is_set() and self.enabled:
            res = self._call(
                "getUpdates",
                http_timeout=40,
                offset=offset,
                timeout=25,
                allowed_updates=["message", "callback_query"],
            )
            if not res or not res.get("ok"):
                time.sleep(3)
                continue
            for update in res.get("result") or []:
                offset = int(update.get("update_id", 0)) + 1
                try:
                    self._update(update, commands, actions)
                except Exception as exc:
                    self.log("update error:", type(exc).__name__)

    def start_serving(self, commands: dict, actions: dict) -> threading.Thread | None:
        if not self.enabled:
            return None
        thread = threading.Thread(target=self.serve, args=(commands, actions), name="bot-poll", daemon=True)
        thread.start()
        return thread
