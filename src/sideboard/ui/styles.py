"""Rich styles for the card and renderer layer.

These are **style names**, not colours: ``"sb-dim"`` and friends are registered on the
application console by :func:`sideboard.ui.theme.build_rich_theme`, and Rich resolves them
when it renders. That is what lets a configured palette recolour card text, not just the
chrome — a ``Style(color="#7e8a95")`` captured at import time would keep the old colour
forever, because every renderer holds its own reference to the object.

Textual's ``Theme.variables`` define the same names for textual CSS (``theme.tcss``);
Rich cannot read those, which is why the names are registered twice, from one palette.

No feature may define its own colour: everything a feature renders goes through these
names.
"""

from __future__ import annotations

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

DIM = "sb-dim"
VALUE = "sb-value"
OK = "sb-ok"
INFO = "sb-info"
WARN = "sb-warn"
ERROR = "sb-error"
ITEM = "sb-item"
READ = "sb-read"
BORDER = "sb-border"
BORDER_FOCUS = "sb-border-focus"
HELD = "sb-held"

STYLES: dict[str, str] = {
    DIM: DIM,
    VALUE: VALUE,
    OK: OK,
    INFO: INFO,
    WARN: WARN,
    ERROR: ERROR,
    ITEM: ITEM,
    READ: READ,
    BORDER: BORDER,
    BORDER_FOCUS: BORDER_FOCUS,
    HELD: HELD,
}
