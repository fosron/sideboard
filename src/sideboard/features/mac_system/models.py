"""Validated payload for a Mac statistics sample.

Every field is optional. An unavailable field stays ``None`` and is reported as
unavailable, never as zero. "No battery" and "unknown time remaining" are separate
states. A RAM percentage is never labelled as macOS memory pressure.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ...core.contracts import SourcePayload

__all__ = [
    "BatteryReading",
    "CpuReading",
    "DiskReading",
    "HostInfo",
    "MacHistoryPoint",
    "MacPayload",
    "MemoryReading",
    "NetworkReading",
    "ProcessReading",
    "SwapReading",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HostInfo(_Frozen):
    """Coarse host metadata. No hardware serial numbers or stable hardware identifiers."""

    cpu_count_logical: int | None = None
    cpu_count_physical: int | None = None
    boot_time_epoch: float | None = None
    uptime_seconds: float | None = None
    platform: str | None = None
    platform_version: str | None = None
    architecture: str | None = None


class CpuReading(_Frozen):
    """Host-wide CPU utilisation on an explicit 0–100 % scale.

    ``warming_up`` is True for the first sample after a start, wake or counter reset:
    the delta baseline does not exist yet, so no percentage is claimed.
    """

    utilisation_percent: float | None = None
    per_core_percent: tuple[float, ...] = ()
    load_average: tuple[float, float, float] | None = None
    interval_seconds: float | None = None
    warming_up: bool = False
    note: str | None = None


class MemoryReading(_Frozen):
    total_bytes: int | None = None
    available_bytes: int | None = None
    used_bytes: int | None = None
    utilisation_percent: float | None = None
    basis: str = "used = total − available; a RAM percentage is not macOS memory pressure"


class SwapReading(_Frozen):
    total_bytes: int | None = None
    used_bytes: int | None = None
    utilisation_percent: float | None = None
    basis: str = "swap in use, not a measurement of current swapping activity"


class DiskReading(_Frozen):
    mount: str
    total_bytes: int | None = None
    free_bytes: int | None = None
    used_percent: float | None = None
    available: bool = True
    note: str | None = None


class NetworkReading(_Frozen):
    interfaces: tuple[str, ...] = ()
    receive_bps: float | None = None
    send_bps: float | None = None
    interval_seconds: float | None = None
    counter_reset: bool = False
    note: str | None = None


class BatteryReading(_Frozen):
    present: bool = False
    percent: float | None = None
    plugged_in: bool | None = None
    seconds_left: float | None = None
    note: str | None = None


class ProcessReading(_Frozen):
    """Names and IDs only: never command lines, environments or document titles."""

    pid: int
    name: str
    cpu_percent: float | None = None
    rss_bytes: int | None = None


class MacHistoryPoint(_Frozen):
    """One in-payload sample, so a card can draw a sparkline without a database read.

    ``value is None`` marks a gap (sleep, counter reset, interface change). Capped at
    fifteen minutes of high-resolution samples; longer retention lives in the host's
    bounded metric history.
    """

    at: float
    cpu_percent: float | None = None
    memory_percent: float | None = None
    receive_bps: float | None = None
    send_bps: float | None = None


class MacPayload(SourcePayload):
    host: HostInfo = HostInfo()
    cpu: CpuReading | None = None
    memory: MemoryReading | None = None
    swap: SwapReading | None = None
    disks: tuple[DiskReading, ...] = ()
    disk_io: dict[str, float | None] = {}
    network: NetworkReading | None = None
    battery: BatteryReading | None = None
    processes: tuple[ProcessReading, ...] = ()
    history: tuple[MacHistoryPoint, ...] = ()
    unavailable: tuple[str, ...] = ()
