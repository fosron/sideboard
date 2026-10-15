"""Pure presentation of Hacker News payloads.

Compact output is an item list preserving the API (and configured list) order.
``relevance_filter`` is an explicit, deterministic Overview ranking; the card always
declares it so it is never confused with the raw Hacker News ranking. Age is derived
here from ``published_at`` and ``view_context.now``. Presentation performs no I/O.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from ...core.contracts import (
    InputBinding,
    Item,
    PresentInput,
    Severity,
    StatusView,
    ViewContext,
    ViewModel,
    WidgetSpec,
)
from ...core.present import status_from_envelope
from .config import HackerNewsWidgetSettings
from .models import PAYLOAD_SCHEMA, HackerNewsPayload, Story, discussion_url

__all__ = [
    "RELEVANCE_NOTE",
    "WIDGET",
    "age_text",
    "present",
    "relevance_rank",
]

RELEVANCE_NOTE = "relevance filtered (not the raw HN ranking)"
DETAIL_MIN_ROWS = 30


def relevance_rank(stories: Sequence[Story], now: datetime) -> list[Story]:
    """Deterministic Overview ranking that is deliberately not the raw HN order.

    Rule: a story's *engagement rate* is ``score / age_in_hours`` (age floored at one
    minute so a just-posted story cannot divide by zero). Only stories with both a
    score and a timestamp carry that signal. Keep the stories whose rate is at or
    above the median rate, ordered by rate descending; ties keep the API order.
    When no story has a usable score and timestamp the API order is returned
    unchanged, so the card is never empty merely because the source omitted scores.

    Pure: the same stories and ``now`` always produce the same list.
    """
    rates: list[tuple[float, int]] = []
    for index, story in enumerate(stories):
        rate = _engagement_rate(story, now)
        if rate is not None:
            rates.append((rate, index))
    if not rates:
        return list(stories)
    median = statistics.median(rate for rate, _ in rates)
    kept = sorted(
        (pair for pair in rates if pair[0] >= median),
        key=lambda pair: (-pair[0], pair[1]),
    )
    return [stories[index] for _, index in kept]


def _engagement_rate(story: Story, now: datetime) -> float | None:
    if story.score is None or story.time is None:
        return None
    reference = _as_utc(now)
    published = _as_utc(story.time)
    age_hours = max((reference - published).total_seconds() / 3600.0, 1.0 / 60.0)
    return story.score / age_hours


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def age_text(published_at: datetime | None, now: datetime) -> str | None:
    """Short age label (``5m``, ``3h``, ``2d``); ``None`` when the source gave no time."""
    if published_at is None:
        return None
    seconds = max(0.0, (_as_utc(now) - _as_utc(published_at)).total_seconds())
    minutes = int(seconds // 60)
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 30:
        return f"{days}d"
    months = days // 30
    if months < 12:
        return f"{months}mo"
    return f"{days // 365}y"


def present(request: PresentInput) -> ViewModel:
    """Render one card. A missing or unconfigured payload yields a status, never raises."""
    envelope = request.envelope("primary")
    context = request.context
    status = status_from_envelope(
        envelope,
        offline=context.offline,
        demo=context.demo,
        privacy=context.privacy,
    )
    try:
        settings = HackerNewsWidgetSettings.model_validate(request.settings)
    except ValidationError:
        return ViewModel(
            status=StatusView(
                severity=Severity.ERROR,
                headline="Invalid widget settings",
                detail="hackernews.list settings failed validation.",
            ),
            footnote="error",
        )

    payload = envelope.payload if envelope is not None else None
    if not isinstance(payload, HackerNewsPayload):
        return ViewModel(
            status=status
            or StatusView(
                severity=Severity.WARNING,
                headline="Unsupported payload",
                detail="This card expects a hackernews.stories payload.",
            ),
            footnote="no data",
        )

    stories = [
        story for story in payload.stories if settings.list is None or story.list == settings.list
    ]
    subtitle: str | None = None
    ordering_note: str | None = None
    if settings.relevance_filter:
        # The note explained a filter. A list is a list; how it was ordered is not drawn.
        subtitle = None
        ranked = relevance_rank(stories, context.now)
        if ranked == stories:
            ordering_note = "no score/age signal; API order kept"
        stories = ranked

    if not stories:
        return ViewModel(
            status=status
            or StatusView(
                severity=Severity.INFO,
                headline="No stories",
                detail=(
                    f"No usable stories in the {settings.list!r} list yet."
                    if settings.list is not None
                    else "No usable stories yet."
                ),
                hints=("Waiting for the next successful refresh.",),
            ),
            subtitle=subtitle,
            footnote=_footnote(payload, ordering_note),
        )

    limit = _row_limit(settings, context)
    items = tuple(_item(story, settings=settings, context=context) for story in stories[:limit])
    return ViewModel(
        status=status,
        subtitle=subtitle,
        items=items,
        footnote=_footnote(payload, ordering_note),
    )


def _row_limit(settings: HackerNewsWidgetSettings, context: ViewContext) -> int:
    """Detail mode shows more rows; compact mode honours the configured limit."""
    if context.compact:
        return max(1, settings.limit)
    return max(1, settings.limit, DETAIL_MIN_ROWS)


def _item(story: Story, *, settings: HackerNewsWidgetSettings, context: ViewContext) -> Item:
    meta: list[tuple[str, str]] = []
    if settings.show_score and story.score is not None:
        meta.append(("pts", str(story.score)))
    if story.descendants is not None:
        meta.append(("c", str(story.descendants)))
    if settings.show_age:
        age = age_text(story.time, context.now)
        if age is not None:
            meta.append(("age", age))
    return Item(
        id=f"hn:{story.id}",
        title=story.title,
        url=story.url or discussion_url(story.id),
        # The comments page is a second destination, not a replacement: `o` opens the
        # article, `c` opens the thread. A story with no article URL already points at its
        # discussion, so it offers no second link.
        alt_url=discussion_url(story.id) if story.url else None,
        published_at=story.time,
        meta=tuple(meta),
    )


def _footnote(payload: HackerNewsPayload, ordering_note: str | None) -> str | None:
    coverage = _coverage_footnote(payload)
    parts = [part for part in (coverage, ordering_note) if part]
    return " · ".join(parts) or None


def _coverage_footnote(payload: HackerNewsPayload) -> str | None:
    coverage = payload.coverage
    if not coverage.is_partial:
        return None
    observed = coverage.observed if coverage.observed is not None else 0
    expected = coverage.expected if coverage.expected is not None else 0
    return f"partial fetch: {observed} of {expected} items"


WIDGET: WidgetSpec = WidgetSpec(
    type_id="hackernews.list",
    settings_model=HackerNewsWidgetSettings,
    inputs=(InputBinding(name="primary", accepts=frozenset({PAYLOAD_SCHEMA})),),
    present=present,
    description="Hacker News Top/New/Show/Ask item list with score, comments and age.",
    default_category="software",
    min_width=26,
    min_height=4,
)
