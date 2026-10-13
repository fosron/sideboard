"""Explicit live smoke probes.

These are the only tests that touch the public internet. They are deselected by
default (``addopts = "-m 'not live'"``) and must be requested:

.. code-block:: sh

    uv run pytest -m live -q

A failure here is a fact about this network and this moment. It is not proof that a
provider is down, and it is not evidence that the provider's terms permit automated
access. Nothing here writes to any account and nothing is mutated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from sideboard.core.capabilities import HttpReader, HttpScope
from sideboard.core.clock import SystemClock
from sideboard.core.contracts import ProviderBuild
from sideboard.features.hackernews.config import HackerNewsSettings
from sideboard.features.hackernews.provider import HackerNewsProvider
from sideboard.features.news.config import FeedSetting, NewsSettings
from sideboard.features.news.provider import NewsProvider
from sideboard.features.weather.config import WeatherSettings
from sideboard.features.weather.provider import WeatherProvider

pytestmark = pytest.mark.live

KLAIPEDA = {"latitude": 55.7033, "longitude": 21.1443, "timezone": "Europe/Vilnius"}


@pytest.fixture
async def http() -> AsyncIterator[HttpReader]:
    reader = HttpReader(
        client=httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
        ),
        scope=HttpScope.PUBLIC,
    )
    try:
        yield reader
    finally:
        await reader.aclose()


async def test_open_meteo_forecast(http: HttpReader) -> None:
    settings = WeatherSettings(label="Klaipėda", **KLAIPEDA)
    provider = WeatherProvider()
    payload = await provider.fetch(
        ProviderBuild(source_id="weather", settings=settings, clock=SystemClock(), http=http)
    )
    assert payload.location_label
    assert payload.daily, "the daily forecast should not be empty"
    assert payload.hourly, "the hourly forecast should not be empty"
    assert payload.as_of is not None


async def test_hacker_news_top(http: HttpReader) -> None:
    settings = HackerNewsSettings(lists=["top"], items_per_list=5)
    provider = HackerNewsProvider(settings)
    payload = await provider.fetch(
        ProviderBuild(source_id="hn", settings=settings, clock=SystemClock(), http=http)
    )
    assert len(payload.stories) >= 1
    first = payload.stories[0]
    assert first.title
    assert first.id > 0


async def test_publisher_feeds_parse(http: HttpReader) -> None:
    feeds = [
        FeedSetting(
            id="bbc-world", url="https://feeds.bbci.co.uk/news/world/rss.xml", category="world"
        ),
        FeedSetting(id="dw", url="https://rss.dw.com/rdf/rss-en-all", category="europe"),
        FeedSetting(
            id="atviraklaipeda",
            url="https://www.atviraklaipeda.lt/feed/",
            category="local",
            language="lt",
        ),
        FeedSetting(id="lrt", url="https://www.lrt.lt/?rss", category="lithuania", language="lt"),
    ]
    settings = NewsSettings(feeds=feeds, max_items_per_feed=5)
    provider = NewsProvider(settings=settings)
    payload = await provider.fetch(
        ProviderBuild(source_id="news", settings=settings, clock=SystemClock(), http=http)
    )
    ok = {report.feed_id for report in payload.reports if report.status == "ok"}
    assert {"bbc-world", "dw"} <= ok, f"expected the world feeds to parse, got {ok}"
    assert payload.articles
    # A published date that is in the future would be a parser bug, not provider data.
    horizon = datetime.now(UTC) + timedelta(hours=6)
    assert all(
        article.published is None or article.published <= horizon for article in payload.articles
    )


async def test_youtube_channel_feed_parses(http: HttpReader) -> None:
    response = await http.get(
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCsBjURrPoezykLs9EqgamOA"
    )
    assert response.status == 200
    assert b"<feed" in response.content[:200]
