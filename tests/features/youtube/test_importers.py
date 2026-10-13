"""Offline tests for the local Google Takeout importers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sideboard.core.errors import ParseError
from sideboard.features.youtube.importers import (
    parse_subscriptions_csv,
    parse_watch_history,
    parse_watch_history_with_stats,
    subscriptions_report,
    video_id_from_url,
    watch_history_report,
)

FIXTURES = Path(__file__).parent / "fixtures"

ALPHA = "UCaaaa0000000000000000aa"
BETA = "UCbbbb0000000000000000bb"
GAMMA = "UCcccc0000000000000000cc"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_subscriptions_variant_a() -> None:
    channels, skipped, warnings = parse_subscriptions_csv(_read("subscriptions_variant_a.csv"))
    assert [channel.channel_id for channel in channels] == [ALPHA, BETA, GAMMA]
    assert channels[1].title == "Beta, Synthetic Channel"
    assert channels[0].url == f"https://www.youtube.com/channel/{ALPHA}"
    assert skipped == 0
    assert warnings == ()


def test_subscriptions_variant_b_same_channels() -> None:
    variant_a, skipped_a, _ = parse_subscriptions_csv(_read("subscriptions_variant_a.csv"))
    variant_b, skipped_b, warnings_b = parse_subscriptions_csv(_read("subscriptions_variant_b.csv"))
    assert {channel.channel_id for channel in variant_a} == {
        channel.channel_id for channel in variant_b
    }
    assert variant_b[1].title == "Beta, Synthetic Channel"
    assert skipped_a == 0
    assert skipped_b == 0
    assert warnings_b == ()


def test_bom_and_semicolon_inline() -> None:
    text = "\ufeffChannel Title;Channel ID\nBom Semicolon Channel;UCzzzz0000000000000000zz\n"
    channels, skipped, warnings = parse_subscriptions_csv(text)
    assert [channel.channel_id for channel in channels] == ["UCzzzz0000000000000000zz"]
    assert skipped == 0
    assert warnings == ()


def test_malformed_rows_are_counted_as_skipped() -> None:
    text = (
        "Channel Id,Channel Url,Channel Title\n"
        ",https://example.invalid/x,Missing Id Row\n"
        "UCdddd0000000000000000dd,https://example.invalid/y,Kept Row\n"
        "UCdddd0000000000000000dd,https://example.invalid/y,Duplicate Row\n"
    )
    channels, skipped, warnings = parse_subscriptions_csv(text)
    assert skipped == 1
    assert [channel.channel_id for channel in channels] == ["UCdddd0000000000000000dd"]
    assert any("no channel id" in warning for warning in warnings)
    assert any("duplicate" in warning for warning in warnings)


def test_subscriptions_missing_id_column_reports_rows() -> None:
    text = "Something,Else\none,two\n"
    channels, skipped, warnings = parse_subscriptions_csv(text)
    assert channels == ()
    assert skipped == 1
    assert warnings


def test_watch_history_skips_removed_videos() -> None:
    events = parse_watch_history(_read("watch-history.json"))
    assert [event.video_id for event in events] == ["vid-alpha-1", "vid-beta-1", "vid-noc-1"]
    assert events[0].channel_id == ALPHA
    assert events[0].channel_name == "Alpha Synthetic Channel"
    assert events[0].watched_at == datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    assert events[1].channel_id == BETA
    assert events[2].channel_id is None


def test_watch_history_stats_and_report() -> None:
    events, skipped, warnings = parse_watch_history_with_stats(_read("watch-history.json"))
    assert skipped == 1
    assert warnings
    report = watch_history_report(events, skipped, warnings)
    assert report.kind == "watch-history"
    assert report.records == 4
    assert report.accepted == 3
    assert report.skipped == 1
    assert report.unresolved_channels == 1


def test_subscriptions_report() -> None:
    channels, skipped, warnings = parse_subscriptions_csv(_read("subscriptions_variant_a.csv"))
    report = subscriptions_report(channels, skipped, warnings)
    assert report.kind == "subscriptions"
    assert report.records == 3
    assert report.accepted == 3
    assert report.skipped == 0


def test_malformed_watch_history_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_watch_history(_read("malformed_watch_history.json"))


def test_oversized_watch_history_raises_parse_error() -> None:
    with pytest.raises(ParseError, match="cap"):
        parse_watch_history("[{}]", max_chars=3)


def test_imports_are_idempotent() -> None:
    history = _read("watch-history.json")
    subscriptions = _read("subscriptions_variant_a.csv")
    assert parse_watch_history(history) == parse_watch_history(history)
    assert parse_subscriptions_csv(subscriptions) == parse_subscriptions_csv(subscriptions)


def test_video_id_from_url() -> None:
    assert video_id_from_url("https://www.youtube.com/watch?v=vid-alpha-1") == "vid-alpha-1"
    assert video_id_from_url("https://youtu.be/vid-alpha-1") == "vid-alpha-1"
    assert video_id_from_url("https://www.youtube.com/shorts/vid-alpha-1") == "vid-alpha-1"
    assert video_id_from_url(None) is None
    assert video_id_from_url("https://www.youtube.com/feed/subscriptions") is None
