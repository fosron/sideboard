"""Reddit collection: link-only by default, public RSS only after the user validated it.

Hard rules (plan section 6.6, repository ``AGENTS.md``):

* never rotate proxies, never use an alternate host or mirror
* never substitute the ``.json`` endpoints for a blocked feed
* never impersonate a browser, copy cookies or reuse a session token
* never retry a 401/403; an access refusal is reported as a typed failure, not evaded
* no network I/O at all in ``links``/``disabled`` mode, or when the host is offline

``feedparser`` is handed the *already fetched* document text, so it performs no
network I/O itself. Every request goes through the host's bounded
:class:`~sideboard.core.capabilities.HttpReader`.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

import feedparser

from ...core.capabilities import HttpReader
from ...core.contracts import Coverage, Precision, ProviderBuild, SourcePayload
from ...core.errors import (
    BlockedError,
    CapabilityError,
    ParseError,
    ProviderError,
    RateLimitedError,
    TransientNetworkError,
    UnauthorizedError,
    UnavailableError,
)
from .config import RedditSettings
from .models import CommunityReport, RedditPayload, Thread

__all__ = [
    "FEED_URL_TEMPLATE",
    "PROVENANCE_DEMO",
    "PROVENANCE_RSS",
    "RedditProvider",
    "build_provider",
    "demo_payload",
    "feed_url",
    "plain_text",
]

REDDIT_HOST = "www.reddit.com"

FEED_URL_TEMPLATE = "https://www.reddit.com/r/{community}/top/.rss?t=day"

RSS_ACCEPT = "application/atom+xml, application/rss+xml;q=0.9, application/xml;q=0.8"

VALID_FOR_HOURS = 48.0

SUMMARY_LIMIT = 240

PROVENANCE_RSS = "reddit-public-rss"

PROVENANCE_DEMO = "reddit-demo-synthetic"

_TAG = re.compile(r"<[^>]*>")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACE = re.compile(r"\s+")


def feed_url(community: str) -> str:
    """Candidate public RSS form for one community (plan section 6.6)."""
    return FEED_URL_TEMPLATE.format(community=quote(community, safe=""))


def plain_text(value: str, *, limit: int = SUMMARY_LIMIT) -> str:
    """Strip markup and control characters, collapse whitespace, bound the length.

    The result is plain text for the shared renderer to escape; no producer markup
    survives.
    """
    text = _TAG.sub(" ", value)
    text = html.unescape(text)
    text = _CONTROL.sub(" ", text)
    text = _SPACE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


@dataclass(slots=True)
class RedditProvider:
    """One bounded acquisition per configured community. Owns no polling loop."""

    settings: RedditSettings
    _closed: bool = False

    async def fetch(self, context: ProviderBuild) -> SourcePayload:
        """Collect permitted feeds, or raise the typed error for the configured mode."""
        settings = self.settings
        if settings.mode in {"links", "disabled"}:
            # A deliberately configured mode is not a failure: it performs no I/O and
            # returns a payload that says so. Raising here would log a "blocked" failure
            # on every refresh and back the source off for an hour, which would be a
            # false report about a mode the user chose.
            return self._non_collecting_payload(settings)
        if context.offline:
            raise TransientNetworkError("public network access is suppressed in offline mode")
        http = context.http
        if http is None:
            raise CapabilityError("the reddit source has no approved HTTP reader")
        communities = list(settings.communities)
        if not communities:
            raise UnavailableError("no Reddit communities are configured")

        now = context.clock.now()
        threads: list[Thread] = []
        reports: list[CommunityReport] = []
        errors: dict[str, ProviderError] = {}
        spacing = float(getattr(settings, "request_spacing_seconds", 0.0) or 0.0)
        for index, community in enumerate(communities):
            if index and spacing > 0:
                # Politeness, not evasion: the same host, just not in one burst.
                await context.clock.sleep(spacing)
            try:
                collected = await _collect_community(
                    http, community, limit=settings.max_items_per_community
                )
            except ProviderError as error:
                errors[community] = error
                reports.append(_report_for(community, error))
                continue
            threads.extend(collected)
            reports.append(
                CommunityReport(
                    community=community,
                    status="ok",
                    note=f"{len(collected)} thread(s) in this refresh",
                )
            )
        if errors and len(errors) == len(communities):
            # Every configured community failed: surface the first typed error so the
            # host records the real category (blocked / rate limited / parse).
            raise next(iter(errors.values()))

        published = [thread.published for thread in threads if thread.published is not None]
        as_of = max(published) if published else now
        missing = tuple(errors)
        return RedditPayload(
            threads=tuple(_dedupe(threads)),
            community_reports=tuple(reports),
            as_of=as_of,
            precision=Precision.INSTANT,
            valid_until=as_of + timedelta(hours=VALID_FOR_HOURS),
            provenance=PROVENANCE_RSS,
            coverage=Coverage(
                expected=len(communities),
                observed=len(communities) - len(errors),
                missing=missing,
                notes=_coverage_notes(errors),
            ),
        )

    def _non_collecting_payload(self, settings: RedditSettings) -> RedditPayload:
        """The honest payload for ``links``/``disabled`` mode: configured, no request."""
        now_utc = datetime.now(UTC)
        reports = tuple(
            CommunityReport(
                community=community,
                status="skipped",
                note=(
                    "link-only mode: no feed was requested"
                    if settings.mode == "links"
                    else "disabled: no feed was requested"
                ),
            )
            for community in settings.communities
        )
        note = (
            "link-only mode: Reddit access is conditional, so no feed is requested"
            if settings.mode == "links"
            else "disabled: this source is switched off and requests nothing"
        )
        return RedditPayload(
            threads=(),
            community_reports=reports,
            mode=settings.mode,
            as_of=None,
            precision=Precision.UNKNOWN,
            valid_until=now_utc + timedelta(hours=VALID_FOR_HOURS),
            provenance=f"reddit-{settings.mode}",
            coverage=Coverage(
                expected=len(settings.communities),
                observed=0,
                missing=tuple(settings.communities),
                notes=note,
            ),
        )

    async def aclose(self) -> None:
        """Release owned resources. The HTTP reader belongs to the host; idempotent."""
        self._closed = True


async def _collect_community(http: HttpReader, community: str, *, limit: int) -> list[Thread]:
    """Fetch and parse one community's public feed. Raises a typed error on failure."""
    response = await http.get(feed_url(community), accept=RSS_ACCEPT)
    if response.truncated:
        raise ParseError(f"the feed for r/{community} exceeded the response size cap")
    text = response.text()
    if not text.strip():
        raise ParseError(f"the feed for r/{community} was empty")
    parsed = feedparser.parse(text)
    entries = list(parsed.entries)
    if parsed.bozo and not entries:
        raise ParseError(f"the feed for r/{community} is not valid RSS/Atom")
    threads: list[Thread] = []
    for entry in entries:
        thread = _thread_from_entry(entry, community)
        if thread is not None:
            threads.append(thread)
    if entries and not threads:
        raise ParseError(f"the feed for r/{community} contained no usable entries")
    return threads[:limit]


def _thread_from_entry(entry: Any, community: str) -> Thread | None:
    """Convert one feed entry, or ``None`` when it has no usable link."""
    link = _text(entry.get("link"))
    if link is None:
        return None
    entry_id = _text(entry.get("id"))
    title = _text(entry.get("title")) or "(untitled)"
    summary = _entry_summary(entry)
    return Thread(
        id=entry_id or f"link-{hashlib.sha256(link.encode('utf-8')).hexdigest()[:16]}",
        community=community,
        title=title[:300],
        url=link,
        permalink=_permalink(link),
        author=_text(entry.get("author")),
        published=_entry_time(entry),
        summary=summary,
    )


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _SPACE.sub(" ", _CONTROL.sub(" ", value)).strip()
    return cleaned or None


def _entry_time(entry: Any) -> datetime | None:
    """Newest timestamp the entry provides; feedparser parses these as UTC."""
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if not value:
            continue
        try:
            return datetime(*value[:6], tzinfo=UTC)
        except TypeError, ValueError:  # pragma: no cover - feedparser guarantees a tuple
            continue
    return None


def _entry_summary(entry: Any) -> str | None:
    raw = entry.get("summary")
    if not isinstance(raw, str) or not raw.strip():
        content = entry.get("content")
        if isinstance(content, list) and content:
            candidate = content[0].get("value")
            raw = candidate if isinstance(candidate, str) else None
    if not isinstance(raw, str):
        return None
    text = plain_text(raw, limit=SUMMARY_LIMIT)
    return text or None


def _permalink(link: str) -> str | None:
    parts = urlsplit(link)
    host = (parts.hostname or "").lower()
    if host != REDDIT_HOST and not host.endswith(f".{REDDIT_HOST}"):
        return None
    path = parts.path or "/"
    return path[:_PERMALINK_LIMIT]


_PERMALINK_LIMIT = 512


def _dedupe(threads: list[Thread]) -> list[Thread]:
    seen: set[str] = set()
    out: list[Thread] = []
    for thread in threads:
        if thread.id in seen:
            continue
        seen.add(thread.id)
        out.append(thread)
    return out


def _report_for(community: str, error: ProviderError) -> CommunityReport:
    blocked = isinstance(error, UnauthorizedError | BlockedError | RateLimitedError)
    return CommunityReport(
        community=community,
        status="blocked" if blocked else "error",
        note=f"{error.category}: {error.message}",
    )


def _coverage_notes(errors: dict[str, ProviderError]) -> str | None:
    if not errors:
        return None
    parts = [f"r/{community}: {error.category}" for community, error in list(errors.items())[:4]]
    return plain_text("; ".join(parts), limit=200)


def build_provider(context: ProviderBuild) -> RedditProvider:
    """Provider factory for ``reddit.threads``."""
    settings = context.settings
    if not isinstance(settings, RedditSettings):
        raise CapabilityError("the reddit source received unexpected settings")
    return RedditProvider(settings=settings)


def demo_payload() -> RedditPayload:
    """Synthetic payload for ``--demo``. Performs no network I/O."""
    now = datetime.now(UTC).replace(microsecond=0)
    threads = (
        Thread(
            id="t3_demo001",
            community="selfhosted",
            title="Synthetic: backup strategy that survived a disk failure",
            url="https://www.reddit.com/r/selfhosted/comments/demo001/synthetic_backup/",
            permalink="/r/selfhosted/comments/demo001/synthetic_backup/",
            author="/u/example_author",
            published=now - timedelta(hours=2),
            summary="Synthetic demo entry. Not a real Reddit post and not a real backup.",
        ),
        Thread(
            id="t3_demo002",
            community="selfhosted",
            title="Synthetic: monitoring a home server with 12 watts",
            url="https://www.reddit.com/r/selfhosted/comments/demo002/synthetic_power/",
            permalink="/r/selfhosted/comments/demo002/synthetic_power/",
            author="/u/example_author",
            published=now - timedelta(hours=7),
        ),
        Thread(
            id="t3_demo003",
            community="homelab",
            title="Synthetic: replacing a 2014 firewall appliance",
            url="https://www.reddit.com/r/homelab/comments/demo003/synthetic_firewall/",
            permalink="/r/homelab/comments/demo003/synthetic_firewall/",
            published=now - timedelta(hours=13),
        ),
        Thread(
            id="t3_demo004",
            community="PHP",
            title="Synthetic: reading a 40 GB log file line by line",
            url="https://www.reddit.com/r/PHP/comments/demo004/synthetic_logs/",
            permalink="/r/PHP/comments/demo004/synthetic_logs/",
            published=now - timedelta(days=1, hours=3),
        ),
    )
    reports = tuple(
        CommunityReport(community=community, status="ok", note="synthetic demo data")
        for community in ("selfhosted", "homelab", "PHP")
    )
    return RedditPayload(
        threads=threads,
        community_reports=reports,
        as_of=now,
        precision=Precision.INSTANT,
        valid_until=now + timedelta(hours=VALID_FOR_HOURS),
        provenance=PROVENANCE_DEMO,
        coverage=Coverage(expected=3, observed=3, notes="synthetic demo data"),
    )
