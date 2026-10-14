"""News collector and digest rules: dedup, diversity caps, honest empty buckets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from sideboard.core.capabilities import HttpReader, HttpScope
from sideboard.core.clock import FakeClock
from sideboard.core.contracts import Precision, ProviderBuild, ViewContext, ViewMode
from sideboard.features.news.config import (
    DigestRules,
    FeedSetting,
    NewsSettings,
    NewsWidgetSettings,
)
from sideboard.features.news.models import Article, FeedReport, NewsPayload
from sideboard.features.news.presenter import build_digest, present_news
from sideboard.features.news.provider import NewsProvider, canonical_url

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Demo Feed</title>
<item><title>Local pier opens</title><link>https://example.com/a?utm_source=x</link>
<pubDate>Mon, 13 Oct 2026 09:00:00 GMT</pubDate>
<description>&lt;p&gt;A &lt;b&gt;short&lt;/b&gt; excerpt&lt;/p&gt;</description></item>
<item><title>An LLM ships a new model</title><link>https://example.com/b</link>
<pubDate>Mon, 13 Oct 2026 08:00:00 GMT</pubDate><description>Second excerpt</description></item>
</channel></rss>
"""


def build(settings: NewsSettings) -> tuple[NewsProvider, ProviderBuild]:
    client = httpx.AsyncClient()
    http = HttpReader(client=client, scope=HttpScope.PUBLIC)
    clock = FakeClock(NOW)
    return NewsProvider(settings=settings), ProviderBuild(
        source_id="news", settings=settings, clock=clock, http=http
    )


def test_canonical_url_drops_tracking_and_fragment() -> None:
    assert (
        canonical_url("https://Example.com/a?utm_source=x&id=7#top") == "https://example.com/a?id=7"
    )


@respx.mock
async def test_provider_parses_and_sanitises_entries() -> None:
    respx.get("https://example.com/feed").mock(
        return_value=httpx.Response(200, text=RSS, headers={"etag": '"v1"'})
    )
    settings = NewsSettings(
        feeds=[FeedSetting(id="demo", url="https://example.com/feed", category="local")]
    )
    provider, context = build(settings)
    payload = await provider.fetch(context)
    assert len(payload.articles) == 2
    first = payload.articles[0]
    assert first.title == "Local pier opens"
    assert first.excerpt == "A short excerpt"  # markup stripped, no HTML fetched
    assert first.ai_related is False
    assert payload.articles[1].ai_related is True  # heuristic keyword label
    assert payload.validators == {"https://example.com/feed": '"v1"'}
    assert payload.coverage.observed == 1
    await context.http.aclose()  # type: ignore[union-attr]


@respx.mock
async def test_not_modified_reuses_the_previous_article_date() -> None:
    respx.get("https://example.com/feed").mock(return_value=httpx.Response(304))
    settings = NewsSettings(
        feeds=[FeedSetting(id="demo", url="https://example.com/feed", category="world")]
    )
    provider, context = build(settings)
    previous_article = Article(
        id="abc",
        title="Yesterday's story",
        url="https://example.com/old",
        canonical="https://example.com/old",
        source_id="demo",
        source_label="Demo",
        category="world",
        language="en",
        published=NOW - timedelta(days=1),
    )
    from sideboard.core.contracts import Connectivity, Freshness, SourceEnvelope

    previous = SourceEnvelope(
        source_id="news",
        payload_schema="news.feeds",
        payload_schema_version=1,
        payload=NewsPayload(
            articles=(previous_article,),
            reports=(FeedReport(feed_id="demo", label="Demo"),),
            validators={"https://example.com/feed": '"v1"'},
        ),
        connectivity=Connectivity.OK,
        freshness=Freshness.FRESH,
    )
    context = ProviderBuild(
        source_id="news",
        settings=settings,
        clock=FakeClock(NOW),
        http=context.http,
        previous=previous,
    )
    payload = await provider.fetch(context)
    assert payload.articles[0].published == NOW - timedelta(days=1)
    assert payload.articles[0].title == "Yesterday's story"
    await context.http.aclose()  # type: ignore[union-attr]


@respx.mock
async def test_one_failing_feed_does_not_fail_the_refresh() -> None:
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, text=RSS))
    respx.get("https://example.com/bad").mock(return_value=httpx.Response(500))
    settings = NewsSettings(
        feeds=[
            FeedSetting(id="good", url="https://example.com/good", category="world"),
            FeedSetting(id="bad", url="https://example.com/bad", category="world"),
        ]
    )
    provider, context = build(settings)
    payload = await provider.fetch(context)
    statuses = {report.feed_id: report.status for report in payload.reports}
    assert statuses["ok" if False else "good"] == "ok"
    assert statuses["bad"] in {"error", "offline"}
    assert payload.coverage.missing == ("bad",)
    await context.http.aclose()  # type: ignore[union-attr]


async def test_no_enabled_feed_is_a_clear_error() -> None:
    from sideboard.core.errors import UnavailableError

    settings = NewsSettings(feeds=[FeedSetting(id="x", url="https://example.com/x", enabled=False)])
    provider, context = build(settings)
    with pytest.raises(UnavailableError):
        await provider.fetch(context)
    await context.http.aclose()  # type: ignore[union-attr]


# --------------------------------------------------------------------------- digest


def article(
    index: int,
    *,
    category: str,
    ai: bool = False,
    age_hours: int = 1,
    source: str = "s1",
) -> Article:
    return Article(
        id=f"id{index}",
        title=f"Story {index}",
        url=f"https://example.com/{index}",
        canonical=f"https://example.com/{index}",
        source_id=source,
        source_label=source,
        category=category,
        language="en",
        published=NOW - timedelta(hours=age_hours),
        ai_related=ai,
    )


def test_digest_prefers_one_per_bucket_in_bucket_order() -> None:
    articles = (
        article(1, category="world", source="w"),
        article(2, category="local", source="l"),
        article(3, category="lithuania", source="lt"),
        article(4, category="business", source="b"),
        article(5, category="software", source="s"),
        article(6, category="gadgets", source="g"),
    )
    digest = build_digest(articles, now=NOW, digest_size=6)
    assert [item.category for item in digest] == [
        "local",
        "lithuania",
        "world",
        "business",
        "software",
        "gadgets",
    ]


def test_ai_cap_is_enforced() -> None:
    articles = (
        *(article(index, category="ai", ai=True) for index in range(6)),
        article(99, category="world"),
    )
    digest = build_digest(articles, now=NOW, digest_size=6, max_ai=2)
    assert sum(1 for item in digest if item.ai_related) <= 2
    assert any(item.category == "world" for item in digest)


def test_per_source_cap_prevents_one_feed_dominating() -> None:
    categories = ("local", "lithuania", "world", "business")
    articles = tuple(
        article(index, category=category, source="noisy")
        for index, category in enumerate(categories)
    )
    digest = build_digest(articles, now=NOW, digest_size=6, max_per_source=2)
    assert len(digest) == 2
    assert {item.source_id for item in digest} == {"noisy"}


def test_stale_articles_are_not_used_to_fill_a_bucket() -> None:
    articles = (
        article(1, category="world", age_hours=200),
        article(2, category="local"),
    )
    digest = build_digest(articles, now=NOW, digest_size=6, max_age_hours=48)
    assert [item.category for item in digest] == ["local"]


def test_digest_is_deterministic_for_shuffled_input() -> None:
    articles = (
        article(3, category="world"),
        article(1, category="world"),
        article(2, category="local"),
    )
    assert build_digest(articles, now=NOW) == build_digest(tuple(reversed(articles)), now=NOW)


def digest_view(payload: NewsPayload | None, settings: NewsWidgetSettings | None = None):
    from sideboard.core.contracts import Connectivity, PresentInput, SourceEnvelope

    envelope = None
    if payload is not None:
        envelope = SourceEnvelope(
            source_id="news",
            payload_schema="news.feeds",
            payload_schema_version=1,
            payload=payload,
            connectivity=Connectivity.OK,
            as_of=payload.as_of,
            fetched_at=NOW,
        )
    return present_news(
        PresentInput(
            settings=settings or NewsWidgetSettings(),
            inputs={"primary": envelope},
            context=ViewContext(now=NOW, width=80, height=24),
        )
    )


def test_digest_view_reports_empty_buckets_instead_of_padding() -> None:
    """The card lists headlines; the detail view names the buckets with nothing fresh.

    A compact card that footnotes "unavailable buckets: money, AI, h/w, infra" beside six
    readable headlines is a warning about feeds the reader is not looking at. The same
    fact is stated where it can be acted on: the detail view, next to the failing feeds.
    """
    from sideboard.core.contracts import Connectivity, PresentInput, SourceEnvelope

    payload = NewsPayload(
        articles=(article(1, category="world"),),
        reports=(FeedReport(feed_id="s1", label="s1"),),
        as_of=NOW,
        precision=Precision.INSTANT,
    )
    compact = digest_view(payload)
    assert compact.footnote is None
    detail = present_news(
        PresentInput(
            settings=NewsWidgetSettings(),
            inputs={
                "primary": SourceEnvelope(
                    source_id="news",
                    payload_schema="news.feeds",
                    payload_schema_version=1,
                    payload=payload,
                    connectivity=Connectivity.OK,
                    as_of=NOW,
                )
            },
            context=ViewContext(now=NOW, mode=ViewMode.DETAIL, width=120, height=40),
        )
    )
    assert "no article for" in (detail.footnote or "")
    assert "local" in (detail.footnote or "")


def test_digest_view_with_nothing_fresh_says_so() -> None:
    payload = NewsPayload(
        articles=(article(1, category="world", age_hours=500),),
        reports=(),
        as_of=NOW,
        precision=Precision.INSTANT,
    )
    view = digest_view(payload)
    assert view.status is not None
    assert view.status.headline == "Nothing fresh right now"


def test_digest_view_without_a_payload_is_a_status() -> None:
    view = digest_view(None)
    assert view.status is not None
    assert view.items == ()


def test_headline_entities_are_decoded() -> None:
    """Feeds encode quotes as entities; they are text, not markup.

    "Musk&#x27;s SpaceX" reached the screen literally, because tags were stripped and the
    remaining entities were never decoded.
    """
    from sideboard.features.news.provider import _clean

    assert _clean("Ukraine says Musk&#x27;s SpaceX limits Kyiv&#x27;s Starlink access") == (
        "Ukraine says Musk's SpaceX limits Kyiv's Starlink access"
    )
    assert _clean("M&amp;A &amp;amp; more") == "M&A &amp; more"


def test_detail_view_lists_articles_and_failing_feeds() -> None:
    from sideboard.core.contracts import Connectivity, PresentInput, SourceEnvelope

    payload = NewsPayload(
        articles=tuple(article(index, category="world") for index in range(3)),
        reports=(
            FeedReport(feed_id="good", label="Good"),
            FeedReport(feed_id="bad", label="Bad", status="error", note="timeout"),
        ),
        as_of=NOW,
        precision=Precision.INSTANT,
    )
    envelope = SourceEnvelope(
        source_id="news",
        payload_schema="news.feeds",
        payload_schema_version=1,
        payload=payload,
        connectivity=Connectivity.OK,
        as_of=NOW,
    )
    view = present_news(
        PresentInput(
            settings=NewsWidgetSettings(),
            inputs={"primary": envelope},
            context=ViewContext(now=NOW, mode=ViewMode.DETAIL, width=120, height=40),
        )
    )
    assert len(view.items) == 3
    assert "failing feeds: Bad" in (view.subtitle or "")


def test_digest_rules_defaults_are_documented_values() -> None:
    rules = DigestRules()
    assert rules.digest_size == 6
    assert rules.max_ai_items_per_digest == 2
    assert "local" in rules.bucket_order


def test_a_longer_digest_fills_from_the_same_buckets() -> None:
    """A digest of ten gets ten when ten are fresh, not six.

    The builder filled one article per bucket and then only added articles from *other*
    categories, so asking for more than the bucket count produced exactly the bucket count:
    a card with room for ten headlines showed six and looked cropped.
    """
    articles = tuple(
        article(
            index,
            category=("local", "lithuania", "europe", "world", "business", "software")[index % 6],
            source=f"feed-{index % 8}",
            age_hours=index,
        )
        for index in range(20)
    )
    digest = build_digest(articles, now=NOW, digest_size=10, max_per_source=3, max_ai=0)
    assert len(digest) == 10
    categories = [item.category for item in digest]
    assert categories[0:6] == ["local", "lithuania", "europe", "world", "business", "software"]
    # The second round repeats the buckets in order rather than draining one feed.
    assert len(set(categories[6:])) >= 2
