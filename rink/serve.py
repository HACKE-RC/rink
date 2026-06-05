"""Helpers for `rink serve`: Worker scaffolding and receive-link creation."""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import util
from .config import Config

DEFAULT_WORKER_DIR = Path("rink-serve-worker")
DEFAULT_MAX_SIZE = 512 * 1024 * 1024
ADMIN_SECRET_NAME = "RINK_SERVE_ADMIN_TOKEN"
_WORKERS_DEV_URL = re.compile(
    r"https://[A-Za-z0-9.-]+\.workers\.dev(?:/[^\s'\"<>]*)?"
)

_SIZE_UNITS = {
    "b": 1,
    "kb": 1024,
    "mb": 1024**2,
    "gb": 1024**3,
}


@dataclass
class ReceiveLink:
    id: str
    upload_url: str
    expires_at: int
    max_uploads: int
    max_bytes: int
    max_download_views: int
    prefix: str


@dataclass
class DeployResult:
    output: str
    worker_url: str | None


def parse_size(text: str) -> int:
    raw = text.strip().lower().replace(" ", "")
    if not raw:
        raise ValueError("empty size")
    if raw.isdigit():
        return int(raw)
    for unit, multiplier in sorted(_SIZE_UNITS.items(), key=lambda item: -len(item[0])):
        if raw.endswith(unit):
            number = raw[: -len(unit)]
            if not number or not number.isdigit():
                break
            return int(number) * multiplier
    raise ValueError("invalid size; use bytes or units like 10MB, 1GB")


def new_admin_token() -> str:
    return secrets.token_urlsafe(32)


def user_agent() -> str:
    try:
        package_version = version("rink")
    except PackageNotFoundError:
        package_version = "dev"
    return f"rink/{package_version}"


def parse_deploy_url(output: str) -> str | None:
    match = _WORKERS_DEV_URL.search(output)
    if not match:
        return None
    return match.group(0).rstrip(".,);]")


def read_worker_token(worker_dir: Path) -> str | None:
    path = worker_dir / ".dev.vars"
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        if key.strip() == ADMIN_SECRET_NAME:
            return value.strip().strip("\"'")
    return None


def write_worker_project(target: Path, cfg: Config, *, overwrite: bool = False) -> str:
    if target.exists() and any(target.iterdir()) and not overwrite:
        raise FileExistsError(f"{target} already exists and is not empty")

    target.mkdir(parents=True, exist_ok=True)
    template_root = resources.files("rink.worker_template")
    token = new_admin_token()
    for item in template_root.rglob("*"):
        rel = item.relative_to(template_root)
        if rel.parts and rel.parts[0] == "__pycache__":
            continue
        if len(rel.parts) == 1 and rel.name == "__init__.py":
            continue
        dest = target / rel
        if item.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = item.read_bytes()
        if item.name == "wrangler.jsonc":
            data = data.decode("utf-8").replace("__RINK_BUCKET__", cfg.bucket).encode()
        dest.write_bytes(data)
    (target / ".dev.vars").write_text(f"{ADMIN_SECRET_NAME}={token}\n")
    (target / ".dev.vars").chmod(0o600)
    return token


def _run_wrangler(worker_dir: Path, args: list[str], input_text: str | None = None) -> str:
    if not shutil.which("npx"):
        raise RuntimeError("npx is required to manage the Worker")
    proc = subprocess.run(
        ["npx", "wrangler", *args],
        cwd=worker_dir,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = proc.stdout or ""
    if proc.returncode != 0:
        detail = output.strip() or f"wrangler exited with status {proc.returncode}"
        raise RuntimeError(detail)
    return output


def deploy_worker(worker_dir: Path) -> DeployResult:
    output = _run_wrangler(worker_dir, ["deploy", "--config", "wrangler.jsonc"])
    return DeployResult(output=output, worker_url=parse_deploy_url(output))


def put_worker_secret(worker_dir: Path, token: str) -> str:
    return _run_wrangler(
        worker_dir,
        ["secret", "put", ADMIN_SECRET_NAME, "--config", "wrangler.jsonc"],
        input_text=f"{token}\n",
    )


def create_receive_link(
    worker_url: str,
    token: str,
    *,
    prefix: str,
    label: str | None,
    ttl_seconds: int,
    max_uploads: int,
    max_bytes: int,
    max_download_views: int,
) -> ReceiveLink:
    url = worker_url.rstrip("/") + "/api/drops"
    payload = {
        "prefix": prefix,
        "label": label,
        "ttlSeconds": ttl_seconds,
        "maxUploads": max_uploads,
        "maxBytes": max_bytes,
        "maxDownloadViews": max_download_views,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": user_agent(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"Worker rejected request ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach Worker: {exc.reason}") from exc

    return ReceiveLink(
        id=str(data["id"]),
        upload_url=str(data["uploadUrl"]),
        expires_at=int(data["expiresAt"]),
        max_uploads=int(data["maxUploads"]),
        max_bytes=int(data["maxBytes"]),
        max_download_views=int(data["maxDownloadViews"]),
        prefix=str(data["prefix"]),
    )


def normalize_prefix(prefix: str, randomize: bool) -> str:
    cleaned = "/".join(part for part in prefix.strip("/").split("/") if part)
    if not randomize:
        return cleaned or "rink-inbox"
    token = util.random_token()
    return f"{cleaned}/{token}" if cleaned else token
