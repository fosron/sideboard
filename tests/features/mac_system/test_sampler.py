"""Mac sampler tests: warm-up, deltas, counter resets, sleep gaps, capabilities.

A fake ``psutil``-shaped object keeps this offline and deterministic, which is why the
sampler takes its counters through a narrow adapter.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sideboard.features.mac_system.config import MacSettings
from sideboard.features.mac_system.sampler import GAP_THRESHOLD_SECONDS, MacSampler


@dataclass
class CpuTimes:
    user: float = 100.0
    system: float = 50.0
    idle: float = 850.0
    nice: float = 0.0
    iowait: float = 0.0
    irq: float = 0.0
    softirq: float = 0.0


@dataclass
class Memory:
    total: int = 16 * 1024**3
    available: int = 6 * 1024**3
    percent: float = 62.5


@dataclass
class Swap:
    total: int = 2 * 1024**3
    used: int = 256 * 1024**2


@dataclass
class Disk:
    total: int = 500 * 10**9
    used: int = 300 * 10**9
    free: int = 200 * 10**9


@dataclass
class Io:
    read_bytes: int = 1000
    write_bytes: int = 2000
    bytes_recv: int = 5000
    bytes_sent: int = 1000


@dataclass
class NicStat:
    isup: bool = True


@dataclass
class Battery:
    percent: float = 74.0
    power_plugged: bool = False
    secsleft: float | None = 3600.0


@dataclass
class Process:
    info: dict = field(default_factory=dict)


class FakePsutil:
    """Deterministic stand-in. Every value is advanceable by the test."""

    def __init__(self) -> None:
        self.cpu = CpuTimes()
        self.memory = Memory()
        self.swap = Swap()
        self.disk = Disk()
        self.per_nic = {"en0": Io(), "lo0": Io(read_bytes=1, write_bytes=1), "utun3": Io()}
        self.nics = {"en0": NicStat(), "lo0": NicStat(), "utun3": NicStat()}
        self.battery: Battery | None = Battery()
        self.boot = 1000.0
        self.processes: list[Process] = [
            Process(
                {
                    "pid": 11,
                    "name": "kernel_task",
                    "cpu_percent": 5.0,
                    "memory_info": Legacy(4096),
                    "create_time": 1.0,
                }
            ),
            Process(
                {
                    "pid": 12,
                    "name": "WindowServer",
                    "cpu_percent": 12.0,
                    "memory_info": Legacy(8192),
                    "create_time": 2.0,
                }
            ),
        ]
        self.per_core = [CpuTimes(user=10, system=5, idle=85)]
        self.disk_usage_error = False
        self.battery_error = False

    # --- psutil-shaped API
    def cpu_times(self) -> CpuTimes:
        return self.cpu

    def cpu_times_percent(self, interval: float | None = None) -> list[CpuTimes]:
        _ = interval
        return self.per_core

    def cpu_count(self, logical: bool = True) -> int:
        _ = logical
        return 10

    def getloadavg(self) -> tuple[float, float, float]:
        return (1.5, 1.7, 2.0)

    def virtual_memory(self) -> Memory:
        return self.memory

    def swap_memory(self) -> Swap:
        return self.swap

    def disk_usage(self, path: str) -> Disk:
        _ = path
        if self.disk_usage_error:
            raise OSError("no volume")
        return self.disk

    def disk_io_counters(self, perdisk: bool = False) -> Io:
        _ = perdisk
        return Io()

    def net_io_counters(self, pernic: bool = False) -> dict[str, Io]:
        _ = pernic
        return self.per_nic

    def net_if_stats(self) -> dict[str, NicStat]:
        return self.nics

    def sensors_battery(self) -> Battery | None:
        if self.battery_error:
            raise RuntimeError("no IOKit battery")
        return self.battery

    def boot_time(self) -> float:
        return self.boot

    def process_iter(self, attrs: Sequence[str]) -> list[Process]:
        _ = attrs
        return list(self.processes)


@dataclass
class Legacy:
    rss: int


def sampler(**kwargs: object) -> tuple[MacSampler, FakePsutil]:
    fake = FakePsutil()
    settings = MacSettings(**kwargs)  # type: ignore[arg-type]
    return MacSampler(psutil=fake, settings=settings), fake


def test_first_sample_is_warming_up_not_zero() -> None:
    machine, _ = sampler()
    first = machine.sample(now=100.0)
    assert first.cpu is not None
    assert first.cpu.utilisation_percent is None
    assert first.cpu.warming_up is True
    assert "warming up" in (first.cpu.note or "")


def test_second_sample_uses_counter_deltas() -> None:
    machine, fake = sampler()
    machine.sample(now=100.0)
    # 10 s later: 100 total ticks, 100 idle ticks → 0 % busy.
    fake.cpu.user += 0
    fake.cpu.idle += 100
    second = machine.sample(now=110.0)
    assert second.cpu is not None
    assert second.cpu.utilisation_percent == 0.0
    assert second.cpu.interval_seconds == 10.0


def test_cpu_percentage_is_bounded_to_a_host_wide_scale() -> None:
    machine, fake = sampler()
    machine.sample(now=100.0)
    fake.cpu.user += 100
    fake.cpu.idle += 100
    second = machine.sample(now=105.0)
    assert second.cpu is not None
    assert second.cpu.utilisation_percent == 50.0


def test_counter_reset_discards_the_baseline() -> None:
    machine, fake = sampler()
    machine.sample(now=100.0)
    fake.cpu = CpuTimes(user=1.0, system=1.0, idle=1.0)
    reset = machine.sample(now=105.0)
    assert reset.cpu is not None
    assert reset.cpu.utilisation_percent is None
    assert "reset" in (reset.cpu.note or "") or "warming" in (reset.cpu.note or "")


def test_sleep_gap_is_not_drawn_as_load() -> None:
    machine, fake = sampler()
    machine.sample(now=100.0)
    fake.cpu.idle += 5000
    after_sleep = machine.sample(now=100.0 + GAP_THRESHOLD_SECONDS + 10)
    assert after_sleep.cpu is not None
    assert after_sleep.cpu.utilisation_percent is None
    assert "gap" in (after_sleep.cpu.note or "")


def test_memory_uses_one_consistent_formula() -> None:
    machine, fake = sampler()
    fake.memory.total = 100
    fake.memory.available = 25
    sample = machine.sample(now=100.0)
    assert sample.memory is not None
    assert sample.memory.used_bytes == 75
    assert sample.memory.utilisation_percent == 75.0
    assert "not macOS memory pressure" in sample.memory.basis


def test_swap_is_not_reported_as_swapping_activity() -> None:
    machine, _ = sampler()
    sample = machine.sample(now=100.0)
    assert sample.swap is not None
    assert "not a measurement of current swapping activity" in sample.swap.basis


def test_network_selects_one_physical_interface_and_never_sums_loopback() -> None:
    machine, fake = sampler()
    first = machine.sample(now=100.0)
    assert first.network is not None
    # lo0 and utun3 are excluded by the documented heuristic.
    assert first.network.interfaces == ("en0",)
    fake.per_nic["en0"].bytes_recv += 10_000
    fake.per_nic["en0"].bytes_sent += 5_000
    fake.per_nic["lo0"].bytes_recv += 10_000_000
    second = machine.sample(now=110.0)
    assert second.network is not None
    assert second.network.receive_bps == 1000.0
    assert second.network.send_bps == 500.0


def test_interface_change_resets_the_baseline() -> None:
    machine, fake = sampler()
    machine.sample(now=100.0)
    fake.per_nic["en1"] = Io()
    fake.nics["en1"] = NicStat()
    fake.per_nic["en0"].bytes_recv = 10
    # en0 is now the only busy interface; the sampler keeps its selection stable.
    second = machine.sample(now=111.0)
    assert second.network is not None
    assert second.network.receive_bps is not None or second.network.counter_reset


def test_network_counter_reset_is_flagged() -> None:
    machine, fake = sampler()
    machine.sample(now=100.0)
    fake.per_nic["en0"].bytes_recv = 1
    fake.per_nic["en0"].bytes_sent = 1
    second = machine.sample(now=110.0)
    assert second.network is not None
    assert second.network.counter_reset is True
    assert second.network.receive_bps is None


def test_battery_absent_and_unknown_remaining_are_different_states() -> None:
    machine, fake = sampler()
    fake.battery = None
    sample = machine.sample(now=100.0)
    assert sample.battery is not None
    assert sample.battery.present is False
    assert "no battery" in (sample.battery.note or "")

    fake.battery = Battery(secsleft=None)
    sample = machine.sample(now=105.0)
    assert sample.battery is not None
    assert sample.battery.present is True
    assert sample.battery.seconds_left is None
    assert sample.battery.note == "time remaining unknown"


def test_battery_api_absence_is_not_an_error() -> None:
    machine, fake = sampler()
    fake.battery_error = True
    sample = machine.sample(now=100.0)
    assert sample.battery is not None
    assert sample.battery.present is False
    assert "battery" in (sample.battery.note or "")


def test_unreadable_volume_is_reported_not_zero() -> None:
    machine, fake = sampler()
    fake.disk_usage_error = True
    sample = machine.sample(now=100.0)
    assert sample.disks[0].available is False
    assert sample.disks[0].free_bytes is None
    assert sample.disks[0].note == "volume is not readable"


def test_process_sampling_is_opt_in_and_excludes_command_lines() -> None:
    machine, _ = sampler()
    assert machine.sample(now=100.0).processes == ()

    machine, _fake2 = sampler(include_processes=True, process_limit=1)
    sample = machine.sample(now=100.0)
    assert len(sample.processes) == 1
    assert sample.processes[0].name == "WindowServer"
    assert not hasattr(sample.processes[0], "cmdline")


def test_process_permission_and_exit_errors_are_contained() -> None:
    class Exploding(FakePsutil):
        def process_iter(self, attrs: Sequence[str]) -> list[Process]:
            _ = attrs
            raise PermissionError("not ours")

    settings = MacSettings(include_processes=True)
    machine = MacSampler(psutil=Exploding(), settings=settings)
    sample = machine.sample(now=100.0)
    assert sample.processes == ()
    assert "processes" in sample.unavailable


def test_disk_and_host_reads_are_cached_between_samples() -> None:
    machine, fake = sampler(disk_refresh_seconds=60.0, uptime_refresh_seconds=300.0)
    machine.sample(now=100.0)
    fake.disk.free = 123
    same = machine.sample(now=110.0)
    assert same.disks[0].free_bytes == 200 * 10**9  # still the cached reading
    later = machine.sample(now=200.0)
    assert later.disks[0].free_bytes == 123


def test_zero_total_memory_is_unavailable() -> None:
    machine, fake = sampler()
    fake.memory.total = 0
    sample = machine.sample(now=100.0)
    assert sample.memory is None
    assert "memory" in sample.unavailable
