"""Provider tests for the Reddit feature.

Every test runs against synthetic fixtures. Nothing here touches the public network:
``respx`` intercepts the host's bounded HTTP reader and the tests assert that link-only
mode, disabled mode and offline mode issue no request at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import ValidationError

from sideboard.core.capabilities import HttpReader
from sideboard.core.clock import FakeClock
from sideboard.core.contracts import Cadence, Capability, ProviderBuild
from sideboard.core.errors import (
    ParseError,
    RateLimitedError,
    TransientNetworkError,
    UnauthorizedError,
    UnavailableError,
)
from sideboard.features.reddit.config import RedditSettings
from sideboard.features.reddit.feature import FEATURE, REDDIT_SOURCE, REDDIT_WIDGET
from sideboard.features.reddit.models import REDDIT_PAYLOAD_SCHEMA, RedditPayload
from sideboard.features.reddit.provider import (
    PROVENANCE_DEMO,
    PROVENANCE_RSS,
    RedditProvider,
    demo_payload,
    feed_url,
)

FIXTURES = Path(__file__).parent / "fixtures"

CLOCK_START = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

SELFHOSTED_URL = feed_url("selfhosted")

HOMELAB_URL = feed_url("homelab")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=CLOCK_START)


@pytest.fixture
async def reader() -> AsyncIterator[HttpReader]:
    instance = HttpReader(client=httpx.AsyncClient())
    try:
        yield instance
    finally:
        await instance.aclose()


def feed_response(name: str) -> httpx.Response:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return httpx.Response(
        200, text=text, headers={"content-type": "application/atom+xml; charset=UTF-8"}
    )


def html_response() -> httpx.Response:
    return httpx.Response(
        200,
        text=(FIXTURES / "html_block_page.txt").read_text(encoding="utf-8"),
        headers={"content-type": "text/html"},
    )


def provider_for(settings: RedditSettings, clock: FakeClock) -> RedditProvider:
    built = REDDIT_SOURCE.factory(
        ProviderBuild(source_id="reddit_home", settings=settings, clock=clock)
    )
    assert isinstance(built, RedditProvider)
    return built


def build_context(
    settings: RedditSettings,
    clock: FakeClock,
    *,
    http: HttpReader | None = None,
    offline: bool = False,
) -> ProviderBuild:
    return ProviderBuild(
        source_id="reddit_home",
        settings=settings,
        clock=clock,
        http=http,
        offline=offline,
    )


async def collect(
    settings: RedditSettings, clock: FakeClock, reader: HttpReader, *, offline: bool = False
) -> RedditPayload:
    provider = provider_for(settings, clock)
    payload = await provider.fetch(build_context(settings, clock, http=reader, offline=offline))
    assert isinstance(payload, RedditPayload)
    return payload


# --------------------------------------------------------------------------- #
# Modes that must not touch the network
# --------------------------------------------------------------------------- #


async def test_links_mode_reports_the_mode_and_never_touches_http(
    respx_mock: respx.MockRouter, clock: FakeClock, reader: HttpReader
) -> None:
    route = respx_mock.route(host="www.reddit.com").mock(
        return_value=feed_response("selfhosted_top_day.xml")
    )
    settings = RedditSettings(mode="links")
    provider = provider_for(settings, clock)
    payload = await provider.fetch(build_context(settings, clock, http=reader))
    # A configured mode is a success with no request, not a failure. Raising here used
    # to log "blocked" on every refresh and back the source off for an hour, which is a
    # false report about a mode the user chose.
    assert isinstance(payload, RedditPayload)
    assert payload.mode == "links"
    assert payload.threads == ()
    assert payload.provenance == "reddit-links"
    assert "link-only" in (payload.coverage.notes or "")
    assert len(respx_mock.calls) == 0
    assert route.call_count == 0


async def test_disabled_mode_reports_the_mode_and_never_touches_http(
    respx_mock: respx.MockRouter, clock: FakeClock, reader: HttpReader
) -> None:
    respx_mock.route(host="www.reddit.com").mock(
        return_value=feed_response("selfhosted_top_day.xml")
    )
    settings = RedditSettings(mode="disabled")
    provider = provider_for(settings, clock)
    payload = await provider.fetch(build_context(settings, clock, http=reader))
    assert isinstance(payload, RedditPayload)
    assert payload.mode == "disabled"
    assert len(respx_mock.calls) == 0


async def test_offline_mode_never_touches_http(
    respx_mock: respx.MockRouter, clock: FakeClock, reader: HttpReader
) -> None:
    respx_mock.route(host="www.reddit.com").mock(
        return_value=feed_response("selfhosted_top_day.xml")
    )
    settings = RedditSettings(mode="rss")
    provider = provider_for(settings, clock)
    with pytest.raises(TransientNetworkError):
        await provider.fetch(build_context(settings, clock, http=reader, offline=True))
    assert len(respx_mock.calls) == 0


async def test_no_communities_raises_unavailable_without_http(
    respx_mock: respx.MockRouter, clock: FakeClock, reader: HttpReader
) -> None:
    respx_mock.route(host="www.reddit.com").mock(
        return_value=feed_response("selfhosted_top_day.xml")
    )
    settings = RedditSettings(mode="rss", communities=[])
    provider = provider_for(settings, clock)
    with pytest.raises(UnavailableError):
        await provider.fetch(build_context(settings, clock, http=reader))
    assert len(respx_mock.calls) == 0


# --------------------------------------------------------------------------- #
# RSS collection
# --------------------------------------------------------------------------- #


async def test_rss_mode_parses_a_synthetic_feed(
    respx_mock: respx.MockRouter, clock: FakeClock, reader: HttpReader
) -> None:
    respx_mock.get(SELFHOSTED_URL).mock(return_value=feed_response("selfhosted_top_day.xml"))
    settings = RedditSettings(mode="rss", communities=["selfhosted"], include_scores=True)
    payload = await collect(settings, clock, reader)

    assert payload.provenance == PROVENANCE_RSS
    assert [thread.id for thread in payload.threads] == ["t3_demo001", "t3_demo002", "t3_demo003"]
    first = payload.threads[0]
    assert first.community == "selfhosted"
    assert first.url == "https://www.reddit.com/r/selfhosted/comments/demo001/backup_strategy/"
    assert first.permalink == "/r/selfhosted/comments/demo001/backup_strategy/"
    assert first.author is not None and "example_author" in first.author
    assert first.published == datetime(2026, 10, 13, 9, 55, tzinfo=UTC)
    # include_scores must never invent a value for a source that reports none.
    assert first.score is None
    assert first.comment_count is None
    assert first.summary is not None and "<" not in first.summary

    long_summary = payload.threads[1].summary
    assert long_summary is not None
    assert len(long_summary) <= 240
    assert long_summary.endswith("…")

    undated = payload.threads[2]
    assert undated.published == datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    assert undated.author is None or "example_author" not in undated.author

    as_of = payload.as_of
    assert as_of is not None
    assert as_of == datetime(2026, 10, 13, 10, 30, tzinfo=UTC)
    assert payload.valid_until == as_of + timedelta(hours=48)
    assert payload.coverage.expected == 1
    assert payload.coverage.observed == 1
    assert not payload.coverage.is_partial
    assert [report.status for report in payload.community_reports] == ["ok"]


async def test_rss_mode_truncates_to_max_items_per_community(
    respx_mock: respx.MockRouter, clock: FakeClock, reader: HttpReader
) -> None:
    respx_mock.get(SELFHOSTED_URL).mock(return_value=feed_response("selfhosted_top_day.xml"))
    settings = RedditSettings(mode="rss", communities=["selfhosted"], max_items_per_community=2)
    payload = await collect(settings, clock, reader)
    assert len(payload.threads) == 2


async def test_rss_partial_coverage_keeps_good_communities(
    respx_mock: respx.MockRouter, clock: FakeClock, reader: HttpReader
) -> None:
    respx_mock.get(SELFHOSTED_URL).mock(return_value=feed_response("selfhosted_top_day.xml"))
    respx_mock.get(HOMELAB_URL).mock(return_value=httpx.Response(403, text="blocked"))
    settings = RedditSettings(mode="rss", communities=["selfhosted", "homelab"])
    payload = await collect(settings, clock, reader)

    assert len(payload.threads) == 3
    assert {thread.community for thread in payload.threads} == {"selfhosted"}
    reports = {report.community: report.status for report in payload.community_reports}
    assert reports == {"selfhosted": "ok", "homelab": "blocked"}
    assert payload.coverage.missing == ("homelab",)
    assert payload.coverage.observed == 1
    assert payload.coverage.is_partial
    assert payload.coverage.notes is not None and "homelab" in payload.coverage.notes


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, UnauthorizedError),
        (403, UnauthorizedError),
        (404, UnavailableError),
        (429, RateLimitedError),
    ],
)
async def test_rss_failures_surface_the_typed_reader_error_without_retry(
    respx_mock: respx.MockRouter,
    clock: FakeClock,
    reader: HttpReader,
    status: int,
    expected: type[Exception],
) -> None:
    route = respx_mock.get(SELFHOSTED_URL).mock(return_value=httpx.Response(status, text="no"))
    settings = RedditSettings(mode="rss", communities=["selfhosted"])
    provider = provider_for(settings, clock)
    with pytest.raises(expected):
        await provider.fetch(build_context(settings, clock, http=reader))
    assert route.call_count == 1


async def test_all_communities_blocked_raises_the_first_error(
    respx_mock: respx.MockRouter, clock: FakeClock, reader: HttpReader
) -> None:
    first = respx_mock.get(SELFHOSTED_URL).mock(return_value=httpx.Response(403, text="no"))
    second = respx_mock.get(HOMELAB_URL).mock(return_value=httpx.Response(429, text="slow down"))
    settings = RedditSettings(mode="rss", communities=["selfhosted", "homelab"])
    provider = provider_for(settings, clock)
    with pytest.raises(UnauthorizedError):
        await provider.fetch(build_context(settings, clock, http=reader))
    assert first.call_count == 1
    assert second.call_count == 1


async def test_html_error_page_is_a_parse_error(
    respx_mock: respx.MockRouter, clock: FakeClock, reader: HttpReader
) -> None:
    respx_mock.get(SELFHOSTED_URL).mock(return_value=html_response())
    settings = RedditSettings(mode="rss", communities=["selfhosted"])
    provider = provider_for(settings, clock)
    with pytest.raises(ParseError):
        await provider.fetch(build_context(settings, clock, http=reader))


async def test_aclose_is_safe_twice(clock: FakeClock) -> None:
    provider = provider_for(RedditSettings(mode="rss"), clock)
    await provider.aclose()
    await provider.aclose()


async def test_demo_payload_is_synthetic_and_offline(
    respx_mock: respx.MockRouter,
) -> None:
    payload = demo_payload()
    assert isinstance(payload, RedditPayload)
    assert payload.provenance == PROVENANCE_DEMO
    assert payload.threads
    assert all(thread.id.startswith("t3_demo") for thread in payload.threads)
    assert all(thread.score is None for thread in payload.threads)
    as_of = payload.as_of
    assert as_of is not None
    assert payload.valid_until == as_of + timedelta(hours=48)
    assert len(respx_mock.calls) == 0


# --------------------------------------------------------------------------- #
# Settings and registration
# --------------------------------------------------------------------------- #


def test_settings_deduplicate_and_validate_communities() -> None:
    settings = RedditSettings(communities=["selfhosted", "Selfhosted", " homelab "])
    assert settings.communities == ["selfhosted", "homelab"]
    with pytest.raises(ValidationError):
        RedditSettings(communities=["r/selfhosted"])
    with pytest.raises(ValidationError):
        RedditSettings(communities=["x"])
    with pytest.raises(ValidationError):
        RedditSettings.model_validate({"mode": "json"})


def test_settings_reject_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        RedditSettings.model_validate({"mode": "links", "refresh_seconds": 30})


def test_feed_url_is_the_documented_public_form() -> None:
    assert feed_url("selfhosted") == "https://www.reddit.com/r/selfhosted/top/.rss?t=day"


def test_registration_matches_the_contract() -> None:
    assert FEATURE.id == "reddit"
    assert FEATURE.sources == (REDDIT_SOURCE,)
    assert FEATURE.widgets == (REDDIT_WIDGET,)
    assert REDDIT_SOURCE.type_id == "reddit.threads"
    assert REDDIT_SOURCE.payload_schema == REDDIT_PAYLOAD_SCHEMA == "reddit.threads"
    assert REDDIT_SOURCE.payload_schema_version == 1
    assert REDDIT_SOURCE.capabilities == frozenset({Capability.HTTP_PUBLIC})
    assert REDDIT_SOURCE.cadence == Cadence(
        active_seconds=3600.0, ambient_seconds=3600.0, background_seconds=7200.0
    )
    assert REDDIT_SOURCE.retention_seconds == 48 * 3600
    # Both modes are complete configurations: link-only performs no I/O and RSS mode is
    # a deliberate choice by the operator. Conditional access is stated on the card, and
    # is not a missing setting, so it must not be reported as a setup gap.
    assert REDDIT_SOURCE.requires_setup is False
    assert REDDIT_SOURCE.setup_hint == (
        "Reddit access is conditional; validate a permitted feed or stay in link-only mode."
    )
    assert REDDIT_SOURCE.demo_builder is demo_payload
    assert REDDIT_WIDGET.type_id == "reddit.list"
    binding = REDDIT_WIDGET.binding("primary")
    assert binding is not None
    assert binding.accepts == frozenset({"reddit.threads"})
    assert binding.required is False
    assert REDDIT_SOURCE.validate_settings({"mode": "links"}) == RedditSettings()
