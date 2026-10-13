"""Pure ordering and decay math for the YouTube uploads feature.

The documented ordering is: favorites first, then recent viewing frequency
(decayed imported watch events), then upload recency, with ``video_id`` as the
final deterministic tie-break. Every function here takes explicit arguments and
performs no I/O, so the ordering is unit-testable without a provider.

The decay for one viewing event is ``2 ** (-age_days / half_life_days)`` over the
configured trailing window. This is a *recent viewing frequency*, never a watch
duration claim.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .models import Upload

__all__ = [
    "DEFAULT_HALF_LIFE_DAYS",
    "DEFAULT_HISTORY_WINDOW_DAYS",
    "ChannelHistory",
    "ChannelRank",
    "ViewEventLike",
    "decayed_weight",
    "dedupe_uploads",
    "rank_uploads",
    "recent_view_history",
]

DEFAULT_HALF_LIFE_DAYS = 30.0
DEFAULT_HISTORY_WINDOW_DAYS = 90


class ViewEventLike(Protocol):
    """Structural shape of one imported watch event used for scoring."""

    @property
    def channel_id(self) -> str | None: ...

    @property
    def watched_at(self) -> datetime | None: ...


@dataclass(frozen=True, slots=True)
class ChannelHistory:
    """Aggregated viewing evidence for one channel."""

    score: float = 0.0
    events: int = 0


@dataclass(frozen=True, slots=True)
class ChannelRank:
    """Explicit channel-level ranking inputs."""

    channel_id: str
    label: str | None = None
    favorite: bool = False


def decayed_weight(age_days: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """One viewing event's contribution: ``2 ** (-age_days / half_life_days)``.

    Ages below zero (clock skew or future timestamps) weigh as a fresh event.
    """
    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    return 2.0 ** (-max(0.0, age_days) / half_life_days)


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def recent_view_history(
    events: Sequence[ViewEventLike],
    *,
    now: datetime,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    window_days: int = DEFAULT_HISTORY_WINDOW_DAYS,
) -> dict[str, ChannelHistory]:
    """Sum decayed weights per channel over the trailing ``window_days``.

    Events without a channel ID or without a usable timestamp contribute nothing:
    an unknown instant cannot be placed inside the window. Nothing is invented.
    """
    reference = _as_utc(now)
    cutoff = reference - timedelta(days=window_days)
    scores: dict[str, float] = {}
    counts: dict[str, int] = {}
    for event in events:
        channel_id = event.channel_id
        watched_at = event.watched_at
        if not channel_id or watched_at is None:
            continue
        moment = _as_utc(watched_at)
        if moment < cutoff:
            continue
        age_days = max(0.0, (reference - moment).total_seconds() / 86400.0)
        scores[channel_id] = scores.get(channel_id, 0.0) + decayed_weight(age_days, half_life_days)
        counts[channel_id] = counts.get(channel_id, 0) + 1
    return {
        channel_id: ChannelHistory(score=score, events=counts[channel_id])
        for channel_id, score in scores.items()
    }


def dedupe_uploads(uploads: Sequence[Upload]) -> tuple[Upload, ...]:
    """Keep one entry per ``video_id`` (first wins); OR the favorite flag across copies."""
    seen: dict[str, Upload] = {}
    for upload in uploads:
        existing = seen.get(upload.video_id)
        if existing is None:
            seen[upload.video_id] = upload
        elif upload.favorite and not existing.favorite:
            seen[upload.video_id] = existing.model_copy(update={"favorite": True})
    return tuple(seen.values())


def _moment(upload: Upload) -> datetime | None:
    moment = upload.published or upload.updated
    return _as_utc(moment) if moment is not None else None


def rank_uploads(
    uploads: Sequence[Upload],
    channels: Mapping[str, ChannelRank],
    history_scores: Mapping[str, ChannelHistory],
    now: datetime,
    basis: str,
) -> tuple[Upload, ...]:
    """Order uploads: favorites → recent viewing frequency → recency → video ID.

    Also stamps each upload with its ``favorite`` flag, ``recent_view_score``,
    ``view_events`` and ``rank_basis`` so the rendered list carries its evidence.
    Pure and deterministic for identical inputs.
    """
    reference = _as_utc(now)

    def key(upload: Upload) -> tuple[int, float, float, str]:
        channel_id = upload.channel_id
        rank = channels.get(channel_id) if channel_id else None
        history = history_scores.get(channel_id) if channel_id else None
        favorite = bool(upload.favorite or (rank.favorite if rank is not None else False))
        score = history.score if history is not None else 0.0
        moment = _moment(upload)
        age_seconds = (reference - moment).total_seconds() if moment is not None else float("inf")
        return (0 if favorite else 1, -score, age_seconds, upload.video_id)

    ordered: list[Upload] = []
    for upload in sorted(uploads, key=key):
        channel_id = upload.channel_id
        rank = channels.get(channel_id) if channel_id else None
        history = history_scores.get(channel_id) if channel_id else None
        ordered.append(
            upload.model_copy(
                update={
                    "favorite": bool(
                        upload.favorite or (rank.favorite if rank is not None else False)
                    ),
                    "recent_view_score": history.score if history is not None else None,
                    "view_events": history.events if history is not None else None,
                    "rank_basis": basis,
                }
            )
        )
    return tuple(ordered)
