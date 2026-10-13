"""Deterministic, offline tests for the ranking and decay math."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sideboard.features.youtube.importers import WatchedEvent
from sideboard.features.youtube.models import (
    RANK_BASIS_FAVORITES,
    RANK_BASIS_RECENT_VIEWING,
    Upload,
)
from sideboard.features.youtube.ranking import (
    ChannelHistory,
    ChannelRank,
    decayed_weight,
    dedupe_uploads,
    rank_uploads,
    recent_view_history,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
ALPHA = "UCaaaa0000000000000000aa"
BETA = "UCbbbb0000000000000000bb"


def _upload(
    video_id: str,
    channel_id: str,
    *,
    hours_ago: float,
    favorite: bool = False,
) -> Upload:
    published = NOW - timedelta(hours=hours_ago)
    return Upload(
        video_id=video_id,
        channel_id=channel_id,
        channel_label=channel_id,
        title=video_id,
        published=published,
        url=f"https://www.youtube.com/watch?v={video_id}",
        updated=published,
        favorite=favorite,
        rank_basis=RANK_BASIS_FAVORITES,
    )


def _event(channel_id: str, *, days_ago: float) -> WatchedEvent:
    return WatchedEvent(
        title="Watched a synthetic video",
        channel_id=channel_id,
        watched_at=NOW - timedelta(days=days_ago),
    )


def test_decay_value_at_zero_and_half_life() -> None:
    assert decayed_weight(0.0, 30.0) == pytest.approx(1.0)
    assert decayed_weight(30.0, 30.0) == pytest.approx(0.5)
    assert decayed_weight(60.0, 30.0) == pytest.approx(0.25)


def test_decay_rejects_non_positive_half_life() -> None:
    with pytest.raises(ValueError):
        decayed_weight(1.0, 0.0)


def test_window_filters_old_events() -> None:
    events = [_event(ALPHA, days_ago=10), _event(ALPHA, days_ago=120)]
    history = recent_view_history(events, now=NOW, half_life_days=30.0, window_days=90)
    assert history[ALPHA].events == 1
    assert history[ALPHA].score == pytest.approx(decayed_weight(10.0, 30.0))


def test_events_without_channel_or_time_are_ignored() -> None:
    events = [
        WatchedEvent(title="no channel", channel_id=None, watched_at=NOW),
        WatchedEvent(title="no time", channel_id=ALPHA, watched_at=None),
    ]
    assert recent_view_history(events, now=NOW) == {}


def test_favorites_come_first_even_with_lower_frequency() -> None:
    uploads = [
        _upload("vid-b", BETA, hours_ago=1),
        _upload("vid-a", ALPHA, hours_ago=50),
    ]
    channels = {
        ALPHA: ChannelRank(channel_id=ALPHA, favorite=True),
        BETA: ChannelRank(channel_id=BETA),
    }
    history = {BETA: ChannelHistory(score=9.0, events=9)}
    ranked = rank_uploads(uploads, channels, history, NOW, RANK_BASIS_RECENT_VIEWING)
    assert [upload.video_id for upload in ranked] == ["vid-a", "vid-b"]
    assert ranked[0].favorite is True
    assert ranked[0].recent_view_score is None
    assert ranked[0].rank_basis == RANK_BASIS_RECENT_VIEWING
    assert ranked[1].recent_view_score == pytest.approx(9.0)
    assert ranked[1].view_events == 9


def test_frequency_ordering_beats_recency() -> None:
    uploads = [
        _upload("vid-new", ALPHA, hours_ago=1),
        _upload("vid-old", BETA, hours_ago=200),
    ]
    channels = {ALPHA: ChannelRank(channel_id=ALPHA), BETA: ChannelRank(channel_id=BETA)}
    history = {
        ALPHA: ChannelHistory(score=1.0, events=1),
        BETA: ChannelHistory(score=3.0, events=3),
    }
    ranked = rank_uploads(uploads, channels, history, NOW, RANK_BASIS_RECENT_VIEWING)
    assert [upload.video_id for upload in ranked] == ["vid-old", "vid-new"]


def test_ties_break_by_recency_then_video_id() -> None:
    uploads = [
        _upload("vid-old", ALPHA, hours_ago=100),
        _upload("vid-new", ALPHA, hours_ago=2),
    ]
    channels = {ALPHA: ChannelRank(channel_id=ALPHA)}
    history = {ALPHA: ChannelHistory(score=2.0, events=2)}
    ranked = rank_uploads(uploads, channels, history, NOW, RANK_BASIS_RECENT_VIEWING)
    assert [upload.video_id for upload in ranked] == ["vid-new", "vid-old"]

    same_instant = [
        _upload("vid-b", ALPHA, hours_ago=5),
        _upload("vid-a", ALPHA, hours_ago=5),
    ]
    ranked_same = rank_uploads(same_instant, channels, {}, NOW, RANK_BASIS_FAVORITES)
    assert [upload.video_id for upload in ranked_same] == ["vid-a", "vid-b"]


def test_ranking_is_deterministic_for_shuffled_input() -> None:
    uploads = [
        _upload("vid-1", ALPHA, hours_ago=5),
        _upload("vid-2", BETA, hours_ago=50),
        _upload("vid-3", ALPHA, hours_ago=2),
    ]
    channels = {
        ALPHA: ChannelRank(channel_id=ALPHA),
        BETA: ChannelRank(channel_id=BETA, favorite=True),
    }
    history = {
        ALPHA: ChannelHistory(score=1.5, events=2),
        BETA: ChannelHistory(score=2.5, events=3),
    }
    first = rank_uploads(uploads, channels, history, NOW, RANK_BASIS_RECENT_VIEWING)
    second = rank_uploads(
        list(reversed(uploads)), channels, history, NOW, RANK_BASIS_RECENT_VIEWING
    )
    assert [upload.video_id for upload in first] == [upload.video_id for upload in second]
    assert [upload.video_id for upload in first] == ["vid-2", "vid-3", "vid-1"]


def test_dedupe_uploads_keeps_first_and_ors_favorite() -> None:
    first = _upload("vid-shared", ALPHA, hours_ago=5)
    second = _upload("vid-shared", BETA, hours_ago=4, favorite=True)
    unique = dedupe_uploads([first, second])
    assert len(unique) == 1
    assert unique[0].channel_id == ALPHA
    assert unique[0].favorite is True
