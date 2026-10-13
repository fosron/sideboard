"""Local Google Takeout parsing for the YouTube feature.

This module is file parsing only: it never opens a network connection, never reads
a browser cookie store and never calls the YouTube Data API. Everything parsed is
treated as untrusted data.

Documented size caps:

* subscriptions CSV — :data:`SUBSCRIPTIONS_MAX_BYTES` (4 MiB)
* watch history JSON — :data:`WATCH_HISTORY_MAX_BYTES` (32 MiB). The watch history is
  handed to ``json.loads`` only when the decoded text is within
  :data:`WATCH_HISTORY_MAX_CHARS`; larger exports raise :class:`ParseError` instead
  of being streamed.

Imports are snapshots, not continuous account synchronization. Repeated imports of
the same bytes produce identical results (idempotent).
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ...core.errors import ParseError

__all__ = [
    "SUBSCRIPTIONS_MAX_BYTES",
    "WATCH_HISTORY_MAX_BYTES",
    "WATCH_HISTORY_MAX_CHARS",
    "ImportReport",
    "ImportedChannel",
    "WatchedEvent",
    "parse_iso_datetime",
    "parse_subscriptions_csv",
    "parse_watch_history",
    "parse_watch_history_with_stats",
    "subscriptions_report",
    "video_id_from_url",
    "watch_history_report",
]

SUBSCRIPTIONS_MAX_BYTES = 4 * 1024 * 1024
WATCH_HISTORY_MAX_BYTES = 32 * 1024 * 1024
WATCH_HISTORY_MAX_CHARS = WATCH_HISTORY_MAX_BYTES

_ID_HEADERS = ("channelid", "youtubechannelid", "id")
_URL_HEADERS = ("channelurl", "url")
_TITLE_HEADERS = ("channeltitle", "title", "name")

_REMOVED_TITLE_PREFIXES = (
    "watched a video that has been removed",
    "watched a video that has been deleted",
    "watched a video that is unavailable",
    "watched a private video",
)

_VIDEO_ID_RE = re.compile(r"(?:[?&]v=|/shorts/|/live/|youtu\.be/)([A-Za-z0-9_-]{6,})")
_CHANNEL_URL_RE = re.compile(r"/channel/(UC[\w-]{10,})")


@dataclass(frozen=True, slots=True)
class ImportedChannel:
    """One subscription row with the minimum fields we keep."""

    channel_id: str
    title: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class WatchedEvent:
    """One watch-history record. ``video_id``/``channel_id`` may be unknown."""

    title: str
    video_id: str | None = None
    channel_id: str | None = None
    channel_name: str | None = None
    watched_at: datetime | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Honest summary of one local import: what was accepted and what was skipped."""

    kind: str
    records: int
    accepted: int
    skipped: int
    unresolved_channels: int
    warnings: tuple[str, ...] = ()


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO-8601-ish timestamp; naive values are treated as UTC.

    Returns ``None`` for unparseable input rather than inventing an instant.
    """
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def video_id_from_url(url: str | None) -> str | None:
    """Extract a plausible video ID from a watch/short/live URL, else ``None``."""
    if not url:
        return None
    match = _VIDEO_ID_RE.search(url)
    return match.group(1) if match else None


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value.strip().lower())


def _column_index(header: Sequence[str], names: Sequence[str]) -> int | None:
    normalized = [_normalize_header(cell) for cell in header]
    for name in names:
        if name in normalized:
            return normalized.index(name)
    return None


def _cell(row: Sequence[str], index: int | None) -> str | None:
    if index is None or index >= len(row):
        return None
    value = row[index].strip()
    return value or None


def _read_rows(text: str) -> list[list[str]]:
    """Read CSV rows, trying comma then semicolon so both export variants parse."""
    try:
        for delimiter in (",", ";"):
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
            if rows and _column_index(rows[0], _ID_HEADERS) is not None:
                return rows
        return list(csv.reader(io.StringIO(text), delimiter=","))
    except csv.Error as exc:
        raise ParseError(f"subscriptions file is not parseable CSV: {exc}") from exc


def parse_subscriptions_csv(
    text: str,
) -> tuple[tuple[ImportedChannel, ...], int, tuple[str, ...]]:
    """Parse a Takeout ``subscriptions.csv`` snapshot.

    Handles both known variants: comma or semicolon delimiters, an optional BOM,
    quoted fields, and ``Channel Id`` / ``Channel ID`` / ``channel_id`` header
    spellings in any column order.

    Returns ``(channels, skipped_count, warnings)``. Rows without a channel ID are
    counted in ``skipped_count`` and described in ``warnings`` instead of being
    silently dropped. Duplicate channel IDs are kept once.
    """
    cleaned = text.lstrip("\ufeff")
    if not cleaned.strip():
        return (), 0, ("subscriptions file is empty",)
    rows = _read_rows(cleaned)
    if not rows:
        return (), 0, ("subscriptions file has no rows",)
    header, *data_rows = rows
    id_index = _column_index(header, _ID_HEADERS)
    if id_index is None:
        skipped = sum(1 for row in data_rows if any(cell.strip() for cell in row))
        return (
            (),
            skipped,
            (f"no channel id column in the subscriptions header: {', '.join(header[:6])}",),
        )
    url_index = _column_index(header, _URL_HEADERS)
    title_index = _column_index(header, _TITLE_HEADERS)
    channels: list[ImportedChannel] = []
    seen: set[str] = set()
    skipped = 0
    duplicates = 0
    for row in data_rows:
        if not any(cell.strip() for cell in row):
            continue
        channel_id = _cell(row, id_index)
        if channel_id is None:
            skipped += 1
            continue
        if channel_id in seen:
            duplicates += 1
            continue
        seen.add(channel_id)
        channels.append(
            ImportedChannel(
                channel_id=channel_id,
                title=_cell(row, title_index),
                url=_cell(row, url_index),
            )
        )
    warnings: list[str] = []
    if skipped:
        warnings.append(f"{skipped} subscriptions rows had no channel id")
    if duplicates:
        warnings.append(f"{duplicates} duplicate subscription rows ignored")
    return tuple(channels), skipped, tuple(warnings)


def parse_watch_history(
    text: str, *, max_chars: int = WATCH_HISTORY_MAX_CHARS
) -> tuple[WatchedEvent, ...]:
    """Parse Takeout ``watch-history.json`` into watched events.

    Records without a video (for example ``Watched a video that has been removed``)
    and malformed records are skipped. ``titleUrl`` may be absent, in which case the
    event keeps ``video_id=None`` rather than guessing.
    """
    events, _, _ = parse_watch_history_with_stats(text, max_chars=max_chars)
    return events


def parse_watch_history_with_stats(
    text: str, *, max_chars: int = WATCH_HISTORY_MAX_CHARS
) -> tuple[tuple[WatchedEvent, ...], int, tuple[str, ...]]:
    """Like :func:`parse_watch_history`, also returning skipped count and warnings."""
    if len(text) > max_chars:
        raise ParseError(f"watch history exceeds the {max_chars} character import cap")
    if not text.strip():
        return (), 0, ("watch history file is empty",)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"watch history is not valid JSON: {exc.msg}") from exc
    if not isinstance(document, list):
        raise ParseError("watch history JSON is not an array of records")
    events: list[WatchedEvent] = []
    skipped = 0
    for record in document:
        if not isinstance(record, dict):
            skipped += 1
            continue
        raw_title = record.get("title")
        title = raw_title.strip() if isinstance(raw_title, str) else ""
        if not title or _is_unavailable_title(title):
            skipped += 1
            continue
        raw_url = record.get("titleUrl")
        url = raw_url.strip() if isinstance(raw_url, str) and raw_url.strip() else None
        channel_id, channel_name = _channel_from_subtitles(record.get("subtitles"))
        events.append(
            WatchedEvent(
                title=title,
                video_id=video_id_from_url(url),
                channel_id=channel_id,
                channel_name=channel_name,
                watched_at=_record_time(record),
                url=url,
            )
        )
    warnings: list[str] = []
    no_timestamp = sum(1 for event in events if event.watched_at is None)
    if skipped:
        warnings.append(f"{skipped} watch-history records had no usable video")
    if no_timestamp:
        warnings.append(f"{no_timestamp} watch-history records had no usable timestamp")
    if not events:
        warnings.append("watch history contained no usable records")
    return tuple(events), skipped, tuple(warnings)


def subscriptions_report(
    channels: Sequence[ImportedChannel], skipped: int, warnings: Sequence[str]
) -> ImportReport:
    """Summarise a subscriptions CSV parse."""
    return ImportReport(
        kind="subscriptions",
        records=len(channels) + skipped,
        accepted=len(channels),
        skipped=skipped,
        unresolved_channels=0,
        warnings=tuple(warnings),
    )


def watch_history_report(
    events: Sequence[WatchedEvent], skipped: int, warnings: Sequence[str]
) -> ImportReport:
    """Summarise a watch-history parse."""
    return ImportReport(
        kind="watch-history",
        records=len(events) + skipped,
        accepted=len(events),
        skipped=skipped,
        unresolved_channels=sum(1 for event in events if event.channel_id is None),
        warnings=tuple(warnings),
    )


def _is_unavailable_title(title: str) -> bool:
    lowered = title.strip().lower()
    return any(lowered.startswith(prefix) for prefix in _REMOVED_TITLE_PREFIXES)


def _record_time(record: dict[str, object]) -> datetime | None:
    raw = record.get("time")
    if not isinstance(raw, str):
        return None
    return parse_iso_datetime(raw)


def _channel_from_subtitles(subtitles: object) -> tuple[str | None, str | None]:
    if not isinstance(subtitles, list):
        return None, None
    for entry in subtitles:
        if not isinstance(entry, dict):
            continue
        raw_url = entry.get("url")
        channel_id: str | None = None
        if isinstance(raw_url, str):
            match = _CHANNEL_URL_RE.search(raw_url)
            if match:
                channel_id = match.group(1)
        raw_name = entry.get("name")
        channel_name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        return channel_id, channel_name
    return None, None
