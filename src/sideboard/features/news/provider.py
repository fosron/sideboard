"""RSS/Atom collector.

One bounded acquisition: fetch every enabled feed with a conditional request, parse
with :mod:`feedparser`, sanitise every field, deduplicate by canonical URL and report
each feed's outcome. A single unreachable publisher never fails the whole refresh.

A ``304 Not Modified`` may validate a representation but must never advance the
publisher's own publication date, so the previous article is reused unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import feedparser

from ...core.capabilities import HttpReader
from ...core.contracts import (
    Cadence,
    Capability,
    Coverage,
    Precision,
    ProviderBuild,
    SourcePayload,
    SourceSpec,
)
from ...core.errors import CapabilityError, ParseError, SideboardError, UnavailableError
from .config import FeedSetting, NewsSettings
from .models import Article, FeedReport, NewsPayload

log = logging.getLogger("sideboard.news")

MAX_EXCERPT = 320
TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref_src")


def _clean(value: object, limit: int = 400) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def canonical_url(url: str) -> str:
    """Canonical form used for cross-feed deduplication.

    Drops the fragment and known tracking parameters, keeps the path and query order.
    """
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    netloc = host if not parts.port else f"{host}:{parts.port}"
    query = "&".join(
        chunk
        for chunk in parts.query.split("&")
        if chunk and not chunk.split("=", 1)[0].lower().startswith(TRACKING_PREFIXES)
    )
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


def _entry_time(entry: object) -> datetime | None:
    for attribute in ("published_parsed", "updated_parsed"):
        value = getattr(entry, attribute, None)
        if value:
            with contextlib.suppress(ValueError, OverflowError, TypeError):
                return datetime(*value[:6], tzinfo=UTC)
    return None


def _excerpt(entry: object) -> str | None:
    for attribute in ("summary", "description"):
        raw = getattr(entry, attribute, None)
        if raw:
            return _clean(_strip_tags(str(raw)), MAX_EXCERPT)
    content = getattr(entry, "content", None)
    if content:
        with contextlib.suppress(TypeError, IndexError, KeyError):
            return _clean(_strip_tags(str(content[0]["value"])), MAX_EXCERPT)
    return None


def _strip_tags(value: str) -> str:
    """Remove markup and every control character. Feeds are untrusted data."""
    out: list[str] = []
    depth = 0
    for char in value:
        if char == "<":
            depth += 1
            continue
        if char == ">":
            depth = max(0, depth - 1)
            continue
        if depth:
            continue
        if char == "&":
            out.append(" ")
            continue
        code = ord(char)
        if code < 32 or code == 127:
            out.append(" ")
            continue
        out.append(char)
    return "".join(out)


def _entry_link(entry: object) -> str | None:
    link = getattr(entry, "link", None)
    if isinstance(link, str) and link.startswith(("http://", "https://")):
        return link
    links = getattr(entry, "links", None) or []
    for candidate in links:
        with contextlib.suppress(TypeError, KeyError):
            href = str(candidate.get("href", ""))
            if href.startswith(("http://", "https://")):
                return href
    return None


class NewsProvider:
    """Collects configured publisher feeds."""

    def __init__(self, *, settings: NewsSettings) -> None:
        self.settings = settings

    async def fetch(self, context: ProviderBuild) -> NewsPayload:
        http = context.http
        if http is None:
            raise CapabilityError("news requires HTTP access")
        settings = context.settings
        assert isinstance(settings, NewsSettings)
        previous = context.previous.payload if context.previous else None
        previous_articles = previous.articles if isinstance(previous, NewsPayload) else ()
        validators = dict(previous.validators) if isinstance(previous, NewsPayload) else {}
        last_modified = dict(previous.last_modified) if isinstance(previous, NewsPayload) else {}

        enabled = [feed for feed in settings.feeds if feed.enabled]
        if not enabled:
            raise UnavailableError(
                "no news feeds are enabled; add at least one validated feed", setup="news.feeds"
            )

        semaphore = asyncio.Semaphore(max(1, min(settings.concurrency, 8)))
        results = await asyncio.gather(
            *(
                self._one_feed(
                    http,
                    feed,
                    validators.get(feed.url),
                    last_modified.get(feed.url),
                    semaphore,
                    settings,
                    tuple(a for a in previous_articles if a.source_id == feed.id),
                )
                for feed in enabled
            ),
            return_exceptions=True,
        )

        articles: list[Article] = []
        reports: list[FeedReport] = []
        new_validators: dict[str, str] = {}
        new_last_modified: dict[str, str] = {}
        failures: list[str] = []

        for feed, result in zip(enabled, results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, SideboardError):
                    category = str(result.category)
                else:  # pragma: no cover - unexpected
                    category = "internal"
                if isinstance(result, UnavailableError):
                    failures.append(feed.id)
                reports.append(
                    FeedReport(
                        feed_id=feed.id,
                        label=feed.label or feed.id,
                        status="error" if category != "network" else "offline",
                        note=f"{category}: {str(result)[:120]}",
                    )
                )
                cached = [a for a in previous_articles if a.source_id == feed.id]
                articles.extend(cached)
                if validators.get(feed.url):
                    new_validators[feed.url] = validators[feed.url]
                continue
            fetched, report, etag, modified = result
            articles.extend(fetched)
            reports.append(report)
            if etag:
                new_validators[feed.url] = etag
            if modified:
                new_last_modified[feed.url] = modified

        if not articles:
            if reports and all(report.not_modified for report in reports):
                if isinstance(previous, NewsPayload):
                    # Return the cached payload object itself: identical content, so the
                    # host only refreshes freshness and keeps the original article dates.
                    return previous
                raise ParseError("the feed reported 304 but no cached copy is available")
            if len(failures) == len(enabled):
                raise UnavailableError("no enabled news feed could be collected")
            raise ParseError("no usable news items were returned")

        deduped: dict[str, Article] = {}
        for article in articles:
            existing = deduped.get(article.canonical)
            if existing is None:
                deduped[article.canonical] = article
                continue
            if (article.published or datetime.min.replace(tzinfo=UTC)) > (
                existing.published or datetime.min.replace(tzinfo=UTC)
            ):
                # Same canonical URL from two feeds: keep the newest, ignore divergence.
                deduped[article.canonical] = article
        ordered = sorted(
            deduped.values(),
            key=lambda a: a.published or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        observed = sum(1 for report in reports if report.status == "ok")
        return NewsPayload(
            articles=tuple(ordered),
            reports=tuple(reports),
            validators=new_validators,
            last_modified=new_last_modified,
            as_of=ordered[0].published if ordered else None,
            precision=Precision.INSTANT,
            valid_until=context.clock.now() + timedelta(seconds=1200),
            provenance="publisher-rss-atom",
            coverage=Coverage(
                expected=len(enabled),
                observed=observed,
                missing=tuple(report.label for report in reports if report.status != "ok"),
                notes="bundles one request per enabled publisher feed",
            ),
        )

    async def _one_feed(
        self,
        http: HttpReader,
        feed: FeedSetting,
        etag: str | None,
        last_modified: str | None,
        semaphore: asyncio.Semaphore,
        settings: NewsSettings,
        cached_articles: tuple[Article, ...] = (),
    ) -> tuple[list[Article], FeedReport, str | None, str | None]:
        async with semaphore:
            response = await http.get(
                feed.url,
                etag=etag,
                last_modified=last_modified,
                accept="application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
            )
        if response.not_modified:
            # A 304 validates the representation: the publisher's own publication dates
            # are reused unchanged, and the host must not advance the source date.
            return (
                list(cached_articles),
                FeedReport(
                    feed_id=feed.id,
                    label=feed.label or feed.id,
                    status="ok",
                    not_modified=True,
                    items=len(cached_articles),
                ),
                response.header("etag") or etag,
                response.header("last-modified") or last_modified,
            )
        if response.truncated:
            raise ParseError(f"feed {feed.id} exceeded the payload cap")
        parsed = feedparser.parse(response.content)
        label = feed.label or _clean(getattr(parsed.feed, "title", "") or feed.id, 60)
        if parsed.bozo and not parsed.entries:
            raise ParseError(
                f"feed {feed.id} could not be parsed: {_clean(parsed.bozo_exception, 100)}"
            )
        limit = min(settings.max_items_per_feed, feed.max_items)
        articles: list[Article] = []
        for entry in parsed.entries[:limit]:
            url = _entry_link(entry)
            if not url:
                continue
            canonical = canonical_url(url)
            title = _clean(getattr(entry, "title", ""), 240)
            if not title:
                continue
            articles.append(
                Article(
                    id=hashlib.sha1(canonical.encode()).hexdigest()[:16],
                    title=title,
                    url=url,
                    canonical=canonical,
                    source_id=feed.id,
                    source_label=label,
                    category=feed.category,
                    language=feed.language,
                    published=_entry_time(entry),
                    excerpt=_excerpt(entry),
                    ai_related=_is_ai_related(title, feed.category, settings),
                )
            )
        return (
            articles,
            FeedReport(feed_id=feed.id, label=label, status="ok", items=len(articles)),
            response.header("etag"),
            response.header("last-modified"),
        )

    async def aclose(self) -> None:
        return None


def _is_ai_related(title: str, category: str, settings: NewsSettings) -> bool:
    """Conservative keyword tagging. A heuristic label, never a model judgement."""
    if category == "ai":
        return True
    lowered = title.lower()
    return any(keyword in lowered for keyword in settings.ai_keywords)


def make_spec() -> SourceSpec:
    return SourceSpec(
        type_id="news.feeds",
        payload_model=NewsPayload,
        settings_model=NewsSettings,
        payload_schema="news.feeds",
        payload_schema_version=1,
        factory=lambda build: NewsProvider(settings=_settings_of(build)),
        capabilities=frozenset({Capability.HTTP_PUBLIC}),
        cadence=Cadence(active_seconds=1200, ambient_seconds=1200, background_seconds=3600),
        retention_seconds=3 * 24 * 3600,
        description="Curated publisher RSS/Atom feeds with conditional requests.",
        requires_setup=True,
        setup_hint=(
            "Add at least one validated publisher feed under settings.feeds; "
            "candidate URLs must be checked on your Mac first."
        ),
        demo_builder=demo_payload,
        setup_check=lambda settings: any(feed.enabled for feed in settings.feeds),
    )


def _settings_of(build: ProviderBuild) -> NewsSettings:
    settings = build.settings
    assert isinstance(settings, NewsSettings)
    return settings


def demo_payload() -> SourcePayload:
    """Synthetic headlines for ``--demo``. Clearly labelled as demo data."""
    now = datetime.now(UTC)
    synthetic = (
        ("DEMO: Klaipėda opens a new pier section", "local", "Atvira Klaipėda", "lt", False),
        (
            "DEMO: Lithuanian startups set a funding record",
            "lithuania",
            "DEMO Lithuania",
            "lt",
            False,
        ),
        ("DEMO: European regulators publish a draft rule", "europe", "DEMO Europe", "en", False),
        ("DEMO: Global shipping reroutes around the drought", "world", "DEMO World", "en", False),
        ("DEMO: Bond yields steady as inflation cools", "business", "DEMO Business", "en", False),
        (
            "DEMO: Index funds keep beating stock pickers",
            "personal_finance",
            "DEMO Finance",
            "en",
            False,
        ),
        (
            "DEMO: A release candidate ships a new query planner",
            "software",
            "DEMO Engineering",
            "en",
            False,
        ),
        ("DEMO: Open weights model runs on a laptop", "ai", "DEMO AI Weekly", "en", True),
        ("DEMO: A tiny NAS draws 9 watts idle", "gadgets", "DEMO Hardware", "en", False),
        (
            "DEMO: Backup tool adds verified restore drills",
            "infrastructure",
            "DEMO Ops",
            "en",
            False,
        ),
    )
    articles = tuple(
        Article(
            id=f"demo-news-{index}",
            title=title,
            url=f"https://example.invalid/demo-news/{index}",
            canonical=f"https://example.invalid/demo-news/{index}",
            source_id=category,
            source_label=source,
            category=category,
            language=language,
            published=now - timedelta(hours=index),
            excerpt="Synthetic demonstration excerpt. Not a real article.",
            ai_related=ai,
        )
        for index, (title, category, source, language, ai) in enumerate(synthetic)
    )
    return NewsPayload(
        articles=articles,
        reports=tuple(
            FeedReport(feed_id=category, label=label, status="ok", items=1)
            for category, label in (
                ("local", "Atvira Klaipėda"),
                ("lithuania", "DEMO Lithuania"),
                ("world", "DEMO World"),
                ("software", "DEMO Engineering"),
            )
        ),
        as_of=articles[0].published if articles else None,
        precision=Precision.INSTANT,
        provenance="demo",
        coverage=Coverage(expected=len(articles), observed=len(articles)),
    )
