"""LinkForge — Core hunt cycle orchestrator.

Ties together SMS provider rentals, Jio authentication, link hunting, and
storage.  Designed to be driven by the CLI layer (``cli.py``) so that the
live-dashboard can observe worker state in real time.

The SMS provider is chosen at run time (``create_provider``): PVACodes by
default, GrizzlySMS when ``PROVIDER=grizzly``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum, auto

import requests

from linkforge import proxy
from linkforge.config import Config
from linkforge.console import tg_send
from linkforge.jio.auth import (
    OTP_ERROR,
    OTP_NOT_JIO,
    OTP_OK,
    OTP_RATE_LIMITED,
    UA,
    jio_send_otp_detail,
    jio_validate_otp,
)
from linkforge.jio.check import UNKNOWN, check_subscriber, normalize_phone
from linkforge.jio.hunt import hunt_link
from linkforge.refund import RefundWorker
from linkforge.sms import LowBalance, SMSProvider
from linkforge.storage.sessions import SessionStore
from linkforge.storage.store import Store

# Jio throttles sendOtp globally. One worker hitting the wall means every
# worker will hit it, so park the whole pool instead of burning a paid number
# per attempt.
_otp_lock = threading.Lock()
_otp_cooldown_until = 0.0
OTP_MAX_RETRIES = 3
OTP_RETRY_DELAY = 20.0

# A completely drained balance is not an error: the refund worker returns
# money for cancelled numbers, so park the whole pool on it and keep going.
_funds_lock = threading.Lock()
_funds_gate_until = 0.0
FUNDS_WAIT = 60.0


def funds_gate_left() -> float:
    """> 0 while the pool is parked waiting for auto-refunded credits."""
    with _funds_lock:
        return max(0.0, _funds_gate_until - time.time())


def _wait_for_funds_window(state: WorkerState) -> None:
    """Hold off renting while the account has no credits to spend."""
    while True:
        left = funds_gate_left()
        if left <= 0:
            return
        state.phase = Phase.RENTING
        state.error = "недостаточно баланса — ждём авто-возврата"
        time.sleep(min(left, 5.0))

class HuntError(Exception):
    """Recoverable problem during one hunt attempt."""


# ------------------------------------------------------------------- provider protocol

# ``SMSProvider`` lives in ``linkforge.sms`` so both clients and the CLI can use
# it without dragging the orchestrator in; re-exported here for compatibility.


def poll_otp(provider: SMSProvider, act_id: str, timeout: int = 180, interval: int = 5) -> str:
    """Poll the provider until the OTP arrives or the deadline expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state, code = provider.status(act_id)
        if state == "OK" and code:
            return code
        if state == "CANCEL":
            raise HuntError("активация отменена на стороне провайдера")
        time.sleep(interval)
    raise HuntError(f"таймаут OTP после {timeout} с")


def _wait_for_otp_window(state: WorkerState) -> None:
    """Sleep out any global sendOtp cooldown before renting a new number."""
    while True:
        with _otp_lock:
            left = _otp_cooldown_until - time.time()
        if left <= 0:
            return
        state.phase = Phase.SENDING_OTP
        state.error = f"Jio rate-limit OTP, пауза {int(left)} с"
        time.sleep(min(left, 5.0))


def _send_otp_with_backoff(
    s: requests.Session, state: WorkerState
) -> str | None:
    """Send the OTP, reusing the paid number across a throttle.

    Returns the OTP_OK status on success.  On a global throttle it waits and
    retries the *same* number (still paid for) up to OTP_MAX_RETRIES, parking
    the whole pool via the cooldown so parallel workers stop renting.  If the
    number itself is dead it returns OTP_ERROR at once, and if Jio says the
    number is not on its network it returns OTP_NOT_JIO at once.

    Returns None when the number has to be given up on; the reason is left in
    ``state.error``.
    """
    global _otp_cooldown_until
    for attempt in range(OTP_MAX_RETRIES):
        status, detail = jio_send_otp_detail(s, state.phone)
        if status == OTP_OK:
            return status

        if status in (OTP_ERROR, OTP_NOT_JIO):
            # The number is wrong or rejected outright — retrying will not
            # help. Keep Jio's own wording: a bare "OTP gagal dikirim" hides
            # which of the two it was.
            state.error = f"{status}:{detail}" if detail else status
            return None

        # OTP_RATE_LIMITED: throttle is global, park every worker and retry
        # this same number once the window reopens.
        delay = OTP_RETRY_DELAY * (attempt + 1)
        with _otp_lock:
            _otp_cooldown_until = max(_otp_cooldown_until, time.time() + delay)
        state.error = f"Jio rate-limit OTP, ждём {int(delay)} с (номер {state.phone})"
        time.sleep(delay)

    state.error = OTP_RATE_LIMITED
    return None


# ------------------------------------------------------------------- worker state


class Phase(Enum):
    IDLE = auto()
    RENTING = auto()
    CHECKING = auto()
    SENDING_OTP = auto()
    WAITING_OTP = auto()
    VALIDATING = auto()
    HUNTING = auto()
    DONE = auto()
    ERROR = auto()


@dataclass
class WorkerState:
    wid: int
    phase: Phase = Phase.IDLE
    phone: str = ""
    act_id: str = ""
    otp: str = ""
    logged_in: bool = False
    link: str | None = None
    error: str = ""
    attempts: int = 0


# ------------------------------------------------------------------- hunt cycle


def hunt_once(
    provider: SMSProvider,
    store: Store,
    refunds: RefundWorker,
    slots: threading.Semaphore,
    cfg: Config,
    state: WorkerState,
    sessions: SessionStore | None = None,
) -> str | None:
    """Run one complete hunt attempt, updating *state* for live display.

    Returns the link if found, otherwise None.
    """
    slots.acquire()
    state.phase = Phase.RENTING
    state.phone = ""
    state.act_id = ""
    state.otp = ""
    state.logged_in = False
    state.link = None
    state.error = ""
    completed = False
    global _funds_gate_until

    try:
        # 0) never rent while Jio is throttling sendOtp — a number bought now
        #    would just be thrown away.
        _wait_for_otp_window(state)

        # 1) rent a number --------------------------------------------------
        _wait_for_funds_window(state)
        state.phase = Phase.RENTING
        state.error = ""
        try:
            state.act_id, raw = provider.rent(cfg.effective_max_price)
        except LowBalance:
            # Nothing dramatic: the account is empty, the refund worker will
            # return money for cancelled numbers. Park the pool briefly and
            # show a calm one-liner instead of a scary error.
            with _funds_lock:
                _funds_gate_until = max(_funds_gate_until, time.time() + FUNDS_WAIT)
            state.phase = Phase.RENTING
            state.error = "недостаточно баланса — ждём авто-возврата"
            return None
        state.phone = normalize_phone(raw)

        # 2) check subscriber -----------------------------------------------
        # Jio rate-limits this endpoint (429 CAPTCHA_REQUIRED); UNKNOWN means
        # "we could not tell" — send the OTP anyway instead of wasting the rent.
        # The probe goes out over the same exit IP the session will use, so the
        # two calls look like one client instead of two addresses.
        exit_ip = proxy.for_requests()
        state.phase = Phase.CHECKING
        verdict = check_subscriber(state.phone, proxies=exit_ip)
        if verdict == "NOT_JIO":
            state.phase = Phase.ERROR
            state.error = "не абонент Jio"
            # Probe the cancel at once: pvacodes answers with its own remaining
            # lock time, and the refund worker wakes on it. Waiting first would
            # only push the same cancel later.
            refunds.schedule(state.act_id, 0, aggressive=True)
            return None
        if verdict == UNKNOWN:
            state.error = "проверка абонента в rate-limit, продолжаем OTP"

        # 3) create Jio session --------------------------------------------
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
        if exit_ip:
            # One proxy for the whole hunt: sendOtp, validate and hunt must all
            # leave from the address the cookies were earned on.
            s.proxies.update(exit_ip)

        # 4) send OTP -------------------------------------------------------
        # The number is already paid for: on a throttle wait it out and reuse
        # it instead of dropping it and renting another.
        state.phase = Phase.SENDING_OTP
        sent = _send_otp_with_backoff(s, state)
        if sent is None:
            state.phase = Phase.ERROR
            # _send_otp_with_backoff leaves "STATUS" or "STATUS:jio's message"
            # in state.error; split off the raw message so the database keeps
            # what Jio actually said.
            status, _, detail = state.error.partition(":")
            if status == OTP_RATE_LIMITED:
                state.error = "OTP rate-limit: Jio не отдал SMS, возврат запланирован"
                refunds.schedule(state.act_id, cfg.otp_fail_delay)
            elif status == OTP_NOT_JIO:
                # Jio confirmed the number is not on its network. Same verdict
                # as the subscriber check, so it gets the same fast cancel
                # instead of sitting out the 7-minute OTP failure delay.
                state.error = "не абонент Jio (через OTP)"
                refunds.schedule(state.act_id, 0, aggressive=True)
            elif status == OTP_ERROR:
                state.error = f"OTP не отправлен ({detail})" if detail else "OTP не отправлен"
                refunds.schedule(state.act_id, cfg.otp_fail_delay)
            return None
        state.error = ""

        provider.ready(state.act_id)

        # 5) poll OTP -------------------------------------------------------
        state.phase = Phase.WAITING_OTP
        state.otp = poll_otp(provider, state.act_id, timeout=cfg.otp_timeout)

        # 6) validate OTP ---------------------------------------------------
        state.phase = Phase.VALIDATING
        if not jio_validate_otp(s, state.phone, state.otp):
            state.phase = Phase.ERROR
            state.error = "неверный OTP"
            refunds.schedule(state.act_id, cfg.cancel_delay)
            return None

        state.logged_in = True

        # Save the login at once: from here on the cookies are the only proof
        # we ever authenticated, and they cost a paid rent to obtain. If the
        # hunt below dies we still keep a session we can re-mint from later.
        if sessions is not None:
            try:
                sessions.save(state.phone, s.cookies.get_dict())
            except Exception:
                pass

        # 7) hunt link ------------------------------------------------------
        # fresh_only: refuse numbers a previous renter already activated —
        # their Google token redeems to "subscription already activated".
        state.phase = Phase.HUNTING
        link, hunt_status = hunt_link(s, fresh_only=True)
        # From this line the number is settled with the provider: the rent is
        # consumed whether or not a link came out, so no refund may follow.
        provider.complete(state.act_id)
        completed = True

        # The hunt just walked several Jio calls; if any of them rotated the
        # auth cookies, the copy saved after the OTP is already stale.
        if sessions is not None:
            try:
                sessions.save_cookies(state.phone, s.cookies.get_dict())
            except Exception:
                pass

        if link:
            state.link = link
            state.phase = Phase.DONE
            if sessions is not None:
                try:
                    sessions.set_link(state.phone, link)
                except Exception:
                    pass
            # File first: a dead bot must never eat a link we paid for.
            from linkforge.runfile import append_link

            saved = append_link(cfg.run_file, state.phone, link)
            if not saved and cfg.run_file:
                state.error = "ссылка есть, но не удалось записать файл"
            # Notification is best-effort: a failure here must not turn a
            # found link into a miss (the caller counts returns, not files).
            try:
                tg_send(
                    f"🎯 Ссылка Google AI Pro!\n\nНомер : {state.phone}\nСсылка :\n{link}",
                    cfg.tg_bot_token,
                    cfg.tg_chat_id,
                )
            except Exception:
                pass
        else:
            state.phase = Phase.DONE
            state.error = hunt_status

        return link

    except KeyboardInterrupt:
        raise
    except Exception as e:
        state.phase = Phase.ERROR
        state.error = str(e)
        if state.act_id and not completed:
            refunds.schedule(state.act_id, cfg.cancel_delay)
        return None
    finally:
        state.attempts += 1
        # release() must run even if persisting the row fails: a leaked slot
        # would permanently shrink the pool until the run wedges.
        try:
            if state.phone or state.act_id:
                store.save(
                    state.phone,
                    state.act_id,
                    state.otp,
                    state.logged_in,
                    state.link,
                    state.error or None,
                )
        except Exception:
            pass
        finally:
            slots.release()


# ------------------------------------------------------------------- provider factory


def create_provider(cfg: Config) -> SMSProvider:
    """Create the SMS client named by ``PROVIDER`` (default: pvacodes)."""
    kind = (cfg.provider or "pvacodes").strip().lower()

    if kind in ("grizzly", "grizzlysms", "grizzly-sms"):
        from linkforge.grizzly.client import GrizzlySMS

        pool = proxy.active()
        client: object = GrizzlySMS(
            cfg.grizzly_api_key,
            service=cfg.grizzly_service,
            country=cfg.grizzly_country,
            proxies=pool.as_requests() if pool else None,
            max_price=cfg.effective_max_price,
        )
    elif kind == "pvacodes":
        from linkforge.pvacodes.client import PVACodes

        client = PVACodes(
            cfg.email,
            cfg.password,
            country=cfg.country,
            services=cfg.service,
            session_id=cfg.pvacodes_session_id,
        )
    elif kind in ("stacked", "pvacodes-api", "pva-api"):
        from linkforge.stacked.client import StackedSMS

        pool = proxy.active()
        client = StackedSMS(
            cfg.stacked_api_key,
            services=cfg.stacked_service,
            country=cfg.stacked_country,
            proxies=pool.as_requests() if pool else None,
            max_price=cfg.effective_max_price,
        )
    else:
        raise SystemExit(
            f"PROVIDER='{cfg.provider}' неизвестен — поддерживаются: pvacodes, stacked, grizzly"
        )

    if not isinstance(client, SMSProvider):
        raise SystemExit(f"Провайдер '{kind}' не реализует контракт SMSProvider")

    return client
