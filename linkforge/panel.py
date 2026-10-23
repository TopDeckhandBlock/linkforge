"""Admin panel: one message, inline buttons, live farm state.

Every button answers with the same message edited in place, so the chat keeps a
single control card instead of a wall of replies.
"""

from __future__ import annotations

from typing import Any, Callable

Row = list[dict]
View = tuple[str, list[Row]]


def _btn(text: str, action: str) -> dict:
    return {"text": text, "callback_data": action}


def _kb(*rows: list[dict]) -> list[Row]:
    return [list(row) for row in rows]


class Panel:
    def __init__(self, manager: Any):
        self.manager = manager

    # --- views ----------------------------------------------------------

    def menu(self, _arg: str = "") -> View:
        state = "работает" if self.manager.running else "на паузе"
        lines = [
            "LinkForge — панель",
            "-" * 28,
            "Состояние: %s" % state,
            "Баланс: %s" % self.manager.money(),
            "Попыток: %d   Найдено: %d" % (self.manager.attempts, len(self.manager.found)),
            "Номера: %s" % self.manager.cfg.service,
        ]
        keyboard = _kb(
            [_btn("Статус", "st"), _btn("Баланс", "bal")],
            [_btn("Ссылки", "lnk"), _btn("Одна охота", "hnt")],
            [_btn("Запуск 60 мин", "run:60"), _btn("Запуск 240 мин", "run:240")],
            [_btn("Стоп", "stp"), _btn("Настройки", "cfg")],
            [_btn("Логи", "log"), _btn("Обновить", "m")],
        )
        return "\n".join(lines), keyboard

    def status(self, _arg: str = "") -> View:
        text, keyboard = self.menu()
        return self.manager.status_text() + "\n\n" + text, keyboard

    def balance(self, _arg: str = "") -> View:
        text, keyboard = self.menu()
        return "Баланс провайдера: %s\n\n%s" % (self.manager.money(), text), keyboard

    def links(self, _arg: str = "") -> View:
        text, keyboard = self.menu()
        return "Последние ссылки:\n\n%s\n\n%s" % (self.manager.last_links(5), text), keyboard

    def hunt(self, _arg: str = "") -> View:
        started = self.manager.hunt_async()
        text, keyboard = self.menu()
        return "%s\n\n%s" % (started, text), keyboard

    def start(self, arg: str = "") -> View:
        minutes = int(arg or 240)
        answer = self.manager.start(minutes=minutes)
        text, keyboard = self.menu()
        return "%s\n\n%s" % (answer, text), keyboard

    def stop(self, _arg: str = "") -> View:
        answer = self.manager.stop()
        text, keyboard = self.menu()
        return "%s\n\n%s" % (answer, text), keyboard

    def settings(self, _arg: str = "") -> View:
        proxy = "включены" if self.manager.cfg.proxy_enabled else "выключены"
        lines = [
            "Настройки",
            "-" * 28,
            "Провайдер: %s" % self.manager.cfg.provider,
            "Очередь номеров: %s" % self.manager.cfg.service,
            "Макс. цена: $%.2f" % self.manager.cfg.max_price,
            "Таймаут OTP: %d с" % self.manager.cfg.otp_timeout,
            "Параллельность: %d" % self.manager.cfg.concurrency,
            "Прокси: %s" % proxy,
            "Чат: %s" % (self.manager.cfg.tg_chat_id or "-"),
            "База: %s" % self.manager.cfg.db_path,
            "Ссылки: %s" % self.manager.out,
            "",
            "Правки прокси применяются сразу, остальное — в .env и перезапуск.",
        ]
        keyboard = _kb(
            [_btn("Прокси: вкл", "px:1"), _btn("Прокси: выкл", "px:0")],
            [_btn("Назад", "m")],
        )
        return "\n".join(lines), keyboard

    def proxy_on(self, _arg: str = "") -> View:
        return self._proxy(True)

    def proxy_off(self, _arg: str = "") -> View:
        return self._proxy(False)

    def proxy_by_arg(self, arg: str = "1") -> View:
        return self._proxy(arg.strip() == "1")

    def _proxy(self, enabled: bool) -> View:
        answer = self.manager.set_proxy(enabled)
        text, keyboard = self.settings()
        return "%s\n\n%s" % (answer, text), keyboard

    def logs(self, _arg: str = "") -> View:
        tail = self.manager.logs_tail(18)
        keyboard = _kb([_btn("Обновить", "log"), _btn("Назад", "m")])
        return "Логи (последние %d):\n\n%s" % (len(tail.splitlines()), tail), keyboard

    # --- routing --------------------------------------------------------

    def actions(self) -> dict[str, Callable[[str], View]]:
        return {
            "m": self.menu,
            "st": self.status,
            "bal": self.balance,
            "lnk": self.links,
            "hnt": self.hunt,
            "run": self.start,
            "stp": self.stop,
            "cfg": self.settings,
            "log": self.logs,
            "px": self.proxy_by_arg,
        }

    def commands(self) -> dict[str, Callable[[], View | str]]:
        return {
            "start": self.menu,
            "menu": self.menu,
            "status": self.status,
            "balance": self.balance,
            "links": self.links,
            "hunt": self.hunt,
            "settings": self.settings,
            "logs": self.logs,
            "run": lambda: self.start("240"),
            "stop": self.stop,
            "help": self.help_text,
        }

    def help_text(self) -> str:
        return (
            "Команды: /menu /status /balance /links /hunt /run /stop /settings /logs\n"
            "Запуск по кнопке — 60 или 240 минут; /run — 240 минут.\n"
            "Ссылки приходят отдельным сообщением сразу после добычи."
        )
