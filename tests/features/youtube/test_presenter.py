"""Offline presenter tests: stable IDs, honest footnotes and no exceptions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sideboard.core.contracts import (
    Coverage,
    PresentInput,
    Severity,
    SourceEnvelope,
    ViewContext,
    ViewMode,
    ViewModel,
)
from sideboard.features.youtube.config import YouTubeWidgetSettings
from sideboard.features.youtube.models import (
    CHANNEL_STATUS_ERROR,
    CHANNEL_STATUS_UNRESOLVED,
    RANK_BASIS_FAVORITES,
    RANK_BASIS_RECENT_VIEWING,
    ChannelReport,
    Upload,
    YouTubePayload,
)
from sideboard.features.youtube.presenter import present

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
ALPHA = "UCaaaa0000000000000000aa"
FREQUENCY_FOOTNOTE = "ordered by recent viewing frequency (imported history, 90 d)"
FAVORITES_FOOTNOTE = "favorites-only ordering — no watch history imported"


def _upload(
    video_id: str,
    *,
    hours_ago: float = 3.0,
    favorite: bool = False,
    label: str | None = "Alpha Synthetic Channel",
    score: float | None = None,
    events: int | None = None,
) -> Upload:
    published = NOW - timedelta(hours=hours_ago)
    return Upload(
        video_id=video_id,
        channel_id=ALPHA,
        channel_label=label,
        title=f"Video {video_id}",
        published=published,
        url=f"https://www.youtube.com/watch?v={video_id}",
        updated=published,
        favorite=favorite,
        recent_view_score=score,
        view_events=events,
        rank_basis=RANK_BASIS_FAVORITES,
    )


def _payload(
    uploads: list[Upload] | tuple[Upload, ...] = (),
    reports: tuple[ChannelReport, ...] = (),
    *,
    basis: str = RANK_BASIS_FAVORITES,
    imported_at: datetime | None = None,
    expected: int | None = None,
) -> YouTubePayload:
    count = len(uploads) if expected is None else expected
    return YouTubePayload(
        uploads=tuple(uploads),
        channel_reports=reports,
        rank_basis=basis,
        history_imported_at=imported_at,
        coverage=Coverage(expected=count, observed=count),
    )


def _envelope(payload: YouTubePayload) -> SourceEnvelope:
    return SourceEnvelope(
        source_id="youtube_test",
        payload_schema="youtube.uploads",
        payload_schema_version=1,
        payload=payload,
    )


def _present(
    settings: YouTubeWidgetSettings | None = None,
    payload: YouTubePayload | None = None,
    *,
    mode: ViewMode = ViewMode.COMPACT,
) -> ViewModel:
    return present(
        PresentInput(
            settings=settings or YouTubeWidgetSettings(),
            inputs={"primary": None if payload is None else _envelope(payload)},
            context=ViewContext(mode=mode, now=NOW, width=80, height=24),
        )
    )


def test_missing_payload_returns_status_view() -> None:
    view = _present()
    assert view.status is not None
    assert view.status.severity is Severity.INFO
    assert view.items == ()
    assert view.footnote == "unconfigured"


def test_items_have_stable_ids_urls_and_subtitles() -> None:
    view = _present(payload=_payload([_upload("vid-alpha-1")]))
    assert len(view.items) == 1
    item = view.items[0]
    assert item.id == "yt:vid-alpha-1"
    assert item.url == "https://www.youtube.com/watch?v=vid-alpha-1"
    assert item.title == "Video vid-alpha-1"
    assert item.subtitle == "Alpha Synthetic Channel · 3 h ago"
    assert item.badge is None


def test_favorites_only_filter_and_badge() -> None:
    payload = _payload(
        [
            _upload("vid-fav", favorite=True),
            _upload("vid-plain"),
        ]
    )
    view = _present(YouTubeWidgetSettings(favorites_only=True), payload)
    assert [item.id for item in view.items] == ["yt:vid-fav"]
    assert view.items[0].badge == "fav"
    unfiltered = _present(payload=payload)
    assert [item.id for item in unfiltered.items] == ["yt:vid-fav", "yt:vid-plain"]


def test_limit_is_applied() -> None:
    payload = _payload([_upload(f"vid-{index}") for index in range(4)])
    view = _present(YouTubeWidgetSettings(limit=2), payload)
    assert len(view.items) == 2


def test_footnote_states_recent_viewing_frequency() -> None:
    """The ranking basis is an explanation: the card is a list, the detail view says how."""
    payload = _payload(
        [_upload("vid-alpha-1", score=2.0, events=2)],
        basis=RANK_BASIS_RECENT_VIEWING,
    )
    assert _present(payload=payload).footnote is None
    detail = _present(payload=payload, mode=ViewMode.DETAIL)
    assert detail.footnote == FREQUENCY_FOOTNOTE


def test_footnote_mentions_import_date_when_present() -> None:
    payload = _payload(
        [_upload("vid-alpha-1", score=2.0, events=2)],
        basis=RANK_BASIS_RECENT_VIEWING,
        imported_at=datetime(2026, 10, 12, 8, 0, tzinfo=UTC),
    )
    footnote = _present(payload=payload, mode=ViewMode.DETAIL).footnote
    assert footnote is not None
    assert footnote.startswith(FREQUENCY_FOOTNOTE)
    assert "imported 2026-10-12" in footnote


def test_footnote_states_favorites_only_ordering() -> None:
    payload = _payload([_upload("vid-alpha-1")])
    assert _present(payload=payload).footnote is None
    assert _present(payload=payload, mode=ViewMode.DETAIL).footnote == FAVORITES_FOOTNOTE


def test_no_channels_configured_is_an_honest_status() -> None:
    view = _present(payload=_payload(expected=0))
    assert view.status is not None
    assert view.status.severity is Severity.INFO
    assert "No channels configured" in view.status.headline


def test_detail_mode_lists_channel_problems() -> None:
    reports = (
        ChannelReport(
            channel_id=None,
            label="Handle Setup",
            status=CHANNEL_STATUS_UNRESOLVED,
            note="channel reference could not be resolved",
        ),
        ChannelReport(
            channel_id=None,
            label="broken channel",
            status=CHANNEL_STATUS_ERROR,
            note="network: request failed",
        ),
    )
    payload = _payload([_upload("vid-alpha-1")], reports)
    view = _present(payload=payload, mode=ViewMode.DETAIL)
    assert view.table is not None
    assert len(view.table.rows) == 2
    assert view.table.rows[0].cells["channel"] == "Handle Setup"
    assert view.table.rows[0].severity is Severity.WARNING
    assert view.table.rows[1].severity is Severity.ERROR
    assert view.table.footnote is not None
    assert "channel ID" in view.table.footnote


def test_compact_mode_leaves_problems_to_the_status_line() -> None:
    reports = (
        ChannelReport(
            channel_id=None,
            label="Handle Setup",
            status=CHANNEL_STATUS_UNRESOLVED,
            note="channel reference could not be resolved",
        ),
    )
    payload = _payload([_upload("vid-alpha-1")], reports)
    view = _present(payload=payload)
    assert view.table is None
    assert view.items


def test_saved_and_read_state_come_from_the_context() -> None:
    payload = _payload([_upload("vid-alpha-1")])
    item_id = "yt:vid-alpha-1"
    view = present(
        PresentInput(
            settings=YouTubeWidgetSettings(),
            inputs={"primary": _envelope(payload)},
            context=ViewContext(
                now=NOW,
                width=80,
                height=24,
                saved_ids=frozenset({item_id}),
                read_ids=frozenset({item_id}),
            ),
        )
    )
    assert view.items[0].saved is True
    assert view.items[0].read is True


def test_group_by_channel_sorts_by_channel_label() -> None:
    payload = _payload(
        [
            _upload("vid-a1", label="Alpha Synthetic Channel"),
            _upload("vid-b1", label="Beta Synthetic Channel"),
        ]
    )
    view = _present(YouTubeWidgetSettings(group_by_channel=True), payload)
    assert [item.id for item in view.items] == ["yt:vid-a1", "yt:vid-b1"]
    assert view.items[0].category == "Alpha Synthetic Channel"
