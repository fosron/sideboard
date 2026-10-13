"""Offline presenter tests for the Hacker News list widget."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from sideboard.core.contracts import (
    Connectivity,
    Coverage,
    Freshness,
    Precision,
    PresentInput,
    SourceEnvelope,
    SourcePayload,
    ViewContext,
    ViewMode,
    ViewModel,
)
from sideboard.features.hackernews.config import HackerNewsWidgetSettings
from sideboard.features.hackernews.feature import FEATURE
from sideboard.features.hackernews.models import HackerNewsPayload, Story
from sideboard.features.hackernews.presenter import (
    RELEVANCE_NOTE,
    WIDGET,
    age_text,
    present,
    relevance_rank,
)
from sideboard.ui.text import single_line

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _story(
    story_id: int,
    list_key: str = "top",
    title: str | None = None,
    *,
    score: int | None = None,
    descendants: int | None = None,
    age_hours: float | None = 3.0,
    url: str | None = None,
) -> Story:
    published = None if age_hours is None else NOW - timedelta(hours=age_hours)
    return Story(
        id=story_id,
        list=list_key,
        title=title or f"Synthetic story {story_id}",
        by="tester",
        url=url,
        score=score,
        descendants=descendants,
        time=published,
        type="story",
    )


def _payload(stories: tuple[Story, ...], *, expected: int | None = None) -> HackerNewsPayload:
    times = [story.time for story in stories if story.time is not None]
    as_of = max(times) if times else None
    return HackerNewsPayload(
        stories=stories,
        as_of=as_of,
        precision=Precision.INSTANT,
        valid_until=as_of + timedelta(minutes=5) if as_of is not None else None,
        provenance="hacker-news-official-api",
        coverage=Coverage(
            expected=len(stories) if expected is None else expected,
            observed=len(stories),
        ),
    )


def _envelope(payload: SourcePayload | None) -> SourceEnvelope:
    return SourceEnvelope(
        source_id="hn",
        payload_schema="hackernews.stories",
        payload_schema_version=1,
        generation=1,
        payload=payload,
        connectivity=Connectivity.OK,
        freshness=Freshness.FRESH,
        last_attempt_at=NOW,
        last_success_at=NOW if payload is not None else None,
        fetched_at=NOW,
        as_of=payload.as_of if payload is not None else None,
        valid_until=payload.valid_until if payload is not None else None,
        provenance=payload.provenance if payload is not None else "",
        coverage=payload.coverage if payload is not None else Coverage(),
    )


def _context(mode: ViewMode = ViewMode.COMPACT) -> ViewContext:
    return ViewContext(mode=mode, width=80, height=24, now=NOW)


def _present(
    payload: SourcePayload | None,
    *,
    settings: HackerNewsWidgetSettings | None = None,
    mode: ViewMode = ViewMode.COMPACT,
) -> ViewModel:
    return present(
        PresentInput(
            settings=settings if settings is not None else HackerNewsWidgetSettings(),
            inputs={"primary": _envelope(payload)},
            context=_context(mode),
        )
    )


def test_item_ids_are_stable_and_url_falls_back_to_the_discussion() -> None:
    payload = _payload((_story(101, url="https://example.com/one"), _story(102)))
    view = _present(payload)

    assert [item.id for item in view.items] == ["hn:101", "hn:102"]
    assert view.items[0].url == "https://example.com/one"
    assert view.items[1].url == "https://news.ycombinator.com/item?id=102"
    assert view.items[0].published_at == NOW - timedelta(hours=3)


def test_list_setting_filters_without_reordering() -> None:
    payload = _payload(
        (_story(101, "top"), _story(201, "new"), _story(102, "top"), _story(202, "new"))
    )
    view = _present(payload, settings=HackerNewsWidgetSettings(list="new"))

    assert [item.id for item in view.items] == ["hn:201", "hn:202"]
    assert view.subtitle is None


def test_relevance_filter_differs_from_raw_order_and_declares_it() -> None:
    stories = (
        _story(1, score=2, age_hours=30.0),
        _story(2, score=3, age_hours=20.0),
        _story(3, score=90, age_hours=5.0),
        _story(4, score=100, age_hours=10.0),
    )
    payload = _payload(stories)
    raw = _present(payload, settings=HackerNewsWidgetSettings(limit=4))
    filtered = _present(payload, settings=HackerNewsWidgetSettings(limit=4, relevance_filter=True))

    assert [item.id for item in raw.items] == ["hn:1", "hn:2", "hn:3", "hn:4"]
    assert raw.subtitle is None
    assert filtered.subtitle == RELEVANCE_NOTE
    assert [item.id for item in filtered.items] == ["hn:3", "hn:4"]
    assert {item.id for item in filtered.items} != {item.id for item in raw.items}
    assert relevance_rank(list(stories), NOW) == relevance_rank(list(stories), NOW)


def test_relevance_rank_without_score_signal_keeps_api_order() -> None:
    stories = [_story(1), _story(2)]
    assert relevance_rank(stories, NOW) == stories
    view = _present(
        _payload(tuple(stories)), settings=HackerNewsWidgetSettings(relevance_filter=True)
    )
    assert view.subtitle == RELEVANCE_NOTE
    assert [item.id for item in view.items] == ["hn:1", "hn:2"]
    assert view.footnote is not None and "API order" in view.footnote


def test_missing_score_and_descendants_are_omitted_not_zeroed() -> None:
    payload = _payload(
        (_story(101, score=None, descendants=None), _story(102, score=0, descendants=0))
    )
    view = _present(payload)

    assert dict(view.items[0].meta) == {"age": "3h"}
    assert dict(view.items[1].meta) == {"pts": "0", "c": "0", "age": "3h"}


def test_show_score_and_show_age_toggles() -> None:
    payload = _payload((_story(101, score=12, descendants=4, age_hours=30.0),))
    hidden = _present(payload, settings=HackerNewsWidgetSettings(show_score=False, show_age=False))
    assert dict(hidden.items[0].meta) == {"c": "4"}

    shown = _present(payload)
    assert dict(shown.items[0].meta) == {"pts": "12", "c": "4", "age": "1d"}


def test_age_text_derives_short_labels() -> None:
    assert age_text(None, NOW) is None
    assert age_text(NOW - timedelta(seconds=20), NOW) == "now"
    assert age_text(NOW - timedelta(minutes=5), NOW) == "5m"
    assert age_text(NOW - timedelta(hours=3), NOW) == "3h"
    assert age_text(NOW - timedelta(days=2), NOW) == "2d"
    assert age_text(NOW + timedelta(hours=1), NOW) == "now"


def test_story_without_time_has_no_age_and_no_published_at() -> None:
    payload = _payload((_story(101, score=5, age_hours=None),))
    view = _present(payload)

    assert view.items[0].published_at is None
    assert dict(view.items[0].meta) == {"pts": "5"}


def test_long_title_is_kept_raw_and_the_renderer_sanitises_controls() -> None:
    title = (
        "Synthetic headline with \x07bell, \x1b[31mansi\x1b[0m and \u202eredrum override "
        + "x" * 400
    )
    payload = _payload((_story(101, title=title),))
    view = _present(payload)

    stored = view.items[0].title
    assert len(stored) > 300
    assert stored == title
    rendered = single_line(stored, 80)
    assert len(rendered) <= 80
    assert "\x07" not in rendered
    assert "\x1b" not in rendered
    assert "\u202e" not in rendered


def test_missing_payload_and_unconfigured_source_return_status() -> None:
    view = _present(None)
    assert view.items == ()
    assert view.status is not None

    unbound = present(
        PresentInput(
            settings=HackerNewsWidgetSettings(),
            inputs={},
            context=_context(),
        )
    )
    assert unbound.items == ()
    assert unbound.status is not None
    assert unbound.status.headline == "Source not configured"

    unbound_none = present(
        PresentInput(
            settings=HackerNewsWidgetSettings(),
            inputs={"primary": None},
            context=_context(),
        )
    )
    assert unbound_none.status is not None


def test_filter_with_no_matching_stories_reports_status() -> None:
    view = _present(_payload((_story(101, "top"),)), settings=HackerNewsWidgetSettings(list="show"))
    assert view.items == ()
    assert view.status is not None
    assert view.status.headline == "No stories"


def test_detail_mode_shows_more_rows() -> None:
    payload = _payload(tuple(_story(100 + index, score=index + 1) for index in range(12)))
    compact = _present(payload)
    detail = _present(payload, mode=ViewMode.DETAIL)

    assert len(compact.items) == 8
    assert len(detail.items) == 12


def test_partial_coverage_is_reported_in_the_footnote() -> None:
    payload = _payload((_story(101, score=1),), expected=60)
    view = _present(payload)

    assert payload.coverage.is_partial
    assert view.footnote is not None
    assert "60" in view.footnote


def test_widget_spec_matches_the_contract() -> None:
    assert WIDGET.type_id == "hackernews.list"
    assert WIDGET.default_category == "software"
    assert WIDGET.min_width == 26
    assert WIDGET.min_height == 4
    binding = WIDGET.binding("primary")
    assert binding is not None
    assert binding.accepts == frozenset({"hackernews.stories"})
    settings = WIDGET.validate_settings({"list": "new", "limit": 3})
    assert isinstance(settings, HackerNewsWidgetSettings)
    assert settings.list == "new"


def test_feature_spec_registers_source_and_widget() -> None:
    assert FEATURE.id == "hackernews"
    assert [spec.type_id for spec in FEATURE.sources] == ["hackernews.stories"]
    assert [spec.type_id for spec in FEATURE.widgets] == ["hackernews.list"]
    source = FEATURE.source("hackernews.stories")
    assert source is not None
    assert source.payload_schema == "hackernews.stories"
    assert source.payload_schema_version == 1
    assert source.requires_setup is False
    assert source.cadence.active_seconds == 300.0
    assert source.cadence.background_seconds == 900.0


def test_widget_settings_reject_unknown_lists() -> None:
    with pytest.raises(ValidationError, match="unknown Hacker News list"):
        HackerNewsWidgetSettings(list="front")
