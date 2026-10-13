"""Validated payload models for ``reddit.threads`` (payload schema version 1).

Only minimal thread metadata is kept: title, link, optional author/date and a plain
text excerpt. ``score`` and ``comment_count`` remain ``None`` when the permitted
source does not report them - public RSS reports neither - and are never defaulted
to ``0``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...core.contracts import SourcePayload

__all__ = [
    "REDDIT_PAYLOAD_SCHEMA",
    "CommunityReport",
    "CommunityReportStatus",
    "RedditPayload",
    "Thread",
]

REDDIT_PAYLOAD_SCHEMA = "reddit.threads"

SUMMARY_LIMIT = 240

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACE = re.compile(r"\s+")

CommunityReportStatus = Literal["ok", "blocked", "error", "skipped"]


def _plain(value: str, *, limit: int) -> str:
    """Collapse whitespace, drop control characters and bound the length."""
    text = _SPACE.sub(" ", _CONTROL.sub(" ", value)).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


class Thread(BaseModel):
    """One thread as reported by the permitted feed.

    ``id`` is the provider's stable entry id (``t3_…``) or a deterministic digest of
    the link when the feed omits one. ``url`` is the thread link; ``permalink`` is the
    reddit.com path when the link points at reddit.com.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    community: str = Field(min_length=1, max_length=30)
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(max_length=512)
    permalink: str | None = Field(default=None, max_length=512)
    author: str | None = Field(default=None, max_length=80)
    published: datetime | None = None
    score: int | None = Field(default=None, ge=0)
    comment_count: int | None = Field(default=None, ge=0)
    summary: str | None = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("thread url must be an absolute http(s) URL")
        return value

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = _plain(value, limit=SUMMARY_LIMIT)
        return text or None


class CommunityReport(BaseModel):
    """Outcome of one collection attempt, so partial coverage stays visible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    community: str = Field(min_length=1, max_length=30)
    status: CommunityReportStatus
    note: str = Field(default="", max_length=200)

    @field_validator("note")
    @classmethod
    def _validate_note(cls, value: str) -> str:
        return _plain(value, limit=200)


class RedditPayload(SourcePayload):
    """Threads collected from permitted Reddit feeds.

    ``as_of`` is the newest entry timestamp the feed reported (or the collection time
    when the feed carried none) and ``valid_until`` is 48 hours later. A failed
    community never fabricates entries: it is reported in ``community_reports`` and
    ``coverage.missing``.
    """

    threads: tuple[Thread, ...]
    community_reports: tuple[CommunityReport, ...] = ()

    @classmethod
    def schema_id(cls) -> str:
        return REDDIT_PAYLOAD_SCHEMA
