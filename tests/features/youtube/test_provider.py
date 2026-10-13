"""Offline provider tests: synthetic feeds, typed failures, bounded selection.

No live network: ``respx`` intercepts the documented Atom endpoint and every file
is a synthetic fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from sideboard.core.capabilities import FileReader, HttpReader, HttpScope
from sideboard.core.clock import FakeClock
from sideboard.core.contracts import ProviderBuild
from sideboard.core.errors import TransientNetworkError
from sideboard.features.youtube.config import ChannelSetting, YouTubeSettings
from sideboard.features.youtube.models import (
    RANK_BASIS_FAVORITES,
    RANK_BASIS_RECENT_VIEWING,
    YouTubePayload,
)
from sideboard.features.youtube.provider import YouTubeProvider, demo_payload

FIXTURES = Path(__file__).parent / "fixtures"
FEED_ENDPOINT = "https://www.youtube.com/feeds/videos.xml"

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
ALPHA = "UCaaaa0000000000000000aa"
BETA = "UCbbbb0000000000000000bb"
GAMMA = "UCcccc0000000000000000cc"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _feed(channel_id: str, title: str, videos: list[tuple[str, str]]) -> str:
    entries = "\n".join(
        f"""  <entry>
    <id>yt:video:{video_id}</id>
    <yt:videoId>{video_id}</yt:videoId>
    <yt:channelId>{channel_id}</yt:channelId>
    <title>{title} {video_id}</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v={video_id}"/>
    <published>{published}</published>
    <updated>{published}</updated>
  </entry>"""
        for video_id, published in videos
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <title>{title}</title>
  <yt:channelId>{channel_id}</yt:channelId>
{entries}
</feed>"""


def _context(settings: YouTubeSettings, client: httpx.AsyncClient) -> ProviderBuild:
    return ProviderBuild(
        source_id="youtube_test",
        settings=settings,
        clock=FakeClock(NOW),
        http=HttpReader(client=client, scope=HttpScope.PUBLIC),
        files=FileReader(allowed_roots=(str(FIXTURES),)),
    )


def _route(router: respx.Router, channel_id: str, text: str) -> None:
    router.get(FEED_ENDPOINT, params={"channel_id": channel_id}).mock(
        return_value=httpx.Response(200, text=text)
    )


async def test_feed_uploads_parsed_and_validated() -> None:
    settings = YouTubeSettings(channels=[ChannelSetting(id=ALPHA)])
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            _route(router, ALPHA, _read("channel_feed.xml"))
            payload = await YouTubeProvider(settings).fetch(_context(settings, client))
    assert isinstance(payload, YouTubePayload)
    assert [upload.video_id for upload in payload.uploads] == ["vid-alpha-1", "vid-alpha-2"]
    first = payload.uploads[0]
    assert first.channel_id == ALPHA
    assert first.channel_label == "Alpha Synthetic Channel"
    assert first.url == "https://www.youtube.com/watch?v=vid-alpha-1"
    assert first.published == datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    assert payload.rank_basis == RANK_BASIS_FAVORITES
    assert first.recent_view_score is None
    assert payload.as_of is not None
    assert payload.as_of == datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
    assert payload.valid_until == payload.as_of + timedelta(minutes=30)
    assert payload.provenance == "youtube-channel-atom"
    report = payload.channel_reports[0]
    assert report.status == "ok"
    assert report.item_count == 2


async def test_handle_is_unresolved_without_scraping() -> None:
    settings = YouTubeSettings(
        channels=[
            ChannelSetting(id="@somehandle", label="Handle Setup"),
            ChannelSetting(id=ALPHA),
        ]
    )
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            _route(router, ALPHA, _read("channel_feed.xml"))
            payload = await YouTubeProvider(settings).fetch(_context(settings, client))
            assert len(router.calls) == 1
    unresolved = [report for report in payload.channel_reports if report.status == "unresolved"]
    assert len(unresolved) == 1
    assert unresolved[0].label == "Handle Setup"
    assert "channel ID" in (unresolved[0].note or "")
    assert payload.uploads


async def test_one_channel_failure_keeps_payload() -> None:
    settings = YouTubeSettings(channels=[ChannelSetting(id=ALPHA), ChannelSetting(id=BETA)])
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            _route(router, ALPHA, _read("channel_feed.xml"))
            router.get(FEED_ENDPOINT, params={"channel_id": BETA}).mock(
                return_value=httpx.Response(500)
            )
            payload = await YouTubeProvider(settings).fetch(_context(settings, client))
    statuses = {report.channel_id: report.status for report in payload.channel_reports}
    assert statuses[ALPHA] == "ok"
    assert statuses[BETA] == "error"
    assert payload.uploads
    assert payload.coverage.is_partial
    assert payload.coverage.missing


async def test_all_channels_failing_raises_typed_error() -> None:
    settings = YouTubeSettings(channels=[ChannelSetting(id=ALPHA), ChannelSetting(id=BETA)])
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            for channel_id in (ALPHA, BETA):
                router.get(FEED_ENDPOINT, params={"channel_id": channel_id}).mock(
                    return_value=httpx.Response(503)
                )
            with pytest.raises(TransientNetworkError):
                await YouTubeProvider(settings).fetch(_context(settings, client))


async def test_active_channel_limit_fetches_favorites_first() -> None:
    settings = YouTubeSettings(
        channels=[
            ChannelSetting(id=ALPHA),
            ChannelSetting(id=BETA),
            ChannelSetting(id=GAMMA),
        ],
        favorite_channel_ids=[GAMMA],
        active_channel_limit=2,
    )
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            for channel_id, name in ((ALPHA, "Alpha"), (BETA, "Beta"), (GAMMA, "Gamma")):
                _route(
                    router,
                    channel_id,
                    _feed(channel_id, name, [(f"vid-{name.lower()}", "2026-10-09T09:00:00+00:00")]),
                )
            payload = await YouTubeProvider(settings).fetch(_context(settings, client))
            assert len(router.calls) == 2
    ok = [report.channel_id for report in payload.channel_reports if report.status == "ok"]
    not_fetched = [
        report.channel_id for report in payload.channel_reports if report.status == "not-fetched"
    ]
    assert ok == [GAMMA, ALPHA]
    assert not_fetched == [BETA]
    assert payload.coverage.expected == 3
    assert payload.coverage.observed == 2


async def test_video_dedupe_across_channels() -> None:
    settings = YouTubeSettings(channels=[ChannelSetting(id=ALPHA), ChannelSetting(id=BETA)])
    shared = "vid-shared1"
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            _route(
                router,
                ALPHA,
                _feed(
                    ALPHA,
                    "Alpha",
                    [
                        (shared, "2026-10-10T09:00:00+00:00"),
                        ("vid-alpha2", "2026-10-06T09:00:00+00:00"),
                    ],
                ),
            )
            _route(router, BETA, _feed(BETA, "Beta", [(shared, "2026-10-10T09:00:00+00:00")]))
            payload = await YouTubeProvider(settings).fetch(_context(settings, client))
    video_ids = [upload.video_id for upload in payload.uploads]
    assert video_ids.count(shared) == 1
    assert sorted(video_ids) == ["vid-alpha2", "vid-shared1"]


async def test_history_import_orders_by_recent_viewing() -> None:
    settings = YouTubeSettings(
        channels=[ChannelSetting(id=ALPHA), ChannelSetting(id=BETA)],
        takeout_watch_history_json=str(FIXTURES / "watch-history.json"),
    )
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            _route(
                router,
                ALPHA,
                _feed(ALPHA, "Alpha", [("vid-alpha-1", "2026-10-09T09:00:00+00:00")]),
            )
            _route(router, BETA, _feed(BETA, "Beta", [("vid-beta-1", "2026-09-19T09:00:00+00:00")]))
            payload = await YouTubeProvider(settings).fetch(_context(settings, client))
    assert payload.rank_basis == RANK_BASIS_RECENT_VIEWING
    assert payload.history_imported_at is not None
    assert [upload.video_id for upload in payload.uploads] == ["vid-alpha-1", "vid-beta-1"]
    alpha_score = payload.uploads[0].recent_view_score
    beta_score = payload.uploads[1].recent_view_score
    assert alpha_score is not None
    assert beta_score is not None
    assert alpha_score > beta_score
    assert payload.uploads[0].view_events == 1


async def test_missing_takeout_file_is_reported_not_raised() -> None:
    settings = YouTubeSettings(
        channels=[ChannelSetting(id=ALPHA)],
        takeout_watch_history_json=str(FIXTURES / "does_not_exist.json"),
    )
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            _route(router, ALPHA, _read("channel_feed.xml"))
            payload = await YouTubeProvider(settings).fetch(_context(settings, client))
    file_errors = [
        report
        for report in payload.channel_reports
        if report.status == "error" and report.channel_id is None
    ]
    assert len(file_errors) == 1
    assert "does not exist" in (file_errors[0].note or "")
    assert payload.rank_basis == RANK_BASIS_FAVORITES
    assert payload.coverage.missing
    assert payload.uploads


async def test_subscriptions_csv_seeds_channel_pool() -> None:
    settings = YouTubeSettings(
        takeout_subscriptions_csv=str(FIXTURES / "subscriptions_variant_a.csv")
    )
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            for channel_id, name in ((ALPHA, "Alpha"), (BETA, "Beta"), (GAMMA, "Gamma")):
                _route(
                    router,
                    channel_id,
                    _feed(channel_id, name, [(f"vid-{name.lower()}", "2026-10-09T09:00:00+00:00")]),
                )
            payload = await YouTubeProvider(settings).fetch(_context(settings, client))
    assert payload.coverage.expected == 3
    assert payload.coverage.observed == 3
    assert payload.subscriptions_imported_at is not None
    assert {upload.channel_id for upload in payload.uploads} == {ALPHA, BETA, GAMMA}


async def test_imported_channel_ids_extend_the_pool() -> None:
    settings = YouTubeSettings(
        channels=[ChannelSetting(id=ALPHA)],
        imported_channel_ids=[GAMMA],
    )
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            _route(
                router,
                ALPHA,
                _feed(ALPHA, "Alpha", [("vid-alpha1", "2026-10-09T09:00:00+00:00")]),
            )
            _route(
                router,
                GAMMA,
                _feed(GAMMA, "Gamma", [("vid-gamma1", "2026-10-08T09:00:00+00:00")]),
            )
            payload = await YouTubeProvider(settings).fetch(_context(settings, client))
    assert payload.coverage.expected == 2
    assert payload.coverage.observed == 2


async def test_provider_aclose_is_safe_twice() -> None:
    provider = YouTubeProvider(YouTubeSettings())
    await provider.aclose()
    await provider.aclose()


def test_demo_payload_is_synthetic() -> None:
    payload = demo_payload()
    assert isinstance(payload, YouTubePayload)
    assert payload.uploads
    assert payload.rank_basis == RANK_BASIS_RECENT_VIEWING
    assert all(upload.recent_view_score is not None for upload in payload.uploads)
