"""Command line entry point.

    linkforge run        — farm loop with Telegram delivery
    linkforge hunt       — one hunt, prints the link
    linkforge balance    — provider balance
    linkforge links      — links already mined
    linkforge check      — probe saved links (alive / dead / unknown)
    linkforge relink     — re-issue a token from saved cookies

All commands read ``.env`` from the working directory.
"""

from __future__ import annotations

import os

import typer

from .checker import check_link, format_age
from .config import load_config
from .console import console
from .hunter import create_provider
from .jio.relink import relink as do_relink
from .runner import Farm, run as run_loop
from .storage.sessions import SessionStore
from .storage.store import Store

app = typer.Typer(add_completion=False, help="Google AI Pro redemption link farm on Jio selfcare.")


def _log(kind: str, *rest: object) -> None:
    console.print("[dim]%s[/dim] %s" % (kind, " ".join(str(x) for x in rest)), highlight=False)


@app.command()
def run(
    minutes: int = typer.Option(240, "--minutes", "-m", help="How long to farm, in minutes."),
    target: int = typer.Option(0, "--target", "-t", help="Stop after this many new links (0 = run full time)."),
    telegram: bool = typer.Option(True, "--telegram/--no-telegram", help="Push links to Telegram."),
) -> None:
    """Farm until the time budget is spent."""
    found = run_loop(minutes=minutes, target=target, telegram=telegram, log=_log)
    console.print("[success]found %d link(s)[/success]" % len(found))


@app.command()
def hunt(
    telegram: bool = typer.Option(False, "--telegram/--no-telegram", help="Push the link to Telegram."),
) -> None:
    """Rent one number and mine one link."""
    cfg = load_config(require_credentials=True)
    farm = Farm(cfg, log=_log, telegram=telegram)
    link = farm.hunt()
    if farm.deliver(link):
        console.print(link, highlight=False)
    else:
        console.print("[warning]no link this time[/warning]")
        raise typer.Exit(code=1)


@app.command()
def balance() -> None:
    """Show the SMS provider balance."""
    cfg = load_config(require_credentials=True)
    provider = create_provider(cfg)
    console.print("$%.2f" % float(provider.balance()))


@app.command()
def links(
    limit: int = typer.Option(15, "--limit", "-n", help="How many rows to print."),
    out: str = typer.Option("", "--out", "-o", help="Write the links to this file."),
) -> None:
    """List the links already mined (newest first)."""
    store = Store(load_config().db_path)
    rows = store.hunt_links()[:limit]
    if not rows:
        console.print("[warning]nothing mined yet[/warning]")
        return
    lines = [row["link"] for row in rows]
    for row in rows:
        console.print("%s  %s" % (row.get("created_at", "")[:19], row["link"]), highlight=False)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        console.print("[info]written:[/info] %s" % os.path.abspath(out))


@app.command()
def check(
    source: str = typer.Option("", "--file", "-f", help="File with one link per line (default: database)."),
    limit: int = typer.Option(10, "--limit", "-n", help="How many links to probe."),
) -> None:
    """Probe links and classify them.

    A probe cannot prove a link is still redeemable: Google answers both a live
    and a consumed token with a sign-in page. DEAD means the token was rejected
    outright, UNKNOWN is the normal answer for everything else.
    """
    rows: list[tuple[str, str | None]] = []
    if source:
        with open(source, encoding="utf-8") as fh:
            rows = [(line.strip(), None) for line in fh if line.strip()]
    else:
        rows = [(row["link"], row.get("created_at")) for row in Store(load_config().db_path).hunt_links()]
    if not rows:
        console.print("[warning]no links to check[/warning]")
        return
    for url, stamp in rows[:limit]:
        verdict, probe, age = check_link(url, stamp)
        colour = {"DEAD": "error", "SUSPECT": "warning"}.get(verdict, "info")
        console.print(
            "[%s]%s[/%s]  http=%s  age=%s  %s"
            % (colour, verdict, colour, probe.status_code or probe.error or "-", format_age(age), url),
            highlight=False,
        )


@app.command()
def relink(
    phone: str = typer.Option(..., "--phone", "-p", help="Phone whose saved cookies to reuse."),
) -> None:
    """Re-issue a fresh token from the cookies of an older session."""
    cfg = load_config(require_credentials=True)
    link, status, error = do_relink(phone, SessionStore(cfg.db_path))
    console.print("%s %s" % (status, link or error or ""), highlight=False)
    if not link:
        raise typer.Exit(code=1)


@app.callback()
def main() -> None:
    """Keep typer from collapsing into a single command."""


if __name__ == "__main__":
    app()
