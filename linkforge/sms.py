"""LinkForge — provider contract shared by every SMS client.

``PVACodes`` and ``GrizzlySMS`` are interchangeable behind this protocol, so
the hunt cycle never imports a concrete provider. The error classes exist so
the orchestrator can react to the two cases it must treat differently without
knowing which provider raised them:

* ``LowBalance`` — the account is empty; park the pool and let the refund
  worker return credits instead of hammering the API.
* ``LockedError`` — the provider refused a cancel and told us how long its
  own lock window still runs; the refund worker reschedules on that number
  instead of guessing.

Both are plain exceptions, so a third provider only needs to raise them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class SmsError(Exception):
    """Base error for every provider client."""


class LowBalance(SmsError):
    """The provider refused the rent: not enough credits on the account."""


class LockedError(SmsError):
    """The provider refuses the cancel while its own lock window is open.

    ``retry_after`` is the number of seconds the provider itself asked us to
    wait; the refund worker wakes on exactly that moment.
    """

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


class RegionBlocked(SmsError):
    """The provider refuses to serve our IP (region or service restriction)."""


@runtime_checkable
class SMSProvider(Protocol):
    """Protocol that every SMS provider client must implement."""

    def balance(self) -> float: ...
    def rent(self, max_price: float | None = None) -> tuple[str, str]: ...
    def status(self, act_id: str) -> tuple[str, str | None]: ...
    def ready(self, act_id: str) -> None: ...
    def complete(self, act_id: str) -> None: ...
    def cancel(self, act_id: str) -> None: ...
