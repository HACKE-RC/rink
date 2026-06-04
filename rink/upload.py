"""The upload pipeline: resolve inputs → upload → return (key, size) pairs.

This layer has no command/flag knowledge; it takes a boto3 client + Config and
does the work, delegating all presentation to `render`.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path

from . import uploader, util
from .render import _fail, console, err, progress_bar


def resolve_sources(paths: list[str]):
    """Validate inputs into (kind, value) pairs. kind: stdin | file | folder."""
    out = []
    for raw in paths:
        if raw == "-":
            out.append(("stdin", None))
            continue
        p = Path(raw)
        if not p.exists():
            _fail(
                f"path does not exist: {raw}",
                hint="check the path, or use '-' to read from stdin (with --name).",
            )
        out.append(("file" if p.is_file() else "folder", p))
    return out


def effective_prefix(prefix: str, random_key: bool) -> str:
    if not random_key:
        return prefix
    token = util.random_token()
    base = prefix.strip("/")
    return f"{base}/{token}" if base else token


def upload_source(client, cfg, kind, src, eff_prefix, name, zip_folder, extra, quiet, workers):
    """Upload one source, returning a list of (key, size)."""
    if kind == "stdin":
        if not name:
            _fail(
                "reading from stdin ('-') requires --name.",
                hint="e.g. cat report.pdf | rink up - --name report.pdf",
            )
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
    return upload_recursive(client, cfg, src, eff_prefix, extra, quiet, workers)


def _upload_one(client, cfg, src: Path, key: str, extra, quiet) -> tuple[str, int]:
    """Upload a single file (with a progress bar unless quiet). Returns (key, size)."""
    size = src.stat().st_size
    if quiet:
        uploader.upload_file(client, cfg.bucket, src, key, extra=extra)
    else:
        with progress_bar() as progress:
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


def upload_recursive(client, cfg, folder, eff_prefix, extra, quiet, workers) -> list[tuple[str, int]]:
    files = list(uploader.iter_files(folder))
    if not files:
        _fail(
            f"no files found under {folder}.",
            hint="the folder is empty (or only has empty subdirs); nothing to upload.",
        )
    base_prefix = uploader.build_key(eff_prefix, folder.name)
    total = sum(src.stat().st_size for src, _ in files)
    out: list[tuple[str, int]] = []
    failures: list[tuple[str, Exception]] = []

    progress = None if quiet else progress_bar()
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
