"""Terminal output: shared consoles, the result model, and all presentation.

This module owns everything user-facing (rich Consoles, tables, progress bars,
size/duration formatting) so the command layer and upload pipeline stay thin.
"""

from __future__ import annotations

import json as jsonlib
import time
from dataclasses import asdict, dataclass

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from . import util

console = Console()
err = Console(stderr=True)


def _fail(message: str) -> None:
    err.print(f"[bold red]error:[/] {message}")
    raise typer.Exit(code=1)


@dataclass
class LinkResult:
    """One published object + its shareable link."""

    key: str
    size: int
    url: str
    link_type: str  # "presigned" | "public"
    expires_at: int | None


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def _human_duration(secs: int) -> str:
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins and not days:
        parts.append(f"{mins}m")
    return " ".join(parts) or "<1m"


def _is_expired(expires_at: int | None, now: int | None = None) -> bool:
    """True only for a presigned link whose deadline has passed."""
    if expires_at is None:
        return False
    return expires_at <= (now if now is not None else int(time.time()))


def _fmt_remaining(expires_at: int | None) -> str:
    """Human 'time left' for a presigned link, or a marker for other states."""
    if expires_at is None:
        return "[green]permanent[/]"
    if _is_expired(expires_at):
        return "[red]expired[/]"
    return _human_duration(expires_at - int(time.time()))


def progress_bar() -> Progress:
    return Progress(
        TextColumn("[bold blue]{task.description}", justify="right"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
    )


def _summary(rows: list[tuple[str, int]]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("file")
    table.add_column("size", justify="right")
    for key, size in rows:
        table.add_row(key, _human(size))
    console.print(table)


def render_results(
    results: list[LinkResult], public, expiry, quiet, json_out, copy, qr
) -> None:
    """Render upload/link results respecting the output flags."""
    if not results:
        _fail("nothing was uploaded.")
    urls = [r.url for r in results]

    if json_out:
        print(jsonlib.dumps([asdict(r) for r in results], indent=2))
    elif quiet:
        for u in urls:
            print(u)
    else:
        _summary([(r.key, r.size) for r in results])
        if len(results) == 1:
            tail = "" if public else f" (expires in {_human_duration(expiry)})"
            console.print(f"[dim]link{tail}:[/]")
        else:
            tail = "" if public else f" (expire in {_human_duration(expiry)})"
            console.print(f"\n[bold]{len(results)} object(s) uploaded[/][dim]{tail}:[/]")
        for u in urls:
            print(u)
        if qr:
            if len(results) == 1:
                art = util.render_qr(urls[0])
                console.print(art if art else "[yellow](install segno for QR codes)[/]")
            else:
                console.print("[dim](--qr is shown for single uploads only)[/]")

    if copy:
        tool = util.copy_to_clipboard("\n".join(urls))
        if tool and not quiet and not json_out:
            console.print(f"[green]copied to clipboard[/] ({tool})")
        elif not tool:
            # Always warn (to stderr) — the user explicitly asked to copy.
            err.print("[yellow]no clipboard tool found (install wl-copy/xclip/pbcopy)[/]")
