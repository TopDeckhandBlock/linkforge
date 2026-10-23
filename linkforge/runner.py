"""Farm loop and its supervisor.

One hunt at a time, sequential on purpose: the expensive step is renting a
number and the yielding step is the Jio OTP, and both are rate limited
globally -- parallel workers mostly burn numbers, not time.

Every mined link is appended to ``links/REDEEM.txt`` and pushed to Telegram
before anything else runs, so a crash later cannot lose it.

``FarmManager`` is the handle the bot panel drives: start/stop the loop, run a
single hunt, toggle the proxy pool, read state.
"""

from __future__ import annotations

import collections
import os
import threading
import time
from typing import Any, Callable

from .bot import Bot
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


def _pause(seconds: float, stop: threading.Event | None) -> bool:
    """Sleep unless asked to stop. Returns False when the wait was cut short."""
    end = time.time() + seconds
    while time.time() < end:
        if stop is not None and stop.is_set():
            return False
        time.sleep(0.2)
    return True


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

    def last_links(self, limit: int = 5) -> str:
        if not os.path.exists(self.out):
            return "Ссылок пока нет."
        with open(self.out, encoding="utf-8") as fh:
            rows = [line.strip() for line in fh if line.strip()]
        return "\n\n".join(rows[-limit:]) or "Ссылок пока нет."

    def loop(self, minutes: float = 240, target: int | None = None, stop: threading.Event | None = None) -> list[str]:
        if self.bot.enabled:
            self.bot.send("Ферма запущена на %d мин. Ссылок в базе: %d." % (minutes, len(self.seen)))
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            if stop is not None and stop.is_set():
                self.log("stop requested")
                break
            if target and len(self.found) >= target:
                self.log("target reached:", target)
                break
            try:
                money = self.balance()
            except Exception as exc:
                self.log("balance error:", type(exc).__name__)
                if not _pause(20, stop):
                    break
                continue
            if money < MIN_BALANCE:
                self.log("balance %.2f — waiting for refunds" % money)
                if not _pause(30, stop):
                    break
                continue
            began = time.time()
            link = self.hunt()
            took = int(time.time() - began)
            if self.deliver(link):
                self.log("link #%d mined in %ds" % (len(self.found), took))
            else:
                self.log("attempt %d: nothing in %ds" % (self.attempts, took))
                if not _pause(RETRY_PAUSE, stop):
                    break
        if self.bot.enabled:
            self.bot.send("Ферма остановлена. Найдено за запуск: %d." % len(self.found))
        return self.found


class FarmManager:
    """Handle the bot panel drives: lifecycle, one-off hunt, proxy switch, state."""

    def __init__(self, cfg: Config, log: Callable[..., Any] = print, telegram: bool = True):
        self._sink = log
        self._lines: collections.deque[str] = collections.deque(maxlen=400)
        self.cfg = cfg
        self.farm = Farm(cfg, log=self.log, telegram=telegram)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._hunt_lock = threading.Lock()

    # --- logging --------------------------------------------------------

    def log(self, *parts: Any) -> None:
        line = " ".join(str(part) for part in parts)
        self._lines.append("%s %s" % (time.strftime("%H:%M:%S"), line))
        try:
            self._sink(line)
        except Exception:
            pass

    def logs_tail(self, limit: int = 18) -> str:
        return "\n".join(list(self._lines)[-limit:]) or "пусто"

    # --- state ----------------------------------------------------------

    @property
    def attempts(self) -> int:
        return self.farm.attempts

    @property
    def found(self) -> list[str]:
        return self.farm.found

    @property
    def out(self) -> str:
        return self.farm.out

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def money(self) -> str:
        try:
            return "$%.2f" % self.farm.balance()
        except Exception as exc:
            return "н/д (%s)" % type(exc).__name__

    # --- lifecycle ------------------------------------------------------

    def start(self, minutes: float = 240, target: int | None = None) -> str:
        if self.running:
            return "Ферма уже работает."
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(minutes, target), name="farm", daemon=True)
        self._thread.start()
        return "Запуск: %d мин." % minutes

    def _run(self, minutes: float, target: int | None) -> None:
        try:
            self.farm.loop(minutes=minutes, target=target, stop=self._stop)
        except Exception as exc:
            self.log("farm crashed:", type(exc).__name__, str(exc)[:200])

    def stop(self) -> str:
        if not self.running:
            return "Ферма уже стоит."
        self._stop.set()
        return "Останавливаю после текущей попытки."

    def wait(self) -> None:
        if self._thread:
            self._thread.join()

    def hunt_async(self) -> str:
        if self.running:
            return "Ферма работает — охота идёт сама."
        if self._hunt_lock.locked():
            return "Охота уже идёт."
        threading.Thread(target=self._hunt, name="hunt", daemon=True).start()
        return "Охота запущена, результат придёт сообщением."

    def _hunt(self) -> None:
        with self._hunt_lock:
            link = self.farm.hunt()
            if not self.farm.deliver(link):
                self.log("attempt %d: nothing" % self.farm.attempts)
                self.farm.bot.send("Охота: ссылки нет — номер не подошёл или пул пуст.")

    def set_proxy(self, enabled: bool) -> str:
        self.cfg.proxy_enabled = bool(enabled)
        install_pool(self.cfg, lambda msg: self.log("proxy:", msg))
        return "Прокси включены." if enabled else "Прокси выключены."

    # --- text -----------------------------------------------------------

    def status_text(self) -> str:
        uptime = int((time.time() - self.farm.started) / 60)
        try:
            money = "$%.2f" % self.farm.balance()
        except Exception as exc:
            money = "н/д (%s)" % type(exc).__name__
        return "\n".join(
            [
                "Состояние: %s" % ("работает" if self.running else "на паузе"),
                "Баланс: %s" % money,
                "Попыток: %d" % self.farm.attempts,
                "Найдено за запуск: %d" % len(self.farm.found),
                "Всего в базе: %d" % len(self.farm.seen),
                "Аптайм процесса: %d мин" % uptime,
                "Файл: %s" % self.farm.out,
                "Прокси: %s" % ("включены" if self.cfg.proxy_enabled else "выключены"),
            ]
        )

    def last_links(self, limit: int = 5) -> str:
        return self.farm.last_links(limit)


def run(minutes: float = 240, target: int = 0, telegram: bool = True, log: Callable[..., Any] = print) -> list[str]:
    """Blocking farm run (no panel)."""
    cfg = load_config(require_credentials=True)
    manager = FarmManager(cfg, log=log, telegram=telegram)
    log(manager.start(minutes=minutes, target=target or None))
    manager.wait()
    return manager.found
