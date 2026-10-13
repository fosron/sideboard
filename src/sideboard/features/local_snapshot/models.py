"""Validated models for the generic local snapshot format (schema version 1).

The format is the plan's section 6.7 contract: ``schema_version`` (must be 1),
``source_id``, ``generated_at``, optional ``expires_at``, ``privacy`` (default
``private``), optional ``title``, ``metrics`` and ``items``.

Choices, documented rather than implicit:

* **Unknown keys are rejected** (``extra="forbid"`` everywhere). The format is
  versioned, so an unrecognised key means the producer is speaking a newer or
  different contract; silently ignoring it would hide data or meaning.
* **Metric values must be scalar** (``string | number | boolean``) and must not contain
  newlines or other control characters: a value is data for a renderer, never a command
  or executable markup. The shared renderers escape text, and this second layer refuses
  control characters outright.
* **Timestamps must carry a timezone offset.** A naive local timestamp is ambiguous and
  is rejected instead of being assumed to be UTC.
* **``history`` entries may be ``null``** to mark a gap. Gaps are preserved as
  :class:`~sideboard.core.contracts.SeriesPoint` values with ``value=None`` so a
  sparkline breaks instead of drawing a fabricated zero.
* **``expires_at`` in the past is not an error.** The payload keeps
  ``valid_until = expires_at`` so the host marks the data stale/expired honestly and the
  card can still show what the producer last wrote.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from ...core.contracts import Precision, Privacy, SourcePayload

__all__ = [
    "ID_PATTERN",
    "LOCAL_SNAPSHOT_SCHEMA",
    "MAX_HISTORY_POINTS",
    "MAX_ITEMS",
    "MAX_METRICS",
    "SCHEMA_VERSION",
    "SnapshotDocument",
    "SnapshotItem",
    "SnapshotMetric",
    "SnapshotPayload",
]

LOCAL_SNAPSHOT_SCHEMA = "local_snapshot.file"

SCHEMA_VERSION = 1

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

MAX_METRICS = 200
MAX_ITEMS = 500
MAX_HISTORY_POINTS = 5000

_CONTROL_MESSAGE = "must not contain newlines or control characters"


def _reject_control(value: str, field: str) -> str:
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{field} {_CONTROL_MESSAGE}")
    return value


def _iso_string(value: Any, info: ValidationInfo) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value
    raise ValueError(f"{info.field_name} must be an ISO 8601 string with a timezone offset")


def _aware(value: datetime, info: ValidationInfo) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{info.field_name} must include a timezone offset")
    return value


class SnapshotMetric(BaseModel):
    """One labelled measurement plus an optional bounded numeric history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    label: str = Field(min_length=1, max_length=120)
    value: bool | int | float | str
    unit: str = Field(default="", max_length=24)
    basis: str = Field(default="", max_length=64)
    history: tuple[float | None, ...] = ()

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: bool | int | float | str) -> bool | int | float | str:
        if isinstance(value, str):
            return _reject_control(value, "metric value")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("metric value must be a finite number")
        return value

    @field_validator("label", "unit", "basis")
    @classmethod
    def _validate_text(cls, value: str, info: ValidationInfo) -> str:
        return _reject_control(value, str(info.field_name))

    @field_validator("history")
    @classmethod
    def _validate_history(cls, value: tuple[float | None, ...]) -> tuple[float | None, ...]:
        if len(value) > MAX_HISTORY_POINTS:
            raise ValueError(f"history must hold at most {MAX_HISTORY_POINTS} points")
        for point in value:
            if point is not None and not math.isfinite(point):
                raise ValueError("history entries must be finite numbers or null gaps")
        return value


class SnapshotItem(BaseModel):
    """One short list entry with a stable id and an optional HTTP(S) link."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=512)
    subtitle: str | None = Field(default=None, max_length=200)
    published_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _reject_control(value, "item title")

    @field_validator("subtitle")
    @classmethod
    def _validate_subtitle(cls, value: str | None) -> str | None:
        return None if value is None else _reject_control(value, "item subtitle")

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("item url must be an absolute http(s) URL")
        _reject_control(value, "item url")
        return value

    @field_validator("published_at", mode="before")
    @classmethod
    def _validate_published(cls, value: Any, info: ValidationInfo) -> Any:
        return _iso_string(value, info)

    @field_validator("published_at")
    @classmethod
    def _validate_published_aware(
        cls, value: datetime | None, info: ValidationInfo
    ) -> datetime | None:
        return None if value is None else _aware(value, info)


class SnapshotDocument(BaseModel):
    """The exact JSON document producers write (and the JSON Schema describes)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    source_id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    generated_at: datetime
    expires_at: datetime | None = None
    privacy: Privacy = Privacy.PRIVATE
    title: str | None = Field(default=None, min_length=1, max_length=120)
    metrics: tuple[SnapshotMetric, ...] = ()
    items: tuple[SnapshotItem, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def _validate_version(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str | None:
        return None if value is None else _reject_control(value, "title")

    @field_validator("generated_at", "expires_at", mode="before")
    @classmethod
    def _parse_timestamp(cls, value: Any, info: ValidationInfo) -> Any:
        return _iso_string(value, info)

    @field_validator("generated_at", "expires_at")
    @classmethod
    def _require_offset(cls, value: datetime | None, info: ValidationInfo) -> datetime | None:
        return None if value is None else _aware(value, info)

    @field_validator("metrics")
    @classmethod
    def _bounded_metrics(cls, value: tuple[SnapshotMetric, ...]) -> tuple[SnapshotMetric, ...]:
        if len(value) > MAX_METRICS:
            raise ValueError(f"metrics must hold at most {MAX_METRICS} entries")
        return value

    @field_validator("items")
    @classmethod
    def _bounded_items(cls, value: tuple[SnapshotItem, ...]) -> tuple[SnapshotItem, ...]:
        if len(value) > MAX_ITEMS:
            raise ValueError(f"items must hold at most {MAX_ITEMS} entries")
        return value


class SnapshotPayload(SourcePayload):
    """Validated snapshot plus the freshness the host derives from the document.

    ``as_of`` is the producer's ``generated_at`` and ``valid_until`` its ``expires_at``
    (possibly already in the past), so freshness is computed from the producer's own
    claim instead of the collection time.
    """

    document: SnapshotDocument

    @classmethod
    def schema_id(cls) -> str:
        return LOCAL_SNAPSHOT_SCHEMA

    @model_validator(mode="after")
    def _derive_freshness(self) -> SnapshotPayload:
        object.__setattr__(self, "as_of", self.document.generated_at)
        object.__setattr__(self, "valid_until", self.document.expires_at)
        object.__setattr__(self, "precision", Precision.INSTANT)
        return self

    @property
    def title(self) -> str | None:
        return self.document.title

    @property
    def metrics(self) -> tuple[SnapshotMetric, ...]:
        return self.document.metrics

    @property
    def items(self) -> tuple[SnapshotItem, ...]:
        return self.document.items

    @property
    def privacy(self) -> Privacy:
        return self.document.privacy
