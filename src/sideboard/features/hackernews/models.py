"""Validated payload models for the Hacker News source.

Only minimal story metadata is kept: no comment trees, no article bodies and no
author profiles. Ask HN text is reduced to a short plain-text excerpt.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ...core.contracts import SourcePayload

__all__ = [
    "PAYLOAD_SCHEMA",
    "PAYLOAD_SCHEMA_VERSION",
    "PROVENANCE",
    "STORY_LISTS",
    "TEXT_EXCERPT_LIMIT",
    "VALID_FOR_SECONDS",
    "HackerNewsPayload",
    "Story",
    "discussion_url",
]

PAYLOAD_SCHEMA = "hackernews.stories"
PAYLOAD_SCHEMA_VERSION = 1
PROVENANCE = "hacker-news-official-api"
VALID_FOR_SECONDS = 300.0
TEXT_EXCERPT_LIMIT = 300

STORY_LISTS: tuple[str, ...] = ("top", "new", "show", "ask")

_DISCUSSION_URL = "https://news.ycombinator.com/item?id={item_id}"


def discussion_url(item_id: int) -> str:
    """Hacker News discussion permalink, used when a story has no external URL."""
    return _DISCUSSION_URL.format(item_id=item_id)


class Story(BaseModel):
    """One Hacker News item, reduced to the fields a card renders."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    list: str
    title: str
    by: str | None = None
    url: str | None = None
    score: int | None = None
    descendants: int | None = None
    time: datetime | None = None
    type: str | None = None
    text_excerpt: str | None = None


class HackerNewsPayload(SourcePayload):
    """One collection across every configured list, in configured list order."""

    stories: tuple[Story, ...] = ()

    @classmethod
    def schema_id(cls) -> str:
        return PAYLOAD_SCHEMA
