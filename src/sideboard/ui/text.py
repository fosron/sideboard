"""Text sanitisation for untrusted content.

Every feed, article, snapshot and file import is treated as untrusted data. It is
**never** rendered as markup, never highlighted, and control sequences are removed
before it reaches the terminal.
"""

from __future__ import annotations

import re
import unicodedata

from rich.style import Style
from rich.text import Text

from ..core.contracts import Item, Privacy, Severity
from .styles import DIM, ERROR, INFO, ITEM, OK, READ, VALUE, WARN

__all__ = [
    "MASK",
    "SEVERITY_STYLE",
    "control_free",
    "item_text",
    "label_value_text",
    "masked",
    "severity_symbol",
    "single_line",
]

MASK = "••••"

SEVERITY_STYLE: dict[Severity, Style] = {
    Severity.OK: OK,
    Severity.INFO: INFO,
    Severity.WARNING: WARN,
    Severity.ERROR: ERROR,
    Severity.UNKNOWN: DIM,
}

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069]")


def control_free(value: object) -> str:
    """Remove ANSI escapes, control characters and bidi overrides from untrusted text."""
    if value is None:
        return ""
    text = str(value)
    text = _ANSI.sub("", text)
    text = _CONTROL.sub(" ", text)
    text = _BIDI.sub("", text)
    text = text.replace("\u2028", " ").replace("\u2029", " ")
    text = unicodedata.normalize("NFC", text)
    return " ".join(text.split())


def single_line(value: object, limit: int = 200) -> str:
    text = control_free(value)
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def masked(value: object, *, privacy: bool) -> str:
    if privacy:
        return MASK
    return single_line(value)


def severity_symbol(severity: Severity) -> str:
    """ASCII/Unicode-safe severity marker. No emoji is required for meaning."""
    return {
        Severity.OK: "ok",
        Severity.INFO: "info",
        Severity.WARNING: "warn",
        Severity.ERROR: "error",
        Severity.UNKNOWN: "?",
    }[severity]


def item_text(item: Item, *, privacy: bool, width: int) -> Text:
    """Render one item as a styled :class:`Text` with no markup parsing."""
    hide = privacy and item.privacy is Privacy.PRIVATE
    text = Text()
    marker = "◆" if item.saved else " "
    text.append(f"{marker} ")
    style = READ if item.read else ITEM
    text.append(single_line(item.title, max(20, width - 4)), style=style)
    if item.badge:
        text.append("  ")
        text.append(f"[{single_line(item.badge, 24)}]", style=DIM)
    if item.subtitle:
        text.append("\n  ")
        text.append("—" if hide else masked(item.subtitle, privacy=False), style=DIM)
    if item.meta:
        parts = [
            f"{single_line(key, 12)} {masked(value, privacy=hide or key.lower() in {'score', 'comments'})}"
            for key, value in item.meta
        ]
        text.append("\n  ")
        text.append(" · ".join(parts), style=DIM)
    return text


def label_value_text(
    label: str, value: str, *, privacy: bool, hide: bool = False, width: int = 80
) -> Text:
    text = Text()
    label_text = single_line(label, 40)
    value_text = MASK if (privacy and hide) else single_line(value, 80)
    pad = max(1, min(18, width // 3) - len(label_text))
    text.append(label_text, style=DIM)
    text.append(" " * pad)
    text.append(value_text, style=VALUE)
    return text
