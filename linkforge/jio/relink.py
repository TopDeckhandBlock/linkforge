"""LinkForge — re-issue Google AI Pro links from a saved Jio session.

The farm used to be one-shot: rent, OTP, link, throw the session away. When
the link expired (30-60 minutes) the number was gone and a new rent was the
only option. Keeping the cookies lets us come back later and ask Jio again.

The open question this module answers at run time: does the ``google-ai``
endpoint hand out a NEW token on a repeat call, or the same spent one? Jio's
own MyJio app offers "take a new link", which says re-issue is supported by
the backend — and ``hunt_link`` deliberately keeps walking the chain when
``activate`` reports "already claimed", which is exactly the state a repeat
call lands in. We compare the token of the fresh link against the one we last
stored and report the verdict instead of assuming.

Session lifetime, measured on real captures (2026-10): the ``Authorization``
cookie is a JWT whose ``exp`` is issued 900 s (15 min) after ``iat``, and
after that ``authJsonData`` keeps answering HTTP 200 with ``loginFlag: null``
— the session is genuinely dead server side. ``sso-token`` carries no ``exp``
but cannot revive a dead ``Authorization`` on its own. No refresh endpoint
shows up in the selfcare SPA bundles, so the working hypothesis is that Jio
rotates cookies on ordinary calls while the token is still fresh. That is why
every call here persists the jar back (``SessionStore.save_cookies``): if the
rotation is real, a ping every few minutes keeps the session alive forever;
if it is not, nothing can, and the farm must spend the 15-minute window.
"""

from __future__ import annotations

import base64
import json
import time

import requests

from linkforge import proxy
from linkforge.jio.auth import UA
from linkforge.jio.hunt import hunt_link
from linkforge.links import extract_token as extract_token
from linkforge.storage.sessions import SessionStore

# ------------------------------------------------------------------- statuses

RELINK_NEW = "новая_ссылка"
RELINK_SAME = "та_же_ссылка"
SESSION_INVALID = "сессия_мертва"
NO_URL = "нет_url"
ACTIVATION_FAILED = "ошибка_активации"
ALREADY = "уже_активна"
NETWORK = "сетевая_ошибка"

RELINK_STATUSES = (
    RELINK_NEW,
    RELINK_SAME,
    SESSION_INVALID,
    NO_URL,
    ACTIVATION_FAILED,
    ALREADY,
    NETWORK,
)

# keepalive verdicts: "no answer" means inconclusive (WAF page, 5xx, timeout),
# the session keeps its status and gets another chance on the next ping.
KEEPALIVE_ALIVE = "жива"
KEEPALIVE_DEAD = "мертва"
KEEPALIVE_NO_ANSWER = "нет_ответа"

# ------------------------------------------------------------------- helpers



def same_link(old: str | None, new: str | None) -> bool:
    """True when *old* and *new* point at the same promotion.

    Compares tokens when both URLs carry one — the token is the part that
    actually changes on re-issue — and falls back to full-URL equality.
    """
    if not old or not new:
        return False
    ot, nt = extract_token(old), extract_token(new)
    if ot and nt:
        return ot == nt
    return old == new


def restore_session(cookies: dict, proxies: dict | None = None) -> requests.Session:
    """Rebuild a logged-in Jio session from a saved cookie dict.

    ``proxies=None`` follows the session policy (``proxy.for_sessions()``), which
    is a direct connection unless ``PROXY_SESSIONS=1``: these cookies were earned
    on some other exit IP, and a login that suddenly appears from a different
    country may read as a hijacked session. Pass ``{}`` to force direct.
    """
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.jio.com",
            "Referer": "https://www.jio.com/selfcare/login/",
        }
    )
    exit_ip = proxy.for_sessions() if proxies is None else proxies
    if exit_ip:
        s.proxies.update(exit_ip)
    s.cookies.update(cookies)
    return s


def auth_exp(cookies: dict) -> int | None:
    """``exp`` claim of the stored ``Authorization`` JWT, or None.

    The farm measured exp = iat + 900 s on live logins; when it is missing the
    cookie is not a JWT and the caller must not read a lifetime out of it.
    """
    token = (cookies or {}).get("Authorization", "")
    parts = str(token).split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data["exp"])
    except (ValueError, TypeError, KeyError):
        return None


def token_left(cookies: dict) -> int | None:
    """Seconds until the stored Authorization token expires (may be negative)."""
    exp = auth_exp(cookies)
    if exp is None:
        return None
    return exp - int(time.time())


# ------------------------------------------------------------------- main


def relink(phone: str, store: SessionStore) -> tuple[str | None, str, str | None]:
    """Ask Jio for a fresh link using a saved session.

    Returns ``(link, status, previous)`` — *previous* is the link we had
    stored for comparison, so the caller can show what changed.

    status is one of:
        новая_ссылка      — a link came back with a different token
        та_же_ссылка      — a link came back but it is the same spent token
        сессия_мертва     — authenticate says the session is no longer logged in
        уже_активна       — chain ran, offer claimed, no URL at all
        нет_url           — chain ran, no redirectionURL surfaced
        ошибка_активации  — activate returned a non-200 errorCode
        сетевая_ошибка    — request level failure before we got an answer
    """
    cookies = store.get_cookies(phone)
    if not cookies:
        return None, SESSION_INVALID, None

    previous = store.get_link(phone)
    s = restore_session(cookies)

    try:
        link, hunt_status = hunt_link(s)
    except requests.RequestException:
        return None, NETWORK, previous

    if hunt_status == "session_invalid":
        store.mark_dead(phone)
        return None, SESSION_INVALID, previous

    # The call went through and the session was not condemned: whatever the
    # server rotated into the jar (fresh Authorization, WAF cookies) is now
    # the best copy of this login — keep it before doing anything else.
    store.save_cookies(phone, s.cookies.get_dict())

    if not link:
        return None, _map_status(hunt_status), previous

    status = RELINK_SAME if same_link(previous, link) else RELINK_NEW
    store.set_link(phone, link)
    return link, status, previous


def keepalive(phone: str, store: SessionStore) -> str:
    """Ping Jio to keep a saved session warm.

    Returns one of ``KEEPALIVE_ALIVE`` / ``KEEPALIVE_DEAD`` /
    ``KEEPALIVE_NO_ANSWER``. Only an explicit verdict flips the stored state:
    a dead ``loginFlag`` (or 401/403) marks the session dead, a live one saves
    the rotated jar back and revives a session that was wrongly buried, and
    anything else (WAF page, 5xx, timeout) leaves the row untouched.
    """
    from linkforge.jio.hunt import AUTH_URL, DASH_HEADERS, _response_json

    cookies = store.get_cookies(phone)
    if not cookies:
        return KEEPALIVE_DEAD

    s = restore_session(cookies)
    try:
        res = s.get(AUTH_URL, headers=DASH_HEADERS, timeout=15)
    except requests.RequestException:
        # Network noise is not proof the session died — leave it as it is.
        return KEEPALIVE_NO_ANSWER

    data = _response_json(res)
    if str(data.get("loginFlag", "")).lower() == "true":
        store.save_cookies(phone, s.cookies.get_dict())
        return KEEPALIVE_ALIVE

    if res.status_code in (401, 403) or "loginFlag" in data:
        store.mark_dead(phone)
        return KEEPALIVE_DEAD

    return KEEPALIVE_NO_ANSWER


# ------------------------------------------------------------------- private


def _map_status(hunt_status: str) -> str:
    if hunt_status == "already_active":
        return ALREADY
    if hunt_status == "activation_failed":
        return ACTIVATION_FAILED
    if hunt_status == "network":
        return NETWORK
    return NO_URL
