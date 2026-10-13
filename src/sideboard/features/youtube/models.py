"""Validated payload models for ``youtube.uploads``.

Missing feed fields stay missing: an absent timestamp or channel label is ``None``,
never a guess. ``rank_basis`` records which evidence ordered the list so a
favorites-only ranking is never presented as viewing-frequency ranking.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ...core.contracts import SourcePayload

__all__ = [
    "CHANNEL_STATUS_ERROR",
    "CHANNEL_STATUS_NOT_FETCHED",
    "CHANNEL_STATUS_OK",
    "CHANNEL_STATUS_UNRESOLVED",
    "RANK_BASIS_FAVORITES",
    "RANK_BASIS_RECENT_VIEWING",
    "ChannelReport",
    "Upload",
    "YouTubePayload",
]

RANK_BASIS_FAVORITES = "favorites-only"
RANK_BASIS_RECENT_VIEWING = "recent-viewing-frequency"

CHANNEL_STATUS_OK = "ok"
CHANNEL_STATUS_UNRESOLVED = "unresolved"
CHANNEL_STATUS_NOT_FETCHED = "not-fetched"
CHANNEL_STATUS_ERROR = "error"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Upload(_Frozen):
    """One video from a channel Atom feed. Absent fields remain unknown."""

    video_id: str
    channel_id: str | None = None
    channel_label: str | None = None
    title: str
    published: datetime | None = None
    url: str
    updated: datetime | None = None
    favorite: bool = False
    recent_view_score: float | None = None
    view_events: int | None = None
    rank_basis: str


class ChannelReport(_Frozen):
    """Per-channel outcome of one refresh, including honest non-fetching."""

    channel_id: str | None = None
    label: str | None = None
    status: str
    newest: datetime | None = None
    item_count: int = 0
    note: str | None = None


class YouTubePayload(SourcePayload):
    """Uploads plus per-channel coverage and import provenance.

    ``history_imported_at`` / ``subscriptions_imported_at`` are file-derived import
    dates (local file modification time), shown so a stale Takeout snapshot is visible.
    """

    uploads: tuple[Upload, ...] = ()
    channel_reports: tuple[ChannelReport, ...] = ()
    history_imported_at: datetime | None = None
    subscriptions_imported_at: datetime | None = None
    history_window_days: int = 90
    rank_basis: str = RANK_BASIS_FAVORITES

    @classmethod
    def schema_id(cls) -> str:
        """Stable payload schema identifier registered on the source spec."""
        return "youtube.uploads"
