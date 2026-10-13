"""Validation entry points for the local snapshot format.

:func:`validate_snapshot` turns a decoded JSON document into a typed
:class:`~sideboard.features.local_snapshot.models.SnapshotPayload`, raising a readable
:class:`~sideboard.core.errors.ParseError` when the document does not match schema
version 1. :data:`SNAPSHOT_SCHEMA` exposes the shipped JSON Schema.

The JSON Schema and the pydantic models are two views of one contract and are kept in
agreement by tests (``tests/features/local_snapshot/test_schema.py``). The schema file is
loaded lazily on first access so importing this module performs no I/O.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ...core.errors import ParseError
from .models import SnapshotDocument, SnapshotPayload

__all__ = [
    "SNAPSHOT_SCHEMA",
    "SNAPSHOT_SCHEMA_PATH",
    "load_snapshot_schema",
    "validate_snapshot",
]

SNAPSHOT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "local-snapshot.schema.json"
)

PROBLEM_LIMIT = 8


class _LazySchemaMapping(Mapping[str, Any]):
    """Read-only mapping that loads the JSON Schema on first access, not at import."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _data(self) -> Mapping[str, Any]:
        return load_snapshot_schema()

    def __getitem__(self, key: str) -> Any:
        return self._data()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())


@lru_cache(maxsize=1)
def load_snapshot_schema() -> Mapping[str, Any]:
    """Load and cache ``src/sideboard/schemas/local-snapshot.schema.json``."""
    try:
        text = SNAPSHOT_SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ParseError("the bundled local snapshot schema is missing") from exc
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - packaging guard
        raise ParseError("the bundled local snapshot schema is not valid JSON") from exc
    if not isinstance(decoded, dict):  # pragma: no cover - packaging guard
        raise ParseError("the bundled local snapshot schema must be a JSON object")
    return decoded


SNAPSHOT_SCHEMA: Mapping[str, Any] = _LazySchemaMapping(SNAPSHOT_SCHEMA_PATH)


def validate_snapshot(document: object) -> SnapshotPayload:
    """Validate a decoded snapshot document, raising :class:`ParseError` on failure."""
    if not isinstance(document, Mapping):
        raise ParseError("snapshot must be a JSON object")
    try:
        parsed = SnapshotDocument.model_validate(dict(document))
    except ValidationError as exc:
        raise ParseError(f"snapshot does not match schema version 1: {_problems(exc)}") from exc
    return SnapshotPayload(document=parsed)


def _problems(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:PROBLEM_LIMIT]:
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        parts.append(f"{location}: {error['msg']}")
    remaining = len(exc.errors()) - PROBLEM_LIMIT
    if remaining > 0:
        parts.append(f"and {remaining} more problem(s)")
    message = "; ".join(parts)
    return message[:400]
