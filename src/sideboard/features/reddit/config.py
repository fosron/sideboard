"""Typed settings and shared defaults for the Reddit feature.

Two settings models exist because two different questions are configured separately:

* :class:`RedditSettings` - the *source*: which communities may be collected and
  whether the user has validated a permitted feed (``mode``).
* :class:`RedditListSettings` - the *widget*: how threads are ranked and shown, plus
  the community links used when the feed is unavailable.

The community list intentionally exists on both sides. The source needs it to decide
what to fetch; the card needs it to render community links in the shipped link-only
default, where no payload (and therefore no community list) is ever collected.
:data:`DEFAULT_COMMUNITIES` keeps both defaults identical.

These are suggested starting communities, never inferred subscriptions.
"""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "COMMUNITY_PATTERN",
    "DEFAULT_COMMUNITIES",
    "RedditListSettings",
    "RedditMode",
    "RedditSettings",
]

COMMUNITY_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,30}$")

DEFAULT_COMMUNITIES: tuple[str, ...] = ("selfhosted", "homelab", "PHP")

RedditMode = Literal["links", "rss", "disabled"]


def _clean_communities(values: list[str]) -> list[str]:
    """Validate bare community names, drop case-insensitive duplicates, keep order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        name = raw.strip()
        if not COMMUNITY_PATTERN.match(name):
            raise ValueError(
                f"community {raw!r} must be a bare name matching {COMMUNITY_PATTERN.pattern}"
            )
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


class RedditSettings(BaseModel):
    """Source settings for ``reddit.threads``.

    ``mode`` is the access decision, not a retry switch:

    * ``links`` - link-only. The provider performs **no** network I/O and reports the
      documented conditional-access state. Shipped default.
    * ``rss`` - public RSS, enabled only after the user validated that form of access
      from this Mac.
    * ``disabled`` - no collection at all, no request made.
    """

    model_config = ConfigDict(extra="forbid")

    mode: RedditMode = "links"
    communities: list[str] = Field(default_factory=lambda: list(DEFAULT_COMMUNITIES))
    # One request per community. Reddit's public feed rate-limits a burst (a 429 is
    # reported per community as partial coverage), so requests are spaced out. Set 0 to
    # fire them together and accept more 429s.
    request_spacing_seconds: float = Field(default=3.0, ge=0.0, le=30.0)
    active_seconds: float | None = Field(default=None, gt=0.0)
    ambient_seconds: float | None = Field(default=None, gt=0.0)
    background_seconds: float | None = Field(default=None, gt=0.0)
    include_scores: bool = False
    """Reserved for a later explicitly permitted API adapter.

    Public RSS reports neither score nor comment count, so enabling this can never
    produce a value: the collector leaves both fields absent rather than inventing 0.
    """

    max_items_per_community: int = Field(default=10, ge=1, le=50)

    @field_validator("communities")
    @classmethod
    def _validate_communities(cls, value: list[str]) -> list[str]:
        return _clean_communities(value)


class RedditListSettings(BaseModel):
    """Widget settings for ``reddit.list``.

    Ranking uses freshness and these weights only; metadata the permitted source does
    not report (score/comment count under RSS) contributes nothing instead of a
    synthesised value.
    """

    model_config = ConfigDict(extra="forbid")

    communities: list[str] = Field(default_factory=lambda: list(DEFAULT_COMMUNITIES))
    max_items: int = Field(default=12, ge=1, le=60)
    max_communities: int = Field(default=8, ge=1, le=30)
    community_weights: dict[str, float] = Field(default_factory=dict)
    half_life_hours: float = Field(default=24.0, gt=0.0, le=8760.0)
    score_weight: float = Field(default=0.25, ge=0.0, le=2.0)
    comment_weight: float = Field(default=0.15, ge=0.0, le=2.0)
    show_community: bool = True

    @field_validator("communities")
    @classmethod
    def _validate_communities(cls, value: list[str]) -> list[str]:
        return _clean_communities(value)

    @field_validator("community_weights")
    @classmethod
    def _validate_weights(cls, value: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for raw_key, weight in value.items():
            key = raw_key.strip()
            if not COMMUNITY_PATTERN.match(key):
                raise ValueError(
                    f"community weight key {raw_key!r} must match {COMMUNITY_PATTERN.pattern}"
                )
            if not math.isfinite(weight) or weight < 0.0:
                raise ValueError(f"weight for {raw_key!r} must be a finite value >= 0")
            out[key] = float(weight)
        return out
