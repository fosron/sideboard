"""Offline provider tests for the Hacker News collector.

No test here touches the network: respx intercepts the httpx transport.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from sideboard.core.capabilities import HttpReader
from sideboard.core.clock import FakeClock
from sideboard.core.contracts import Precision, ProviderBuild
from sideboard.core.errors import CapabilityError, ParseError, TransientNetworkError
from sideboard.features.hackernews.config import HackerNewsSettings
from sideboard.features.hackernews.models import HackerNewsPayload
from sideboard.features.hackernews.provider import (
    API_BASE,
    HackerNewsProvider,
    demo_builder,
    parse_id_list,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _ids(name: str) -> list[int]:
    data = _load(name)
    assert isinstance(data, list)
    return [int(entry) for entry in data]


def _items() -> dict[str, dict[str, object]]:
    data = _load("items.json")
    assert isinstance(data, dict)
    out: dict[str, dict[str, object]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[str(key)] = {str(inner_key): inner for inner_key, inner in value.items()}
    return out


TOP_IDS = _ids("topstories.json")
NEW_IDS = _ids("newstories.json")
SHOW_IDS = _ids("showstories.json")
ASK_IDS = _ids("askstories.json")
ITEMS = _items()


def _epoch(item_id: int) -> int:
    value = ITEMS[str(item_id)]["time"]
    assert isinstance(value, int)
    return value


def _item(item_id: int, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "id": item_id,
        "type": "story",
        "by": "tester",
        "title": f"Synthetic item {item_id}",
        "url": f"https://example.com/{item_id}",
        "score": 10,
        "descendants": 2,
        "time": 1791889200,
    }
    data.update(overrides)
    return data


@pytest.fixture(autouse=True)
def _include_timeout_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these tests focused while core's HttpReader omits the httpx ``pool`` timeout.

    ``sideboard.core.capabilities.HttpReader._stream`` builds
    ``httpx.Timeout(connect=..., read=..., write=...)`` and httpx 0.28 requires all
    four parameters. The fix belongs in core; once core passes ``pool`` this fixture
    detects that and installs nothing.
    """
    try:
        httpx.Timeout(connect=1.0, read=1.0, write=1.0)
    except ValueError:
        original = httpx.Timeout

        def _timeout(*args: Any, **kwargs: Any) -> httpx.Timeout:
            if "pool" not in kwargs and "connect" in kwargs:
                kwargs["pool"] = kwargs["connect"]
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "Timeout", _timeout)


def _context(settings: HackerNewsSettings, client: httpx.AsyncClient) -> ProviderBuild:
    return ProviderBuild(
        source_id="hn",
        settings=settings,
        clock=FakeClock(),
        http=HttpReader(client=client),
    )


def _mock_id_list(router: respx.MockRouter, endpoint: str, ids: list[int]) -> None:
    router.get(f"{API_BASE}/{endpoint}").mock(return_value=httpx.Response(200, json=ids))


def _mock_items(
    router: respx.MockRouter,
    ids: list[int],
    items: dict[str, dict[str, object]] | None = None,
) -> dict[int, respx.Route]:
    source = ITEMS if items is None else items
    routes: dict[int, respx.Route] = {}
    for item_id in ids:
        routes[item_id] = router.get(f"{API_BASE}/item/{item_id}.json").mock(
            return_value=httpx.Response(200, json=source[str(item_id)])
        )
    return routes


async def test_fetch_reads_lists_in_order_and_reports_coverage() -> None:
    settings = HackerNewsSettings(lists=["top", "new"], items_per_list=30)
    with respx.mock() as router:
        _mock_id_list(router, "topstories.json", TOP_IDS)
        _mock_id_list(router, "newstories.json", NEW_IDS)
        _mock_items(router, [*TOP_IDS, *NEW_IDS])
        async with httpx.AsyncClient() as client:
            payload = await HackerNewsProvider(settings).fetch(_context(settings, client))

    assert isinstance(payload, HackerNewsPayload)
    assert [story.id for story in payload.stories] == [*TOP_IDS, *NEW_IDS]
    assert [story.list for story in payload.stories] == ["top"] * len(TOP_IDS) + ["new"] * len(
        NEW_IDS
    )
    assert payload.provenance == "hacker-news-official-api"
    assert payload.precision is Precision.INSTANT
    newest = datetime.fromtimestamp(_epoch(202), tz=UTC)
    assert payload.as_of == newest
    # Measured from the fetch, not from the newest story: in a quiet hour the ranking we
    # hold is still current, and as_of shows the story's own time.
    assert payload.valid_until is not None and payload.valid_until > newest
    assert payload.coverage.expected == 60
    assert payload.coverage.observed == 5
    assert payload.coverage.is_partial


async def test_union_is_deduplicated_and_first_configured_list_wins() -> None:
    settings = HackerNewsSettings(lists=["top", "new"], items_per_list=30)
    with respx.mock() as router:
        _mock_id_list(router, "topstories.json", [101, 102])
        _mock_id_list(router, "newstories.json", [102, 201])
        routes = _mock_items(router, [101, 102, 201])
        async with httpx.AsyncClient() as client:
            payload = await HackerNewsProvider(settings).fetch(_context(settings, client))

    assert [story.id for story in payload.stories] == [101, 102, 201]
    assert payload.stories[1].list == "top"
    assert routes[102].call_count == 1


async def test_dead_deleted_and_job_items_are_skipped_and_reported_partial() -> None:
    settings = HackerNewsSettings(lists=["top"], items_per_list=30)
    ids = [101, 104, 105, 106]
    with respx.mock() as router:
        _mock_id_list(router, "topstories.json", ids)
        _mock_items(router, ids)
        async with httpx.AsyncClient() as client:
            payload = await HackerNewsProvider(settings).fetch(_context(settings, client))

    assert [story.id for story in payload.stories] == [101]
    assert set(payload.coverage.missing) == {"104", "105", "106"}
    assert payload.coverage.is_partial


async def test_jobs_are_kept_when_include_jobs_is_set() -> None:
    settings = HackerNewsSettings(lists=["top"], items_per_list=30, include_jobs=True)
    ids = [101, 106]
    with respx.mock() as router:
        _mock_id_list(router, "topstories.json", ids)
        _mock_items(router, ids)
        async with httpx.AsyncClient() as client:
            payload = await HackerNewsProvider(settings).fetch(_context(settings, client))

    assert [story.id for story in payload.stories] == [101, 106]
    assert payload.stories[1].type == "job"


async def test_missing_item_is_skipped_not_fatal() -> None:
    settings = HackerNewsSettings(lists=["top"], items_per_list=30)
    ids = [101, 102, 103]
    with respx.mock() as router:
        _mock_id_list(router, "topstories.json", ids)
        _mock_items(router, [101, 103])
        router.get(f"{API_BASE}/item/102.json").mock(return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            payload = await HackerNewsProvider(settings).fetch(_context(settings, client))

    assert [story.id for story in payload.stories] == [101, 103]
    assert "102" in payload.coverage.missing


async def test_id_list_failure_raises_the_typed_error() -> None:
    settings = HackerNewsSettings(lists=["top", "new"])
    with respx.mock() as router:
        router.get(f"{API_BASE}/topstories.json").mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as client:
            with pytest.raises(TransientNetworkError):
                await HackerNewsProvider(settings).fetch(_context(settings, client))


async def test_ids_are_capped_at_items_per_list() -> None:
    settings = HackerNewsSettings(lists=["top"], items_per_list=2)
    ids = [1000 + index for index in range(10)]
    items = {str(item_id): _item(item_id) for item_id in ids}
    with respx.mock(assert_all_called=False) as router:
        _mock_id_list(router, "topstories.json", ids)
        routes = _mock_items(router, ids, items)
        async with httpx.AsyncClient() as client:
            payload = await HackerNewsProvider(settings).fetch(_context(settings, client))

    assert [story.id for story in payload.stories] == ids[:2]
    assert routes[ids[2]].call_count == 0


async def test_empty_id_list_raises_parse_error() -> None:
    settings = HackerNewsSettings(lists=["top"])
    with respx.mock() as router:
        _mock_id_list(router, "topstories.json", [])
        async with httpx.AsyncClient() as client:
            with pytest.raises(ParseError, match="no usable stories"):
                await HackerNewsProvider(settings).fetch(_context(settings, client))


async def test_all_items_failing_raises_parse_error() -> None:
    settings = HackerNewsSettings(lists=["top"])
    with respx.mock() as router:
        _mock_id_list(router, "topstories.json", [101, 102])
        for item_id in (101, 102):
            router.get(f"{API_BASE}/item/{item_id}.json").mock(return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ParseError, match="no usable stories"):
                await HackerNewsProvider(settings).fetch(_context(settings, client))


async def test_show_list_uses_the_show_endpoint() -> None:
    settings = HackerNewsSettings(lists=["show"])
    with respx.mock() as router:
        _mock_id_list(router, "showstories.json", SHOW_IDS)
        _mock_items(router, SHOW_IDS)
        async with httpx.AsyncClient() as client:
            payload = await HackerNewsProvider(settings).fetch(_context(settings, client))

    assert [story.id for story in payload.stories] == SHOW_IDS
    assert payload.stories[0].list == "show"
    assert payload.stories[0].title == "Show HN: synthetic project"


async def test_ask_item_text_excerpt_is_plain_and_bounded() -> None:
    settings = HackerNewsSettings(lists=["ask"])
    with respx.mock() as router:
        _mock_id_list(router, "askstories.json", ASK_IDS)
        _mock_items(router, ASK_IDS)
        async with httpx.AsyncClient() as client:
            payload = await HackerNewsProvider(settings).fetch(_context(settings, client))

    story = payload.stories[0]
    assert story.text_excerpt is not None
    assert len(story.text_excerpt) <= 300
    assert "<" not in story.text_excerpt
    assert "& welcome" in story.text_excerpt
    assert story.url is None


async def test_missing_http_capability_raises_capability_error() -> None:
    settings = HackerNewsSettings()
    build = ProviderBuild(source_id="hn", settings=settings, clock=FakeClock())
    with pytest.raises(CapabilityError):
        await HackerNewsProvider(settings).fetch(build)


async def test_aclose_is_idempotent() -> None:
    provider = HackerNewsProvider(HackerNewsSettings())
    await provider.aclose()
    await provider.aclose()


def test_demo_builder_is_synthetic_and_complete() -> None:
    payload = demo_builder()
    assert isinstance(payload, HackerNewsPayload)
    assert payload.stories
    assert any("DEMO" in story.title for story in payload.stories)
    assert payload.provenance == "hacker-news-official-api"
    assert payload.as_of is not None
    assert payload.valid_until is not None and payload.valid_until > payload.as_of
    assert payload.coverage.observed == len(payload.stories)


def test_parse_id_list_rejects_non_arrays_and_drops_junk_ids() -> None:
    with pytest.raises(ParseError):
        parse_id_list({"not": "a list"}, "top")
    assert parse_id_list([1, "2", -3, 0, True, 4, None], "top") == [1, 4]


def test_settings_reject_unknown_lists_and_out_of_range_values() -> None:
    with pytest.raises(ValidationError, match="unknown Hacker News list"):
        HackerNewsSettings(lists=["front"])
    with pytest.raises(ValidationError):
        HackerNewsSettings(lists=[])
    with pytest.raises(ValidationError):
        HackerNewsSettings(items_per_list=61)
    with pytest.raises(ValidationError):
        HackerNewsSettings(concurrency=13)
    assert HackerNewsSettings(lists=["top", "top", "new"]).lists == ["top", "new"]
