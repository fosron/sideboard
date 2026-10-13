"""Typed settings for the generic local snapshot feature.

The source reads exactly one explicitly configured file. ``path`` must be absolute (or
start with ``~``) because the host derives the file allowlist from configured absolute
paths - a relative path could never be approved by the reader.

``refresh_seconds`` is a convenience alias accepted for the three cadence overrides:
the field is applied to ``active_seconds``/``ambient_seconds``/``background_seconds``
when those are unset, so the configuration example in ``docs/adding-widget.md`` keeps
working. Explicit fields always win.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "LocalSnapshotSettings",
    "SnapshotItemsSettings",
    "SnapshotMetricsSettings",
]


def _require_absolute(path: str) -> str:
    text = path.strip()
    if not text:
        return ""
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise ValueError("path must not contain control characters")
    if not text.startswith(("/", "~")):
        raise ValueError(
            "path must be absolute (for example /Users/you/state.json) or start with ~"
        )
    return text


class LocalSnapshotSettings(BaseModel):
    """Source settings for ``local_snapshot.file``."""

    model_config = ConfigDict(extra="forbid")

    path: str = ""
    title: str | None = Field(default=None, max_length=120)
    refresh_seconds: float | None = Field(default=None, gt=0.0)
    active_seconds: float | None = Field(default=None, gt=0.0)
    ambient_seconds: float | None = Field(default=None, gt=0.0)
    background_seconds: float | None = Field(default=None, gt=0.0)
    max_history_points: int = Field(default=120, ge=0, le=5000)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _require_absolute(value)

    @model_validator(mode="after")
    def _apply_refresh_seconds(self) -> LocalSnapshotSettings:
        if self.refresh_seconds is not None:
            if self.active_seconds is None:
                self.active_seconds = self.refresh_seconds
            if self.ambient_seconds is None:
                self.ambient_seconds = self.refresh_seconds
            if self.background_seconds is None:
                self.background_seconds = self.refresh_seconds
        return self


class SnapshotMetricsSettings(BaseModel):
    """Widget settings for ``local_snapshot.metrics``."""

    model_config = ConfigDict(extra="forbid")

    max_metrics: int = Field(default=6, ge=1, le=32)
    show_basis: bool = True
    sparkline: bool = True
    use_snapshot_title: bool = True


class SnapshotItemsSettings(BaseModel):
    """Widget settings for ``local_snapshot.items``.

    ``show_basis`` controls the footnote line that states the values come from a
    producer-supplied snapshot rather than a live query.
    """

    model_config = ConfigDict(extra="forbid")

    max_items: int = Field(default=8, ge=1, le=64)
    show_basis: bool = True
    show_published: bool = True
    use_snapshot_title: bool = True
