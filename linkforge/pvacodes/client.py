"""LinkForge — PVACodes client (beta.pvacodes.com session API).

The key-based API (``app/stacked`` and ``app/api.php``) does not expose the
Jio50 service — both forms answer "App or Country not Found" for it. The web
session endpoints are the only path that can rent Jio50, so this client logs
into the site the same way a browser does (form login + per-session CSRF) and
then drives the same AJAX actions the dashboard uses.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass

import requests

from linkforge.sms import LowBalance

BASE = "https://beta.pvacodes.com/app"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

RETRY_AFTER_EXPIRY = 2


class PvaError(Exception):
    """Base error for the PVACodes client."""


class PvaAuthError(PvaError):
    """Login to the site failed (bad credentials or blocked session)."""


class PvaLowBalance(PvaError, LowBalance):
    """pvacodes refused the rent: not enough credits on the account (short
    message by design — it is shown to the farmer as a plain status line)."""


class PvaLockedError(PvaError):
    """pvacodes refuses the cancel while the number is still inside its lock
    window. ``retry_after`` is the number of seconds the site itself asked us
    to wait — the refund worker reschedules on it instead of guessing."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


_CSRF_INPUT = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
_CSRF_META = re.compile(r'name="csrf-token"\s+content="([^"]+)"')
_BALANCE = re.compile(r'id="balance2"[^>]*>\s*\$?\s*([0-9]+(?:\.[0-9]+)?)')
_ROW_NAME = re.compile(r'<div class="flex-grow-1[^"]*">([^<]+)')
_ROW_NAME_LOOSE = re.compile(r"<div[^>]*>\s*([A-Za-z0-9][\w.\- ]{1,40}?)\s*<")
_ROW_PRICE = re.compile(r'<div class="badge[^"]*">\s*\$?\s*([0-9]+(?:\.[0-9]+)?)\s*</div>')
_ROW_APP = re.compile(r"^\s*'(\d+)'\s*,\s*'([0-9a-fA-F]+)'\s*,\s*(\d+)")
_CARD_PHONE = re.compile(r'<span id="data_(\d+)">([^<]*)</span>')
_CARD_CODE = re.compile(r'<span id="data2_\d+_\d+">([^<]*)</span>')
_CARD_STATUS = re.compile(r'class="status-badge\s+([a-z-]+)"')
_CARD_SPAN = re.compile(r"<span>([^<]+)</span>")
_DIV_TAG = re.compile(r"<div\b[^>]*>")
_DATA_ID = re.compile(r'data-id="(\d+)"')
_SELECT_LABEL = re.compile(r'aria-label="Select ([^"]+)"')
_ALERT = re.compile(r'class="alert[^"]*"[^>]*>(?:\s*<[^>]+>)*\s*([^<]{4,200})')
# "please wait 43 more seconds before cancelling" / "tunggu 43 detik"
_LOCK_WAIT = re.compile(r"(\d+)\s*(?:more\s+)?(?:seconds?|secs?|detik)", re.IGNORECASE)
_LOCK_HINTS = ("wait", "tunggu", "please", "lock", "belum", "masih", "nanti")


def _json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def _status_code(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    status = payload.get("status")
    if not isinstance(status, dict):
        return ""
    return str(status.get("code", ""))


def _status_msg(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    status = payload.get("status")
    if not isinstance(status, dict):
        return ""
    return str(status.get("message") or "").strip()


@dataclass(frozen=True)
class App:
    state_id: str
    app_id: str
    name: str
    price: float


@dataclass(frozen=True)
class NumberCard:
    id: str
    phone: str
    country: str
    app: str
    status: str
    code: str

    @property
    def cancelled(self) -> bool:
        return any(k in self.status for k in ("cancel", "refund", "failed"))


class PVACodes:
    """Thread-safe PVACodes provider implementing the linkforge SMSProvider."""

    MIN_GAP = 0.5
    BALANCE_TTL = 5.0
    APP_TTL = 300.0

    def __init__(
        self,
        email: str,
        password: str,
        country: str = "India",
        services: str = "Jio50",
        session_id: str = "",
    ) -> None:
        self.email = email
        self.password = password
        self.country = country.strip() or "India"
        self.services = [s.strip() for s in (services or "").split(",") if s.strip()]
        if not self.services:
            raise PvaError("PVACODES_SERVICE пуст — укажите хотя бы одно приложение")

        self._s = requests.Session()
        self._s.headers.update(
            {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
        )
        self._io = threading.Lock()
        self._rent_lock = threading.Lock()
        self._throttle_lock = threading.Lock()
        self._last_call = 0.0

        self._csrf = ""
        self._state_id = ""
        self._apps: dict[str, tuple[App, float]] = {}
        self._balance: tuple[float, float] = (0.0, 0.0)
        self._miss: dict[str, int] = {}
        self._session_id = (session_id or "").strip()

        if self._session_id:
            self._s.cookies.set(
                "SECURE_SESSION_ID",
                self._session_id,
                domain="beta.pvacodes.com",
                path="/",
                secure=True,
            )
            self._login_with_cookie()
        else:
            self._login()

    # ------------------------------------------------------------------ http

    def _throttle(self) -> None:
        with self._throttle_lock:
            wait = self.MIN_GAP - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()

    @staticmethod
    def _url(page: str) -> str:
        return f"{BASE}/index.php?page={page}"

    def _headers(self) -> dict:
        return {
            "X-CSRF-TOKEN": self._csrf,
            "Referer": f"{BASE}/index.php?page=user/app",
            "Origin": "https://beta.pvacodes.com",
        }

    @staticmethod
    def _expired(res: requests.Response) -> bool:
        if res.status_code in (401, 403):
            return True
        body = res.text
        return '"code":"403"' in body or '"code": "403"' in body or "Session expired" in body

    @staticmethod
    def _extract_csrf(html: str) -> str:
        m = _CSRF_INPUT.search(html) or _CSRF_META.search(html)
        return m.group(1) if m else ""

    def _login(self) -> None:
        """Open a fresh session (form login) and cache its CSRF token."""
        with self._io:
            self._throttle()
            res = self._s.get(self._url("user/login"), timeout=30)
            res.raise_for_status()
            csrf = self._extract_csrf(res.text)
            if not csrf:
                raise PvaAuthError("не удалось получить csrf_token со страницы входа")
            self._csrf = csrf

            self._throttle()
            res = self._s.post(
                self._url("user/login"),
                data={
                    "csrf_token": csrf,
                    "login_email": self.email,
                    "login_pass": self.password,
                    "login_submit": "1",
                },
                headers={
                    "X-CSRF-TOKEN": csrf,
                    "Referer": self._url("user/login"),
                    "Origin": "https://beta.pvacodes.com",
                },
                timeout=30,
            )
        body = res.text
        payload = _json(body)
        if payload is not None:
            if _status_code(payload) != "1000":
                raise PvaAuthError(
                    f"не удалось войти на beta.pvacodes.com — "
                    f"{_status_msg(payload) or _status_code(payload)}"
                )
            data = payload.get("data")
            if isinstance(data, dict):
                try:
                    self._balance = (float(data.get("credits")), time.time())
                except (TypeError, ValueError):
                    pass
            self._state_id = ""
            return

        if 'name="login_email"' in body:
            err = _ALERT.search(body)
            detail = err.group(1).strip() if err else "проверьте логин и пароль"
            raise PvaAuthError(f"не удалось войти на beta.pvacodes.com — {detail}")
        self._state_id = ""
        self._csrf = self._extract_csrf(body) or csrf

    def _login_with_cookie(self) -> None:
        """Authenticate with an existing SECURE_SESSION_ID cookie (no password).

        Used when a live browser session is handed over directly. Fetches the
        app page to prove the cookie is alive and to cache its CSRF token; a
        page that still shows the login form means the cookie expired.
        """
        with self._io:
            self._throttle()
            res = self._s.get(self._url("user/app"), timeout=30)
        body = res.text
        if res.status_code in (401, 403) or 'name="login_email"' in body:
            raise PvaAuthError(
                "SECURE_SESSION_ID истёк или недействителен — нужен свежий cookie"
            )
        csrf = self._extract_csrf(body)
        if not csrf:
            raise PvaAuthError(
                "cookie-сессия не открыла user/app (нет csrf_token)"
            )
        self._csrf = csrf
        m = _BALANCE.search(body)
        if m:
            try:
                self._balance = (float(m.group(1)), time.time())
            except ValueError:
                pass
        self._state_id = ""

    def _post(self, page: str, data: dict) -> str:
        for attempt in range(RETRY_AFTER_EXPIRY):
            with self._io:
                self._throttle()
                payload = {"csrf_token": self._csrf, **data}
                res = self._s.post(
                    self._url(page),
                    data=payload,
                    headers=self._headers(),
                    timeout=30,
                )
                retry = self._expired(res) and attempt + 1 < RETRY_AFTER_EXPIRY
            if retry:
                self._login()
                continue
            res.raise_for_status()
            return res.text
        return res.text

    def _get(self, page: str) -> str:
        for attempt in range(RETRY_AFTER_EXPIRY):
            with self._io:
                self._throttle()
                res = self._s.get(
                    self._url(page) + f"&_={int(time.time() * 1000)}",
                    headers=self._headers(),
                    timeout=30,
                )
                retry = self._expired(res) and attempt + 1 < RETRY_AFTER_EXPIRY
            if retry:
                self._login()
                continue
            res.raise_for_status()
            return res.text
        return res.text

    # ------------------------------------------------------------- discovery

    def balance(self) -> float:
        now = time.time()
        if now - self._balance[1] < self.BALANCE_TTL:
            return self._balance[0]
        html = self._get("user/dashboard")
        m = _BALANCE.search(html)
        if not m:
            raise PvaError("не удалось прочитать баланс с сайта pvacodes")
        value = float(m.group(1))
        self._balance = (value, now)
        return value

    def _resolve_state(self) -> str:
        if self._state_id:
            return self._state_id
        html = self._get("user/app")
        want = self.country.strip().casefold()
        for tag in _DIV_TAG.findall(html):
            if "data-id=" not in tag:
                continue
            did = _DATA_ID.search(tag)
            label = _SELECT_LABEL.search(tag)
            if did and label and label.group(1).strip().casefold() == want:
                self._state_id = did.group(1)
                return self._state_id
        raise PvaError(f"страна '{self.country}' не найдена в списке pvacodes")

    def _find_app(self, service: str) -> App:
        cached = self._apps.get(service)
        if cached and time.time() - cached[1] < self.APP_TTL:
            return cached[0]

        state_id = self._resolve_state()
        html = self._post(
            "user/app",
            {
                "state_id": state_id,
                "load_apps": 1,
                "page_no": 1,
                "apps_search": service,
                "apps_sort": "price_low",
                "sms_type": "",
            },
        )
        rows: list[App] = []
        for segment in html.split("maybeAskAreaCode(")[1:]:
            m = _ROW_APP.match(segment)
            if not m:
                continue
            name_m = _ROW_NAME.search(segment) or _ROW_NAME_LOOSE.search(segment)
            if not name_m:
                continue
            price_m = _ROW_PRICE.search(segment)
            rows.append(
                App(
                    state_id=m.group(1),
                    app_id=m.group(2),
                    name=name_m.group(1).strip(),
                    price=float(price_m.group(1)) if price_m else 0.0,
                )
            )

        exact = [r for r in rows if r.name.casefold() == service.casefold()]
        if not exact:
            if rows:
                found = ", ".join(r.name for r in rows[:6])
            else:
                found = f"страница каталога не распарсилась ({len(html)} символов)"
            raise PvaError(f"сервис '{service}' не найден на pvacodes — {found}")
        app = exact[0]
        self._apps[service] = (app, time.time())
        return app

    # ---------------------------------------------------------------- orders

    def _number_cards(self) -> dict[str, NumberCard]:
        html = self._post("user/app", {"load_numbers": 1})
        cards: dict[str, NumberCard] = {}
        for segment in html.split('class="number-card')[1:]:
            phone_m = _CARD_PHONE.search(segment)
            if not phone_m:
                continue
            spans = _CARD_SPAN.findall(segment)
            status_m = _CARD_STATUS.search(segment)
            code = next((c.strip() for c in _CARD_CODE.findall(segment) if c.strip()), "")
            cards[phone_m.group(1)] = NumberCard(
                id=phone_m.group(1),
                phone=phone_m.group(2).strip(),
                country=spans[0].strip() if len(spans) > 0 else "",
                app=spans[1].strip() if len(spans) > 1 else "",
                status=status_m.group(1).lower() if status_m else "",
                code=code,
            )
        return cards

    def _wait_for_new(self, before: set[str], timeout: float) -> NumberCard | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            fresh = [c for aid, c in self._number_cards().items() if aid not in before]
            if fresh:
                return fresh[0]
            time.sleep(1.5)
        return None

    def rent(self, max_price: float | None = None) -> tuple[str, str]:
        """Rent the first service in the configured queue that fits the budget."""
        with self._rent_lock:
            before = set(self._number_cards().keys())
            last: PvaError | None = None

            for service in self.services:
                try:
                    app = self._find_app(service)
                except PvaError as exc:
                    last = exc
                    continue

                if max_price is not None and app.price > max_price:
                    last = PvaError(f"{app.name} стоит ${app.price:.2f} > лимит ${max_price:.2f}")
                    continue

                body = self._post(
                    "user/app",
                    {
                        "load_number": 1,
                        "state_id": app.state_id,
                        "app_id": app.app_id,
                        "numberid": "",
                        "duration": 4,
                    },
                )
                payload = _json(body)
                if payload is None:
                    last = PvaError(f"{app.name}: неожиданный ответ при покупке — {body[:120]}")
                    continue

                msg = _status_msg(payload)
                if _status_code(payload) != "1000":
                    low = msg.lower()
                    if "insufficient" in low or "balance" in low or "credits" in low:
                        raise PvaLowBalance("недостаточно баланса")
                    last = PvaError(f"{app.name}: {msg or 'ошибка покупки'}")
                    continue

                card = self._wait_for_new(before, timeout=15)
                if card is None:
                    raise PvaError("номер куплен, но не появился в списке активаций")
                # The rent just consumed credits: drop the cached balance so
                # the dashboard shows the real number, not a stale one.
                self._balance = (self._balance[0], 0.0)
                return card.id, card.phone

            raise last or PvaError("ни одно приложение не подошло под лимит")

    def status(self, act_id: str) -> tuple[str, str | None]:
        """Return ("OK", code) | ("WAIT", None) | ("CANCEL", None)."""
        act_id = str(act_id)
        try:
            self._post("user/app", {"refresh_sms": 1, "id": act_id})
        except (requests.RequestException, PvaError):
            pass

        card = self._number_cards().get(act_id)
        if card is None:
            # A missing card may mean pvacodes cancelled the activation — or
            # that this one fetch returned garbage. Two consecutive misses
            # before declaring it cancelled: one bad fetch must not waste a
            # paid number.
            misses = self._miss.get(act_id, 0) + 1
            self._miss[act_id] = misses
            if misses >= 2:
                self._miss.pop(act_id, None)
                return "CANCEL", None
            return "WAIT", None
        self._miss.pop(act_id, None)
        if card.code:
            return "OK", card.code
        if card.cancelled:
            return "CANCEL", None
        # status "completed" without a code is NOT a cancellation — pvacodes
        # flips the status a beat before the code lands in the card. Keep
        # waiting; poll_otp gives up on its own timeout if no code ever comes.
        return "WAIT", None

    def cancel(self, act_id: str) -> None:
        body = self._post("user/app", {"cancel_number": 1, "id": str(act_id)})
        payload = _json(body)
        if payload is None:
            raise PvaError(f"неожиданный ответ при отмене — {body[:120]}")
        if _status_code(payload) == "1000":
            # A cancelled number returns credits: drop the cached balance so
            # the next dashboard refresh shows the money back.
            self._balance = (self._balance[0], 0.0)
            return
        msg = (_status_msg(payload) or _status_code(payload)).lower()
        if any(
            key in msg
            for key in (
                "not found",
                "no number",
                "already",
                "expired",
                "completed",
                "not exist",
                "released",
                "refunded",
            )
        ):
            return
        # The site blocks cancels for roughly a minute after renting and says
        # how many seconds are left. Read that number so the refund worker
        # can wake up exactly when the window opens instead of polling.
        wait = _LOCK_WAIT.search(msg)
        if wait and any(h in msg for h in _LOCK_HINTS):
            raise PvaLockedError(msg, float(wait.group(1)))
        raise PvaError(f"отмена не прошла: {msg}")

    def ready(self, act_id: str) -> None:
        return None

    def complete(self, act_id: str) -> None:
        return None
