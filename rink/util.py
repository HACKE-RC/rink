"""Small terminal helpers: clipboard, QR codes, random tokens."""

from __future__ import annotations

import io
import secrets
import shutil
import subprocess

# Clipboard commands to try, in order, across platforms.
_CLIPBOARD_CMDS = (
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
    ["pbcopy"],
)


def copy_to_clipboard(text: str) -> str | None:
    """Copy text to the system clipboard. Returns the tool used, or None."""
    for cmd in _CLIPBOARD_CMDS:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True)
                return cmd[0]
            except Exception:  # noqa: BLE001 - try the next backend
                continue
    return None


def render_qr(text: str) -> str:
    """Render a QR code for `text` as terminal-safe text (or '' if unavailable)."""
    try:
        import segno
    except ImportError:
        return ""
    qr = segno.make(text, error="l")
    buf = io.StringIO()
    qr.terminal(out=buf, compact=True)
    return buf.getvalue()


def random_token(nbytes: int = 4) -> str:
    """A short URL-safe random hex token, e.g. for unguessable key prefixes."""
    return secrets.token_hex(nbytes)
