"""LinkForge — Refund Worker.

Background thread that cancels failed activations to reclaim balance.

Three modes:
- **normal** — wait the full delay, then cancel (for OTP timeout cases)
- **aggressive** — retry every 30s starting immediately, cancel as soon as the
  lock window opens (for fast-fail cases like "not a subscriber")
- **lock window** — when pvacodes refuses a cancel it answers with how many
  seconds are left. That number is trusted: the attempt is rescheduled for
  exactly that moment instead of guessed at, which is the fastest legal
  cancel (the lock itself is the floor).
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Protocol


class Cancellable(Protocol):
    """Anything that can cancel an activation."""
    def cancel(self, act_id: str) -> None: ...


# Fallback retry for aggressive mode when the reply carries no countdown
# (network blip, unexpected body). The lock itself is handled separately by
# the site's own number, so this no longer needs to cover a whole window.
AGGRESSIVE_INTERVAL = 5
# Same fallback for a scheduled refund that is not in a hurry
NORMAL_INTERVAL = 10
# Poll granularity: how often the queue is inspected
TICK = 0.5
# Added to the site's own countdown so we never land on the closing edge
LOCK_MARGIN = 1.0
# Cap on a parsed countdown, so a bogus reply cannot park a refund for hours
LOCK_CAP = 180.0
# After this long the refund is abandoned: without it a permanently refused
# cancel keeps wait_all() blocked forever and the CLI never exits.
REFUND_MAX_AGE = 900.0


class RefundWorker:
    """Cancels failed activations after their lock window to reclaim balance."""

    def __init__(self, sms: Cancellable, log: Callable[[str], None] | None = None):
        self.sms = sms
        self.log = log or (lambda _: None)
        self.q: queue.Queue[tuple[str, float, bool, float]] = queue.Queue()
        self.pending = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def schedule(self, act_id: str, delay: float, aggressive: bool = False) -> None:
        """Schedule a refund.

        Args:
            act_id: The activation ID to cancel.
            delay: Seconds to wait before first cancel attempt.
            aggressive: If True, wake on the site's own countdown and retry a
                transient failure in 5s; if False, wait the full delay once.
        """
        with self._lock:
            self.pending += 1
        self.q.put((act_id, time.time() + delay, aggressive, time.time()))
        mode = "aggressive" if aggressive else "normal"
        self.log(f"refund #{act_id[-8:]} запланирован ({mode}, {int(delay)}s)")

    def _loop(self) -> None:
        while True:
            time.sleep(TICK)
            due_now = time.time()
            batch: list[tuple[str, float, bool]] = []
            while True:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break

            remaining: list[tuple[str, float, bool, float]] = []
            for act_id, fire_at, aggressive, created_at in batch:
                if time.time() - created_at > REFUND_MAX_AGE:
                    self.log(
                        f"refund #{act_id[-8:]} сдан после {int(REFUND_MAX_AGE)} с попыток — "
                        f"отмени вручную на сайте"
                    )
                    with self._lock:
                        self.pending -= 1
                    continue
                if due_now >= fire_at:
                    try:
                        self.sms.cancel(act_id)
                        self.log(f"refund #{act_id[-8:]} выполнен ✓")
                        with self._lock:
                            self.pending -= 1
                    except Exception as e:
                        # A cancel refusal that carries its own countdown wins
                        # over any guessed interval (see PvaLockedError).
                        retry_after = getattr(e, "retry_after", None)
                        if isinstance(retry_after, (int, float)) and retry_after > 0:
                            wait = min(float(retry_after) + LOCK_MARGIN, LOCK_CAP)
                            self.log(
                                f"refund #{act_id[-8:]} заблокирован на {int(retry_after)} с "
                                f"— повтор через {int(wait)} с"
                            )
                            remaining.append((act_id, time.time() + wait, aggressive, created_at))
                            continue
                        err = str(e)
                        # no countdown in the reply: fall back to the interval
                        retry_delay = AGGRESSIVE_INTERVAL if aggressive else NORMAL_INTERVAL
                        self.log(f"refund #{act_id[-8:]} не удался: {err} — повтор через {int(retry_delay)} с")
                        remaining.append((act_id, time.time() + retry_delay, aggressive, created_at))
                else:
                    remaining.append((act_id, fire_at, aggressive, created_at))

            for item in remaining:
                self.q.put(item)

    def wait_all(self) -> None:
        """Block until all pending refunds are processed."""
        while True:
            with self._lock:
                if self.pending <= 0:
                    return
            time.sleep(1)
