"""LinkForge — proxy pool (PROXYGRAB API).

Why it exists: Jio throttles ``sendOtp`` and the subscriber check by IP, and
GrizzlySMS geo-blocks some services outright. Routing the Jio/Grizzly traffic
through a fresh exit IP per hunt spreads the rate limit instead of parking the
whole pool on one address.

Wiring, point by point:

* ``install(cfg)`` builds the pool once at startup from ``.env`` — the same
  shape as ``shield.install_doh_shield()``, so no command has to thread a pool
  object through its call stack;
* ``for_requests()`` hands out ONE proxy for a whole hunt (sticky) — a login
  that switches exit IP between sendOtp and validate looks like a hijacked
  session to Jio, and the cookies are bound to the address that earned them;
* when nothing is configured (or the pool is empty) it returns ``None`` and
  every caller keeps talking to the internet directly — the farm never gets
  *worse* because the proxy source is down.

Pool hygiene: entries are fetched with ``test=<url>``, so PROXYGRAB only hands
back proxies it claims can reach that host — but in practice that promise is
optimistic (measured: 3 of 10 socks5 entries reach www.jio.com, 0 of 10 socks4
and http). So every entry is probed for real once, in parallel, before it may
carry a hunt; a proxy that fails on a live request later is dropped as well.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import random
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_API = ""
DEFAULT_TEST_URL = "https://www.jio.com/"


class ProxyError(Exception):
    """Proxy source unreachable or answered nonsense."""


@dataclass
class Proxy:
    """One proxy entry, already in the shape ``requests`` wants."""

    url: str
    protocol: str = ""
    country: str = ""
    anonymity: str = ""
    latency_ms: int = 0

    def as_requests(self) -> dict[str, str]:
        return {"http": self.url, "https": self.url}


class ProxyGrab:
    """Thin client for the PROXYGRAB ``/proxies`` and ``/stats`` routes."""

    def __init__(self, api_key: str, base: str = DEFAULT_API, timeout: int = 45) -> None:
        if not api_key.strip():
            raise ProxyError("PROXYGRAB_KEY пуст — без ключа API отвечает 403")
        self.key = api_key.strip()
        self.base = (base or DEFAULT_API).rstrip("/")
        self.timeout = timeout

    def _get(self, path: str) -> Any:
        req = urllib.request.Request(
            self.base + path,
            headers={"X-API-Key": self.key, "Accept": "application/json"},
        )
        try:
            raw = urllib.request.urlopen(req, timeout=self.timeout).read()
        except Exception as e:
            raise ProxyError(f"PROXYGRAB недоступен: {e}") from e
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as e:
            raise ProxyError("PROXYGRAB вернул не JSON") from e

    def stats(self) -> dict:
        return self._get("/stats")

    def fetch(
        self,
        n: int = 10,
        proto: str = "",
        country: str = "",
        anon: str = "",
        test: str = "",
    ) -> list[Proxy]:
        query = {"n": max(1, min(50, int(n)))}
        if proto:
            query["proto"] = proto
        if country:
            query["country"] = country
        if anon:
            query["anon"] = anon
        if test:
            query["test"] = test
        data = self._get("/proxies?" + urllib.parse.urlencode(query))
        rows = data.get("proxies") if isinstance(data, dict) else None
        if rows is None:
            raise ProxyError(f"неожиданный ответ /proxies: {str(data)[:120]}")
        out: list[Proxy] = []
        for row in rows:
            if isinstance(row, str):
                out.append(Proxy(url=row))
                continue
            if not isinstance(row, dict) or not row.get("proxy"):
                continue
            out.append(
                Proxy(
                    url=str(row["proxy"]),
                    protocol=str(row.get("protocol", "")),
                    country=str(row.get("country", "")),
                    anonymity=str(row.get("anonymity", "")),
                    latency_ms=int(row.get("latency_ms") or 0),
                )
            )
        return out


class FileProxySource:
    """Offline list: one ``host:port`` or ``scheme://host:port`` per line.

    Free lists carry no country/anonymity metadata, so ``proto``/``country``/
    ``anon`` filters are ignored here — the pool's real probe against
    ``test_url`` is what decides. The file is re-read on every refresh, so a
    rotator script can rewrite it while the farm keeps running.
    """

    def __init__(self, path: str) -> None:
        self.path = (path or "").strip()

    def fetch(
        self,
        n: int = 10,
        proto: str = "",
        country: str = "",
        anon: str = "",
        test: str = "",
    ) -> list[Proxy]:
        if not self.path:
            raise ProxyError("PROXY_FILE не задан — файловый источник нечего читать")
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                rows = [
                    line.strip()
                    for line in fh
                    if line.strip() and not line.lstrip().startswith("#")
                ]
        except OSError as e:
            raise ProxyError(f"список прокси не читается: {e}") from e
        if not rows:
            raise ProxyError(f"список прокси пуст: {self.path}")
        random.shuffle(rows)
        out: list[Proxy] = []
        for raw in rows[: max(1, min(50, int(n)))]:
            out.append(Proxy(url=raw if "://" in raw else "http://" + raw))
        return out


class ProxyPool:
    """Rotating pool: one sticky proxy per caller, refreshed on a timer."""

    def __init__(
        self,
        client: ProxyGrab,
        size: int = 10,
        proto: str = "",
        country: str = "",
        anon: str = "elite",
        test_url: str = DEFAULT_TEST_URL,
        rotate_seconds: float = 600.0,
        verify: bool = True,
        verify_timeout: float = 20.0,
    ) -> None:
        self.client = client
        self.size = max(1, int(size))
        self.proto = proto
        self.country = country
        self.anon = anon
        self.test_url = test_url
        self.rotate_seconds = max(30.0, float(rotate_seconds))
        self.verify = verify
        self.verify_timeout = max(3.0, float(verify_timeout))

        self._lock = threading.Lock()
        self._items: list[Proxy] = []
        self._cursor = 0
        self._fetched_at = 0.0
        self._last_error = ""
        self._unverified = 0
        self.refresh()

    # ---------------------------------------------------------------- state

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def unverified(self) -> int:
        """How many entries the last refresh threw away after a real probe."""
        return self._unverified

    def snapshot(self) -> list[Proxy]:
        with self._lock:
            return list(self._items)

    def refresh(self, force: bool = False) -> int:
        """Refill the pool. Returns how many proxies are now held."""
        with self._lock:
            fresh = force or (time.time() - self._fetched_at >= self.rotate_seconds)
            held = list(self._items)
        if not fresh:
            return len(held)
        try:
            items = self.client.fetch(
                n=self.size,
                proto=self.proto,
                country=self.country,
                anon=self.anon,
                test=self.test_url,
            )
        except ProxyError as e:
            # Keep whatever is still in hand: a dead source is not a reason to
            # fall back to one burned-out IP mid-run.
            with self._lock:
                self._last_error = str(e)
                self._fetched_at = time.time()
                return len(self._items)

        good = self._verify(items) if self.verify else items

        with self._lock:
            self._unverified = len(items) - len(good)
            if not good and self._items:
                # Nothing new works: keep the old entries rather than silently
                # dropping to a direct connection (which would expose the host
                # IP to Jio mid-run).
                self._last_error = f"проверку не прошёл ни один из {len(items)} прокси"
                self._fetched_at = time.time()
                return len(self._items)
            self._items = good
            self._cursor = 0
            self._fetched_at = time.time()
            self._last_error = ""
            return len(self._items)

    def _verify(self, items: list[Proxy]) -> list[Proxy]:
        """Probe every entry for real; keep only those that carry traffic.

        The source's ``test=`` parameter is not enough on its own: it claims
        reachability the proxy does not always deliver, and one dead proxy costs
        a whole hunt. Probing in parallel makes the pool honest at the cost of a
        few seconds at startup.
        """
        if not items:
            return items
        try:
            import requests
        except ImportError:  # pragma: no cover - requests is a hard dependency
            return items

        def carries(p: Proxy) -> bool:
            try:
                res = requests.get(
                    self.test_url, proxies=p.as_requests(), timeout=self.verify_timeout
                )
            except Exception:  # noqa: BLE001 - every failure means "not usable"
                return False
            return res.status_code < 400

        workers = min(8, len(items))
        with futures.ThreadPoolExecutor(max_workers=workers) as ex:
            verdicts = list(ex.map(carries, items))
        return [p for p, ok in zip(items, verdicts, strict=True) if ok]

    # ------------------------------------------------------------- rotation

    def pick(self) -> Proxy | None:
        """Next proxy, round-robin, refreshing when the pool ran dry."""
        with self._lock:
            stale = time.time() - self._fetched_at >= self.rotate_seconds
        if stale or not self.snapshot():
            self.refresh(force=stale)
        with self._lock:
            if not self._items:
                return None
            item = self._items[self._cursor % len(self._items)]
            self._cursor += 1
            return item

    def report_bad(self, proxy: Proxy) -> None:
        """Drop a proxy that just failed on a real request."""
        with self._lock:
            self._items = [p for p in self._items if p.url != proxy.url]

    def as_requests(self) -> dict[str, str]:
        """Sticky proxies dict for this caller (empty dict = direct)."""
        item = self.pick()
        return item.as_requests() if item else {}

    def describe(self) -> str:
        with self._lock:
            if not self._items:
                return "пул пуст — трафик идёт напрямую" + (
                    f" ({self._last_error})" if self._last_error else ""
                )
            head = ", ".join(
                f"{p.country or '??'}:{p.latency_ms}ms" for p in self._items[:5]
            )
            dropped = f", отсеяно {self._unverified}" if self._unverified else ""
            return f"{len(self._items)} прокси ({head}{dropped})"


# --------------------------------------------------------------- module state

_pool: ProxyPool | None = None
_session_proxy = False
_install_lock = threading.Lock()


def install(
    enabled: bool,
    api_key: str,
    base: str = DEFAULT_API,
    size: int = 10,
    proto: str = "",
    country: str = "",
    anon: str = "elite",
    test_url: str = DEFAULT_TEST_URL,
    rotate_seconds: float = 600.0,
    verify: bool = True,
    verify_timeout: float = 20.0,
    session_proxy: bool = False,
    source: str = "proxygrab",
    file_path: str = "",
    log: Any = None,
) -> ProxyPool | None:
    """Build the process-wide pool (no-op when disabled).

    ``session_proxy`` decides what ``for_sessions()`` answers; see there.
    ``source`` is ``proxygrab`` (API) or ``file`` (local list, ``file_path``).
    """
    global _pool, _session_proxy
    with _install_lock:
        _session_proxy = bool(session_proxy)
        if not enabled:
            _pool = None
            return None
        try:
            client = (
                FileProxySource(file_path)
                if (source or "").strip().lower() == "file"
                else ProxyGrab(api_key, base=base)
            )
            _pool = ProxyPool(
                client,
                size=size,
                proto=proto,
                country=country,
                anon=anon,
                test_url=test_url,
                rotate_seconds=rotate_seconds,
                verify=verify,
                verify_timeout=verify_timeout,
            )
        except ProxyError as e:
            if callable(log):
                log(f"прокси выключены: {e}")
            _pool = None
            return None
        if callable(log):
            log(f"прокси включены — {_pool.describe()}")
        return _pool


def active() -> ProxyPool | None:
    return _pool


def for_requests() -> dict[str, str]:
    """Sticky proxies for one unit of work; empty dict when disabled."""
    pool = _pool
    return pool.as_requests() if pool else {}


def for_sessions() -> dict[str, str]:
    """Proxies for traffic that reuses an OLD cookie jar — empty by default.

    A hunt earns its cookies and spends them inside one 15-minute window on one
    exit IP, which is coherent. Replaying saved cookies later from a different
    IP is not: Jio sees a login jump countries overnight and may treat it as a
    hijacked session, and a wrong verdict there costs a live link. So reusing
    sessions (``relink``, ``keepalive``) stays on the direct connection unless
    ``PROXY_SESSIONS=1`` says otherwise.
    """
    if not _session_proxy:
        return {}
    return for_requests()


def describe() -> str:
    pool = _pool
    if pool is None:
        return "выключены (PROXY_ENABLED=0)"
    return pool.describe()
