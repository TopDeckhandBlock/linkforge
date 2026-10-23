"""LinkForge — Jio selfcare authentication helpers."""

from __future__ import annotations

import time

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

JIO_SEND_OTP = "https://www.jio.com/api/jio-login-service/login/sendOtp"
JIO_VALIDATE_OTP = "https://www.jio.com/api/jio-login-service/login/validateOtp"


def _response_error(res: requests.Response) -> bool:
    """Jio returns HTTP 200 even on logical errors — inspect the body too."""
    if not res.ok:
        return True
    try:
        data = res.json()
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("errorMessage") or data.get("error"):
        return True
    return str(data.get("status", "")).lower() in {"failed", "failure", "error", "false"}


# sendOtp tri-state: a bare bool cannot tell a throttle apart from a dead
# number, and throttling is global — treating it as failure makes the worker
# burn a freshly rented number every few seconds.
OTP_OK = "OK"
OTP_RATE_LIMITED = "RATE_LIMITED"
OTP_ERROR = "ERROR"
# Jio rejects a number that is not on its network with HTTP 400
# INVALID_JIONUMBER_ERROR. That is the same verdict the subscriber check
# gives, just one step later — so it must get the same treatment (cancel at
# once), not the slow OTP-failure refund.
OTP_NOT_JIO = "NOT_JIO"

_RATE_HINTS = (
    "rate",
    "limit",
    "captcha",
    "too many",
    "try again",
    "frequent",
    "temporarily",
    "blocked",
    "exceeded",
)

# Exact error codes Jio uses for "this number is not ours". Kept as codes,
# not free text, so a wording change cannot silently reclassify them.
_NOT_JIO_CODES = (
    "INVALID_JIONUMBER_ERROR",
    "INVALID_JIO_NUMBER",
    "NOT_A_JIO_NUMBER",
    "NOT_JIO_USER",
    "NOT_SUBSCRIBED_USER",
)


def _not_jio(data) -> bool:
    """True when Jio says the number is not on its network."""
    if not isinstance(data, dict):
        return False
    blob = " ".join(
        str(data.get(k) or "")
        for k in ("errorCode", "errorMessage", "error", "message", "responseMessage")
    )
    return any(code in blob for code in _NOT_JIO_CODES)


def _throttled(res: requests.Response) -> bool:
    if res.status_code == 429:
        return True
    try:
        data = res.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        blob = " ".join(
            str(data.get(k) or "")
            for k in ("errorMessage", "error", "message", "description")
        ).lower()
        if blob and any(h in blob for h in _RATE_HINTS):
            return True
    blob = (res.text or "").lower()
    return any(h in blob for h in ("captcha_required", "access denied", "429"))


def jio_send_otp_detail(s: requests.Session, phone: str) -> tuple[str, str]:
    """POST to Jio sendOtp, returning (status, jio_message).

    Status is OTP_OK / OTP_RATE_LIMITED / OTP_NOT_JIO / OTP_ERROR so the
    caller can back off and reuse the already-paid number instead of renting
    another one. OTP_NOT_JIO means Jio itself said the number is not on its
    network.

    The second element is the raw ``errorMessage`` (empty on success). It
    exists because a bare "OTP gagal dikirim" in the database hides what
    actually happened — Jio distinguishes a dead number from a throttle from a
    missing field, and those need different handling.
    """
    try:
        res = s.post(
            JIO_SEND_OTP,
            json={
                "mobileNumber": phone,
                "loginFlowType": "MOBILE",
                "alternateNumber": "",
            },
            timeout=10,
        )
    except requests.RequestException as e:
        return OTP_ERROR, f"сеть: {type(e).__name__}"
    if _throttled(res):
        return OTP_RATE_LIMITED, _jio_message(res)
    if _response_error(res):
        try:
            data = res.json()
        except ValueError:
            data = None
        if _not_jio(data):
            return OTP_NOT_JIO, _jio_message(res) or "INVALID_JIONUMBER_ERROR"
        return OTP_ERROR, _jio_message(res) or f"http {res.status_code}"
    return OTP_OK, ""


def _jio_message(res: requests.Response) -> str:
    """Pull Jio's errorMessage/errorCode out of a response, if any."""
    try:
        data = res.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("errorMessage", "errorCode", "error", "message"):
        value = data.get(key)
        if value:
            return str(value).strip()
    return ""


def jio_validate_otp(
    s: requests.Session,
    phone: str,
    otp: str,
    tries: int = 4,
    backoff: float = 15.0,
) -> bool:
    """POST to Jio validateOtp endpoint. Returns True on success.

    A throttle or a network hiccup is not the same thing as a rejected OTP.
    The number behind this code is already paid for, so retry those instead of
    reporting "wrong OTP" and dropping a code that was probably correct.
    """
    for attempt in range(tries):
        last = attempt + 1 >= tries
        try:
            res = s.post(
                JIO_VALIDATE_OTP,
                json={"mobileNumber": phone, "otp": otp},
                timeout=10,
            )
        except requests.RequestException:
            if last:
                return False
            time.sleep(backoff)
            continue
        if _throttled(res):
            if last:
                return False
            time.sleep(backoff)
            continue
        return not _response_error(res)
    return False