"""LinkForge — Google AI Pro link hunting endpoints.

The real redemption flow (mirrors Jio's own selfcare SPA) is a strict chain:

    authenticate  -> confirms the API session is live (loginFlag == "true")
    navigate      -> primes the OTT subscription context (product Z0241)
    activate      -> registers the offer against the number
    google-ai     -> returns redirectionURL holding the serviceactivation token

Do NOT call the ``submission/submit`` endpoint between ``google-ai`` and the
user actually redeeming on Google: Jio treats that ping as "offer already
processed" and the token comes back dead ("activation link already used").
Finalization belongs to a post-redemption callback, not to the hunt.

Hitting ``google-ai`` without the preceding steps yields an empty payload,
which is why an earlier guessed-endpoint version never produced links.
"""

from __future__ import annotations

import requests

from linkforge.links import find_google_links

# ------------------------------------------------------------------- constants

PRODUCT_CODE = "Z0241"

AUTH_URL = "https://www.jio.com/api/jio-authenticate-service/authenticate/authJsonData"
NAVIGATE_URL = f"https://www.jio.com/api/jio-ott-service/ott/subscription/navigate/{PRODUCT_CODE}"
ACTIVATE_URL = (
    f"https://www.jio.com/api/jio-ott-service/ott/subscription/activate/"
    f"{PRODUCT_CODE}?source=JIO"
)
GOOGLE_URL = "https://www.jio.com/api/jio-ott-service/ott/subscription/google-ai"

GOOGLE_PAGE = (
    f"https://www.jio.com/selfcare/googleai/?header=no&type={PRODUCT_CODE}&source=JIO"
)

DASH_HEADERS = {"Accept": "*/*", "Referer": "https://www.jio.com/selfcare/dashboard/"}
OFFER_HEADERS = {"Accept": "*/*", "Referer": GOOGLE_PAGE}

ALREADY_ACTIVE = ("already", "availed", "claimed")


# ------------------------------------------------------------------- helpers


def _response_json(res: requests.Response) -> dict:
    try:
        data = res.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _api_message(data: dict) -> str:
    for name in ("errorMessage", "responseMessage", "responseMsg", "message"):
        if data.get(name):
            return str(data[name])
    return ""


def _already_active(message: str) -> bool:
    low = message.lower()
    return any(k in low for k in ALREADY_ACTIVE)


# ------------------------------------------------------------------- main


def hunt_link(s: requests.Session, fresh_only: bool = False) -> tuple[str | None, str]:
    """Run the full activation chain and return (link, status).

    status is one of:
        session_invalid   — authenticate says we are not logged in
        network           — no clear verdict (WAF page, 5xx, timeout); retry
        activation_failed — activate returned a non-200 errorCode
        already_active    — the offer was already claimed on this number
        no_url            — chain ran but no redirectionURL surfaced
        recycled_number   — fresh_only: a previous renter already activated
                            this number, its token is dead on arrival
        ok                — a redemption link was extracted
    """
    # 1) authenticate — confirm the session is really live. Only an explicit
    # "loginFlag" that is not true (or a 401/403) proves death: a WAF page or
    # a 5xx must not condemn a session that was never really asked.
    try:
        res = s.get(AUTH_URL, headers=DASH_HEADERS, timeout=15)
    except requests.RequestException:
        return None, "network"
    auth = _response_json(res)
    if str(auth.get("loginFlag", "")).lower() != "true":
        if res.status_code in (401, 403) or "loginFlag" in auth:
            return None, "session_invalid"
        return None, "network"

    # 2) navigate — prime OTT subscription context
    try:
        s.get(NAVIGATE_URL, headers=DASH_HEADERS, timeout=15)
    except requests.RequestException:
        pass

    # 3) activate — register the offer against the number
    try:
        res = s.get(ACTIVATE_URL, headers=OFFER_HEADERS, timeout=15)
    except requests.RequestException:
        return None, "activation_failed"
    act = _response_json(res)
    # "already availed / claimed" here means the registration already sits on
    # this number — which is exactly the state where google-ai still hands back
    # the redirectionURL. Keep going instead of throwing the number away.
    already = _already_active(_api_message(act))
    if not already and (not res.ok or str(act.get("errorCode", "200")) != "200"):
        return None, "activation_failed"
    # fresh_only: a first hunt landing on "already availed" means a PREVIOUS
    # renter activated this number — its Google token is dead on arrival
    # ("subscription already activated"). Refuse it so the farmer never ships
    # a recycled link. relink() re-issues from OUR own session, where
    # "already availed" is expected and the fresh token is valid, so it calls
    # with fresh_only=False.
    if already and fresh_only:
        return None, "recycled_number"

    # 4) google-ai — pull the redirectionURL with the Google token
    try:
        res = s.get(GOOGLE_URL, headers=OFFER_HEADERS, timeout=15)
    except requests.RequestException:
        return None, "no_url"
    if res.status_code in (401, 403, 429) or res.status_code >= 500:
        return None, "network"
    gdata = _response_json(res)
    found = (
        find_google_links(gdata.get("redirectionURL"))
        or find_google_links(gdata)
        or find_google_links(res.text)
    )
    if found:
        return found[0], "ok"

    # No URL anywhere: only now report "already claimed" as the reason.
    if already or _already_active(_api_message(gdata)):
        return None, "already_active"

    return None, "no_url"
