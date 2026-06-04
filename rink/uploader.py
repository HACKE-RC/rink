"""R2 upload logic: client construction, single-file/multipart uploads, folder walks."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig

from .config import Config
from .links import guess_content_type

# Files at/above this size are uploaded in 8 MiB multipart chunks.
_MB = 1024 * 1024
_TRANSFER = TransferConfig(multipart_threshold=8 * _MB, multipart_chunksize=8 * _MB)


def make_client(cfg: Config):
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        region_name="auto",
        config=BotoConfig(signature_version="s3v4"),
    )


def list_buckets(client) -> list[str]:
    """Return the names of all buckets in the account."""
    resp = client.list_buckets()
    return [b["Name"] for b in resp.get("Buckets", [])]


def list_objects(client, bucket: str, prefix: str = ""):
    """Yield {'Key','Size','LastModified'} for every object, handling pagination."""
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            yield obj


def delete_object(client, bucket: str, key: str) -> None:
    client.delete_object(Bucket=bucket, Key=key)


def head_object(client, bucket: str, key: str) -> dict:
    """Return object metadata (raises ClientError 404 if it doesn't exist)."""
    return client.head_object(Bucket=bucket, Key=key)


def upload_file(
    client, bucket: str, src: Path, key: str, progress=None, extra: dict | None = None
) -> None:
    """Upload a single file, with automatic multipart for large files.

    `progress` is an optional callable receiving bytes-transferred per chunk.
    `extra` adds/overrides S3 ExtraArgs (e.g. ContentDisposition).
    """
    extra_args = {"ContentType": guess_content_type(key)}
    if extra:
        extra_args.update(extra)
    client.upload_file(
        str(src),
        bucket,
        key,
        ExtraArgs=extra_args,
        Config=_TRANSFER,
        Callback=progress,
    )


def zip_folder(folder: Path) -> Path:
    """Zip a folder into a temp .zip and return its path. Caller must delete it."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="rink-"))
    archive_base = tmp_dir / folder.name
    archive = shutil.make_archive(str(archive_base), "zip", root_dir=str(folder))
    return Path(archive)


def iter_files(folder: Path):
    """Yield (file_path, relative_posix_path) for every file under `folder`."""
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            yield path, path.relative_to(folder).as_posix()


def build_key(prefix: str, name: str) -> str:
    """Join an optional prefix with an object name into a clean key."""
    prefix = prefix.strip("/")
    name = name.lstrip("/")
    return f"{prefix}/{name}" if prefix else name
