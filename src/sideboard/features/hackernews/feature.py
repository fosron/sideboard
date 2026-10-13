"""Feature registration for Hacker News."""

from __future__ import annotations

from ...core.contracts import FeatureSpec
from .presenter import WIDGET
from .provider import SOURCE

__all__ = ["FEATURE", "HN_LIST", "HN_SOURCE"]

HN_SOURCE = SOURCE
HN_LIST = WIDGET

FEATURE: FeatureSpec = FeatureSpec(
    id="hackernews",
    sources=(HN_SOURCE,),
    widgets=(HN_LIST,),
    description="Hacker News Top/New/Show/Ask lists from the official public API.",
)
