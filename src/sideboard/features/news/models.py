"""Validated payloads for the news feature."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ...core.contracts import SourcePayload

__all__ = ["Article", "FeedReport", "NewsPayload"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Article(_Frozen):
    """One feed entry: bounded metadata plus the publisher's own excerpt."""

    id: str
    title: str
    url: str
    canonical: str
    source_id: str
    source_label: str
    category: str
    language: str
    published: datetime | None = None
    excerpt: str | None = None
    ai_related: bool = False


class FeedReport(_Frozen):
    """Per-feed outcome, so a partial refresh is visible rather than silent."""

    feed_id: str
    label: str
    status: str = "ok"
    not_modified: bool = False
    items: int = 0
    note: str | None = None


class NewsPayload(SourcePayload):
    """Bounded news set plus per-feed reports and HTTP validators."""

    articles: tuple[Article, ...] = ()
    reports: tuple[FeedReport, ...] = ()
    validators: dict[str, str] = Field(default_factory=dict)
    last_modified: dict[str, str] = Field(default_factory=dict)
    offline: bool = False
