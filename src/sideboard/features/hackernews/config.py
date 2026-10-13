"""Typed settings for the Hacker News feature."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import STORY_LISTS

__all__ = [
    "DEFAULT_LISTS",
    "STORY_LISTS",
    "HackerNewsSettings",
    "HackerNewsWidgetSettings",
]

DEFAULT_LISTS: tuple[str, ...] = ("top", "new")


class _StrictSettings(BaseModel):
    """Reject unknown keys: a typo must not silently become a default."""

    model_config = ConfigDict(extra="forbid")


class HackerNewsSettings(_StrictSettings):
    """Source settings for ``hackernews.stories``."""

    lists: list[str] = Field(default_factory=lambda: list(DEFAULT_LISTS), min_length=1)
    items_per_list: int = Field(default=30, ge=1, le=60)
    concurrency: int = Field(default=6, ge=1, le=12)
    include_jobs: bool = False
    # Optional cadence overrides; the host reads these fields by name.
    active_seconds: float | None = None
    ambient_seconds: float | None = None
    background_seconds: float | None = None

    @field_validator("lists")
    @classmethod
    def _known_lists(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for entry in value:
            key = str(entry).strip().lower()
            if key not in STORY_LISTS:
                expected = ", ".join(STORY_LISTS)
                raise ValueError(f"unknown Hacker News list {entry!r}; expected one of: {expected}")
            if key not in cleaned:
                cleaned.append(key)
        return cleaned


class HackerNewsWidgetSettings(_StrictSettings):
    """Widget settings for ``hackernews.list``.

    ``list=None`` shows a mixed list; otherwise only that list's stories are shown.
    """

    list: str | None = None
    limit: int = 8
    show_score: bool = True
    show_age: bool = True
    relevance_filter: bool = False

    @field_validator("list")
    @classmethod
    def _known_list(cls, value: str | None) -> str | None:
        if value is None:
            return None
        key = str(value).strip().lower()
        if key not in STORY_LISTS:
            expected = ", ".join(STORY_LISTS)
            raise ValueError(f"unknown Hacker News list {value!r}; expected one of: {expected}")
        return key
