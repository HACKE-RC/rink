"""rink CLI — `rink config` and `rink up <path>`."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TransferSpeedColumn,
)
from rich.prompt import Prompt
from rich.table import Table

from . import config as cfgmod
from . import db, links, uploader
from .config import Config, ConfigError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Upload files/folders to Cloudflare R2 and get a shareable link.",
)
console = Console()
err = Console(stderr=True)


def _fail(message: str) -> None:
    err.print(f"[bold red]error:[/] {message}")
    raise typer.Exit(code=1)


@app.command()
def config() -> None:
    """Set up or edit R2 credentials and bucket."""
    existing = cfgmod.read_raw()
    editing = bool(existing)

    if editing:
        console.print(
            f"[bold]Editing config[/] at {cfgmod.CONFIG_PATH} — press Enter to keep "
            "the current value.\n"
        )
    else:
        console.print(
            "[bold]Cloudflare R2 setup[/] — find these in the Cloudflare dashboard "
            "under R2.\n"
        )

    account_id = Prompt.ask("Account ID", default=existing.get("account_id") or None)
    access_key_id = Prompt.ask(
        "R2 Access Key ID", default=existing.get("access_key_id") or None
    )
    # Secret is never echoed back as a default; blank keeps the current one.
    secret_prompt = (
        "R2 Secret Access Key (blank = keep current)"
        if existing.get("secret_access_key")
        else "R2 Secret Access Key"
    )
    secret_input = Prompt.ask(secret_prompt, password=True, default="")
    secret_access_key = secret_input.strip() or existing.get("secret_access_key", "")

    bucket = Prompt.ask("Bucket name", default=existing.get("bucket") or None)
    public_base_url = Prompt.ask(
        "Public base URL (optional, e.g. https://pub-xxxx.r2.dev)",
        default=existing.get("public_base_url", ""),
    )
    current_expiry = int(existing.get("default_expiry", cfgmod.DEFAULT_EXPIRY))
    expiry_input = Prompt.ask(
        "Default presigned link expiry (e.g. 30m, 1h, 7d)",
        default=_human_duration(current_expiry),
    )
    try:
        default_expiry = cfgmod.parse_duration(expiry_input)
    except ValueError as exc:
        _fail(str(exc))

    cfg = Config(
        account_id=account_id.strip(),
        access_key_id=access_key_id.strip(),
        secret_access_key=secret_access_key.strip(),
        bucket=bucket.strip(),
        public_base_url=public_base_url.strip() or None,
        default_expiry=int(default_expiry),
    )
    path = cfgmod.save_config(cfg)
    verb = "Updated" if editing else "Saved"
    console.print(f"\n[green]{verb}[/] config at {path} (mode 600).")


@app.command()
def buckets() -> None:
    """List all buckets in the account (the default is marked)."""
    cfg = _load()
    client = uploader.make_client(cfg)
    try:
        names = uploader.list_buckets(client)
    except Exception as exc:  # noqa: BLE001 - surface any boto/network error cleanly
        _fail(f"could not list buckets: {exc}")

    if not names:
        console.print("No buckets found in this account.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("", width=2)
    table.add_column("bucket")
    for name in names:
        marker = "[green]●[/]" if name == cfg.bucket else ""
        style = "bold" if name == cfg.bucket else ""
        table.add_row(marker, f"[{style}]{name}[/]" if style else name)
    console.print(table)
    console.print(f"\n[dim]default:[/] {cfg.bucket}")


@app.command()
def use(
    bucket: str = typer.Argument(
        None, help="Bucket to set as default. Omit for an interactive picker."
    ),
) -> None:
    """Set the default bucket used by `rink up`."""
    cfg = _load()
    client = uploader.make_client(cfg)

    names: list[str] = []
    try:
        names = uploader.list_buckets(client)
    except Exception:  # noqa: BLE001 - token may be scoped to one bucket; allow manual set
        pass

    if bucket is None:
        if not names:
            _fail(
                "could not list buckets, so pass a name explicitly: `rink use <bucket>`"
            )
        console.print("[bold]Select a default bucket:[/]")
        for i, name in enumerate(names, 1):
            current = " [dim](current)[/]" if name == cfg.bucket else ""
            console.print(f"  {i}. {name}{current}")
        choice = Prompt.ask(
            "Number",
            choices=[str(i) for i in range(1, len(names) + 1)],
        )
        bucket = names[int(choice) - 1]
    elif names and bucket not in names:
        proceed = Prompt.ask(
            f"[yellow]'{bucket}' is not in the account's bucket list. Set it anyway?[/]",
            choices=["y", "n"],
            default="n",
        )
        if proceed != "y":
            console.print("Aborted.")
            raise typer.Exit()

    cfg.bucket = bucket
    cfgmod.save_config(cfg)
    console.print(f"[green]Default bucket set to[/] {bucket}")


def _fmt_remaining(expires_at: int | None) -> str:
    """Human 'time left' for a presigned link, or a marker for other states."""
    if expires_at is None:
        return "[green]permanent[/]"
    remaining = expires_at - int(time.time())
    if remaining <= 0:
        return "[red]expired[/]"
    return _human_duration(remaining)


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


@app.command(name="ls")
def ls(
    prefix: str = typer.Argument("", help="Only show keys starting with this prefix."),
    expired: bool = typer.Option(
        False, "--expired", help="Only show entries whose presigned link has expired."
    ),
) -> None:
    """List objects in the bucket with their share-link expiry."""
    cfg = _load()
    client = uploader.make_client(cfg)
    try:
        objects = list(uploader.list_objects(client, cfg.bucket, prefix))
    except Exception as exc:  # noqa: BLE001
        _fail(f"could not list objects: {exc}")

    tracked = db.records_for(cfg.bucket, prefix)

    if not objects:
        console.print(f"No objects in [bold]{cfg.bucket}[/]" + (f" under '{prefix}'" if prefix else ""))
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("file")
    table.add_column("size", justify="right")
    table.add_column("link", justify="left")

    shown = 0
    for obj in objects:
        key = obj["Key"]
        row = tracked.get(key)
        expires_at = row["expires_at"] if row else None
        link_type = row["link_type"] if row else None

        if expired:
            # Only presigned links can be "expired".
            if expires_at is None or expires_at - int(time.time()) > 0:
                continue

        if row is None:
            link_cell = "[dim]untracked[/]"
        elif link_type == "public":
            link_cell = "[green]public[/]"
        else:
            link_cell = _fmt_remaining(expires_at)

        table.add_row(key, _human(obj["Size"]), link_cell)
        shown += 1

    if shown == 0:
        console.print("Nothing to show.")
        return
    console.print(table)
    console.print(
        "[dim]link column: time left on the presigned link · "
        "'public' = permanent · 'untracked' = uploaded outside rink (expiry unknown)[/]"
    )


@app.command(name="rm")
def rm(
    keys: list[str] = typer.Argument(..., help="Object key(s) to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete object(s) from the bucket (this is how you revoke access)."""
    cfg = _load()
    client = uploader.make_client(cfg)

    if not yes:
        console.print("About to delete from [bold]{}[/]:".format(cfg.bucket))
        for k in keys:
            console.print(f"  • {k}")
        confirm = Prompt.ask("Delete?", choices=["y", "n"], default="n")
        if confirm != "y":
            console.print("Aborted.")
            raise typer.Exit()

    for k in keys:
        try:
            uploader.delete_object(client, cfg.bucket, k)
            db.delete(cfg.bucket, k)
            console.print(f"[green]deleted[/] {k}")
        except Exception as exc:  # noqa: BLE001
            err.print(f"[red]failed[/] {k}: {exc}")


def _load() -> Config:
    try:
        return cfgmod.load_config()
    except ConfigError as exc:
        _fail(str(exc))


def _make_link(client, cfg: Config, key: str, public: bool, expiry: int) -> str:
    if public:
        return links.public_url(cfg, key)
    return links.presigned_url(client, cfg.bucket, key, expiry)


def _record(cfg: Config, key: str, size: int, public: bool, expiry: int, url: str) -> None:
    """Log the upload locally so `rink ls` can show link expiry."""
    expires_at = None if public else int(time.time()) + expiry
    db.record(
        bucket=cfg.bucket,
        key=key,
        size=size,
        link_type="public" if public else "presigned",
        expires_at=expires_at,
        url=url,
    )


@app.command()
def up(
    path: Path = typer.Argument(..., exists=True, help="File or folder to upload."),
    public: bool = typer.Option(
        False,
        "--public/--presigned",
        help="Return a permanent public URL instead of a presigned one.",
    ),
    expiry: str = typer.Option(
        None,
        "--expiry",
        help="Presigned link lifetime, e.g. 30m, 2h, 7d (max 7d). Default from config.",
    ),
    zip_folder: bool = typer.Option(
        True,
        "--zip/--recursive",
        help="For folders: zip into one object (default) or upload recursively.",
    ),
    prefix: str = typer.Option("", "--prefix", help="Key prefix inside the bucket."),
    bucket: str = typer.Option(
        None, "--bucket", help="Override the configured bucket."
    ),
) -> None:
    """Upload a file or folder and print the link(s)."""
    cfg = _load()
    if bucket:
        cfg.bucket = bucket

    if expiry is None:
        expiry = cfg.default_expiry
    else:
        try:
            expiry = cfgmod.parse_duration(expiry)
        except ValueError as exc:
            _fail(str(exc))

    if not public and not (1 <= expiry <= cfgmod.MAX_EXPIRY):
        _fail("--expiry must be between 1s and 7d.")

    client = uploader.make_client(cfg)

    if path.is_file():
        _upload_single(client, cfg, path, prefix, public, expiry)
    elif zip_folder:
        _upload_zipped(client, cfg, path, prefix, public, expiry)
    else:
        _upload_recursive(client, cfg, path, prefix, public, expiry)


def _progress_bar():
    return Progress(
        TextColumn("[bold blue]{task.description}", justify="right"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
    )


def _upload_one_with_bar(client, bucket, src: Path, key: str):
    size = src.stat().st_size
    with _progress_bar() as progress:
        task = progress.add_task(src.name, total=size)
        uploader.upload_file(
            client,
            bucket,
            src,
            key,
            progress=lambda n: progress.update(task, advance=n),
        )


def _summary(rows: list[tuple[str, int]]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("file")
    table.add_column("size", justify="right")
    for key, size in rows:
        table.add_row(key, _human(size))
    console.print(table)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _print_link(label: str, url: str, expiry: int | None) -> None:
    if expiry is not None:
        console.print(f"[dim]{label} (expires in {_human_duration(expiry)}):[/]")
    else:
        console.print(f"[dim]{label}:[/]")
    # Plain print so it is easy to copy / pipe.
    print(url)


def _upload_single(client, cfg, path, prefix, public, expiry):
    key = uploader.build_key(prefix, path.name)
    _upload_one_with_bar(client, cfg.bucket, path, key)
    size = path.stat().st_size
    _summary([(key, size)])
    url = _make_link(client, cfg, key, public, expiry)
    _record(cfg, key, size, public, expiry, url)
    _print_link("link", url, None if public else expiry)


def _upload_zipped(client, cfg, folder, prefix, public, expiry):
    console.print(f"[dim]Zipping {folder}…[/]")
    archive = uploader.zip_folder(folder)
    try:
        key = uploader.build_key(prefix, archive.name)
        _upload_one_with_bar(client, cfg.bucket, archive, key)
        size = archive.stat().st_size
        _summary([(key, size)])
        url = _make_link(client, cfg, key, public, expiry)
        _record(cfg, key, size, public, expiry, url)
        _print_link("link", url, None if public else expiry)
    finally:
        # Clean up the temp dir holding the archive.
        import shutil

        shutil.rmtree(archive.parent, ignore_errors=True)


def _upload_recursive(client, cfg, folder, prefix, public, expiry):
    files = list(uploader.iter_files(folder))
    if not files:
        _fail(f"No files found under {folder}.")

    base_prefix = uploader.build_key(prefix, folder.name)
    rows: list[tuple[str, int]] = []
    results: list[tuple[str, str]] = []

    with _progress_bar() as progress:
        for src, rel in files:
            key = f"{base_prefix}/{rel}"
            task = progress.add_task(rel, total=src.stat().st_size)
            uploader.upload_file(
                client,
                cfg.bucket,
                src,
                key,
                progress=lambda n, t=task: progress.update(t, advance=n),
            )
            fsize = src.stat().st_size
            url = _make_link(client, cfg, key, public, expiry)
            _record(cfg, key, fsize, public, expiry, url)
            rows.append((key, fsize))
            results.append((key, url))

    _summary(rows)
    console.print(
        f"\n[bold]{len(results)} file(s) uploaded under[/] {base_prefix}/"
    )
    if public:
        console.print("[dim]links:[/]")
    else:
        console.print(f"[dim]links (expire in {_human_duration(expiry)}):[/]")
    for key, url in results:
        print(url)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        err.print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
