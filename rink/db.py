"""Local SQLite log of uploads, so we can show presigned-link expiry.

R2 does not record when a presigned URL was generated or when it expires (that's
math baked into the URL string), so we track it ourselves. One row per (bucket, key);
re-uploading the same key updates the row to reflect the latest link.

DB lives at ~/.local/share/rink/rink.db (stdlib sqlite3, no extra deps).
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DB_DIR = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
) / "rink"
DB_PATH = DB_DIR / "rink.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    bucket       TEXT    NOT NULL,
    key          TEXT    NOT NULL,
    size         INTEGER NOT NULL,
    uploaded_at  INTEGER NOT NULL,
    link_type    TEXT    NOT NULL,
    expires_at   INTEGER,
    url          TEXT,
    PRIMARY KEY (bucket, key)
);
"""


def connect() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def record(
    bucket: str,
    key: str,
    size: int,
    link_type: str,
    expires_at: int | None,
    url: str | None,
) -> None:
    """Upsert one upload record, stamping uploaded_at with the current time."""
    now = int(time.time())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO uploads (bucket, key, size, uploaded_at, link_type, expires_at, url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket, key) DO UPDATE SET
                size=excluded.size,
                uploaded_at=excluded.uploaded_at,
                link_type=excluded.link_type,
                expires_at=excluded.expires_at,
                url=excluded.url
            """,
            (bucket, key, size, now, link_type, expires_at, url),
        )


def records_for(bucket: str, prefix: str = "") -> dict[str, sqlite3.Row]:
    """Return {key: row} for a bucket, optionally filtered by key prefix."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM uploads WHERE bucket = ? AND key LIKE ? ORDER BY key",
            (bucket, f"{prefix}%"),
        ).fetchall()
    return {row["key"]: row for row in rows}


def delete(bucket: str, key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM uploads WHERE bucket = ? AND key = ?", (bucket, key))
