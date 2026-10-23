"""LinkForge — per-run link file.

Every ``run`` gets its own timestamped text file.  A found link is appended
here *before* the Telegram alert, so a dead bot or network blip can never eat
a link that we already paid a number for.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()


def create_run_file(links_dir: str) -> Path:
    """Create and return a fresh timestamped file for this run."""
    base = Path(links_dir)
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = base / f"run-{stamp}.txt"
    # two runs in the same second must not share a file
    n = 1
    while path.exists():
        path = base / f"run-{stamp}-{n}.txt"
        n += 1
    path.write_text("", encoding="utf-8")
    return path


def append_link(path: str | Path, phone: str, link: str) -> bool:
    """Append one link to the run file, one link per line.

    The file holds bare URLs only — no header, no phone, no timestamp — so it
    can be pasted or piped anywhere as-is. *phone* is accepted for the call
    signature and ignored.
    """
    if not path:
        return False
    try:
        with _lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(link.strip() + "\n")
    except OSError:
        return False
    return True
