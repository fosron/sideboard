"""Bounded collection of public YouTube channel uploads.

One acquisition per refresh uses Google's documented per-channel Atom feed:

``https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID``

Only channel IDs are fetched. Handles and bare names are reported as unresolved
setup problems; no HTML page is scraped and no handle-resolution pipeline exists.
Google Takeout subscriptions and watch history are parsed from the local files the
user explicitly selected; a missing or unreadable import is reported per file and
never raised. Every default module import here performs no I/O.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import feedparser

from ...core.capabilities import FileReader, HttpReader
from ...core.contracts import Coverage, Precision, ProviderBuild
from ...core.errors import CapabilityError, ParseError, ProviderError, SideboardError
from .config import YouTubeSettings
from .importers import (
    SUBSCRIPTIONS_MAX_BYTES,
    WATCH_HISTORY_MAX_BYTES,
    ImportedChannel,
    ImportReport,
    WatchedEvent,
    parse_iso_datetime,
    parse_subscriptions_csv,
    parse_watch_history_with_stats,
    subscriptions_report,
    video_id_from_url,
    watch_history_report,
)
from .models import (
    CHANNEL_STATUS_ERROR,
    CHANNEL_STATUS_NOT_FETCHED,
    CHANNEL_STATUS_OK,
    CHANNEL_STATUS_UNRESOLVED,
    RANK_BASIS_FAVORITES,
    RANK_BASIS_RECENT_VIEWING,
    ChannelReport,
    Upload,
    YouTubePayload,
)
from .ranking import (
    ChannelHistory,
    ChannelRank,
    dedupe_uploads,
    rank_uploads,
    recent_view_history,
)

__all__ = [
    "FEED_URL",
    "PROVENANCE",
    "VALID_UNTIL_SECONDS",
    "YouTubeProvider",
    "demo_payload",
    "resolve_channel_id",
]

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
PROVENANCE = "youtube-channel-atom"
VALID_UNTIL_SECONDS = 30.0 * 60.0

_CHANNEL_ID_IN_URL = re.compile(r"(?:/channel/|[?&]channel_id=)(UC[\w-]{10,})")
_RAW_CHANNEL_ID = re.compile(r"UC[\w-]{10,}")

_RESOLVED_SETUP_HINT = (
    "supply a UC… channel ID (or a channel URL containing one) or a Takeout import"
)


def resolve_channel_id(raw: str) -> str | None:
    """Resolve a configured channel reference to a channel ID, or ``None``.

    Accepted: a raw ``UC…`` ID, or a URL containing ``/channel/UC…`` or
    ``channel_id=UC…``. ``@handle`` and bare names are deliberately unresolved —
    Sideboard does not scrape the channel page to find the canonical ID.
    """
    candidate = raw.strip()
    if not candidate:
        return None
    if _RAW_CHANNEL_ID.fullmatch(candidate):
        return candidate
    match = _CHANNEL_ID_IN_URL.search(candidate)
    return match.group(1) if match else None


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One channel in this refresh's fetch pool."""

    channel_id: str
    label: str | None
    favorite: bool
    origin: str
    order: int


@dataclass(frozen=True, slots=True)
class _ChannelFetch:
    """Result of one channel feed fetch."""

    uploads: tuple[Upload, ...]
    report: ChannelReport


class YouTubeProvider:
    """Collects uploads from configured channels plus optional local Takeout imports.

    The host owns scheduling, caching and freshness; this provider performs exactly
    one bounded acquisition per :meth:`fetch` call and owns no polling loop.
    """

    def __init__(self, settings: YouTubeSettings) -> None:
        self._settings = settings
        self._closed = False

    async def fetch(self, context: ProviderBuild) -> YouTubePayload:
        """Fetch the selected channels and build one validated payload."""
        settings = self._settings
        now = context.clock.now()
        reports: list[ChannelReport] = []
        missing: list[str] = []
        notes: list[str] = []

        pool: list[_Candidate] = []
        seen: dict[str, _Candidate] = {}
        favorites = self._favorite_ids(settings)

        def add_candidate(channel_id: str, label: str | None, favorite: bool, origin: str) -> None:
            existing = seen.get(channel_id)
            if existing is None:
                candidate = _Candidate(
                    channel_id=channel_id,
                    label=label,
                    favorite=favorite,
                    origin=origin,
                    order=len(pool),
                )
                seen[channel_id] = candidate
                pool.append(candidate)
            elif favorite and not existing.favorite:
                merged = _Candidate(
                    channel_id=existing.channel_id,
                    label=existing.label or label,
                    favorite=True,
                    origin=existing.origin,
                    order=existing.order,
                )
                seen[channel_id] = merged
                pool[pool.index(existing)] = merged

        for setting in settings.channels:
            resolved = resolve_channel_id(setting.id)
            display = setting.label or setting.id
            if resolved is None:
                reports.append(
                    ChannelReport(
                        channel_id=None,
                        label=display,
                        status=CHANNEL_STATUS_UNRESOLVED,
                        note=f"channel reference could not be resolved; {_RESOLVED_SETUP_HINT}",
                    )
                )
                missing.append(display)
                continue
            add_candidate(
                resolved,
                setting.label,
                setting.favorite or resolved in favorites,
                "settings",
            )

        subscriptions_imported_at, subs_summary = self._import_subscriptions(
            context, settings, favorites, add_candidate, reports, missing
        )
        history_imported_at, history_events, history_summary = self._import_watch_history(
            context, settings, reports, missing
        )

        for raw in settings.imported_channel_ids:
            resolved = resolve_channel_id(raw)
            if resolved is None:
                reports.append(
                    ChannelReport(
                        channel_id=None,
                        label=raw,
                        status=CHANNEL_STATUS_UNRESOLVED,
                        note=f"imported channel reference could not be resolved; {_RESOLVED_SETUP_HINT}",
                    )
                )
                missing.append(raw)
                continue
            add_candidate(resolved, None, resolved in favorites, "imported")

        history_scores = recent_view_history(
            history_events,
            now=now,
            half_life_days=settings.half_life_days,
            window_days=settings.history_window_days,
        )
        basis = RANK_BASIS_RECENT_VIEWING if history_events else RANK_BASIS_FAVORITES

        ordered_pool = sorted(
            pool,
            key=lambda candidate: (
                0 if candidate.favorite else 1,
                -history_scores.get(candidate.channel_id, ChannelHistory()).score,
                candidate.order,
            ),
        )
        limit = settings.active_channel_limit
        selected = ordered_pool[:limit]
        not_fetched = ordered_pool[limit:]
        for candidate in not_fetched:
            reports.append(
                ChannelReport(
                    channel_id=candidate.channel_id,
                    label=candidate.label,
                    status=CHANNEL_STATUS_NOT_FETCHED,
                    note=(f"beyond active_channel_limit={limit}; raise the limit to fetch it"),
                )
            )

        uploads, observed = await self._fetch_selected(context, selected, reports, missing)

        channel_ranks = {
            candidate.channel_id: ChannelRank(
                channel_id=candidate.channel_id,
                label=candidate.label,
                favorite=candidate.favorite,
            )
            for candidate in pool
        }
        ranked = rank_uploads(dedupe_uploads(uploads), channel_ranks, history_scores, now, basis)

        as_of = max(
            (moment for upload in ranked if (moment := _upload_moment(upload)) is not None),
            default=None,
        )

        unresolved_count = sum(
            1 for report in reports if report.status in {CHANNEL_STATUS_UNRESOLVED}
        )
        failed_count = sum(
            1
            for report in reports
            if report.status == CHANNEL_STATUS_ERROR and report.channel_id is not None
        )
        expected = len(pool) + unresolved_count

        if expected:
            notes.append(f"{observed}/{expected} channels fetched")
        else:
            notes.append(
                "no channels configured; add channel IDs or import a Takeout subscriptions CSV"
            )
        if not_fetched:
            notes.append(f"{len(not_fetched)} not fetched (active_channel_limit={limit})")
        if unresolved_count:
            notes.append(f"{unresolved_count} unresolved; {_RESOLVED_SETUP_HINT}")
        if failed_count:
            notes.append(f"{failed_count} channel feed(s) failed")
        for summary in (subs_summary, history_summary):
            if summary is not None:
                notes.append(_summary_note(summary))
        if subscriptions_imported_at is not None:
            notes.append(f"subscriptions imported {_date(subscriptions_imported_at)}")
        if history_imported_at is not None:
            notes.append(f"watch history imported {_date(history_imported_at)}")

        notes_text = "; ".join(notes)[:500] or None
        return YouTubePayload(
            uploads=ranked,
            channel_reports=tuple(reports),
            history_imported_at=history_imported_at,
            subscriptions_imported_at=subscriptions_imported_at,
            history_window_days=settings.history_window_days,
            rank_basis=basis,
            coverage=Coverage(
                expected=expected,
                observed=observed,
                missing=tuple(missing),
                notes=notes_text,
            ),
            as_of=as_of,
            precision=Precision.INSTANT,
            # From the fetch, not from the newest upload: a channel that posts weekly
            # would otherwise look expired seconds after a successful refresh.
            valid_until=context.clock.now() + timedelta(seconds=VALID_UNTIL_SECONDS),
            provenance=PROVENANCE,
        )

    async def aclose(self) -> None:
        """Release owned resources. Safe to call more than once."""
        self._closed = True

    # ------------------------------------------------------------------ imports

    def _favorite_ids(self, settings: YouTubeSettings) -> frozenset[str]:
        resolved = (resolve_channel_id(raw) for raw in settings.favorite_channel_ids)
        return frozenset(cid for cid in resolved if cid is not None)

    def _import_subscriptions(
        self,
        context: ProviderBuild,
        settings: YouTubeSettings,
        favorites: frozenset[str],
        add_candidate: Callable[[str, str | None, bool, str], None],
        reports: list[ChannelReport],
        missing: list[str],
    ) -> tuple[datetime | None, ImportReport | None]:
        path = settings.takeout_subscriptions_csv
        if not path:
            return None, None
        label = "takeout subscriptions CSV"
        try:
            text = _read_text(context, path, SUBSCRIPTIONS_MAX_BYTES)
            channels, skipped, warnings = parse_subscriptions_csv(text)
        except SideboardError as exc:
            reports.append(
                ChannelReport(
                    channel_id=None,
                    label=label,
                    status=CHANNEL_STATUS_ERROR,
                    note=_error_note(exc),
                )
            )
            missing.append(label)
            return None, None
        summary = subscriptions_report(channels, skipped, warnings)
        if not channels and (skipped or warnings):
            reports.append(
                ChannelReport(
                    channel_id=None,
                    label=label,
                    status=CHANNEL_STATUS_ERROR,
                    note=_join_warnings(summary),
                )
            )
            missing.append(label)
            return _file_mtime(context, path), summary
        for channel in channels:
            _add_imported_channel(add_candidate, channel, favorites)
        return _file_mtime(context, path), summary

    def _import_watch_history(
        self,
        context: ProviderBuild,
        settings: YouTubeSettings,
        reports: list[ChannelReport],
        missing: list[str],
    ) -> tuple[datetime | None, tuple[WatchedEvent, ...], ImportReport | None]:
        path = settings.takeout_watch_history_json
        if not path:
            return None, (), None
        label = "takeout watch history"
        try:
            text = _read_text(context, path, WATCH_HISTORY_MAX_BYTES)
            events, skipped, warnings = parse_watch_history_with_stats(text)
        except SideboardError as exc:
            reports.append(
                ChannelReport(
                    channel_id=None,
                    label=label,
                    status=CHANNEL_STATUS_ERROR,
                    note=_error_note(exc),
                )
            )
            missing.append(label)
            return None, (), None
        summary = watch_history_report(events, skipped, warnings)
        return _file_mtime(context, path), events, summary

    # -------------------------------------------------------------------- fetch

    async def _fetch_selected(
        self,
        context: ProviderBuild,
        selected: Sequence[_Candidate],
        reports: list[ChannelReport],
        missing: list[str],
    ) -> tuple[list[Upload], int]:
        if not selected:
            return [], 0
        http = context.http
        if http is None:
            raise ProviderError("no HTTP reader was granted to the youtube.uploads source")
        outcomes = await asyncio.gather(
            *(self._fetch_channel(http, candidate) for candidate in selected),
            return_exceptions=True,
        )
        uploads: list[Upload] = []
        observed = 0
        failures: list[BaseException] = []
        for candidate, outcome in zip(selected, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                failures.append(outcome)
                reports.append(
                    ChannelReport(
                        channel_id=candidate.channel_id,
                        label=candidate.label,
                        status=CHANNEL_STATUS_ERROR,
                        note=_error_note(outcome),
                    )
                )
                missing.append(candidate.label or candidate.channel_id)
                continue
            uploads.extend(outcome.uploads)
            reports.append(outcome.report)
            observed += 1
        if observed == 0 and failures:
            raise _as_provider_error(failures[0])
        return uploads, observed

    async def _fetch_channel(self, http: HttpReader, candidate: _Candidate) -> _ChannelFetch:
        url = FEED_URL.format(channel_id=candidate.channel_id)
        response = await http.get(url)
        text = response.text()
        parsed: Any = _parse_feed(text)
        entries = list(parsed.get("entries") or [])
        feed = parsed.get("feed") or {}
        feed_title = feed.get("title") if hasattr(feed, "get") else None
        label = candidate.label or (
            feed_title.strip() if isinstance(feed_title, str) and feed_title.strip() else None
        )
        uploads = tuple(
            upload
            for entry in entries
            if (upload := _entry_to_upload(entry, candidate, label)) is not None
        )
        if not uploads and not isinstance(feed_title, str):
            raise ParseError("channel feed could not be parsed as an Atom document")
        newest = max(
            (moment for upload in uploads if (moment := _upload_moment(upload)) is not None),
            default=None,
        )
        return _ChannelFetch(
            uploads=uploads,
            report=ChannelReport(
                channel_id=candidate.channel_id,
                label=label,
                status=CHANNEL_STATUS_OK,
                newest=newest,
                item_count=len(uploads),
                note=None,
            ),
        )


def _parse_feed(text: str) -> Any:
    """Parse feed text with feedparser, refusing anything that looks like a URL.

    ``feedparser.parse`` would fetch a bare URL string; an Atom document never looks
    like one, and a response that does must not turn into an outbound request.
    """
    stripped = text.lstrip()
    if stripped.startswith(("http://", "https://", "file://", "ftp://")):
        raise ParseError("channel feed response was not an Atom document")
    return feedparser.parse(text)


def _entry_to_upload(entry: Any, candidate: _Candidate, label: str | None) -> Upload | None:
    video_id = _text(entry.get("yt_videoid"))
    link = _text(entry.get("link"))
    if not video_id and link:
        video_id = video_id_from_url(link)
    if not video_id:
        return None
    return Upload(
        video_id=video_id,
        channel_id=_text(entry.get("yt_channelid")) or candidate.channel_id,
        channel_label=label,
        title=_text(entry.get("title")) or "(untitled)",
        published=_entry_time(entry, "published_parsed", "published"),
        url=link or f"https://www.youtube.com/watch?v={video_id}",
        updated=_entry_time(entry, "updated_parsed", "updated"),
        favorite=candidate.favorite,
        rank_basis=RANK_BASIS_FAVORITES,
    )


def _entry_time(entry: Any, parsed_key: str, string_key: str) -> datetime | None:
    parsed = entry.get(parsed_key)
    if isinstance(parsed, time.struct_time | tuple) and len(parsed) >= 6:
        try:
            return datetime(*parsed[:6], tzinfo=UTC)
        except TypeError, ValueError:  # pragma: no cover - defensive
            return None
    raw = entry.get(string_key)
    if isinstance(raw, str):
        return parse_iso_datetime(raw)
    return None


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _upload_moment(upload: Upload) -> datetime | None:
    return upload.published or upload.updated


def _read_text(context: ProviderBuild, path: str, max_bytes: int) -> str:
    files: FileReader | None = context.files
    if files is None:
        raise CapabilityError("file access was not granted to this source")
    return files.read_text(path, max_bytes=max_bytes)


def _file_mtime(context: ProviderBuild, path: str) -> datetime | None:
    if context.files is None:
        return None
    info = context.files.stat_path(path)
    if info is None:
        return None
    return datetime.fromtimestamp(info.st_mtime, tz=UTC)


def _add_imported_channel(
    add_candidate: Callable[[str, str | None, bool, str], None],
    channel: ImportedChannel,
    favorites: frozenset[str],
) -> None:
    add_candidate(
        channel.channel_id,
        channel.title,
        channel.channel_id in favorites,
        "subscriptions",
    )


def _as_provider_error(error: BaseException) -> BaseException:
    if isinstance(error, SideboardError):
        return error
    return ProviderError(f"{type(error).__name__}: unexpected channel failure")


def _error_note(error: BaseException) -> str:
    if isinstance(error, SideboardError):
        return f"{error.category}: {error.message}"[:160]
    return f"{type(error).__name__}: unexpected failure"[:160]


def _join_warnings(report: ImportReport) -> str:
    if report.warnings:
        return "; ".join(report.warnings)[:160]
    return f"{report.kind} import produced no usable records"


def _summary_note(report: ImportReport) -> str:
    parts = [f"{report.kind}: {report.accepted} accepted"]
    if report.skipped:
        parts.append(f"{report.skipped} skipped")
    if report.unresolved_channels:
        parts.append(f"{report.unresolved_channels} without channel id")
    return ", ".join(parts)


def _date(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d")


def demo_payload() -> YouTubePayload:
    """Synthetic uploads for ``--demo``. No network, no file access."""
    now = datetime.now(UTC)

    def demo_upload(
        video_id: str,
        channel_id: str,
        label: str,
        title: str,
        hours_ago: float,
        score: float,
        events: int,
        favorite: bool = False,
    ) -> Upload:
        published = now - timedelta(hours=hours_ago)
        return Upload(
            video_id=video_id,
            channel_id=channel_id,
            channel_label=label,
            title=title,
            published=published,
            updated=published,
            url=f"https://www.youtube.com/watch?v={video_id}",
            favorite=favorite,
            recent_view_score=score,
            view_events=events,
            rank_basis=RANK_BASIS_RECENT_VIEWING,
        )

    alpha = "UCdemo0000000000000000aa"
    beta = "UCdemo0000000000000000bb"
    uploads = (
        demo_upload(
            "demo-yt-1", alpha, "Demo Systems", "Demo: a synthetic upload", 3.0, 2.5, 4, True
        ),
        demo_upload("demo-yt-2", alpha, "Demo Systems", "Demo: second channel post", 26.0, 2.5, 4),
        demo_upload("demo-yt-3", beta, "Demo Workshop", "Demo: woodworking clip", 8.0, 1.5, 2),
        demo_upload("demo-yt-4", beta, "Demo Workshop", "Demo: older workshop clip", 70.0, 1.5, 2),
    )
    reports = (
        ChannelReport(
            channel_id=alpha,
            label="Demo Systems",
            status=CHANNEL_STATUS_OK,
            newest=uploads[0].published,
            item_count=2,
        ),
        ChannelReport(
            channel_id=beta,
            label="Demo Workshop",
            status=CHANNEL_STATUS_OK,
            newest=uploads[2].published,
            item_count=2,
        ),
    )
    as_of = max(moment for upload in uploads if (moment := _upload_moment(upload)) is not None)
    return YouTubePayload(
        uploads=uploads,
        channel_reports=reports,
        history_imported_at=now - timedelta(days=1),
        subscriptions_imported_at=now - timedelta(days=1),
        history_window_days=90,
        rank_basis=RANK_BASIS_RECENT_VIEWING,
        coverage=Coverage(expected=2, observed=2, notes="synthetic demo data"),
        as_of=as_of,
        precision=Precision.INSTANT,
        valid_until=as_of + timedelta(seconds=VALID_UNTIL_SECONDS),
        provenance=PROVENANCE,
    )
