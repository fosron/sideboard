"""Pure presentation for the news digest and the saved-reading list.

The digest is deliberately dumb and deterministic: no model, no ranking service, no
translation. A bucket with no fresh content stays visibly empty.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from ...core.contracts import (
    Item,
    Metric,
    PresentInput,
    Privacy,
    Severity,
    StatusView,
    ViewContext,
    ViewModel,
)
from ...core.present import status_from_envelope
from .config import NewsWidgetSettings, SavedWidgetSettings
from .models import Article, NewsPayload

__all__ = ["DEFAULT_BUCKETS", "build_digest", "present_news", "present_saved"]

DEFAULT_BUCKETS = (
    "local",
    "lithuania",
    "europe",
    "world",
    "business",
    "personal_finance",
    "software",
    "ai",
    "gadgets",
    "infrastructure",
)

BUCKET_LABELS = {
    "local": "local",
    "lithuania": "LT",
    "europe": "EU",
    "world": "world",
    "business": "biz",
    "personal_finance": "money",
    "software": "dev",
    "ai": "AI",
    "gadgets": "h/w",
    "infrastructure": "infra",
}


def _fresh(article: Article, now: datetime, max_age_hours: int) -> bool:
    if article.published is None:
        return True
    age = (now - article.published).total_seconds() / 3600.0
    return age <= max_age_hours


def build_digest(
    articles: tuple[Article, ...],
    *,
    now: datetime,
    buckets: tuple[str, ...] = DEFAULT_BUCKETS,
    digest_size: int = 6,
    max_ai: int = 2,
    max_per_source: int = 2,
    max_age_hours: int = 48,
) -> tuple[Article, ...]:
    """One fresh article per bucket, in bucket order, respecting the AI and per-source caps.

    Deterministic and independent of feed ordering: within a bucket the newest article
    wins, ties broken by canonical URL so repeated runs are stable.
    """
    fresh = [article for article in articles if _fresh(article, now, max_age_hours)]
    fresh.sort(
        key=lambda a: (-(a.published or datetime.min.replace(tzinfo=UTC)).timestamp(), a.canonical)
    )
    chosen: list[Article] = []
    per_source: dict[str, int] = {}
    ai_count = 0
    used: set[str] = set()

    def take(article: Article) -> bool:
        nonlocal ai_count
        if article.canonical in used:
            return False
        if article.ai_related and ai_count >= max_ai:
            return False
        if per_source.get(article.source_id, 0) >= max_per_source:
            return False
        used.add(article.canonical)
        per_source[article.source_id] = per_source.get(article.source_id, 0) + 1
        if article.ai_related:
            ai_count += 1
        chosen.append(article)
        return True

    # One per bucket per round, then anything else fresh. The first pass alone filled the
    # buckets and stopped, so a digest longer than the bucket count had nothing left to add
    # and the card looked cropped with half of it empty. Extra rounds keep it balanced
    # rather than letting the newest feed take every row.
    for _round in range(max(1, digest_size)):
        added = False
        for bucket in buckets:
            if len(chosen) >= digest_size:
                break
            for article in fresh:
                if article.category != bucket or article.canonical in used:
                    continue
                if take(article):
                    added = True
                    break
        if len(chosen) >= digest_size or not added:
            break
    if len(chosen) < digest_size:
        for article in fresh:
            if len(chosen) >= digest_size:
                break
            take(article)
    return tuple(chosen[:digest_size])


def _item(article: Article, *, badge: bool = True, excerpt: bool = False) -> Item:
    """One digest row.

    The badge already names the bucket, so there is no "cat <bucket>" meta line repeating
    it, and the excerpt is shown only where there is room for it: a compact card lists
    headlines to pick from, and the article itself is one key press away.
    """
    return Item(
        id=f"news:{article.id}",
        title=article.title,
        subtitle=article.excerpt if excerpt else None,
        url=article.url,
        category=article.category,
        published_at=article.published,
        meta=(),
        privacy=Privacy.PUBLIC,
        source_id=article.source_id,
        badge=BUCKET_LABELS.get(article.category) if badge else None,
    )


def _age(article: Article, now: datetime) -> str:
    if article.published is None:
        return "undated"
    hours = (now - article.published).total_seconds() / 3600.0
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 24:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def _digest_view(
    payload: NewsPayload | None,
    settings: NewsWidgetSettings,
    context: ViewContext,
    *,
    envelope_status: StatusView | None,
) -> ViewModel:
    if payload is None:
        return ViewModel(
            title="News",
            status=envelope_status
            or StatusView(
                severity=Severity.INFO,
                headline="No news yet",
                detail="Waiting for the first successful refresh.",
            ),
        )
    if context.mode.value == "detail":
        return _detail_view(payload, settings, context, envelope_status)
    return _compact_view(payload, settings, context, envelope_status)


def _compact_view(
    payload: NewsPayload,
    settings: NewsWidgetSettings,
    context: ViewContext,
    envelope_status: StatusView | None,
) -> ViewModel:
    buckets = tuple(settings.buckets) if settings.buckets else DEFAULT_BUCKETS
    digest = build_digest(
        payload.articles,
        now=context.now,
        buckets=buckets,
        digest_size=max(3, min(settings.limit, 12)),
        max_ai=settings.max_ai_items_per_digest,
        max_age_hours=settings.max_age_hours,
    )
    if not digest:
        available = {article.category for article in payload.articles}
        missing = [BUCKET_LABELS.get(b, b) for b in buckets if b not in available]
        return ViewModel(
            title="News",
            status=StatusView(
                severity=Severity.WARNING,
                headline="Nothing fresh right now",
                detail="No enabled feed had an article inside the freshness window.",
                hints=(f"empty buckets: {', '.join(missing)}" if missing else "",),
            ),
        )
    # One count on the card. "AI 0" and "buckets 6" answered questions nobody asked at a
    # glance; both are in the detail view, where the caps and the configured buckets are
    # spelled out.
    metrics = (
        Metric(id="digest_size", label="items", value_text=str(len(digest)), basis="digest"),
    )
    return ViewModel(
        title="News",
        metrics=metrics,
        items=tuple(_item(article) for article in digest),
        # No footnote on the card: "unavailable buckets" beside six readable headlines is
        # a warning about feeds the reader is not looking at. The detail view lists the
        # feeds and what each of them returned.
        footnote=None,
    )


def _detail_view(
    payload: NewsPayload,
    settings: NewsWidgetSettings,
    context: ViewContext,
    envelope_status: StatusView | None,
) -> ViewModel:
    buckets = tuple(settings.buckets) if settings.buckets else DEFAULT_BUCKETS
    items: list[Item] = []
    for bucket in buckets:
        for article in payload.articles:
            if article.category == bucket and _fresh(article, context.now, settings.max_age_hours):
                items.append(_item(article, badge=False, excerpt=True))
    metrics = [
        Metric(
            id="articles", label="articles", value_text=str(len(payload.articles)), basis="cached"
        ),
        Metric(id="feeds", label="feeds", value_text=str(len(payload.reports)), basis="configured"),
        Metric(
            id="ai_items",
            label="AI in digest",
            value_text=str(sum(1 for article in payload.articles if article.ai_related)),
            basis=f"cap {settings.max_ai_items_per_digest}",
        ),
    ]
    feeds_failed = [report.label for report in payload.reports if report.status != "ok"]
    empty = [
        BUCKET_LABELS.get(bucket, bucket)
        for bucket in buckets
        if not any(
            article.category == bucket and _fresh(article, context.now, settings.max_age_hours)
            for article in payload.articles
        )
    ]
    footnote = "full articles open in the browser; only publisher excerpts are cached"
    if empty:
        # Stated here rather than on the card: a missing bucket is only actionable when
        # the reader can see the buckets that did arrive and the feeds behind them.
        footnote += f" · no article for: {', '.join(empty)}"
    return ViewModel(
        title="News",
        subtitle=(
            " · ".join(BUCKET_LABELS.get(bucket, bucket) for bucket in buckets)
            + (f" · failing feeds: {', '.join(feeds_failed)}" if feeds_failed else "")
        ),
        status=envelope_status,
        metrics=tuple(metrics),
        items=tuple(items[: settings.limit * 4]),
        footnote=footnote,
    )


def present_news(inputs: PresentInput) -> ViewModel:
    settings = inputs.settings
    assert isinstance(settings, NewsWidgetSettings)
    envelope = inputs.envelope("primary")
    payload = envelope.payload if envelope is not None else None
    status = status_from_envelope(
        envelope, offline=inputs.context.offline, demo=inputs.context.demo
    )
    assert payload is None or isinstance(payload, NewsPayload)
    return _digest_view(payload, settings, inputs.context, envelope_status=status)


def present_saved(inputs: PresentInput) -> ViewModel:
    """Saved reading list. Local state only: the host supplies the saved items."""
    settings = inputs.settings
    assert isinstance(settings, SavedWidgetSettings)
    saved = inputs.context.saved_items[: settings.limit]
    if not saved:
        return ViewModel(
            title="Saved",
            status=StatusView(
                severity=Severity.INFO,
                headline="Nothing saved yet",
                detail="Press s on a reading or video item to keep its link here.",
            ),
        )
    # The host hands over Item objects (converted from the local saved-link store), so
    # this presenter needs no repository access and stays pure.
    items = tuple(replace(record, saved=True) for record in saved)
    return ViewModel(
        title="Saved",
        items=items,
        footnote="saved links are kept locally; provider content is not cached indefinitely",
    )
