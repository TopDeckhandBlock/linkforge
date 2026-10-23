"""Parse Google redemption links without opening or redeeming them."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, unquote, urlsplit

REDEMPTION_HOSTS = frozenset({"serviceactivation.google.com", "one.google.com"})
TOKEN_PARAMS = frozenset({"partnerPromotionToken", "token", "code", "promo", "promo_code"})
ASSET_EXTS = (".webp", ".png", ".jpg", ".jpeg", ".svg", ".css", ".js")
URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/-]+={0,2}\Z")
UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _decode_text(text: str) -> str:
    for _ in range(2):
        text = html.unescape(text.replace(r"\/", "/"))
        text = UNICODE_ESCAPE.sub(lambda match: chr(int(match[1], 16)), text)
    return text


def _parse_link(url: str) -> tuple[str, str]:
    if not url:
        return "", ""
    url = _decode_text(url).strip()
    if any(ord(char) <= 32 for char in url) or "\\" in url:
        return "", ""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (
            parsed.scheme.lower() != "https"
            or host not in REDEMPTION_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            return "", ""
    except ValueError:
        return "", ""
    if unquote(parsed.path).lower().endswith(ASSET_EXTS):
        return "", ""

    token = ""
    if parsed.path.startswith("/subscription/new/"):
        token = unquote(parsed.path[len("/subscription/new/"):])
    elif host == "one.google.com":
        prefix = "/activate-plan/subscription/new/"
        if parsed.path.startswith(prefix):
            token = unquote(parsed.path[len(prefix):])
        elif parsed.path.startswith("/promo/") and parsed.path != "/promo/":
            token = unquote(parsed.path[len("/promo/"):])

    if not token and parsed.path.rstrip("/") in ("/subscription/new", "/activate-plan", "/promo"):
        # A literal '+' can be part of the opaque token, not a form space.
        for key, value in parse_qsl(parsed.query.replace("+", "%2B")):
            if key in TOKEN_PARAMS and value:
                token = value
                break

    if not token or TOKEN_PATTERN.fullmatch(token) is None:
        return "", ""
    return url, token


def normalize_google_link(url: str) -> str:
    """Decode transport escaping and reject non-redemption or lookalike URLs."""
    return _parse_link(url)[0]


def extract_token(url: str) -> str:
    """Return the complete token, including base64 padding and decoded escapes."""
    return _parse_link(url)[1]


def find_google_links(payload: object) -> list[str]:
    """Scan text or nested JSON values; retain the first URL for each token."""
    links: dict[str, str] = {}
    pending = [payload]
    visited: set[int] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, (dict, list, tuple)):
            if id(value) in visited:
                continue
            visited.add(id(value))
            children = value.values() if isinstance(value, dict) else value
            pending.extend(reversed(list(children)))
        elif isinstance(value, str):
            for match in URL_PATTERN.finditer(_decode_text(value)):
                url, token = _parse_link(match[0].rstrip(".,;:)]}"))
                if token:
                    links.setdefault(token, url)
    return list(links.values())
