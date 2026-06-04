"""Build shareable links to R2 objects.

Two flavours:
- presigned: a signed, self-expiring URL that needs no bucket config.
- public:    a permanent URL via the bucket's public base (r2.dev or custom domain).
"""

from __future__ import annotations

import mimetypes
from urllib.parse import quote

from .config import MAX_EXPIRY, Config


def guess_content_type(key: str) -> str:
    ctype, _ = mimetypes.guess_type(key)
    return ctype or "application/octet-stream"


def presigned_url(client, bucket: str, key: str, expiry: int) -> str:
    """Generate a presigned GET URL valid for `expiry` seconds."""
    if expiry < 1:
        raise ValueError("expiry must be at least 1 second")
    if expiry > MAX_EXPIRY:
        raise ValueError(f"expiry must be <= {MAX_EXPIRY} seconds (7 days)")
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expiry,
    )


def public_url(cfg: Config, key: str) -> str:
    """Build a permanent public URL from the configured public base."""
    base = cfg.require_public_base_url()
    # Encode each path segment but keep the slashes between them.
    encoded = "/".join(quote(part) for part in key.split("/"))
    return f"{base}/{encoded}"
