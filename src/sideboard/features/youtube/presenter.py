"""Pure presentation of the YouTube uploads payload.

No fetching, no timers, no database: this maps validated uploads plus widget
settings into a standard view model. Ordering and its evidence are stated in the
footnote; unresolved channels and import failures are surfaced in detail mode.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from ...core.contracts import (
    Item,
    ItemState,
    PresentInput,
    Severity,
    StatusView,
    TableColumn,
    TableRow,
    TableView,
    ViewContext,
    ViewMode,
    ViewModel,
)
from .config import YouTubeWidgetSettings
from .models import (
    CHANNEL_STATUS_ERROR,
    CHANNEL_STATUS_UNRESOLVED,
    RANK_BASIS_RECENT_VIEWING,
    ChannelReport,
    Upload,
    YouTubePayload,
)

__all__ = ["present"]

_SETUP_HINT = (
    "Supply a UC… channel ID or import a Google Takeout subscriptions CSV; "
    "handles are not resolved by scraping."
)
_MAX_PROBLEM_ROWS = 50


def present(data: PresentInput) -> ViewModel:
    """Map one ``youtube.uploads`` snapshot into a compact or detail view."""
    settings = _settings(data.settings)
    envelope = data.envelope("primary")
    payload = envelope.payload if envelope is not None else None
    if not isinstance(payload, YouTubePayload):
        return _no_payload_view(has_envelope=envelope is not None)

    uploads = payload.uploads
    if settings.favorites_only:
        uploads = tuple(upload for upload in uploads if upload.favorite)
    visible = uploads[: settings.limit]
    problems = tuple(
        report
        for report in payload.channel_reports
        if report.status in {CHANNEL_STATUS_UNRESOLVED, CHANNEL_STATUS_ERROR}
    )

    if data.context.mode is ViewMode.DETAIL and problems:
        return _problems_view(payload, problems)

    if not visible:
        return _empty_view(payload, settings)

    items = tuple(_item(upload, settings, data.context) for upload in visible)
    if settings.group_by_channel:
        items = tuple(
            item
            for _, item in sorted(
                enumerate(items),
                key=lambda pair: (pair[1].category or "", pair[0]),
            )
        )
    return ViewModel(
        items=items,
        footnote=_ranking_footnote(payload),
    )


def _settings(raw: BaseModel) -> YouTubeWidgetSettings:
    if isinstance(raw, YouTubeWidgetSettings):
        return raw
    return YouTubeWidgetSettings.model_validate(raw)


def _no_payload_view(*, has_envelope: bool) -> ViewModel:
    if not has_envelope:
        return ViewModel(
            status=StatusView(
                severity=Severity.INFO,
                headline="Not configured",
                detail="No youtube.uploads source is bound to this widget.",
                hints=("Bind a youtube.uploads source in config.toml.",),
            ),
            footnote="unconfigured",
        )
    return ViewModel(
        status=StatusView(
            severity=Severity.WARNING,
            headline="No data yet",
            detail="The source has not produced a payload yet.",
            hints=("Waiting for the first successful refresh.",),
        ),
        footnote="no data",
    )


def _empty_view(payload: YouTubePayload, settings: YouTubeWidgetSettings) -> ViewModel:
    footnote = _ranking_footnote(payload)
    if settings.favorites_only and payload.uploads:
        return ViewModel(
            status=StatusView(
                severity=Severity.INFO,
                headline="No favorite uploads",
                detail="The favorites-only filter matched nothing in the current feed.",
            ),
            footnote=footnote,
        )
    expected = payload.coverage.expected
    if expected == 0:
        return ViewModel(
            status=StatusView(
                severity=Severity.INFO,
                headline="No channels configured",
                detail="No channel IDs and no imported subscription list are configured.",
                hints=(_SETUP_HINT,),
            ),
            footnote=footnote,
        )
    problems = [
        report
        for report in payload.channel_reports
        if report.status in {CHANNEL_STATUS_UNRESOLVED, CHANNEL_STATUS_ERROR}
    ]
    if problems:
        first = problems[0]
        return ViewModel(
            status=StatusView(
                severity=Severity.WARNING,
                headline="No uploads available",
                detail=f"{first.label or first.channel_id or 'channel'}: {first.note or first.status}"[
                    :200
                ],
                hints=(_SETUP_HINT,),
            ),
            footnote=footnote,
        )
    return ViewModel(
        status=StatusView(
            severity=Severity.INFO,
            headline="No uploads found",
            detail="The configured channels returned no videos.",
        ),
        footnote=footnote,
    )


def _problems_view(payload: YouTubePayload, problems: tuple[ChannelReport, ...]) -> ViewModel:
    rows: list[TableRow] = []
    for index, report in enumerate(problems[:_MAX_PROBLEM_ROWS]):
        key = report.channel_id or report.label or f"report-{index}"
        rows.append(
            TableRow(
                id=f"yt-report:{key}",
                cells={
                    "channel": report.label or report.channel_id or "—",
                    "status": report.status,
                    "note": report.note or "",
                },
                severity=(
                    Severity.ERROR if report.status == CHANNEL_STATUS_ERROR else Severity.WARNING
                ),
            )
        )
    table = TableView(
        columns=(
            TableColumn(id="channel", label="Channel"),
            TableColumn(id="status", label="Status"),
            TableColumn(id="note", label="Note"),
        ),
        rows=tuple(rows),
        truncated=len(problems) > _MAX_PROBLEM_ROWS,
        footnote=_SETUP_HINT,
    )
    return ViewModel(table=table, footnote=_ranking_footnote(payload))


def _item(upload: Upload, settings: YouTubeWidgetSettings, context: ViewContext) -> Item:
    item_id = f"yt:{upload.video_id}"
    state = context.item_states.get(item_id, ItemState())
    parts: list[str] = []
    if settings.show_channel and upload.channel_label:
        parts.append(upload.channel_label)
    parts.append(_relative_age(upload.published, context.now))
    return Item(
        id=item_id,
        title=upload.title,
        subtitle=" · ".join(parts),
        url=upload.url,
        category=upload.channel_label,
        published_at=upload.published,
        saved=item_id in context.saved_ids or state.saved,
        read=item_id in context.read_ids or state.read,
        badge="fav" if upload.favorite else None,
    )


def _relative_age(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "date unknown"
    published = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    age = (reference - published).total_seconds()
    if age < 90:
        return "just now"
    if age < 3600:
        return f"{int(age // 60)} min ago"
    if age < 48 * 3600:
        return f"{int(age // 3600)} h ago"
    if age < 14 * 86400:
        return f"{int(age // 86400)} d ago"
    return published.strftime("%Y-%m-%d")


def _ranking_footnote(payload: YouTubePayload) -> str:
    if payload.rank_basis == RANK_BASIS_RECENT_VIEWING:
        text = (
            "ordered by recent viewing frequency "
            f"(imported history, {payload.history_window_days} d)"
        )
        if payload.history_imported_at is not None:
            imported = payload.history_imported_at.astimezone(UTC).strftime("%Y-%m-%d")
            text = f"{text} · imported {imported}"
        return text
    return "favorites-only ordering — no watch history imported"
