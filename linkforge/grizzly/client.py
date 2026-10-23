"""LinkForge — GrizzlySMS client (api.grizzlysms.com).

GrizzlySMS speaks the sms-activate ``stubs/handler_api.php`` protocol: one GET
endpoint, the ``action`` parameter picks the operation, the body is a plain
text token (``ACCESS_NUMBER:id:phone``). Its own swagger
(https://api.grizzlysms.com/docs/client/swagger.json) lists the actions used
here; the Jio service code is ``jio`` and India is country ``22``.

Differences from PVACodes that the orchestrator must know about:

* a number may only be cancelled after Grizzly's own lock window (~120 s), so
  ``cancel`` raises ``LockedError`` on ``EARLY_CANCEL_DENIED``;
* ``SERVICE_UNAVAILABLE_REGION`` means our IP is not allowed to buy this
  service — that is what the proxy pool is for, so it is raised as
  ``RegionBlocked`` (a calm, actionable message, not a crash);
* an empty balance is a first-class state (``LowBalance``), like pvacodes.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from linkforge.sms import LockedError, LowBalance, RegionBlocked, SmsError

BASE = "https://api.grizzlysms.com/stubs/handler_api.php"

# Grizzly's own documentation: the activation cannot be cancelled inside the
# first 120 s. Reported to the refund worker as the lock countdown.
LOCK_SECONDS = 120.0

# Concurrent workers share one key, and Grizzly rate-limits per key. One
# process-wide gap keeps the whole pool inside the limit.
MIN_GAP = 1.0


class GrizzlyError(SmsError):
    """Unexpected answer from the GrizzlySMS API."""


class GrizzlyLowBalance(LowBalance):
    """Grizzly answered NO_BALANCE."""


class GrizzlyLockedError(LockedError):
    """Grizzly refused the cancel inside its lock window."""


class GrizzlyRegionBlocked(RegionBlocked):
    """Grizzly refuses to sell this service to our IP."""


class GrizzlySMS:
    """Thread-safe GrizzlySMS client implementing the linkforge SMSProvider."""

    def __init__(
        self,
        api_key: str,
        service: str = "jio",
        country: str = "22",
        proxies: dict[str, str] | None = None,
        max_price: float | None = None,
    ) -> None:
        if not api_key.strip():
            raise GrizzlyError("GRIZZLY_API_KEY пуст — возьми ключ в ЛК grizzlysms.com")
        self.key = api_key.strip()
        self.service = str(service or "jio").strip() or "jio"
        self.country = str(country or "22").strip() or "22"
        self.max_price = max_price
        self.proxies = proxies or None

        self._s = requests.Session()
        self._throttle_lock = threading.Lock()
        self._last_call = 0.0

    # ------------------------------------------------------------------ http

    def _throttle(self) -> None:
        with self._throttle_lock:
            wait = MIN_GAP - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()

    def _get(self, params: dict[str, Any], retries: int = 4) -> str:
        payload = {"api_key": self.key, **params}
        last: Exception | None = None
        for attempt in range(1, retries + 1):
            self._throttle()
            try:
                res = self._s.get(
                    BASE,
                    params=payload,
                    proxies=self.proxies,
                    timeout=25,
                )
            except requests.RequestException as e:
                last = e
                if attempt == retries:
                    raise GrizzlyError(f"сеть GrizzlySMS недоступна: {e}") from e
                time.sleep(2 * attempt)
                continue

            body = res.text.strip()
            if res.status_code == 429:
                time.sleep(3 * attempt)
                continue
            if res.status_code >= 500:
                last = GrizzlyError(f"GrizzlySMS ответил HTTP {res.status_code}")
                time.sleep(2 * attempt)
                continue

            low = body.upper()
            if "SERVICE_UNAVAILABLE_REGION" in low:
                raise GrizzlyRegionBlocked(
                    "GrizzlySMS блокирует наш регион для сервиса "
                    f"{self.service} — включи прокси (PROXY_ENABLED=1)"
                )
            if "BAD_KEY" in low:
                raise GrizzlyError("GrizzlySMS отклонил ключ (BAD_KEY)")
            return body

        raise GrizzlyError(str(last) if last else "GrizzlySMS не ответил")

    def _action(self, params: dict[str, Any], max_price: float | None = None) -> str:
        """Call the API and turn a documented error token into an exception."""
        body = self._get(params)
        low = body.upper()
        if low.startswith("BAD_ACTION"):
            raise GrizzlyError("GrizzlySMS не понял действие (BAD_ACTION)")
        if "NO_BALANCE" in low:
            raise GrizzlyLowBalance("недостаточно баланса на GrizzlySMS")
        if "WRONG_MAX_PRICE" in low:
            raise GrizzlyError(
                "GrizzlySMS: лимит цены ниже минимальной — подними max-price / MAX_PRICE"
            )
        if "NO_NUMBERS" in low:
            # Grizzly answers NO_NUMBERS both when the stock is empty and when
            # maxPrice sits below the cheapest offer (measured: the same key
            # refused $0.11 and sold the same service at $0.24). Naming the cap
            # saves a hunt from silently running on nothing.
            cap = f" ≤ ${max_price:.2f}" if max_price is not None else ""
            raise GrizzlyError(
                f"GrizzlySMS: нет номеров{cap} — подними max-price / MAX_PRICE"
            )
        if "BANNED" in low:
            raise GrizzlyError("GrizzlySMS заблокировал аккаунт (BANNED)")
        return body

    # ---------------------------------------------------------------- orders

    def balance(self) -> float:
        body = self._action({"action": "getBalance"})
        if body.startswith("ACCESS_BALANCE:"):
            try:
                return float(body.split(":", 1)[1])
            except ValueError:
                pass
        raise GrizzlyError(f"неожиданный ответ баланса: {body[:120]}")

    def rent(self, max_price: float | None = None) -> tuple[str, str]:
        price = max_price if max_price is not None else self.max_price
        if price is None:
            # Measured on the live API: omitting maxPrice answers NO_NUMBERS
            # even with ~490k numbers in stock, while the same call with a cap
            # sells one. Never leave the cap out, and never leave it open.
            price = 1.0
        params: dict[str, Any] = {
            "action": "getNumber",
            "service": self.service,
            "country": self.country,
            "maxPrice": price,
        }
        body = self._action(params, max_price=price)
        if body.startswith("ACCESS_NUMBER:"):
            parts = body.split(":")
            if len(parts) >= 3:
                return parts[1].strip(), parts[2].strip()
        raise GrizzlyError(f"неожиданный ответ на аренду: {body[:120]}")

    def status(self, act_id: str) -> tuple[str, str | None]:
        """Return ("OK", code) | ("WAIT", None) | ("CANCEL", None)."""
        body = self._action({"action": "getStatus", "id": str(act_id)})
        if body.startswith("STATUS_OK:"):
            code = body.split(":", 1)[1].strip()
            return ("OK", code) if code else ("WAIT", None)
        if body == "STATUS_CANCEL":
            return "CANCEL", None
        if body in ("STATUS_WAIT_CODE", "STATUS_WAIT_RESEND"):
            return "WAIT", None
        # STATUS_WAIT_RETRY:<lastcode> — the provider is waiting for a resend
        # and hands back the *previous* code. Treating it as an answer would
        # burn the rent on a stale code, so keep polling.
        if body.startswith("STATUS_WAIT_RETRY"):
            return "WAIT", None
        if "NO_ACTIVATION" in body.upper():
            return "CANCEL", None
        raise GrizzlyError(f"неожиданный статус: {body[:120]}")

    def ready(self, act_id: str) -> None:
        self._action({"action": "setStatus", "id": str(act_id), "status": 1})

    def complete(self, act_id: str) -> None:
        self._action({"action": "setStatus", "id": str(act_id), "status": 6})

    def cancel(self, act_id: str) -> None:
        body = self._get({"action": "setStatus", "id": str(act_id), "status": 8})
        low = body.upper()
        if low.startswith("ACCESS_CANCEL"):
            return
        if "EARLY_CANCEL_DENIED" in low or "EARLY_CANCEL" in low:
            raise GrizzlyLockedError(
                f"GrizzlySMS держит номер ещё {int(LOCK_SECONDS)} с", LOCK_SECONDS
            )
        if "NO_ACTIVATION" in low or "BAD_STATUS" in low:
            # Already finished upstream — nothing left to cancel.
            return
        if "NO_BALANCE" in low:
            raise GrizzlyLowBalance("недостаточно баланса на GrizzlySMS")
        raise GrizzlyError(f"отмена не прошла: {body[:120]}")
