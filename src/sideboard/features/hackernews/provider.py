"""Hacker News collector on the official Firebase API.

One bounded acquisition per refresh: read each configured story list, fetch at most
``items_per_list`` items through a bounded worker pool, skip unusable items and report
coverage honestly. Comment trees are never fetched or stored.

Only the host-owned shared HTTP client is used, so global and per-host limits apply
across every source in the application. A single failed item never fails the refresh.
"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from ...core.capabilities import HttpReader
from ...core.contracts import (
    Cadence,
    Capability,
    Coverage,
    Precision,
    ProviderBuild,
    SourceSpec,
)
from ...core.errors import CapabilityError, ConfigError, ParseError, ProviderError
from .config import HackerNewsSettings
from .models import (
    PAYLOAD_SCHEMA,
    PAYLOAD_SCHEMA_VERSION,
    PROVENANCE,
    TEXT_EXCERPT_LIMIT,
    VALID_FOR_SECONDS,
    HackerNewsPayload,
    Story,
)

__all__ = [
    "API_BASE",
    "SOURCE",
    "HackerNewsProvider",
    "demo_builder",
    "parse_id_list",
    "story_from_item",
]

API_BASE = "https://hacker-news.firebaseio.com/v0"
MAX_REPORTED_MISSING = 20

_LIST_ENDPOINTS: dict[str, str] = {
    "top": "topstories.json",
    "new": "newstories.json",
    "show": "showstories.json",
    "ask": "askstories.json",
}

_TAG = re.compile(r"<[^>]*>")


def _optional_int(value: object) -> int | None:
    """Return the value when it is a genuine integer, else ``None`` (never a guess)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _epoch_to_datetime(value: object) -> datetime | None:
    epoch = _optional_int(value)
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except OverflowError, OSError, ValueError:
        return None


def _safe_url(value: object) -> str | None:
    """Accept only http(s) URLs: item data is untrusted and may hold any scheme."""
    text = _optional_text(value)
    if text is None:
        return None
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return text


def _plain_text(value: object, *, limit: int) -> str | None:
    """Strip markup, unescape entities and collapse whitespace from untrusted text."""
    text = _optional_text(value)
    if text is None:
        return None
    stripped = _TAG.sub(" ", text)
    unescaped = html.unescape(stripped)
    collapsed = " ".join(unescaped.split())
    if not collapsed:
        return None
    return collapsed[:limit]


def parse_id_list(data: object, list_key: str) -> list[int]:
    """Return the positive integer item ids of one ``*stories.json`` array."""
    if not isinstance(data, list):
        raise ParseError(f"{list_key} story list was not a JSON array")
    ids: list[int] = []
    for entry in data:
        item_id = _optional_int(entry)
        if item_id is not None and item_id > 0:
            ids.append(item_id)
    return ids


def story_from_item(
    data: object, *, list_key: str, item_id: int, include_jobs: bool
) -> Story | None:
    """Convert one raw API item, or return ``None`` when it is not usable.

    Unusable means: deleted, dead, a job while jobs are excluded, or missing a title.
    """
    if not isinstance(data, dict):
        return None
    if data.get("deleted") or data.get("dead"):
        return None
    item_type = _optional_text(data.get("type"))
    if item_type == "job" and not include_jobs:
        return None
    title = _optional_text(data.get("title"))
    if title is None:
        return None
    excerpt = None
    if list_key == "ask":
        excerpt = _plain_text(data.get("text"), limit=TEXT_EXCERPT_LIMIT)
    return Story(
        id=item_id,
        list=list_key,
        title=title,
        by=_optional_text(data.get("by")),
        url=_safe_url(data.get("url")),
        score=_optional_int(data.get("score")),
        descendants=_optional_int(data.get("descendants")),
        time=_epoch_to_datetime(data.get("time")),
        type=item_type,
        text_excerpt=excerpt,
    )


class HackerNewsProvider:
    """Collects the configured Hacker News lists through the shared HTTP reader."""

    def __init__(self, settings: HackerNewsSettings) -> None:
        self._settings = settings
        self._closed = False

    @property
    def settings(self) -> HackerNewsSettings:
        return self._settings

    async def fetch(self, context: ProviderBuild) -> HackerNewsPayload:
        """Read every configured list and return one payload, or raise a typed error."""
        http = context.http
        if http is None:
            raise CapabilityError("hackernews requires HTTP access")
        settings = context.settings
        if not isinstance(settings, HackerNewsSettings):
            raise ConfigError("hackernews source settings were not validated")

        requested: list[tuple[str, int]] = []
        seen: set[int] = set()
        for list_key in settings.lists:
            response = await http.get_json(f"{API_BASE}/{_LIST_ENDPOINTS[list_key]}")
            ids = parse_id_list(response.json(), list_key)[: settings.items_per_list]
            for item_id in ids:
                if item_id in seen:
                    continue
                seen.add(item_id)
                requested.append((list_key, item_id))

        if not requested:
            raise ParseError("no usable stories")

        results = await self._fetch_items(http, requested, settings)
        stories: list[Story] = []
        missing: list[int] = []
        for _, item_id in requested:
            story = results.get(item_id)
            if story is None:
                missing.append(item_id)
            else:
                stories.append(story)
        if not stories:
            raise ParseError("no usable stories")

        times = [story.time for story in stories if story.time is not None]
        as_of = max(times) if times else None
        return HackerNewsPayload(
            stories=tuple(stories),
            as_of=as_of,
            precision=Precision.INSTANT,
            # From the fetch, not from the newest story: in a quiet hour the ranking we
            # hold is still the current ranking.
            valid_until=context.clock.now() + timedelta(seconds=VALID_FOR_SECONDS),
            provenance=PROVENANCE,
            coverage=Coverage(
                expected=settings.items_per_list * len(settings.lists),
                observed=len(stories),
                missing=tuple(str(item_id) for item_id in missing[:MAX_REPORTED_MISSING]),
                notes=(
                    f"{len(missing) - MAX_REPORTED_MISSING} more unusable items were not listed"
                    if len(missing) > MAX_REPORTED_MISSING
                    else None
                ),
            ),
        )

    async def _fetch_items(
        self,
        http: HttpReader,
        requests: Sequence[tuple[str, int]],
        settings: HackerNewsSettings,
    ) -> dict[int, Story]:
        """Fetch items with a bounded worker pool and a semaphore.

        The pool never creates more tasks than ``settings.concurrency``, and the
        semaphore independently caps in-flight requests at the same number.
        """
        results: dict[int, Story] = {}
        if not requests:
            return results
        queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        for request in requests:
            queue.put_nowait(request)
        semaphore = asyncio.Semaphore(settings.concurrency)

        async def worker() -> None:
            while True:
                try:
                    list_key, item_id = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    async with semaphore:
                        story = await self._fetch_story(
                            http, list_key, item_id, settings.include_jobs
                        )
                except ProviderError:
                    story = None  # one failed item must not fail the whole refresh
                if story is not None:
                    results[item_id] = story

        workers = max(1, min(settings.concurrency, len(requests)))
        async with asyncio.TaskGroup() as group:
            for _ in range(workers):
                group.create_task(worker())
        return results

    @staticmethod
    async def _fetch_story(
        http: HttpReader, list_key: str, item_id: int, include_jobs: bool
    ) -> Story | None:
        response = await http.get_json(f"{API_BASE}/item/{item_id}.json")
        return story_from_item(
            response.json(), list_key=list_key, item_id=item_id, include_jobs=include_jobs
        )

    async def aclose(self) -> None:
        """The HTTP client is host-owned; calling this more than once is safe."""
        self._closed = True


def demo_builder() -> HackerNewsPayload:
    """Synthetic stories for ``--demo``. Performs no network access."""
    now = datetime.now(UTC).replace(microsecond=0)
    stories = (
        Story(
            id=10000001,
            list="top",
            title="DEMO: A local dashboard on an iPad extended display",
            by="demo_user",
            url="https://example.com/demo-dashboard",
            score=412,
            descendants=87,
            time=now - timedelta(hours=3, minutes=12),
            type="story",
        ),
        Story(
            id=10000002,
            list="top",
            title="DEMO: Bounded collectors keep a dashboard honest",
            by="demo_user",
            url="https://example.com/demo-collectors",
            score=188,
            descendants=41,
            time=now - timedelta(hours=5),
            type="story",
        ),
        Story(
            id=10000003,
            list="top",
            title="Ask HN: DEMO question with a plain-text excerpt",
            by="demo_user",
            score=96,
            descendants=153,
            time=now - timedelta(hours=8),
            type="story",
            text_excerpt=(
                "DEMO: Synthetic text so the card can show a short plain-text excerpt "
                "without ever fetching a comment tree."
            ),
        ),
        Story(
            id=10000004,
            list="new",
            title="DEMO: Fresh item without a score yet",
            by="demo_user",
            url="https://example.com/demo-new",
            time=now - timedelta(minutes=6),
            type="story",
        ),
        Story(
            id=10000005,
            list="show",
            title="Show HN: DEMO project built last weekend",
            by="demo_user",
            url="https://example.com/show-demo",
            score=31,
            descendants=9,
            time=now - timedelta(hours=2),
            type="story",
        ),
    )
    as_of = max(story.time for story in stories if story.time is not None)
    return HackerNewsPayload(
        stories=stories,
        as_of=as_of,
        precision=Precision.INSTANT,
        valid_until=as_of + timedelta(seconds=VALID_FOR_SECONDS),
        provenance=PROVENANCE,
        coverage=Coverage(expected=len(stories), observed=len(stories)),
    )


def _settings_of(build: ProviderBuild) -> HackerNewsSettings:
    settings = build.settings
    if not isinstance(settings, HackerNewsSettings):
        raise ConfigError("hackernews source settings were not validated")
    return settings


SOURCE: SourceSpec = SourceSpec(
    type_id="hackernews.stories",
    payload_model=HackerNewsPayload,
    settings_model=HackerNewsSettings,
    payload_schema=PAYLOAD_SCHEMA,
    payload_schema_version=PAYLOAD_SCHEMA_VERSION,
    factory=lambda build: HackerNewsProvider(_settings_of(build)),
    capabilities=frozenset({Capability.HTTP_PUBLIC}),
    cadence=Cadence(active_seconds=300.0, ambient_seconds=300.0, background_seconds=900.0),
    description="Hacker News Top/New/Show/Ask lists from the official public API.",
    requires_setup=False,
    demo_builder=demo_builder,
)
