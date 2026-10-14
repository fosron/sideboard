"""Clock boundary.

Everything that needs the current time goes through :class:`Clock` so tests can
drive rotation, freshness and scheduling deterministically.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

__all__ = ["Clock", "FakeClock", "SystemClock", "display_timezone", "system_timezone_name"]


class Clock(Protocol):
    """Read-only time source."""

    def now(self) -> datetime:
        """Current UTC instant, timezone aware."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds, for dwell times and byte-rate deltas."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep, cancellable by the caller's task."""
        ...


@dataclass(slots=True)
class SystemClock:
    """Real clock. Uses UTC wall time and :func:`time.monotonic`."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


@dataclass(slots=True)
class FakeClock:
    """Deterministic clock for tests.

    ``sleep`` advances virtual time immediately, so rotation and scheduler tests
    do not need real delays.
    """

    _now: datetime
    _monotonic: float = 0.0

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        """Advance virtual time immediately and yield once.

        Yielding matters: a cancelled-await-less coroutine would otherwise spin the
        scheduler loop inside a single event-loop step.
        """
        self.advance(seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        """Move virtual wall and monotonic time forward."""
        self._now = self._now + timedelta(seconds=seconds)
        self._monotonic += seconds

    def set_now(self, moment: datetime) -> None:
        """Jump wall time (for example to cross a DST boundary) keeping monotonic."""
        delta = (moment - self._now).total_seconds()
        self._now = moment
        if delta > 0:
            self._monotonic += delta


def system_timezone_name() -> str:
    """This Mac's own timezone name.

    macOS keeps ``/etc/localtime`` as a symlink into the zoneinfo database, so the name is
    there to read rather than to guess from an offset. UTC is the fallback: a clock that is
    obviously UTC beats one that is confidently an hour wrong.
    """
    for candidate in (Path("/etc/localtime"), Path("/var/db/timezone/localtime")):
        with contextlib.suppress(OSError):
            target = str(candidate.readlink())
            if "zoneinfo/" in target:
                return target.split("zoneinfo/", 1)[1]
    return "UTC"


def display_timezone(name: str) -> ZoneInfo:
    """Resolve a display timezone. ``local`` (or empty) means this Mac's own zone.

    A dashboard on a desk should show the clock in the room. ``app.timezone = "local"``
    is that; naming a zone is for the case where it should not.
    """
    if not name or name.strip().lower() in {"local", "system"}:
        name = system_timezone_name()
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - tzdata always ships on macOS
        return ZoneInfo("UTC")


def to_local(moment: datetime, tz: timezone | ZoneInfo) -> datetime:
    """Convert an aware instant into the presentation timezone."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz)
