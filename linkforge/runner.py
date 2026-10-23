"""Farm loop: rent a number, log in, mine a link, deliver it. Repeat.

One hunt at a time, sequential on purpose: the expensive step is renting a
number and the yielding step is the Jio OTP, and both are rate limited
globally -- parallel workers mostly burn numbers, not time.

Every mined link is appended to ``links/REDEEM.txt`` and pushed to Telegram
before anything else runs, so a crash later cannot lose it.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from .bot import HELP, Bot
from .config import Config, load_config
from .hunter import WorkerState, create_provider, hunt_once
from .jio.relink import extract_token
from .proxy import install as install_proxy
from .refund import RefundWorker
from .shield import install_doh_shield
from .storage.sessions import SessionStore
from .storage.store import Store

MIN_BALANCE = 0.20
RETRY_PAUSE = 6.0


def install_pool(cfg: Config, log: Callable[..., Any]) -> None:
    install_proxy(
        enabled=cfg.proxy_enabled,
        api_key=cfg.proxygrab_key,
        base=cfg.proxygrab_url,
        source=cfg.proxy_source,
        file_path=cfg.proxy_file,
        size=cfg.proxy_pool_size,
        proto=cfg.proxy_proto,
        country=cfg.proxy_country,
        anon=cfg.proxy_anon,
        test_url=cfg.proxy_test_url,
        rotate_seconds=cfg.proxy_rotate_seconds,
        verify=cfg.proxy_verify,
        verify_timeout=cfg.proxy_verify_timeout,
        session_proxy=cfg.proxy_sessions,
        log=log,
    )


class Farm:
    """Owns every long-lived object the loop needs."""

    def __init__(self, cfg: Config, log: Callable[..., Any] = print, telegram: bool = True):
        self.cfg = cfg
        self.log = log
        install_doh_shield()
        install_pool(cfg, lambda msg: log("proxy:", msg))
        self.provider = create_provider(cfg)
        self.store = Store(cfg.db_path)
        self.sessions = SessionStore(cfg.db_path)
        self.refunds = RefundWorker(self.provider, log=lambda *_args: None)
        os.makedirs(cfg.links_dir, exist_ok=True)
        self.out = os.path.join(cfg.links_dir, "REDEEM.txt")
        self.bot = Bot(cfg.tg_bot_token, cfg.tg_chat_id, log=log) if telegram else Bot("", "")
        self.seen = self._known()
        self.attempts = 0
        self.found: list[str] = []
        self.started = time.time()

    def _known(self) -> set[str]:
        keys = {extract_token(link) or link for link in self.store.all_links()}
        if os.path.exists(self.out):
            with open(self.out, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        keys.add(extract_token(line) or line)
        return keys

    def balance(self) -> float:
        return float(self.provider.balance())

    def hunt(self) -> str | None:
        self.attempts += 1
        self.cfg.run_file = os.path.join(self.cfg.links_dir, "run-%03d.txt" % self.attempts)
        state = WorkerState(wid=1)
        try:
            return hunt_once(
                self.provider,
                self.store,
                self.refunds,
                threading.Semaphore(1),
                self.cfg,
                state,
                sessions=self.sessions,
            )
        except Exception as exc:
            self.log("hunt error:", type(exc).__name__, str(exc)[:160])
            return None

    def deliver(self, link: str | None) -> bool:
        if not link:
            return False
        key = extract_token(link) or link
        if key in self.seen:
            self.log("skip duplicate", link[:48])
            return False
        self.seen.add(key)
        with open(self.out, "a", encoding="utf-8") as fh:
            fh.write(link + "\n")
        self.found.append(link)
        self.bot.send("Google AI Pro — свежая ссылка:\n%s\n\nОдноразовая, живёт ~30–60 минут." % link)
        return True

    def status(self) -> str:
        try:
            money = "$%.2f" % self.balance()
        except Exception as exc:
            money = "н/д (%s)" % type(exc).__name__
        uptime = int((time.time() - self.started) / 60)
        return (
            "Баланс: %s\nПопыток: %d\nНайдено: %d\nВ работе: %d мин\nФайл: %s"
            % (money, self.attempts, len(self.found), uptime, self.out)
        )

    def last_links(self, limit: int = 5) -> str:
        if not os.path.exists(self.out):
            return "Ссылок пока нет."
        with open(self.out, encoding="utf-8") as fh:
            rows = [line.strip() for line in fh if line.strip()]
        return "\n\n".join(rows[-limit:]) or "Ссылок пока нет."

    def handlers(self) -> dict[str, Callable[[], str | None]]:
        def no_money() -> str:
            return "Баланс: $%.2f" % self.balance()

        return {
            "start": lambda: "Ферма на связи.\n\n" + HELP,
            "help": lambda: HELP,
            "status": self.status,
            "links": lambda: self.last_links(5),
            "balance": no_money,
        }

    def loop(self, minutes: float = 240, target: int | None = None) -> list[str]:
        self.bot.start_commands(self.handlers())
        if self.bot.enabled:
            self.bot.send("Ферма запущена на %d мин. Ссылок в базе: %d." % (minutes, len(self.seen)))
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            if target and len(self.found) >= target:
                self.log("target reached:", target)
                break
            try:
                money = self.balance()
            except Exception as exc:
                self.log("balance error:", type(exc).__name__)
                time.sleep(20)
                continue
            if money < MIN_BALANCE:
                self.log("balance %.2f — waiting for refunds" % money)
                time.sleep(30)
                continue
            started = time.time()
            link = self.hunt()
            took = int(time.time() - started)
            if self.deliver(link):
                self.log("link #%d mined in %ds" % (len(self.found), took))
            else:
                self.log("attempt %d: nothing in %ds" % (self.attempts, took))
                time.sleep(RETRY_PAUSE)
        if self.bot.enabled:
            self.bot.send("Ферма остановлена. Найдено за запуск: %d." % len(self.found))
        return self.found


def run(minutes: float = 240, target: int = 0, telegram: bool = True, log: Callable[..., Any] = print) -> list[str]:
    cfg = load_config(require_credentials=True)
    farm = Farm(cfg, log=log, telegram=telegram)
    return farm.loop(minutes=minutes, target=target or None)
