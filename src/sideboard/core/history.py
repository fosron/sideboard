"""Bounded metric history.

Two tiers, both bounded:

* in-memory ring: at most ``in_memory_seconds`` (default 15 min) of high-resolution samples
* persisted aggregate: at most one sample per ``aggregate_seconds`` (default 60 s),
  retained ``retention_seconds`` (default 24 h)

Gaps are recorded as :class:`SeriesPoint` with ``value=None`` so a sparkline draws a
break rather than zero load. A gap is declared for a counter reset, an interface
change, a long sampling interval or a sleep/wake jump.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .contracts import Series, SeriesPoint
from .storage import Database

__all__ = ["BoundedHistory", "HistorySeries", "downsample"]

IN_MEMORY_SECONDS = 15 * 60
AGGREGATE_SECONDS = 60
RETENTION_SECONDS = 24 * 3600
DEFAULT_GAP_SECONDS = 90.0


def downsample(points: list[SeriesPoint], max_points: int) -> list[SeriesPoint]:
    """Bucket-average a series, preserving gaps.

    A bucket containing any gap is emitted as a gap, so an average is never
    invented across a sleep or a counter reset.
    """
    if max_points <= 0 or len(points) <= max_points:
        return points
    bucket = math.ceil(len(points) / max_points)
    out: list[SeriesPoint] = []
    for start in range(0, len(points), bucket):
        window = points[start : start + bucket]
        if any(point.value is None for point in window):
            out.append(SeriesPoint(t=window[-1].t, value=None))
            continue
        values = [point.value for point in window if point.value is not None]
        out.append(SeriesPoint(t=window[-1].t, value=sum(values) / len(values)))
    return out


@dataclass(slots=True)
class HistorySeries:
    """One metric's bounded ring."""

    metric_id: str
    unit: str = ""
    label: str = ""
    capacity: int = 4096
    gap_seconds: float = DEFAULT_GAP_SECONDS
    points: deque[SeriesPoint] = field(default_factory=deque)
    _last_t: float | None = None

    def append(self, t: float, value: float | None, *, gap: bool = False) -> None:
        if gap or (self._last_t is not None and t - self._last_t > self.gap_seconds):
            self.points.append(SeriesPoint(t=t, value=None))
        self.points.append(SeriesPoint(t=t, value=value))
        self._last_t = t
        while len(self.points) > self.capacity:
            self.points.popleft()

    def to_series(self, *, max_points: int = 60) -> Series:
        return Series(
            id=self.metric_id,
            label=self.label or self.metric_id,
            unit=self.unit,
            points=tuple(downsample(list(self.points), max_points)),
        )

    def values(self) -> list[float]:
        return [p.value for p in self.points if p.value is not None]

    def latest(self) -> float | None:
        for point in reversed(self.points):
            if point.value is not None:
                return point.value
        return None

    def delta(self) -> float | None:
        values = self.values()
        if len(values) < 2:
            return None
        return values[-1] - values[0]


@dataclass(slots=True)
class BoundedHistory:
    """In-memory rings plus a throttled, bounded persistence path.

    Convention: ``t`` is wall-clock epoch seconds (``time.time()``), so persisted
    history and rendered sparklines share one time axis. Monotonic time is used
    only for dwell and counter-rate deltas.
    """

    database: Database | None = None
    retention_seconds: int = RETENTION_SECONDS
    aggregate_seconds: int = AGGREGATE_SECONDS
    in_memory_seconds: float = float(IN_MEMORY_SECONDS)
    series: dict[tuple[str, str], HistorySeries] = field(default_factory=dict)
    _last_persisted: dict[tuple[str, str], float] = field(default_factory=dict)
    _pending: list[tuple[str, str, float, float]] = field(default_factory=list, repr=False)

    def ring(
        self, source_id: str, metric_id: str, *, unit: str = "", label: str = ""
    ) -> HistorySeries:
        key = (source_id, metric_id)
        existing = self.series.get(key)
        if existing is None:
            capacity = max(16, int(self.in_memory_seconds))
            existing = HistorySeries(metric_id=metric_id, unit=unit, label=label, capacity=capacity)
            self.series[key] = existing
        else:
            if unit:
                existing.unit = unit
            if label:
                existing.label = label
        return existing

    def record(
        self,
        source_id: str,
        metric_id: str,
        t: float,
        value: float | None,
        *,
        unit: str = "",
        label: str = "",
        gap: bool = False,
    ) -> None:
        ring = self.ring(source_id, metric_id, unit=unit, label=label)
        ring.append(t, value, gap=gap)
        key = (source_id, metric_id)
        if value is None:
            return
        last = self._last_persisted.get(key)
        if last is None or t - last >= self.aggregate_seconds:
            self._last_persisted[key] = t
            self._pending.append((source_id, metric_id, t, value))

    def forget_source(self, source_id: str) -> None:
        for key in [k for k in self.series if k[0] == source_id]:
            del self.series[key]
            self._last_persisted.pop(key, None)

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def flush(self) -> int:
        """Persist queued aggregates. Called at most once per scheduler tick."""
        if self.database is None or not self._pending:
            return 0
        queued = self._pending
        self._pending = []
        async with self.database.transaction() as cursor:
            await cursor.executemany(
                "INSERT OR REPLACE INTO metric_samples(source_id, metric_id, ts, value) "
                "VALUES (?, ?, ?, ?)",
                queued,
            )
        return len(queued)

    async def prune(self, now_monotonic: float) -> int:
        """Drop samples older than the retention window."""
        if self.database is None:
            return 0
        cutoff_wall = _wall_now() - self.retention_seconds
        async with self.database.transaction() as cursor:
            await cursor.execute("DELETE FROM metric_samples WHERE ts < ?", (cutoff_wall,))
            return cursor.rowcount or 0

    async def load(self, source_id: str, metric_id: str, *, max_points: int = 60) -> Series | None:
        if self.database is None:
            return None
        cutoff = _wall_now() - self.retention_seconds
        rows = await self.database.fetchall(
            "SELECT ts, value FROM metric_samples WHERE source_id = ? AND metric_id = ? AND ts >= ? "
            "ORDER BY ts ASC",
            (source_id, metric_id, cutoff),
        )
        if not rows:
            return None
        points = [SeriesPoint(t=float(row["ts"]), value=float(row["value"])) for row in rows]
        return Series(id=metric_id, label=metric_id, points=tuple(downsample(points, max_points)))


def _wall_now() -> float:
    import time

    return time.time()
