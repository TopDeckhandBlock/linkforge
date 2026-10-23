"""LinkForge — DNS-over-HTTPS shield for the PVACodes site.

Monkey-patches ``socket.getaddrinfo`` so the PVACodes hostname is always
resolved through Cloudflare DoH, bypassing ISP-level DNS hijacking.
"""

from __future__ import annotations

import json
import socket
import urllib.request

_DOH_CACHE: str | None = None
_PVA_HOST = "beta.pvacodes.com"


def _doh_resolve(host: str) -> str | None:
    global _DOH_CACHE
    if _DOH_CACHE:
        return _DOH_CACHE
    try:
        req = urllib.request.Request(
            f"https://1.1.1.1/dns-query?name={host}&type=A",
            headers={"accept": "application/dns-json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            for ans in json.loads(r.read().decode()).get("Answer", []):
                if ans.get("type") == 1:
                    _DOH_CACHE = ans["data"]
                    return _DOH_CACHE
    except Exception:
        pass
    return None


def install_doh_shield() -> None:
    """Patch socket.getaddrinfo to resolve beta.pvacodes.com via DoH."""
    orig = socket.getaddrinfo

    def patched(host, port, *a, **kw):
        if host == _PVA_HOST:
            ip = _doh_resolve(host)
            if ip:
                return orig(ip, port, *a, **kw)
        return orig(host, port, *a, **kw)

    socket.getaddrinfo = patched
