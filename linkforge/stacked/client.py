"""LinkForge — PVACodes Stacked-API client (beta.pvacodes.com/app/stacked).

Native API provider: authenticates with an ``x-api-key`` header and talks to
clean JSON routes instead of the CSRF form-login the HTML client needs. This is
the documented "Stacked-Compatible API" mapper — same account and key as the
website, no separate credentials.

Routes (verified live, 2026-10):

    GET  /balance                     -> {"balance":"2.00"}
    GET  /services?country=India      -> [{"code","name","price","stock"}]
    POST /getnumber {service,country} -> {"phone_number":"+91...","order_id":"..."}
    GET  /getsms?id=<order>           -> {"code":null} | {"code":"123456"}
    POST /cancel?id=<order>           -> 200 | 400 {"error":"Please wait N more seconds..."}

Errors: 404 out of stock, 402 out of balance, 401 bad/inactive key.
The cancel carries a ~60 s lock window; the exact countdown is parsed and
reported to the refund worker as ``LockedError.retry_after``.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import requests

from linkforge.sms import LockedError, LowBalance, SmsError

BASE = "https://beta.pvacodes.com/app/stacked"

# The key is rate-limited (NORMAL tier = 90 calls/min). One process-wide gap
# keeps the whole worker pool inside the limit.
MIN_GAP = 0.7

# The site holds a freshly rented number ~60 s before it may be cancelled:
#   {"error":"Please wait 55 more seconds before cancelling - ..."}
_LOCK_WAIT = re.compile(r"wait\s+(\d+)\s+more\s+second", re.I)


class StackedError(SmsError):
    """Unexpected answer from the PVACodes Stacked API."""


class StackedLowBalance(LowBalance):
    """The API answered HTTP 402 (out of balance)."""


class StackedLockedError(LockedError):
    """The cancel was refused inside the site's own lock window."""


class StackedSMS:
    """Thread-safe PVACodes Stacked-API client implementing linkforge SMSProvider."""

    BALANCE_TTL = 5.0

    def __init__(
        self,
        api_key: str,
        services: str = "Jio11,Jio14,Jio17,Jio19",
        country: str = "India",
        proxies: dict[str, str] | None = None,
        max_price: float | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise StackedError(
                "STACKED_API_KEY пуст — возьми ключ в ЛК beta.pvacodes.com (раздел API)"
            )
        self.key = api_key.strip()
        self.country = (country or "India").strip() or "India"
        self.services = [s.strip() for s in (services or "").split(",") if s.strip()]
        if not self.services:
            self.services = ["Jio11"]
        self.max_price = max_price
        self.proxies = proxies or None

        self._s = requests.Session()
        self._s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "x-api-key": self.key,
            }
        )
        self._io = threading.Lock()
        self._last = 0.0
        self._balance: tuple[float, float] = (0.0, 0.0)

    # ------------------------------------------------------------------ http

    def _throttle(self) -> None:
        wait = MIN_GAP - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retries: int = 3,
    ) -> requests.Response:
        url = BASE + path
        last: Exception | None = None
        for attempt in range(1, retries + 1):
            with self._io:
                self._throttle()
                try:
                    res = self._s.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        proxies=self.proxies,
                        timeout=30,
                    )
                except requests.RequestException as e:
                    last = e
                    if attempt == retries:
                        raise StackedError(f"сеть Stacked API недоступна: {e}") from e
                    time.sleep(2 * attempt)
                    continue
            if res.status_code == 429:
                time.sleep(3 * attempt)
                continue
            if res.status_code >= 500:
                last = StackedError(f"Stacked API HTTP {res.status_code}")
                time.sleep(2 * attempt)
                continue
            return res
        raise StackedError(str(last) if last else "Stacked API не ответил")

    @staticmethod
    def _json(res: requests.Response) -> Any:
        try:
            return res.json()
        except ValueError:
            return None

    @staticmethod
    def _auth_check(res: requests.Response) -> None:
        if res.status_code == 401:
            raise StackedError(
                "Stacked API отклонил ключ (401) — проверь, что он Active в ЛК"
            )

    # ---------------------------------------------------------------- orders

    def balance(self) -> float:
        now = time.time()
        if now - self._balance[1] < self.BALANCE_TTL:
            return self._balance[0]
        res = self._request("GET", "/balance")
        self._auth_check(res)
        data = self._json(res)
        val = data.get("balance") if isinstance(data, dict) else None
        if val is None:
            raise StackedError(f"неожиданный ответ баланса: {res.text[:120]}") from None
        try:
            f = float(val)
        except (TypeError, ValueError):
            raise StackedError(f"неожиданный ответ баланса: {res.text[:120]}") from None
        self._balance = (f, now)
        return f

    def catalog(self) -> list[dict]:
        """Catalog rows [{code,name,price,stock}] for the configured country."""
        res = self._request("GET", "/services", params={"country": self.country})
        self._auth_check(res)
        data = self._json(res)
        return data if isinstance(data, list) else []

    def rent(self, max_price: float | None = None) -> tuple[str, str]:
        """Rent the first service in the queue with stock. Returns (order_id, phone)."""
        price = max_price if max_price is not None else self.max_price
        last: StackedError | None = None
        for service in self.services:
            res = self._request(
                "POST",
                "/getnumber",
                json_body={"service": service, "country": self.country},
            )
            self._auth_check(res)
            if res.status_code == 402:
                raise StackedLowBalance("недостаточно баланса на PVACodes")
            if res.status_code == 404:
                last = StackedError(f"{service}: нет номеров в стоке")
                continue
            data = self._json(res)
            if not res.ok or not isinstance(data, dict):
                last = StackedError(f"{service}: {res.text[:120]}")
                continue
            oid = data.get("order_id") or data.get("id")
            phone = data.get("phone_number") or data.get("phone")
            if oid and phone:
                if price is not None:
                    try:
                        paid = float(data.get("price") or 0)
                    except (TypeError, ValueError):
                        paid = 0.0
                    if paid and paid > price + 1e-9:
                        # Sold above our cap — cancel straight away and move on.
                        try:
                            self.cancel(str(oid))
                        except SmsError:
                            pass
                        last = StackedError(f"{service}: цена ${paid:.2f} > лимит ${price:.2f}")
                        continue
                # The rent consumed credits: drop the cached balance.
                self._balance = (self._balance[0], 0.0)
                return str(oid), str(phone)
            last = StackedError(f"{service}: ответ без номера/ID — {res.text[:120]}")
        raise last or StackedError("ни один сервис из очереди не дал номер")

    def status(self, act_id: str) -> tuple[str, str | None]:
        """Return ("OK", code) | ("WAIT", None) | ("CANCEL", None)."""
        res = self._request("GET", "/getsms", params={"id": str(act_id)})
        self._auth_check(res)
        if res.status_code == 404:
            return "CANCEL", None
        data = self._json(res)
        if isinstance(data, dict):
            err = data.get("error")
            if err:
                low = str(err).lower()
                if any(k in low for k in ("cancel", "expire", "not found", "refunded")):
                    return "CANCEL", None
                return "WAIT", None
            code = data.get("code")
            if code:
                return "OK", str(code)
        return "WAIT", None

    def ready(self, act_id: str) -> None:
        return None

    def complete(self, act_id: str) -> None:
        return None

    def cancel(self, act_id: str) -> None:
        res = self._request("POST", "/cancel", params={"id": str(act_id)})
        self._auth_check(res)
        if res.ok:
            # A cancelled number returns credits: drop the cached balance.
            self._balance = (self._balance[0], 0.0)
            return
        if res.status_code == 402:
            raise StackedLowBalance("недостаточно баланса на PVACodes")
        data = self._json(res)
        msg = ""
        if isinstance(data, dict):
            msg = str(data.get("error") or data.get("message") or "")
        low = msg.lower()
        if any(
            k in low
            for k in (
                "already",
                "not found",
                "expired",
                "completed",
                "cancelled",
                "canceled",
                "refunded",
                "no order",
            )
        ):
            return
        m = _LOCK_WAIT.search(msg)
        if res.status_code == 400 and m:
            raise StackedLockedError(
                msg or "номер в lock-окне", float(m.group(1))
            )
        raise StackedError(f"отмена не прошла: {msg or res.text[:120]}")
