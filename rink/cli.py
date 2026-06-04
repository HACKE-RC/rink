"""rink CLI — `rink config` and `rink up <path>`."""

from __future__ import annotations

import json as jsonlib
import shutil
import sys
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
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
from . import db, links, uploader, util
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


@dataclass
class LinkResult:
    """One published object + its shareable link."""

    key: str
    size: int
    url: str
    link_type: str  # "presigned" | "public"
    expires_at: int | None


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
    if _is_expired(expires_at):
        return "[red]expired[/]"
    return _human_duration(expires_at - int(time.time()))


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

        if expired and not _is_expired(expires_at):
            continue  # --expired: skip anything still live (or permanent)

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


@app.command()
def link(
    key: str = typer.Argument(..., help="Existing object key to make a fresh link for."),
    public: bool = typer.Option(
        False, "--public/--presigned", help="Permanent public URL vs presigned."
    ),
    expiry: str = typer.Option(None, "--expiry", help="Presigned lifetime, e.g. 2h, 7d."),
    bucket: str = typer.Option(None, "--bucket", help="Override the configured bucket."),
    copy: bool = typer.Option(False, "--copy", "-c", help="Copy the link to the clipboard."),
    qr: bool = typer.Option(False, "--qr", help="Print a QR code for the link."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only the URL."),
    json_out: bool = typer.Option(False, "--json", help="Print result as JSON."),
) -> None:
    """Regenerate a fresh link for an already-uploaded object (no re-upload)."""
    cfg = _apply_bucket(_load(), bucket)
    quiet = quiet or json_out
    _require_public_base(cfg, public)
    expiry = _resolve_expiry(cfg, expiry, public)

    client = uploader.make_client(cfg)
    try:
        head = uploader.head_object(client, cfg.bucket, key)
    except Exception as exc:  # noqa: BLE001
        _fail(_not_found_message(exc, cfg.bucket, key))
    size = int(head.get("ContentLength", 0))

    result = _publish(client, cfg, key, size, public, expiry)
    _render_results([result], public, expiry, quiet, json_out, copy, qr)


@app.command(name="open")
def open_cmd(
    key: str = typer.Argument(..., help="Object key to open in your browser."),
    public: bool = typer.Option(False, "--public/--presigned", help="Link type to open."),
    expiry: str = typer.Option(None, "--expiry", help="Presigned lifetime, e.g. 2h."),
    bucket: str = typer.Option(None, "--bucket", help="Override the configured bucket."),
) -> None:
    """Open an object's link in your default browser."""
    cfg = _apply_bucket(_load(), bucket)
    _require_public_base(cfg, public)
    expiry = _resolve_expiry(cfg, expiry, public)

    client = uploader.make_client(cfg)
    try:
        uploader.head_object(client, cfg.bucket, key)
    except Exception as exc:  # noqa: BLE001
        _fail(_not_found_message(exc, cfg.bucket, key))

    url = _make_link(client, cfg, key, public, expiry)
    if webbrowser.open(url):
        console.print(f"[green]opening[/] {key} in your browser")
    else:
        console.print("[yellow]could not launch a browser; here's the link:[/]")
        print(url)


@app.command()
def prune(
    bucket: str = typer.Option(None, "--bucket", help="Override the configured bucket."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete objects whose tracked presigned link has expired."""
    cfg = _apply_bucket(_load(), bucket)

    now = int(time.time())
    rows = db.records_for(cfg.bucket)
    expired = [k for k, r in rows.items() if _is_expired(r["expires_at"], now)]
    if not expired:
        console.print("Nothing to prune — no expired links tracked.")
        return

    if not yes:
        console.print(
            f"These {len(expired)} object(s) in [bold]{cfg.bucket}[/] have expired "
            "links and will be deleted:"
        )
        for k in expired:
            console.print(f"  • {k}")
        if Prompt.ask("Delete them?", choices=["y", "n"], default="n") != "y":
            console.print("Aborted.")
            raise typer.Exit()

    client = uploader.make_client(cfg)
    for k in expired:
        try:
            uploader.delete_object(client, cfg.bucket, k)
            db.delete(cfg.bucket, k)
            console.print(f"[green]pruned[/] {k}")
        except Exception as exc:  # noqa: BLE001
            err.print(f"[red]failed[/] {k}: {exc}")


def _load() -> Config:
    try:
        return cfgmod.load_config()
    except ConfigError as exc:
        _fail(str(exc))


def _apply_bucket(cfg: Config, bucket: str | None) -> Config:
    """Return cfg with an optional one-off bucket override (no in-place mutation)."""
    return replace(cfg, bucket=bucket) if bucket else cfg


def _require_public_base(cfg: Config, public: bool) -> None:
    """Fail fast (before any upload) if a public link is requested but unconfigured."""
    if public:
        try:
            cfg.require_public_base_url()
        except ConfigError as exc:
            _fail(str(exc))


def _resolve_expiry(cfg: Config, expiry: str | None, public: bool) -> int:
    """Resolve --expiry (human string or None) to seconds and validate the range.

    This is the single home for expiry validation; downstream layers trust it.
    """
    if expiry is None:
        secs = cfg.default_expiry
    else:
        try:
            secs = cfgmod.parse_duration(expiry)
        except ValueError as exc:
            _fail(str(exc))
    if not public and not (1 <= secs <= cfgmod.MAX_EXPIRY):
        _fail("--expiry must be between 1s and 7d.")
    return secs


def _validate_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        _fail("--name must be a plain filename (no empty value, no slashes).")


def _is_expired(expires_at: int | None, now: int | None = None) -> bool:
    """True only for a presigned link whose deadline has passed."""
    if expires_at is None:
        return False
    return expires_at <= (now if now is not None else int(time.time()))


def _not_found_message(exc: Exception, bucket: str, key: str) -> str:
    """Distinguish a genuine 404 from auth/network errors."""
    from botocore.exceptions import ClientError

    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return f"object not found in {bucket}: {key}"
        return f"could not access {key}: {exc}"
    return f"could not access {key}: {exc}"


def _make_link(client, cfg: Config, key: str, public: bool, expiry: int) -> str:
    if public:
        return links.public_url(cfg, key)
    return links.presigned_url(client, cfg.bucket, key, expiry)


def _publish(client, cfg: Config, key: str, size: int, public: bool, expiry: int) -> LinkResult:
    """Build the link, log it locally, and return a typed result (one home for all three)."""
    url = _make_link(client, cfg, key, public, expiry)
    expires_at = None if public else int(time.time()) + expiry
    link_type = "public" if public else "presigned"
    db.record(
        bucket=cfg.bucket,
        key=key,
        size=size,
        link_type=link_type,
        expires_at=expires_at,
        url=url,
    )
    return LinkResult(key=key, size=size, url=url, link_type=link_type, expires_at=expires_at)


@app.command()
def up(
    paths: list[str] = typer.Argument(
        ..., help="File(s)/folder(s) to upload. Use '-' for stdin (needs --name)."
    ),
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
    bucket: str = typer.Option(None, "--bucket", help="Override the configured bucket."),
    name: str = typer.Option(
        None, "--name", help="Object name to use (single upload only; required for stdin)."
    ),
    random_key: bool = typer.Option(
        False, "--random", help="Prefix the key with a random token (unguessable links)."
    ),
    download: bool = typer.Option(
        False, "--download", help="Force browsers to download (Content-Disposition)."
    ),
    copy: bool = typer.Option(False, "--copy", "-c", help="Copy the link(s) to the clipboard."),
    qr: bool = typer.Option(False, "--qr", help="Print a QR code for the link (single upload)."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only the URL(s)."),
    json_out: bool = typer.Option(False, "--json", help="Print results as JSON."),
    workers: int = typer.Option(
        4, "--workers", help="Parallel uploads for recursive folders."
    ),
) -> None:
    """Upload file(s)/folder(s) and print the link(s)."""
    cfg = _apply_bucket(_load(), bucket)
    quiet = quiet or json_out  # machine/quiet modes suppress chatter
    _require_public_base(cfg, public)
    expiry = _resolve_expiry(cfg, expiry, public)

    sources = _resolve_sources(paths)
    if not sources:
        _fail("no paths given.")
    # --name only makes sense when there's a single resulting object.
    single_object = len(sources) == 1 and (
        sources[0][0] in ("file", "stdin")
        or (sources[0][0] == "folder" and zip_folder)
    )
    if name:
        if not single_object:
            _fail("--name only works with a single file, stdin, or a zipped folder.")
        _validate_name(name)

    client = uploader.make_client(cfg)
    extra = {"ContentDisposition": "attachment"} if download else None

    uploaded: list[tuple[str, int]] = []
    for kind, src in sources:
        eff_prefix = _effective_prefix(prefix, random_key)
        uploaded.extend(
            _upload_source(
                client, cfg, kind, src, eff_prefix, name, zip_folder, extra, quiet, workers
            )
        )

    results = [_publish(client, cfg, key, size, public, expiry) for key, size in uploaded]
    _render_results(results, public, expiry, quiet, json_out, copy, qr)


def _resolve_sources(paths: list[str]):
    """Validate inputs into (kind, value) pairs. kind: stdin | file | folder | folder_recursive."""
    out = []
    for raw in paths:
        if raw == "-":
            out.append(("stdin", None))
            continue
        p = Path(raw)
        if not p.exists():
            _fail(f"path does not exist: {raw}")
        out.append(("file" if p.is_file() else "folder", p))
    return out


def _effective_prefix(prefix: str, random_key: bool) -> str:
    if not random_key:
        return prefix
    token = util.random_token()
    base = prefix.strip("/")
    return f"{base}/{token}" if base else token


def _upload_source(client, cfg, kind, src, eff_prefix, name, zip_folder, extra, quiet, workers):
    """Upload one source, returning a list of (key, size)."""
    if kind == "stdin":
        if not name:
            _fail("reading from stdin ('-') requires --name.")
        tmp = Path(tempfile.mkdtemp(prefix="rink-")) / name
        tmp.write_bytes(sys.stdin.buffer.read())
        try:
            key = uploader.build_key(eff_prefix, name)
            return [_upload_one(client, cfg, tmp, key, extra, quiet)]
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)

    if kind == "file":
        key = uploader.build_key(eff_prefix, name or src.name)
        return [_upload_one(client, cfg, src, key, extra, quiet)]

    # folder
    if zip_folder:
        return [_upload_zip(client, cfg, src, name, eff_prefix, extra, quiet)]
    return _upload_recursive(client, cfg, src, eff_prefix, extra, quiet, workers)


def _progress_bar():
    return Progress(
        TextColumn("[bold blue]{task.description}", justify="right"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
    )


def _upload_one(client, cfg, src: Path, key: str, extra, quiet) -> tuple[str, int]:
    """Upload a single file (with a progress bar unless quiet). Returns (key, size)."""
    size = src.stat().st_size
    if quiet:
        uploader.upload_file(client, cfg.bucket, src, key, extra=extra)
    else:
        with _progress_bar() as progress:
            task = progress.add_task(src.name, total=size)
            uploader.upload_file(
                client,
                cfg.bucket,
                src,
                key,
                progress=lambda n: progress.update(task, advance=n),
                extra=extra,
            )
    return key, size


def _upload_zip(client, cfg, folder, name, eff_prefix, extra, quiet) -> tuple[str, int]:
    if not quiet:
        console.print(f"[dim]Zipping {folder}…[/]")
    archive = uploader.zip_folder(folder)
    try:
        obj_name = name or archive.name
        if not obj_name.endswith(".zip"):
            obj_name += ".zip"
        key = uploader.build_key(eff_prefix, obj_name)
        return _upload_one(client, cfg, archive, key, extra, quiet)
    finally:
        shutil.rmtree(archive.parent, ignore_errors=True)


def _upload_recursive(client, cfg, folder, eff_prefix, extra, quiet, workers) -> list[tuple[str, int]]:
    files = list(uploader.iter_files(folder))
    if not files:
        _fail(f"No files found under {folder}.")
    base_prefix = uploader.build_key(eff_prefix, folder.name)
    total = sum(src.stat().st_size for src, _ in files)
    out: list[tuple[str, int]] = []
    failures: list[tuple[str, Exception]] = []

    progress = None if quiet else _progress_bar()
    # rich.Progress.update isn't documented thread-safe; the callback fires from
    # every worker (and boto's internal multipart threads), so guard it.
    lock = threading.Lock()

    def do(src: Path, rel: str, advance) -> tuple[str, int]:
        key = f"{base_prefix}/{rel}"
        uploader.upload_file(client, cfg.bucket, src, key, progress=advance, extra=extra)
        return key, src.stat().st_size

    with (progress or nullcontext()):
        task = progress.add_task(f"{folder.name}/", total=total) if progress else None

        def advance(n, _task=task):
            with lock:
                progress.update(_task, advance=n)

        cb = advance if progress else None
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futmap = {ex.submit(do, src, rel, cb): rel for src, rel in files}
            for fut in as_completed(futmap):
                rel = futmap[fut]
                try:
                    out.append(fut.result())
                except Exception as exc:  # noqa: BLE001 - collect, don't abort the batch
                    failures.append((f"{base_prefix}/{rel}", exc))

    for key, exc in failures:
        err.print(f"[red]failed[/] {key}: {exc}")
    out.sort(key=lambda kv: kv[0])
    return out


def _summary(rows: list[tuple[str, int]]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("file")
    table.add_column("size", justify="right")
    for key, size in rows:
        table.add_row(key, _human(size))
    console.print(table)


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def _render_results(results: list[LinkResult], public, expiry, quiet, json_out, copy, qr) -> None:
    """Render upload results respecting the output flags."""
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


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        err.print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
