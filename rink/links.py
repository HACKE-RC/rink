"""Build shareable links to R2 objects.

Two flavours:
- presigned: a signed, self-expiring URL that needs no bucket config.
- public:    a permanent URL via the bucket's public base (r2.dev or custom domain).
"""

from __future__ import annotations

import mimetypes
from urllib.parse import quote

from .config import Config


def guess_content_type(key: str) -> str:
    ctype, _ = mimetypes.guess_type(key)
    return ctype or "application/octet-stream"


def presigned_url(client, bucket: str, key: str, expiry: int) -> str:
    """Generate a presigned GET URL valid for `expiry` seconds.

    Range validation lives in the CLI's `_resolve_expiry` (the single home), so it
    can fail fast before any upload; this layer trusts its caller.
    """
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
