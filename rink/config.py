"""Load and save rink configuration.

Config lives at ~/.config/rink/config.toml and holds the R2 credentials, bucket,
and optional public base URL. Any field can be overridden by an environment
variable (RINK_*) so secrets can stay out of the file if preferred.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Max lifetime for a SigV4 presigned URL (7 days), used as a hard cap.
MAX_EXPIRY = 604800
DEFAULT_EXPIRY = 3600

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "rink"
CONFIG_PATH = CONFIG_DIR / "config.toml"

# Maps config field -> environment variable override.
_ENV_OVERRIDES = {
    "account_id": "RINK_ACCOUNT_ID",
    "access_key_id": "RINK_ACCESS_KEY_ID",
    "secret_access_key": "RINK_SECRET_ACCESS_KEY",
    "bucket": "RINK_BUCKET",
    "public_base_url": "RINK_PUBLIC_BASE_URL",
}


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_TOKEN = re.compile(r"(\d+)([smhdw])")


def parse_duration(text: str) -> int:
    """Parse a human duration into seconds.

    Accepts a bare integer (seconds), a single unit ('30m', '2h', '7d', '1w'),
    or a compound ('1h30m'). Units: s, m, h, d, w. Case-insensitive.
    """
    text = text.strip().lower()
    if not text:
        raise ValueError("empty duration")
    if text.isdigit():
        return int(text)
    tokens = _DURATION_TOKEN.findall(text)
    # Reject anything we didn't fully consume (e.g. '2x', '1h foo').
    if not tokens or "".join(n + u for n, u in tokens) != text:
        raise ValueError(
            f"invalid duration {text!r} — use e.g. 30m, 2h, 7d, 1h30m, or seconds"
        )
    return sum(int(n) * _UNIT_SECONDS[u] for n, u in tokens)


@dataclass
class Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    public_base_url: str | None = None
    default_expiry: int = DEFAULT_EXPIRY

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    def require_public_base_url(self) -> str:
        if not self.public_base_url:
            raise ConfigError(
                "No public base URL configured. Enable the Public Development URL "
                "on your bucket in the Cloudflare dashboard, then set it via "
                "`rink config` or the RINK_PUBLIC_BASE_URL env var."
            )
        return self.public_base_url.rstrip("/")


def read_raw() -> dict:
    """Read raw values straight from the config file (no env overrides).

    Returns an empty dict if the file does not exist. Used by `rink config` to
    pre-fill prompts when editing an existing config.
    """
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


def load_config() -> Config:
    """Load config from file, applying env-var overrides on top.

    Raises ConfigError if required fields are missing after merging.
    """
    data: dict = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as fh:
            data = tomllib.load(fh)

    for field, env in _ENV_OVERRIDES.items():
        if os.environ.get(env):
            data[field] = os.environ[env]

    required = ("account_id", "access_key_id", "secret_access_key", "bucket")
    missing = [f for f in required if not data.get(f)]
    if missing:
        raise ConfigError(
            "Missing config: "
            + ", ".join(missing)
            + ". Run `rink config` to set them up."
        )

    return Config(
        account_id=data["account_id"],
        access_key_id=data["access_key_id"],
        secret_access_key=data["secret_access_key"],
        bucket=data["bucket"],
        public_base_url=data.get("public_base_url") or None,
        default_expiry=int(data.get("default_expiry", DEFAULT_EXPIRY)),
    )


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def save_config(cfg: Config) -> Path:
    """Write config to disk with 0600 perms (it holds a secret)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        f'account_id = "{_toml_escape(cfg.account_id)}"',
        f'access_key_id = "{_toml_escape(cfg.access_key_id)}"',
        f'secret_access_key = "{_toml_escape(cfg.secret_access_key)}"',
        f'bucket = "{_toml_escape(cfg.bucket)}"',
        f"default_expiry = {cfg.default_expiry}",
    ]
    if cfg.public_base_url:
        lines.append(f'public_base_url = "{_toml_escape(cfg.public_base_url)}"')

    CONFIG_PATH.write_text("\n".join(lines) + "\n")
    CONFIG_PATH.chmod(0o600)
    return CONFIG_PATH
