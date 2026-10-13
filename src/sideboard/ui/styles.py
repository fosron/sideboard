"""Rich styles for the card and renderer layer.

Textual's ``Theme.variables`` defines **textual CSS** variables (used by
``theme.tcss``). They are not Rich style *names*, so a string such as ``"sb-dim"``
fails when Rich tries to parse it as a colour. Everything Rich renders therefore uses
these ``Style`` objects directly.

Keep the colour literals here in sync with ``theme.py``: this module is the single
place a Rich-rendered colour is chosen, and no feature may define its own.
"""

from __future__ import annotations

from rich.style import Style

__all__ = [
    "BORDER",
    "BORDER_FOCUS",
    "DIM",
    "ERROR",
    "HELD",
    "INFO",
    "ITEM",
    "OK",
    "READ",
    "STYLES",
    "VALUE",
    "WARN",
]

DIM = Style(color="#7e8a95")
VALUE = Style(color="#e2e7ec")
OK = Style(color="#7fae7a")
INFO = Style(color="#8fa8bd")
WARN = Style(color="#d8b26a")
ERROR = Style(color="#cf7b7b")
ITEM = Style(color="#cfd6dd")
READ = Style(color="#6f7a84")
BORDER = Style(color="#2a323a")
BORDER_FOCUS = Style(color="#7aa2c8")
HELD = Style(color="#d8b26a")

STYLES: dict[str, Style] = {
    "sb-dim": DIM,
    "sb-value": VALUE,
    "sb-ok": OK,
    "sb-info": INFO,
    "sb-warn": WARN,
    "sb-error": ERROR,
    "sb-item": ITEM,
    "sb-read": READ,
    "sb-border": BORDER,
    "sb-border-focus": BORDER_FOCUS,
    "sb-held": HELD,
}
