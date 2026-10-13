"""Pure presentation for the ``reddit.list`` widget.

No I/O, no timers, no database access: a validated snapshot and the view context go
in, a :class:`~sideboard.core.contracts.ViewModel` comes out.

Honesty rules implemented here:

* link-only mode renders deliberate community links plus a status that explains the
  conditional access, never an empty "broken" card
* score/comment metadata is shown **only** when the payload carries it
* ranking uses freshness and configured weights over genuinely provided metadata; the
  provider's feed order is the stable tie-break
* a failed community is reported, never replaced with an invented thread or a zero
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ...core.contracts import (
    Item,
    PresentInput,
    Privacy,
    Severity,
    SourceEnvelope,
    StatusView,
    TableColumn,
    TableRow,
    TableView,
    ViewModel,
)
from ...core.errors import ErrorCategory, SourceErrorInfo
from ...core.present import status_from_envelope
from .config import RedditListSettings
from .models import CommunityReport, RedditPayload, Thread

__all__ = ["RankWeights", "present", "rank_threads", "thread_rank"]

REDDIT_BASE = "https://www.reddit.com"

RSS_PROVENANCE_PREFIX = "reddit-public-rss"

_NO_SCORES_RSS = "public RSS · no scores available"

DETAIL_ROW_LIMIT = 60


@dataclass(frozen=True, slots=True)
class RankWeights:
    """Configured ranking inputs. Missing metadata contributes nothing."""

    community_weights: Mapping[str, float] = field(default_factory=dict)
    half_life_hours: float = 24.0
    score_weight: float = 0.25
    comment_weight: float = 0.15


def thread_rank(thread: Thread, *, now: datetime, weights: RankWeights) -> float:
    """Ranking score for one thread.

    ``freshness`` halves every ``half_life_hours``; a thread without a published time
    gets no freshness credit (it sorts after dated entries instead of being treated as
    new). ``score``/``comment_count`` add a small logarithmic term **only when the
    source reported them**; RSS contributes neither term.
    """
    if thread.published is None:
        freshness = 0.0
    else:
        age_hours = max(0.0, (now - thread.published).total_seconds() / 3600.0)
        freshness = 0.5 ** (age_hours / max(weights.half_life_hours, 1e-6))
    community = max(0.0, float(weights.community_weights.get(thread.community, 1.0)))
    engagement = 1.0
    if thread.score is not None:
        engagement += weights.score_weight * math.log1p(max(0, thread.score))
    if thread.comment_count is not None:
        engagement += weights.comment_weight * math.log1p(max(0, thread.comment_count))
    return community * freshness * engagement


def rank_threads(
    threads: Iterable[Thread], *, now: datetime, weights: RankWeights
) -> tuple[Thread, ...]:
    """Order threads by :func:`thread_rank`, keeping the provider's order for ties."""
    return tuple(sorted(threads, key=lambda thread: -thread_rank(thread, now=now, weights=weights)))


def present(requested: PresentInput) -> ViewModel:
    """Map one bound ``reddit.threads`` envelope into a standard view."""
    settings = _settings(requested.settings)
    envelope = _envelope(requested)
    error = envelope.error if envelope is not None else None
    mode = error.detail.get("mode") if error is not None else None
    if mode == "disabled":
        return _disabled_view()
    if mode == "links" or _is_link_only(error):
        return _link_only_view(settings, envelope)
    payload = envelope.payload if envelope is not None else None
    if isinstance(payload, RedditPayload):
        return _threads_view(settings, payload, requested)
    return _unavailable_view(settings, envelope, requested)


# --------------------------------------------------------------------------- #
# Modes without a payload
# --------------------------------------------------------------------------- #


def _disabled_view() -> ViewModel:
    return ViewModel(
        status=StatusView(
            severity=Severity.INFO,
            headline="Reddit is disabled",
            detail="This Reddit source is switched off in configuration; no request is made.",
            hints=('Set mode = "links" for community links, or "rss" for a permitted feed.',),
        ),
        footnote="disabled · no request made",
    )


def _link_only_view(settings: RedditListSettings, envelope: SourceEnvelope | None) -> ViewModel:
    communities, truncated = _link_communities(settings, envelope)
    return ViewModel(
        items=_link_items(communities),
        status=StatusView(
            severity=Severity.INFO,
            headline="Link-only mode",
            detail=(
                "Reddit access is conditional. Sideboard fetches no Reddit feed until you "
                "validate a permitted feed from this Mac."
            ),
            hints=(
                'Validate a permitted feed, then set mode = "rss" in this source\'s settings.',
                "Scores and comment counts are not available on a link-only card.",
            ),
        ),
        footnote=(
            "link-only · no Reddit feed is fetched"
            + (f" · showing {len(communities)} communities" if truncated else "")
        ),
    )


def _unavailable_view(
    settings: RedditListSettings, envelope: SourceEnvelope | None, requested: PresentInput
) -> ViewModel:
    communities, truncated = _link_communities(settings, envelope)
    error = envelope.error if envelope is not None else None
    if error is not None and error.category in {
        ErrorCategory.BLOCKED,
        ErrorCategory.UNAUTHORIZED,
        ErrorCategory.RATE_LIMITED,
    }:
        status = StatusView(
            severity=Severity.WARNING,
            headline="Reddit feed unavailable",
            detail=error.message[:200],
            hints=(
                "Authorization may be required. No access evasion is attempted.",
                'Validate a permitted feed from this Mac before setting mode = "rss".',
            ),
        )
    else:
        status = status_from_envelope(
            envelope, offline=requested.context.offline, demo=requested.context.demo
        ) or StatusView(
            severity=Severity.WARNING,
            headline="No Reddit data yet",
            detail="Waiting for the first successful refresh.",
        )
    return ViewModel(
        items=_link_items(communities),
        status=status,
        footnote=(
            "link fallback · no Reddit feed is currently fetched"
            + (f" · showing {len(communities)} communities" if truncated else "")
        ),
    )


def _link_communities(
    settings: RedditListSettings, envelope: SourceEnvelope | None
) -> tuple[list[str], bool]:
    """Communities to link: the last payload's coverage, else the widget's own list."""
    payload = envelope.payload if envelope is not None else None
    if isinstance(payload, RedditPayload) and payload.community_reports:
        names = [report.community for report in payload.community_reports]
    else:
        names = list(settings.communities)
    truncated = len(names) > settings.max_communities
    return names[: settings.max_communities], truncated


def _link_items(communities: Iterable[str]) -> tuple[Item, ...]:
    return tuple(
        Item(
            id=f"reddit:link:{name}",
            title=f"r/{name}",
            subtitle="Open this community on reddit.com",
            url=f"{REDDIT_BASE}/r/{name}/",
            privacy=Privacy.PUBLIC,
        )
        for name in communities
    )


# --------------------------------------------------------------------------- #
# Collected threads
# --------------------------------------------------------------------------- #


def _threads_view(
    settings: RedditListSettings, payload: RedditPayload, requested: PresentInput
) -> ViewModel:
    context = requested.context
    weights = RankWeights(
        community_weights=dict(settings.community_weights),
        half_life_hours=settings.half_life_hours,
        score_weight=settings.score_weight,
        comment_weight=settings.comment_weight,
    )
    ordered = rank_threads(payload.threads, now=context.now, weights=weights)
    status = _coverage_status(payload)
    subtitle = "public RSS · top/day order collected" if _is_rss(payload) else None
    if not context.compact:
        table = _detail_table(ordered, payload, settings)
        return ViewModel(subtitle=subtitle, status=status, table=table, privacy=Privacy.PUBLIC)
    shown = ordered[: settings.max_items]
    return ViewModel(
        subtitle=subtitle,
        status=status,
        items=tuple(_item(thread, settings) for thread in shown),
        footnote=_compact_footnote(payload, shown=len(shown), total=len(ordered)),
        privacy=Privacy.PUBLIC,
    )


def _item(thread: Thread, settings: RedditListSettings) -> Item:
    subtitle_parts: list[str] = []
    if settings.show_community:
        subtitle_parts.append(f"r/{thread.community}")
    if thread.author:
        subtitle_parts.append(thread.author.lstrip("/"))
    meta: list[tuple[str, str]] = []
    if thread.score is not None:
        meta.append(("score", f"{thread.score:,}"))
    if thread.comment_count is not None:
        meta.append(("comments", f"{thread.comment_count:,}"))
    return Item(
        id=f"reddit:{thread.id}",
        title=thread.title,
        subtitle=" · ".join(subtitle_parts) or None,
        url=thread.url,
        published_at=thread.published,
        meta=tuple(meta),
        privacy=Privacy.PUBLIC,
    )


def _coverage_status(payload: RedditPayload) -> StatusView | None:
    missing = payload.coverage.missing
    if not missing:
        return None
    listed = ", ".join(f"r/{name}" for name in missing[:4])
    return StatusView(
        severity=Severity.WARNING,
        headline="Partial Reddit coverage",
        detail=f"No entries from: {listed}. Those communities are missing, not empty.",
        hints=("Revalidate access before adding more communities.",),
    )


def _compact_footnote(payload: RedditPayload, *, shown: int, total: int) -> str:
    parts: list[str] = []
    if not _has_scores(payload.threads):
        parts.append(_NO_SCORES_RSS if _is_rss(payload) else "no scores from this source")
    parts.append(f"ranked by freshness + configured weights · {shown} of {total}")
    if payload.coverage.missing:
        parts.append(
            "unavailable: " + ", ".join(f"r/{name}" for name in payload.coverage.missing[:3])
        )
    return " · ".join(parts)


def _detail_table(
    ordered: tuple[Thread, ...], payload: RedditPayload, settings: RedditListSettings
) -> TableView:
    limited = ordered[:DETAIL_ROW_LIMIT]
    show_scores = any(thread.score is not None for thread in limited)
    show_comments = any(thread.comment_count is not None for thread in limited)
    columns: list[TableColumn] = [
        TableColumn(id="community", label="Community", width=14),
        TableColumn(id="thread", label="Thread"),
        TableColumn(id="published", label="Published", width=16),
    ]
    if show_scores:
        columns.append(TableColumn(id="score", label="Score", align="right", width=7))
    if show_comments:
        columns.append(TableColumn(id="comments", label="Comments", align="right", width=8))

    rows: list[TableRow] = []
    groups: list[tuple[str, tuple[str, ...]]] = []
    for community in _community_order(limited, payload, settings):
        group_rows: list[str] = []
        for thread in limited:
            if thread.community != community:
                continue
            cells: dict[str, str] = {
                "community": f"r/{community}",
                "thread": thread.title,
                "published": _short_time(thread.published),
            }
            if show_scores:
                cells["score"] = f"{thread.score:,}" if thread.score is not None else "—"
            if show_comments:
                cells["comments"] = (
                    f"{thread.comment_count:,}" if thread.comment_count is not None else "—"
                )
            row_id = f"reddit:{thread.id}"
            rows.append(TableRow(id=row_id, cells=cells, url=thread.url))
            group_rows.append(row_id)
        if group_rows:
            groups.append((f"r/{community}", tuple(group_rows)))

    report_rows: list[str] = []
    for report in payload.community_reports:
        row_id = f"reddit:feed:{report.community}"
        cells = {
            "community": f"r/{report.community}",
            "thread": f"feed {report.status}" + (f" · {report.note}" if report.note else ""),
            "published": "—",
        }
        if show_scores:
            cells["score"] = "—"
        if show_comments:
            cells["comments"] = "—"
        rows.append(TableRow(id=row_id, cells=cells, severity=_report_severity(report), url=None))
        report_rows.append(row_id)
    if report_rows:
        groups.append(("Feeds", tuple(report_rows)))

    footnote_parts: list[str] = []
    if not show_scores:
        footnote_parts.append(_NO_SCORES_RSS if _is_rss(payload) else "no scores from this source")
    if payload.community_reports:
        footnote_parts.append(
            "feeds: "
            + ", ".join(f"r/{r.community} {r.status}" for r in payload.community_reports[:6])
        )
    footnote_parts.append("ranked by freshness + configured weights")
    if len(ordered) > len(limited):
        footnote_parts.append(f"{len(limited)} of {len(ordered)} threads")
    return TableView(
        columns=tuple(columns),
        rows=tuple(rows),
        groups=tuple(groups),
        truncated=len(ordered) > len(limited),
        footnote=" · ".join(footnote_parts),
    )


def _community_order(
    ordered: tuple[Thread, ...], payload: RedditPayload, settings: RedditListSettings
) -> list[str]:
    order = [report.community for report in payload.community_reports] or list(settings.communities)
    known = set(order)
    extras = sorted({thread.community for thread in ordered} - known)
    return order + extras


def _report_severity(report: CommunityReport) -> Severity:
    if report.status == "ok":
        return Severity.OK
    if report.status == "blocked":
        return Severity.WARNING
    if report.status == "error":
        return Severity.ERROR
    return Severity.INFO


def _short_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(UTC).strftime("%d %b %H:%M")


def _has_scores(threads: Iterable[Thread]) -> bool:
    return any(thread.score is not None for thread in threads)


def _is_rss(payload: RedditPayload) -> bool:
    return payload.provenance.startswith(RSS_PROVENANCE_PREFIX)


def _is_link_only(error: SourceErrorInfo | None) -> bool:
    """Fallback for an envelope stored before the structured ``mode`` detail existed."""
    return bool(
        error is not None
        and error.category is ErrorCategory.BLOCKED
        and "link-only" in error.message
    )


# --------------------------------------------------------------------------- #
# Input resolution
# --------------------------------------------------------------------------- #


def _settings(raw: object) -> RedditListSettings:
    if isinstance(raw, RedditListSettings):  # pragma: no cover - host guarantees the type
        return raw
    return RedditListSettings()


def _envelope(requested: PresentInput) -> SourceEnvelope | None:
    """Resolve the bound envelope.

    ``PresentInput.envelope``/``payload`` look up by *binding name*, while the host
    currently hands presenters a mapping keyed by *source instance id*. Accept either
    shape so this presenter is correct under both conventions.
    """
    envelope = requested.envelope("primary")
    if envelope is not None:
        return envelope
    for candidate in requested.inputs.values():
        if candidate is not None:
            return candidate
    return None
