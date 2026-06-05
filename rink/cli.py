"""rink CLI — thin command layer over the upload pipeline (`upload`) and renderer (`render`)."""

from __future__ import annotations

import json as jsonlib
import time
import webbrowser
from dataclasses import replace
from pathlib import Path

import typer
from rich.prompt import Prompt
from rich.table import Table

from . import config as cfgmod
from . import db, links, serve as serve_mod, upload, uploader, util
from .config import Config, ConfigError
from .render import (  # re-exported so tests can reach cli._human etc.
    LinkResult,
    _fail,
    _fmt_remaining,
    _human,
    _human_duration,
    _is_expired,
    console,
    err,
    render_results,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,  # plain Click-style errors, no boxed panels
    help="Upload files/folders to Cloudflare R2 and get a shareable link.",
)


# --------------------------------------------------------------------------- glue


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
        _fail(
            "--expiry must be between 1s and 7d.",
            hint="presigned links cap at 7 days; for a permanent link use --public.",
        )
    return secs


def _validate_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        _fail(
            "--name must be a plain filename (no empty value, no slashes).",
            hint="put folders in --prefix instead, e.g. --prefix docs/ --name report.pdf",
        )


def _not_found(exc: Exception, bucket: str, key: str) -> None:
    """Fail with a message that distinguishes a genuine 404 from auth/network errors."""
    from botocore.exceptions import ClientError

    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            _fail(
                f"object not found in {bucket}: {key}",
                hint=f"run `rink ls` to see what's in {bucket}.",
            )
        _fail(
            f"could not access {key}: {exc}",
            hint="check your credentials and bucket with `rink config`.",
        )
    _fail(
        f"could not access {key}: {exc}",
        hint="check your network and credentials, then try again.",
    )


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


# ------------------------------------------------------------------------ commands


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
    serve_url = Prompt.ask(
        "Serve Worker URL (optional, e.g. https://rink-serve.<acct>.workers.dev)",
        default=existing.get("serve_url", ""),
    )
    serve_prompt = (
        "Serve admin token (blank = keep current)"
        if existing.get("serve_token")
        else "Serve admin token (optional)"
    )
    serve_token_input = Prompt.ask(serve_prompt, password=True, default="")
    serve_token = serve_token_input.strip() or existing.get("serve_token", "")
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
        serve_url=serve_url.strip() or None,
        serve_token=serve_token.strip() or None,
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
        _fail(
            f"could not list buckets: {exc}",
            hint="R2 tokens are often scoped to one bucket; use `rink ls` or --bucket instead.",
        )

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

    cfgmod.save_config(replace(cfg, bucket=bucket))
    console.print(f"[green]Default bucket set to[/] {bucket}")


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
        _fail(
            f"could not list objects: {exc}",
            hint="verify the bucket name and credentials with `rink config`.",
        )

    tracked = db.records_for(cfg.bucket, prefix)

    if not objects:
        console.print(
            f"No objects in [bold]{cfg.bucket}[/]" + (f" under '{prefix}'" if prefix else "")
        )
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
        console.print(f"About to delete from [bold]{cfg.bucket}[/]:")
        for k in keys:
            console.print(f"  • {k}")
        if Prompt.ask("Delete?", choices=["y", "n"], default="n") != "y":
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
        _not_found(exc, cfg.bucket, key)
    size = int(head.get("ContentLength", 0))

    result = _publish(client, cfg, key, size, public, expiry)
    render_results([result], public, expiry, quiet, json_out, copy, qr)


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
        _not_found(exc, cfg.bucket, key)

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


def _serve_url(cfg: Config, worker_url: str | None) -> str:
    url = worker_url or cfg.serve_url
    if not url:
        _fail(
            "no Serve Worker URL configured.",
            hint="run `rink serve --init`, set the Worker secret, then run "
            "`rink serve --deploy`.",
        )
    return url


def _serve_token(cfg: Config, token: str | None) -> str:
    resolved = token or cfg.serve_token
    if not resolved:
        _fail(
            "no Serve admin token configured.",
            hint="set RINK_SERVE_TOKEN, or save it with `rink config`.",
        )
    return resolved


def _prompt_for_deploy_url(cfg: Config) -> str | None:
    console.print("[yellow]could not find the Worker URL in wrangler output.[/]")
    entered = Prompt.ask("Serve Worker URL", default=cfg.serve_url or "")
    return entered.strip().rstrip("/") or None


def _save_serve_config(cfg: Config, worker_url: str | None, token: str | None) -> None:
    if not worker_url:
        console.print(
            "[yellow]Serve Worker URL not saved; pass --worker-url or set it with "
            "`rink config`.[/]"
        )
        return

    saved = cfgmod.save_config(
        replace(
            cfg,
            serve_url=worker_url.rstrip("/"),
            serve_token=token or cfg.serve_token,
        )
    )
    console.print(f"[green]saved Serve config[/] {worker_url.rstrip('/')} at {saved}")
    if not (token or cfg.serve_token):
        console.print(
            "[yellow]Serve admin token was not found; save it with `rink config` "
            "or set RINK_SERVE_TOKEN.[/]"
        )


def _sync_worker_secret(worker_dir: Path, token: str | None) -> None:
    if not token:
        console.print(
            "[yellow]Worker secret not synced; no Serve admin token was available.[/]"
        )
        return
    try:
        output = serve_mod.put_worker_secret(worker_dir, token)
    except Exception as exc:  # noqa: BLE001
        _fail(f"could not sync Worker secret: {exc}")
    if output.strip():
        console.print(output.rstrip(), markup=False)


def _deploy_and_configure(
    cfg: Config,
    worker_dir: Path,
    token: str | None,
    worker_url: str | None,
) -> None:
    try:
        result = serve_mod.deploy_worker(worker_dir)
    except Exception as exc:  # noqa: BLE001
        _fail(f"could not deploy Worker: {exc}")

    if result.output.strip():
        console.print(result.output.rstrip(), markup=False)

    resolved_url = worker_url or result.worker_url or _prompt_for_deploy_url(cfg)
    _sync_worker_secret(worker_dir, token)
    _save_serve_config(cfg, resolved_url, token)


def _render_receive_link(
    result: serve_mod.ReceiveLink,
    *,
    quiet: bool,
    json_out: bool,
    copy: bool,
    qr: bool,
) -> None:
    if json_out:
        print(jsonlib.dumps(result.__dict__, indent=2))
    elif quiet:
        print(result.upload_url)
    else:
        table = Table(show_header=True, header_style="bold")
        table.add_column("receive link")
        table.add_column("expires", justify="right")
        table.add_column("uploads", justify="right")
        table.add_column("max file", justify="right")
        table.add_column("download views", justify="right")
        table.add_row(
            result.upload_url,
            _human_duration(max(0, (result.expires_at // 1000) - int(time.time()))),
            str(result.max_uploads),
            _human(result.max_bytes),
            str(result.max_download_views),
        )
        console.print(table)
        console.print(f"[dim]R2 prefix:[/] {result.prefix}")
        if qr:
            art = util.render_qr(result.upload_url)
            console.print(art if art else "[yellow](install segno for QR codes)[/]")

    if copy:
        tool = util.copy_to_clipboard(result.upload_url)
        if tool and not quiet and not json_out:
            console.print(f"[green]copied to clipboard[/] ({tool})")
        elif not tool:
            err.print("warning: could not copy to clipboard (install xclip/xsel/pbcopy/wl-copy).")


@app.command()
def serve(
    worker_url: str = typer.Option(
        None, "--worker-url", help="Deployed rink serve Worker URL."
    ),
    token: str = typer.Option(
        None, "--token", help="Admin token for the deployed Worker."
    ),
    label: str = typer.Option(None, "--label", help="Title shown on the receive page."),
    prefix: str = typer.Option(
        "rink-inbox", "--prefix", help="R2 key prefix for received files."
    ),
    random_key: bool = typer.Option(
        False, "--random", help="Add a random child prefix for this receive link."
    ),
    expiry: str = typer.Option("1d", "--expiry", help="Receive link lifetime."),
    max_uploads: int = typer.Option(
        1, "--max-uploads", help="How many files this receive link accepts."
    ),
    max_size: str = typer.Option("512MB", "--max-size", help="Maximum file size."),
    download_views: int = typer.Option(
        1,
        "--download-views",
        help="Max views per generated download link; 1 makes it one-time.",
    ),
    init: bool = typer.Option(False, "--init", help="Write a deployable Worker project."),
    deploy: bool = typer.Option(
        False, "--deploy", help="Deploy the Worker project with wrangler."
    ),
    worker_dir: Path = typer.Option(
        serve_mod.DEFAULT_WORKER_DIR, "--worker-dir", help="Worker project directory."
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Allow --init to overwrite an existing Worker dir."
    ),
    copy: bool = typer.Option(False, "--copy", "-c", help="Copy the receive link."),
    qr: bool = typer.Option(False, "--qr", help="Print a terminal QR code."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only the receive URL."),
    json_out: bool = typer.Option(False, "--json", help="Print result as JSON."),
    open_browser: bool = typer.Option(False, "--open", help="Open the receive page."),
) -> None:
    """Create a receive link that lets someone upload into your R2 bucket."""
    cfg = _load()
    quiet = quiet or json_out

    if init:
        try:
            generated_token = serve_mod.write_worker_project(
                worker_dir,
                cfg,
                overwrite=overwrite,
            )
        except FileExistsError as exc:
            _fail(str(exc), hint="use --overwrite or choose another --worker-dir.")
        console.print(f"[green]wrote[/] {worker_dir}")
        console.print(
            "[dim]deploy, sync secret, and save URL:[/] "
            f"rink serve --deploy --worker-dir {worker_dir}"
        )
        console.print(f"[dim]local token for RINK_SERVE_TOKEN:[/] {generated_token}")
        if deploy:
            _deploy_and_configure(cfg, worker_dir, generated_token, worker_url)
        return

    if deploy:
        deploy_token = token or cfg.serve_token or serve_mod.read_worker_token(worker_dir)
        _deploy_and_configure(cfg, worker_dir, deploy_token, worker_url)
        return

    try:
        ttl_seconds = _resolve_expiry(cfg, expiry, public=False)
        max_bytes = serve_mod.parse_size(max_size)
    except ValueError as exc:
        _fail(str(exc))
    if max_uploads < 1:
        _fail("--max-uploads must be at least 1.")
    if download_views < 1:
        _fail("--download-views must be at least 1.")

    try:
        result = serve_mod.create_receive_link(
            _serve_url(cfg, worker_url),
            _serve_token(cfg, token),
            prefix=serve_mod.normalize_prefix(prefix, random_key),
            label=label,
            ttl_seconds=ttl_seconds,
            max_uploads=max_uploads,
            max_bytes=max_bytes,
            max_download_views=download_views,
        )
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "401" in text and "unauthorized" in text.lower():
            _fail(
                f"could not create receive link: {exc}",
                hint="the saved Serve token does not match the deployed Worker secret; "
                f"run `rink serve --deploy --worker-dir {worker_dir}` to sync it.",
            )
        _fail(f"could not create receive link: {exc}")

    _render_receive_link(result, quiet=quiet, json_out=json_out, copy=copy, qr=qr)
    if open_browser:
        webbrowser.open(result.upload_url)


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

    sources = upload.resolve_sources(paths)
    if not sources:
        _fail("no paths given.")
    # --name only makes sense when there's a single resulting object.
    single_object = len(sources) == 1 and (
        sources[0][0] in ("file", "stdin")
        or (sources[0][0] == "folder" and zip_folder)
    )
    if name:
        if not single_object:
            _fail(
                "--name only works with a single file, stdin, or a zipped folder.",
                hint="drop --name, or use --prefix to namespace a multi-file upload.",
            )
        _validate_name(name)

    client = uploader.make_client(cfg)
    extra = {"ContentDisposition": "attachment"} if download else None

    uploaded: list[tuple[str, int]] = []
    for kind, src in sources:
        eff_prefix = upload.effective_prefix(prefix, random_key)
        uploaded.extend(
            upload.upload_source(
                client, cfg, kind, src, eff_prefix, name, zip_folder, extra, quiet, workers
            )
        )

    results = [_publish(client, cfg, key, size, public, expiry) for key, size in uploaded]
    render_results(results, public, expiry, quiet, json_out, copy, qr)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        err.print("\nInterrupted.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
