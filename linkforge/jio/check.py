"""LinkForge — Jio subscriber check."""

from __future__ import annotations

import threading
import time

import requests

from linkforge import proxy
from linkforge.jio.auth import UA

JIO_CHECK = "https://www.jio.com/api/jio-recharge-service/recharge/mobility/number/{phone}"

OK = "OK"
NOT_JIO = "NOT_JIO"
UNKNOWN = "UNKNOWN"

MIN_GAP = 4.0
_lock = threading.Lock()
_last = 0.0


def _throttle() -> None:
    global _last
    with _lock:
        wait = MIN_GAP - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()


def check_subscriber(phone: str, retries: int = 3, proxies: dict | None = None) -> str:
    """Classify *phone*.

    Returns:
        OK       — Jio confirmed (HTTP 200)
        NOT_JIO  — Jio explicitly says the number is not a subscriber
        UNKNOWN  — Jio rate-limited us (429 / CAPTCHA_REQUIRED); caller decides

    ``proxies`` is used when given; ``None`` takes the current pool exit IP,
    ``{}`` forces the direct connection (the hunt cycle passes the address its
    session will use, so probe and sendOtp look like one client).
    """
    exit_ip = proxy.for_requests() if proxies is None else proxies
    last = UNKNOWN
    for attempt in range(retries):
        _throttle()
        try:
            res = requests.get(
                JIO_CHECK.format(phone=phone),
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json",
                    "Referer": "https://www.jio.com/",
                },
                proxies=exit_ip or None,
                timeout=10,
            )
        except Exception:
            last = UNKNOWN
        else:
            if res.status_code == 200:
                return OK
            if "NOT_SUBSCRIBED_USER" in res.text:
                return NOT_JIO
            last = UNKNOWN
        if attempt + 1 < retries:
            time.sleep(15 * (attempt + 1))
    return last


def jio_check_subscriber(phone: str) -> bool:
    """Backwards-compatible bool wrapper (UNKNOWN counts as "try anyway")."""
    return check_subscriber(phone) != NOT_JIO


def normalize_phone(raw: str) -> str:
    """Strip non-digits and return the last 10 digits.

    Raises:
        ValueError — the provider handed back something that cannot be an
            Indian mobile number. Better to fail loudly (the number is
            refunded) than to send a malformed number to Jio.
    """
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) > 10:
        digits = digits[-10:]
    if len(digits) != 10:
        raise ValueError(f"провайдер вернул некорректный номер: '{raw}'")
    return digits
