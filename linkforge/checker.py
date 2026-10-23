"""Non-redeeming, anonymous checks for Google activation links.

Sign-in redirects and HTTP 200 do not confirm that a token is redeemable.
Only explicit token errors or HTTP 404/410 mean DEAD; transient failures mean
UNKNOWN. Age is a suspicion threshold, not proof of token expiration.
"""

from __future__ import annotations

import calendar
import time
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests

from linkforge.links import normalize_google_link

LINK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

ALIVE = "жива"
SUSPECT = "подозрительна"
DEAD = "мертва"
UNKNOWN = "неизвестно"

SIGNIN_HOSTS = frozenset({"accounts.google.com", "accounts.google.co.id"})
PROBE_HOSTS = SIGNIN_HOSTS | {"serviceactivation.google.com", "one.google.com"}
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
ERROR_MARKERS = (
    "that link you clicked was broken",
    "this promotion has ended",
    "this offer has expired",
    "this activation link has expired",
    "this subscription link has already been used",
    "this activation link has already been used",
    "this link has already been used",
    "this offer has already been redeemed",
    "this activation link is invalid",
)
TOKEN_ERRORS = frozenset({
    "already_used", "already_redeemed", "already_claimed", "offer_already_used",
    "expired", "invalid_token", "invalid_offer", "offer_not_found",
})

DEFAULT_MAX_AGE = 30 * 60  # seconds


class LinkProbe:
    """Result of one anonymous GET against a redemption link."""

    __slots__ = (
        "definitive_error", "error", "final_host", "ok",
        "redirected_to_signin", "status_code",
    )

    def __init__(self) -> None:
        self.ok = False
        self.final_host = ""
        self.redirected_to_signin = False
        self.status_code = 0
        self.error = ""
        self.definitive_error = ""


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "template"):
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "template"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _probe_host(url: str) -> str:
    if any(ord(char) <= 32 for char in url) or "\\" in url:
        return ""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() == "https"
            and parsed.hostname in PROBE_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        ):
            return parsed.hostname
    except ValueError:
        pass
    return ""


def probe_link(url: str, timeout: int = 20) -> LinkProbe:
    """Use GET only; stop at sign-in and never follow a foreign redirect."""
    p = LinkProbe()
    current = normalize_google_link(url)
    if not current:
        p.definitive_error = "Unsupported or malformed redemption URL"
        return p

    try:
        with requests.Session() as session:
            session.headers.update({
                "User-Agent": LINK_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            for _ in range(6):
                res = session.get(current, timeout=timeout, allow_redirects=False)
                p.ok = True
                p.status_code = res.status_code
                p.final_host = _probe_host(res.url)
                if not p.final_host:
                    p.error = "Untrusted response URL"
                    return p
                if res.status_code in REDIRECT_STATUSES:
                    location = res.headers.get("Location")
                    if not location:
                        p.error = "Redirect without Location"
                        return p
                    try:
                        target = urljoin(current, location)
                    except ValueError:
                        p.error = "Malformed Google redirect URL"
                        return p
                    host = _probe_host(target)
                    if not host:
                        p.error = "Blocked redirect outside trusted Google hosts"
                        return p
                    p.final_host = host
                    if host in SIGNIN_HOSTS:
                        p.redirected_to_signin = True
                        return p
                    for key, value in parse_qsl(urlsplit(target).query):
                        if key in ("error", "status", "reason") and value.lower() in TOKEN_ERRORS:
                            p.definitive_error = value.lower()
                            return p
                    current = target
                    continue
                if res.status_code == 200:
                    page = _VisibleText()
                    page.feed(res.text)
                    visible = " ".join(" ".join(page.parts).lower().split())
                    for marker in ERROR_MARKERS:
                        if marker in visible:
                            p.definitive_error = marker
                            break
                return p
            p.error = "Too many Google redirects"
    except requests.RequestException as exc:
        # Exception strings can contain the full bearer URL; keep it out of logs.
        p.error = f"{type(exc).__name__}: Google request failed"
    return p


def link_age_seconds(link_at: str | None, now: float | None = None) -> float | None:
    """Seconds elapsed since *link_at* (SQLite ``datetime('now')`` = UTC).

    The timestamp string is parsed as UTC — SQLite writes ``datetime('now')``
    in UTC and ``time.mktime`` would otherwise read it as local time, which on
    any machine not running UTC shifts every age by the whole offset.

    Returns None when the timestamp is missing or unparseable, so callers can
    fall back to the HTTP verdict alone instead of guessing an age.
    """
    if not link_at:
        return None
    try:
        stamp = calendar.timegm(time.strptime(link_at, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None
    return max(0.0, (time.time() if now is None else now) - stamp)


def judge(
    probe: LinkProbe,
    age_seconds: float | None = None,
    max_age: float = DEFAULT_MAX_AGE,
) -> str:
    """Classify without treating sign-in, throttling or outages as token proof.

    ALIVE remains an import-compatible constant, but an anonymous probe cannot
    produce it. A stale age only means SUSPECT, never confirmed expiration.
    """
    if probe.definitive_error:
        return DEAD
    if not probe.ok or probe.error:
        return UNKNOWN
    if probe.status_code in (404, 410):
        return DEAD
    if not 200 <= probe.status_code < 400 or probe.status_code in (401, 403, 429):
        return UNKNOWN
    if age_seconds is not None and age_seconds >= max_age:
        return SUSPECT
    return UNKNOWN


def check_link(
    url: str,
    link_at: str | None = None,
    max_age: float = DEFAULT_MAX_AGE,
) -> tuple[str, LinkProbe, float | None]:
    """Probe *url*, judge it, and return ``(verdict, probe, age_seconds)``."""
    probe = probe_link(url)
    age = link_age_seconds(link_at)
    return judge(probe, age, max_age), probe, age


def format_age(seconds: float | None) -> str:
    """Human age like ``42m`` / ``2h10m``; ``?`` when unknown."""
    if seconds is None:
        return "?"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    return f"{total // 3600}h{total % 3600 // 60:02d}m"
