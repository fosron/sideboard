"""Logging setup.

Logs contain provider status, timings and redacted error categories — never mail
bodies, credentials, balances, private channel history or contract data. The file is
capped and rotated, and it lives in the private log directory with owner-only
permissions.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
from pathlib import Path

from .storage import ensure_private_dir

__all__ = ["configure_logging", "redact"]

MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3

_SECRET_MARKERS = (
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "session",
)


def redact(value: object) -> str:
    """Best-effort redaction for diagnostics. Never returns a raw credential."""
    text = str(value)
    lowered = text.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            return "<redacted: credential-like value>"
    return text[:300]


def configure_logging(
    log_dir: Path, *, level: int = logging.INFO, console: bool = False, demo: bool = False
) -> Path:
    """Attach a rotating file handler and return the log file path."""
    ensure_private_dir(log_dir)
    name = "sideboard-demo.log" if demo else "sideboard.log"
    log_path = log_dir / name
    root = logging.getLogger("sideboard")
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(file_handler)
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        root.addHandler(console_handler)
    with contextlib.suppress(OSError):  # pragma: no cover - best-effort tightening
        log_path.chmod(0o600)
    return log_path
