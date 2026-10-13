"""Unit conversion for Docker CLI output.

The CLI prints human units (``1.5GiB``, ``12.5kB``, ``0B``) and percentages with a
trailing ``%``. These helpers are pure and covered by unit tests; the adapter never
parses a human-aligned table.
"""

from __future__ import annotations

import re

from ...core.errors import ParseError

__all__ = ["parse_cpu_percent", "parse_io_pair", "parse_size", "parse_size_pair"]

# Docker uses decimal kB/MB/GB and IEC KiB/MiB/GiB. Both are kept distinct.
_DECIMAL = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4, "pb": 1000**5}
_BINARY = {
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "pib": 1024**5,
    "k": 1024,
    "ki": 1024,
}
_SIZE = re.compile(r"^\s*(?P<number>-?[0-9]*\.?[0-9]+)\s*(?P<unit>[A-Za-z]*)\s*$")


def parse_size(value: str | None) -> int | None:
    """Convert one CLI size to bytes. Unknown or empty input yields ``None``."""
    if value is None:
        return None
    text = value.strip()
    if not text or text in {"-", "N/A", "unknown"}:
        return None
    match = _SIZE.match(text)
    if match is None:
        raise ParseError(f"unrecognised size value: {text[:24]!r}")
    number = float(match.group("number"))
    unit = (match.group("unit") or "b").lower()
    factor = _DECIMAL.get(unit) or _BINARY.get(unit)
    if factor is None:
        raise ParseError(f"unrecognised size unit in {text[:24]!r}")
    return round(number * factor)


def parse_size_pair(value: str | None) -> tuple[int | None, int | None]:
    """Parse ``"12.3MiB / 1GiB"`` into ``(used, limit)``."""
    if not value:
        return None, None
    parts = [part.strip() for part in value.split("/")]
    if len(parts) != 2:
        raise ParseError("expected a 'used / limit' size pair")
    return parse_size(parts[0]), parse_size(parts[1])


def parse_io_pair(value: str | None) -> tuple[int | None, int | None]:
    """Parse ``"1.2kB / 3.4kB"`` into ``(received, sent)`` cumulative counters."""
    return parse_size_pair(value)


def parse_cpu_percent(value: str | None) -> float | None:
    """Parse ``"12.34%"``.

    The container/engine CPU scale is preserved **without clipping**: a multi-core
    container can legitimately report well above 100 %.
    """
    if value is None:
        return None
    text = value.strip().rstrip("%").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise ParseError(f"unrecognised CPU percentage: {value[:16]!r}") from exc
