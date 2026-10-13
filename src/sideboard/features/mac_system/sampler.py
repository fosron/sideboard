"""Capability-aware local sampler.

The sampler owns the retained counter baselines, so a percentage or a byte rate is
only reported when two valid samples exist. Tests inject a fake ``psutil``-shaped
object, which is why every call goes through this narrow adapter.

Rules implemented here:

* CPU uses counter deltas over monotonic elapsed time; the first sample is *warming up*
* a counter reset, an interface change or a long gap yields ``None`` + a marked gap
* disk and battery are slow-changing fields, refreshed at most once per interval
* process sampling is opt-in and never reads command lines or the environment
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import VIRTUAL_INTERFACE_PREFIXES, MacSettings
from .models import (
    BatteryReading,
    CpuReading,
    DiskReading,
    HostInfo,
    MacPayload,
    MemoryReading,
    NetworkReading,
    ProcessReading,
    SwapReading,
)

__all__ = ["GAP_THRESHOLD_SECONDS", "MacSampler", "PsutilLike"]

GAP_THRESHOLD_SECONDS = 90.0


class PsutilLike(Protocol):
    """The subset of :mod:`psutil` this sampler uses. Injecting it keeps tests offline."""

    def cpu_times(self) -> Any: ...
    def cpu_times_percent(self, interval: float | None = None) -> Any: ...
    def cpu_count(self, logical: bool = True) -> int | None: ...
    def getloadavg(self) -> Sequence[float]: ...
    def virtual_memory(self) -> Any: ...
    def swap_memory(self) -> Any: ...
    def disk_usage(self, path: str) -> Any: ...
    def disk_io_counters(self, perdisk: bool = False) -> Any: ...
    def net_io_counters(self, pernic: bool = False) -> Any: ...
    def net_if_stats(self) -> Any: ...
    def sensors_battery(self) -> Any: ...
    def boot_time(self) -> float: ...
    def process_iter(self, attrs: Sequence[str]) -> Any: ...


@dataclass(slots=True)
class _CpuBaseline:
    idle: float = 0.0
    total: float = 0.0
    at: float = 0.0
    valid: bool = False


@dataclass(slots=True)
class _NetBaseline:
    receive: int = 0
    send: int = 0
    at: float = 0.0
    interfaces: tuple[str, ...] = ()
    valid: bool = False


@dataclass(slots=True)
class _CachedValue:
    value: Any = None
    at: float = 0.0


@dataclass(slots=True)
class MacSampler:
    """Collects one bounded sample of supported local counters."""

    psutil: PsutilLike
    settings: MacSettings
    monotonic: Any = time.monotonic
    _cpu: _CpuBaseline = field(default_factory=_CpuBaseline)
    _net: _NetBaseline = field(default_factory=_NetBaseline)
    _disk_cache: _CachedValue = field(default_factory=_CachedValue)
    _host_cache: _CachedValue = field(default_factory=_CachedValue)
    _process_cache: _CachedValue = field(default_factory=_CachedValue)
    _process_primed: bool = False
    unavailable: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------ counters

    def _counter_totals(self) -> tuple[float, float, float] | None:
        try:
            times = self.psutil.cpu_times()
        except Exception:
            self.unavailable.add("cpu")
            return None
        idle = float(getattr(times, "idle", 0.0))
        user = float(getattr(times, "user", 0.0))
        system = float(getattr(times, "system", 0.0))
        nice = float(getattr(times, "nice", 0.0) or 0.0)
        return idle, user + system + nice, idle + user + system + nice

    def _cpu_reading(self, monotonic_now: float) -> CpuReading:
        totals = self._counter_totals()
        load: tuple[float, float, float] | None = None
        try:
            raw_load = self.psutil.getloadavg()
            load = (float(raw_load[0]), float(raw_load[1]), float(raw_load[2]))
        except Exception:
            load = None

        per_core: tuple[float, ...] = ()
        if self.settings.include_per_core:
            per_core = self._per_core()

        if totals is None:
            return CpuReading(load_average=load, per_core_percent=per_core, note="cpu unavailable")

        idle, _busy, total = totals
        baseline = self._cpu
        elapsed = monotonic_now - baseline.at
        reading: CpuReading
        if (
            not baseline.valid
            or elapsed <= 0
            or elapsed > GAP_THRESHOLD_SECONDS
            or total < baseline.total
        ):
            reading = CpuReading(
                per_core_percent=per_core,
                load_average=load,
                warming_up=True,
                note=(
                    "warming up: no counter baseline yet"
                    if not baseline.valid
                    else "counter reset or sampling gap; baseline discarded"
                ),
            )
        else:
            delta_idle = idle - baseline.idle
            delta_total = total - baseline.total
            utilisation = None
            if delta_total > 0:
                utilisation = max(0.0, min(100.0, (delta_total - delta_idle) / delta_total * 100.0))
            reading = CpuReading(
                utilisation_percent=utilisation,
                per_core_percent=per_core,
                load_average=load,
                interval_seconds=elapsed,
                note="host-wide CPU on a 0–100 % scale" if utilisation is not None else None,
            )
        self._cpu = _CpuBaseline(idle=idle, total=total, at=monotonic_now, valid=True)
        return reading

    def _per_core(self) -> tuple[float, ...]:
        try:
            values = self.psutil.cpu_times_percent(interval=None)
        except Exception:
            return ()
        if not isinstance(values, (list, tuple)):
            return ()
        out: list[float] = []
        for value in values:
            with_idle = float(getattr(value, "idle", 0.0))
            total = sum(
                float(getattr(value, name, 0.0) or 0.0)
                for name in ("user", "system", "idle", "nice", "iowait", "irq", "softirq")
            )
            out.append(max(0.0, min(100.0, 100.0 - (with_idle / total * 100.0) if total else 0.0)))
        return tuple(out)

    def _memory(self) -> MemoryReading | None:
        try:
            raw = self.psutil.virtual_memory()
        except Exception:
            self.unavailable.add("memory")
            return None
        total = int(getattr(raw, "total", 0) or 0)
        available = int(getattr(raw, "available", 0) or 0)
        if total <= 0:
            self.unavailable.add("memory")
            return None
        used = total - available
        return MemoryReading(
            total_bytes=total,
            available_bytes=available,
            used_bytes=used,
            utilisation_percent=used / total * 100.0,
        )

    def _swap(self) -> SwapReading | None:
        try:
            raw = self.psutil.swap_memory()
        except Exception:
            self.unavailable.add("swap")
            return None
        total = int(getattr(raw, "total", 0) or 0)
        used = int(getattr(raw, "used", 0) or 0)
        percent = (used / total * 100.0) if total else None
        return SwapReading(total_bytes=total, used_bytes=used, utilisation_percent=percent)

    def _disks(self, monotonic_now: float) -> tuple[DiskReading, ...]:
        if self._disk_cache.value is not None and (
            monotonic_now - self._disk_cache.at < self.settings.disk_refresh_seconds
        ):
            return self._disk_cache.value
        readings: list[DiskReading] = []
        for mount in self.settings.volumes:
            try:
                usage = self.psutil.disk_usage(mount)
            except Exception:
                readings.append(
                    DiskReading(mount=mount, available=False, note="volume is not readable")
                )
                continue
            total = int(getattr(usage, "total", 0) or 0)
            free = int(getattr(usage, "free", 0) or 0)
            readings.append(
                DiskReading(
                    mount=mount,
                    total_bytes=total or None,
                    free_bytes=free if total else None,
                    used_percent=((total - free) / total * 100.0) if total else None,
                    note=None if total else "capacity unavailable",
                )
            )
        result = tuple(readings)
        self._disk_cache = _CachedValue(value=result, at=monotonic_now)
        return result

    def _disk_io(self) -> dict[str, float | None]:
        try:
            counters = self.psutil.disk_io_counters()
        except Exception:
            return {}
        if counters is None:
            return {}
        out: dict[str, float | None] = {}
        for name in ("read_bytes", "write_bytes"):
            value = getattr(counters, name, None)
            out[name] = float(value) if isinstance(value, int | float) else None
        return out

    def _select_interfaces(self) -> tuple[str, ...]:
        if self.settings.network_interfaces:
            return tuple(self.settings.network_interfaces)
        try:
            per_nic = self.psutil.net_io_counters(pernic=True)
            stats = self.psutil.net_if_stats()
        except Exception:
            self.unavailable.add("network")
            return ()
        selected: list[str] = []
        for name in sorted(per_nic):
            lowered = name.lower()
            if lowered.startswith(VIRTUAL_INTERFACE_PREFIXES):
                continue
            stat = stats.get(name) if hasattr(stats, "get") else None
            if stat is not None and not bool(getattr(stat, "isup", True)):
                continue
            selected.append(name)
        # Auto-selection picks the busiest active physical interface, never the sum of
        # every interface (which would count the same traffic twice).
        if len(selected) > 1:

            def volume(name: str) -> int:
                counters = per_nic.get(name)
                if counters is None:
                    return 0
                return int(getattr(counters, "bytes_recv", 0) or 0) + int(
                    getattr(counters, "bytes_sent", 0) or 0
                )

            selected = [max(selected, key=volume)]
        return tuple(selected)

    def _network(self, monotonic_now: float) -> NetworkReading | None:
        interfaces = self._select_interfaces()
        if not interfaces:
            self.unavailable.add("network")
            return None
        try:
            per_nic = self.psutil.net_io_counters(pernic=True)
        except Exception:
            self.unavailable.add("network")
            return None
        receive = 0
        send = 0
        for name in interfaces:
            counters = per_nic.get(name)
            if counters is None:
                return NetworkReading(
                    interfaces=interfaces, note=f"interface {name} disappeared; baseline reset"
                )
            receive += int(getattr(counters, "bytes_recv", 0) or 0)
            send += int(getattr(counters, "bytes_sent", 0) or 0)

        baseline = self._net
        elapsed = monotonic_now - baseline.at
        reset = (
            receive < baseline.receive
            or send < baseline.send
            or baseline.interfaces != interfaces
            or (baseline.valid and elapsed > GAP_THRESHOLD_SECONDS)
        )
        reading: NetworkReading
        if not baseline.valid:
            reading = NetworkReading(
                interfaces=interfaces, note="warming up: no counter baseline yet"
            )
        elif reset or elapsed <= 0:
            reading = NetworkReading(
                interfaces=interfaces,
                counter_reset=True,
                note="counter reset, interface change or sampling gap; rate not derived",
            )
        else:
            reading = NetworkReading(
                interfaces=interfaces,
                receive_bps=(receive - baseline.receive) / elapsed,
                send_bps=(send - baseline.send) / elapsed,
                interval_seconds=elapsed,
            )
        self._net = _NetBaseline(
            receive=receive, send=send, at=monotonic_now, interfaces=interfaces, valid=True
        )
        return reading

    def _battery(self) -> BatteryReading | None:
        try:
            raw = self.psutil.sensors_battery()
        except Exception:
            # Not every Mac exposes a battery; a desktop is not an error.
            return BatteryReading(present=False, note="battery information unavailable")
        if raw is None:
            return BatteryReading(present=False, note="no battery on this device")
        percent = getattr(raw, "percent", None)
        seconds = getattr(raw, "secsleft", None)
        note = None
        if seconds is None or (isinstance(seconds, int) and seconds < 0):
            seconds = None
            note = "time remaining unknown"
        return BatteryReading(
            present=True,
            percent=float(percent) if isinstance(percent, int | float) else None,
            plugged_in=bool(getattr(raw, "power_plugged", False)),
            seconds_left=float(seconds) if isinstance(seconds, int | float) else None,
            note=note,
        )

    def _host(self, monotonic_now: float) -> HostInfo:
        cached = self._host_cache.value
        if cached is not None and (
            monotonic_now - self._host_cache.at < self.settings.uptime_refresh_seconds
        ):
            return cached
        # The stdlib supplies coarse platform metadata. The hostname and any hardware
        # serial are deliberately not collected.
        info: dict[str, str] = {}
        try:
            import platform as platform_module

            uname = platform_module.uname()
            info = {"system": uname.system, "release": uname.release, "machine": uname.machine}
        except Exception:  # pragma: no cover - platform always present
            info = {}
        boot = None
        try:
            boot = float(self.psutil.boot_time())
        except Exception:
            self.unavailable.add("uptime")
        uptime = None
        if boot is not None:
            uptime = max(0.0, time.time() - boot)
        try:
            logical = self.psutil.cpu_count(logical=True)
            physical = self.psutil.cpu_count(logical=False)
        except Exception:
            logical = physical = None
        host = HostInfo(
            cpu_count_logical=int(logical) if isinstance(logical, int) else None,
            cpu_count_physical=int(physical) if isinstance(physical, int) else None,
            boot_time_epoch=boot,
            uptime_seconds=uptime,
            platform=info.get("system"),
            platform_version=info.get("release"),
            architecture=info.get("machine"),
        )
        self._host_cache = _CachedValue(value=host, at=monotonic_now)
        return host

    def _processes(self, monotonic_now: float) -> tuple[ProcessReading, ...]:
        if not self.settings.include_processes:
            return ()
        if self._process_cache.value is not None and (
            monotonic_now - self._process_cache.at < self.settings.disk_refresh_seconds
        ):
            return self._process_cache.value
        rows: list[ProcessReading] = []
        try:
            iterator = self.psutil.process_iter(
                ["pid", "name", "cpu_percent", "memory_info", "create_time"]
            )
        except Exception:
            self.unavailable.add("processes")
            return ()
        for process in iterator:
            try:
                info = process.info
                pid = int(info.get("pid", 0))
                name = str(info.get("name") or "")
                if not pid or not name:
                    continue
                memory = info.get("memory_info")
                rows.append(
                    ProcessReading(
                        pid=pid,
                        name=name[:40],
                        cpu_percent=float(info.get("cpu_percent") or 0.0),
                        rss_bytes=int(getattr(memory, "rss", 0) or 0) or None,
                    )
                )
            except Exception:
                # The process exited or is not ours to inspect. Never fatal.
                continue
        rows.sort(key=lambda row: (-(row.cpu_percent or 0.0), -(row.rss_bytes or 0)))
        result = tuple(rows[: max(1, self.settings.process_limit)])
        self._process_cache = _CachedValue(value=result, at=monotonic_now)
        self._process_primed = True
        return result

    # ------------------------------------------------------------------ public

    def sample(self, *, now: float | None = None) -> MacPayload:
        monotonic_now = self.monotonic() if now is None else now
        self.unavailable.clear()
        return MacPayload(
            host=self._host(monotonic_now),
            cpu=self._cpu_reading(monotonic_now),
            memory=self._memory(),
            swap=self._swap(),
            disks=self._disks(monotonic_now),
            disk_io=self._disk_io(),
            network=self._network(monotonic_now),
            battery=self._battery(),
            processes=self._processes(monotonic_now),
            unavailable=tuple(sorted(self.unavailable)),
        )
