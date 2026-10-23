"""LinkForge — centralised configuration from .env and CLI args."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _find_env() -> Path:
    """Look for .env next to the project root, falling back to cwd."""
    for base in (Path(__file__).resolve().parent.parent, Path.cwd()):
        candidate = base / ".env"
        if candidate.exists():
            return candidate
    return Path(".env")


def _env_number(name: str, default: float, minimum: float = 0.0) -> float:
    """Read a numeric setting, tolerating an unset or malformed value.

    A typo in ``.env`` should not crash with a raw traceback, and a zero or
    negative worker/timeout value should not silently hang the farm.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(
            f"{name} в .env не число: '{raw}' (ожидалось число, напр. {default:g})"
        ) from None
    return max(minimum, value)


def _env_phone_queue() -> str:
    return os.getenv("PVACODES_SERVICE", "Jio50").strip() or "Jio50"


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a yes/no setting. Anything but 1/true/yes/on counts as off."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "да")


def load_config(require_credentials: bool = False) -> Config:
    """Load .env and return a populated Config instance.

    Credentials are only *required* for commands that actually talk to the
    provider (``run``, ``balance``). Read-only commands like ``watch``,
    ``check`` or ``stats`` must keep working on a machine without them.
    """
    env_path = _find_env()
    load_dotenv(env_path)

    email = os.getenv("PVACODES_EMAIL", "").strip()
    password = os.getenv("PVACODES_PASSWORD", "").strip()
    provider = os.getenv("PROVIDER", "pvacodes").strip().lower() or "pvacodes"
    grizzly_key = os.getenv("GRIZZLY_API_KEY", "").strip()
    stacked_key = os.getenv("STACKED_API_KEY", "").strip()
    session_id = os.getenv("PVACODES_SESSION_ID", "").strip()

    if require_credentials:
        if provider.startswith("grizzly"):
            if not grizzly_key:
                raise SystemExit(
                    "GRIZZLY_API_KEY не задан в .env\n"
                    "  1. Возьми API-ключ в ЛК https://grizzlysms.com (Настройки → API)\n"
                    "  2. Впиши его в .env: GRIZZLY_API_KEY=...\n"
                    "  3. Сервис Jio и страна India уже настроены по умолчанию"
                )
        elif provider.startswith("stacked"):
            if not stacked_key:
                raise SystemExit(
                    "STACKED_API_KEY не задан в .env\n"
                    "  1. Открой https://beta.pvacodes.com -> раздел API\n"
                    "  2. Скопируй API-ключ и впиши: STACKED_API_KEY=...\n"
                    "  3. PROVIDER=stacked; сервисы Jio уже в STACKED_SERVICE"
                )
        elif not session_id and (not email or not password):
            msg = (
                "PVACODES_EMAIL / PVACODES_PASSWORD не заданы в .env\n"
                "  1. Зарегистрируйся на https://beta.pvacodes.com и пополни баланс\n"
                "  2. Скопируй .env.example в .env\n"
                "  3. Впиши PVACODES_EMAIL и PVACODES_PASSWORD (аккаунт pvacodes)"
            )
            raise SystemExit(msg)

    return Config(
        provider=provider,
        email=email,
        password=password,
        pvacodes_session_id=session_id,
        service=_env_phone_queue(),
        country=os.getenv("PVACODES_COUNTRY", "India").strip() or "India",
        max_price=_env_number("MAX_PRICE", 1.0),
        cancel_delay=_env_number("CANCEL_DELAY_SECONDS", 150.0),
        otp_fail_delay=_env_number("OTP_FAIL_DELAY_SECONDS", 420.0),
        otp_timeout=int(_env_number("OTP_TIMEOUT_SECONDS", 180, minimum=1)),
        concurrency=int(_env_number("CONCURRENCY", 2, minimum=1)),
        db_path=os.getenv("DB_PATH", "results.db").strip() or "results.db",
        links_dir=os.getenv("LINKS_DIR", "links").strip() or "links",
        tg_bot_token=os.getenv("TG_BOT_TOKEN", "").strip(),
        tg_chat_id=os.getenv("TG_CHAT_ID", "").strip(),
        grizzly_api_key=grizzly_key,
        grizzly_service=os.getenv("GRIZZLY_SERVICE", "jio").strip() or "jio",
        grizzly_country=os.getenv("GRIZZLY_COUNTRY", "22").strip() or "22",
        stacked_api_key=stacked_key,
        stacked_service=os.getenv("STACKED_SERVICE", "Jio11,Jio14,Jio17,Jio19").strip() or "Jio11,Jio14,Jio17,Jio19",
        stacked_country=os.getenv("STACKED_COUNTRY", "India").strip() or "India",
        proxy_enabled=_env_bool("PROXY_ENABLED", False),
        proxy_source=os.getenv("PROXY_SOURCE", "proxygrab").strip().lower(),
        proxy_file=os.getenv("PROXY_FILE", "").strip(),
        proxygrab_url=os.getenv("PROXYGRAB_URL", "").strip(),
        proxygrab_key=os.getenv("PROXYGRAB_KEY", "").strip(),
        proxy_proto=os.getenv("PROXY_PROTO", "").strip().lower(),
        proxy_country=os.getenv("PROXY_COUNTRY", "").strip().upper(),
        proxy_anon=os.getenv("PROXY_ANON", "elite").strip().lower(),
        proxy_pool_size=int(_env_number("PROXY_POOL_SIZE", 10, minimum=1)),
        proxy_rotate_seconds=_env_number("PROXY_ROTATE_SECONDS", 600.0, minimum=30.0),
        proxy_test_url=os.getenv("PROXY_TEST_URL", "https://www.jio.com/").strip(),
        proxy_verify=_env_bool("PROXY_VERIFY", True),
        proxy_verify_timeout=_env_number("PROXY_VERIFY_TIMEOUT", 20.0, minimum=3.0),
        proxy_sessions=_env_bool("PROXY_SESSIONS", False),
    )


@dataclass
class Config:
    """All runtime configuration, sourced from .env and overridden by CLI args."""

    provider: str = "pvacodes"
    email: str = ""
    password: str = ""
    pvacodes_session_id: str = ""  # SECURE_SESSION_ID cookie — вход без пароля
    service: str = "Jio50"  # comma-separated fallback queue, e.g. "Jio50,JIO7"
    country: str = "India"
    max_price: float = 1.0
    cancel_delay: float = 150.0
    otp_fail_delay: float = 420.0
    otp_timeout: int = 180
    concurrency: int = 2
    db_path: str = "results.db"
    links_dir: str = "links"

    # telegram (optional)
    tg_bot_token: str = ""
    tg_chat_id: str = ""

    # grizzly (used when PROVIDER=grizzly)
    grizzly_api_key: str = ""
    grizzly_service: str = "jio"
    grizzly_country: str = "22"

    # stacked (PVACodes native API, used when PROVIDER=stacked)
    stacked_api_key: str = ""
    stacked_service: str = "Jio11,Jio14,Jio17,Jio19"
    stacked_country: str = "India"

    # proxy pool; off by default — direct connection otherwise
    proxy_enabled: bool = False
    # "proxygrab" (API) or "file" (local list rewritten by a rotator)
    proxy_source: str = "proxygrab"
    proxy_file: str = ""
    proxygrab_url: str = ""
    proxygrab_key: str = ""
    proxy_proto: str = ""
    proxy_country: str = ""
    proxy_anon: str = "elite"
    proxy_pool_size: int = 10
    proxy_rotate_seconds: float = 600.0
    proxy_test_url: str = "https://www.jio.com/"
    # The source claims a proxy works; in practice only ~1/3 of the socks5
    # entries survive a real request. Probing here turns that into 100%.
    proxy_verify: bool = True
    proxy_verify_timeout: float = 20.0
    # Reuse saved cookies (relink/keepalive) through a proxy too? Off by
    # default: those cookies were earned on another exit IP, and Jio may read
    # the jump as a hijacked session.
    proxy_sessions: bool = False

    # runtime overrides (set by CLI)
    max_price_override: float | None = field(default=None, repr=False)
    target: int | None = field(default=None, repr=False)
    duration: float | None = field(default=None, repr=False)
    count: int = field(default=1, repr=False)
    concurrency_override: int | None = field(default=None, repr=False)
    run_file: str = field(default="", repr=False)

    @property
    def effective_max_price(self) -> float | None:
        if self.max_price_override is not None:
            return self.max_price_override
        return self.max_price

    @property
    def effective_concurrency(self) -> int:
        # Never return 0: with no workers the run loop would spin forever
        # waiting for a stop condition that only workers can satisfy.
        if self.concurrency_override is not None:
            return max(1, self.concurrency_override)
        return max(1, self.concurrency)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.tg_bot_token and self.tg_chat_id)
