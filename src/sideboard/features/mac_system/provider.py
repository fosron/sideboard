"""Local ``psutil`` collector for Mac statistics.

No account, key, root access, Full Disk Access or installed monitoring daemon is
required. Anything the platform does not expose stays unavailable.

GPU/ANE utilisation, temperature, fans and power draw are **not** provided here:
psutil's documented temperature/fan support is not a general macOS solution, and no
privileged helper or ``sudo powermetrics`` is invoked to fill the gap.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import timedelta
from typing import Any

from ...core.contracts import (
    Cadence,
    Capability,
    Coverage,
    MetricReading,
    Precision,
    ProviderBuild,
    SourcePayload,
    SourceSpec,
)
from .config import MacSettings
from .models import MacHistoryPoint, MacPayload
from .sampler import MacSampler

__all__ = ["MacProvider", "demo_payload", "make_spec", "metrics_of"]

HISTORY_CAPACITY = 180  # 15 minutes at the 5 s active cadence


class MacProvider:
    """Samples supported local counters once per acquisition."""

    def __init__(self, *, settings: MacSettings, sampler_factory: object = None) -> None:
        self.settings = settings
        self.sampler_factory = sampler_factory
        self._sampler: MacSampler | None = None
        self._history: deque[MacHistoryPoint] = deque(maxlen=HISTORY_CAPACITY)

    def _make_sampler(self) -> MacSampler:
        if self.sampler_factory is not None:  # pragma: no cover - test injection point
            factory = self.sampler_factory
            assert callable(factory)
            return factory(self.settings)  # type: ignore[no-any-return]
        import psutil

        # psutil ships no type information; the sampler only uses documented attributes.
        adapter: Any = psutil
        return MacSampler(psutil=adapter, settings=self.settings)

    async def fetch(self, context: ProviderBuild) -> MacPayload:
        settings = context.settings
        assert isinstance(settings, MacSettings)
        if self._sampler is None:
            self._sampler = self._make_sampler()
        monotonic_now = context.clock.monotonic()
        sample = self._sampler.sample(now=monotonic_now)
        point = MacHistoryPoint(
            at=time.time(),
            cpu_percent=sample.cpu.utilisation_percent if sample.cpu else None,
            memory_percent=sample.memory.utilisation_percent if sample.memory else None,
            receive_bps=sample.network.receive_bps if sample.network else None,
            send_bps=sample.network.send_bps if sample.network else None,
        )
        self._history.append(point)
        now = context.clock.now()
        return sample.model_copy(
            update={
                "history": tuple(self._history),
                "as_of": now,
                "precision": Precision.INSTANT,
                "valid_until": now + timedelta(seconds=self._validity_seconds(settings)),
                "provenance": "psutil-local",
                "coverage": Coverage(
                    notes=(
                        f"unavailable fields: {', '.join(sample.unavailable)}"
                        if sample.unavailable
                        else "all sampled fields available"
                    )
                ),
            }
        )

    def _validity_seconds(self, settings: MacSettings) -> float:
        base = settings.ambient_seconds or settings.active_seconds or 15.0
        return max(60.0, float(base) * 3)

    async def aclose(self) -> None:
        return None


def metrics_of(payload: SourcePayload) -> tuple[MetricReading, ...]:
    """Numeric readings recorded in the host's bounded metric history."""
    if not isinstance(payload, MacPayload):
        return ()
    readings: list[MetricReading] = []
    if payload.cpu is not None and payload.cpu.utilisation_percent is not None:
        readings.append(
            MetricReading(
                metric_id="cpu_percent",
                value=payload.cpu.utilisation_percent,
                unit="%",
                label="CPU",
                gap=payload.cpu.warming_up,
            )
        )
    if payload.memory is not None and payload.memory.utilisation_percent is not None:
        readings.append(
            MetricReading(
                metric_id="memory_percent",
                value=payload.memory.utilisation_percent,
                unit="%",
                label="RAM",
            )
        )
    if payload.network is not None:
        if payload.network.receive_bps is not None:
            readings.append(
                MetricReading(
                    metric_id="net_receive_bps",
                    value=payload.network.receive_bps,
                    unit="B/s",
                    label="Net in",
                )
            )
        if payload.network.send_bps is not None:
            readings.append(
                MetricReading(
                    metric_id="net_send_bps",
                    value=payload.network.send_bps,
                    unit="B/s",
                    label="Net out",
                )
            )
    for disk in payload.disks:
        if disk.used_percent is not None:
            readings.append(
                MetricReading(
                    metric_id=f"disk_used_percent:{disk.mount}",
                    value=disk.used_percent,
                    unit="%",
                    label=f"disk {disk.mount}",
                )
            )
    return tuple(readings)


def make_spec() -> SourceSpec:
    return SourceSpec(
        type_id="mac.metrics",
        payload_model=MacPayload,
        settings_model=MacSettings,
        payload_schema="mac.metrics",
        payload_schema_version=1,
        factory=lambda build: MacProvider(settings=_settings_of(build)),
        capabilities=frozenset({Capability.LOCAL_METRICS}),
        cadence=Cadence(active_seconds=5, ambient_seconds=15, background_seconds=60),
        retention_seconds=24 * 3600,
        description="Local Mac CPU, memory, swap, disk, network, battery and uptime.",
        requires_setup=False,
        metrics_extractor=metrics_of,
        demo_builder=demo_payload,
    )


def _settings_of(build: ProviderBuild) -> MacSettings:
    settings = build.settings
    assert isinstance(settings, MacSettings)
    return settings


def demo_payload() -> SourcePayload:
    """Synthetic Mac counters for ``--demo``. No system access at all."""
    from .models import (
        BatteryReading,
        CpuReading,
        DiskReading,
        HostInfo,
        MacHistoryPoint,
        MemoryReading,
        NetworkReading,
        SwapReading,
    )

    now = time.time()
    history = tuple(
        MacHistoryPoint(
            at=now - (HISTORY_CAPACITY - index) * 5,
            cpu_percent=18.0 + (index % 7),
            memory_percent=61.0 + (index % 3) * 0.4,
            receive_bps=120_000.0 + (index % 11) * 8_000,
            send_bps=40_000.0 + (index % 5) * 5_000,
        )
        for index in range(60)
    )
    return MacPayload(
        host=HostInfo(
            cpu_count_logical=10,
            cpu_count_physical=10,
            uptime_seconds=3 * 3600,
            platform="Darwin",
            platform_version="demo",
            architecture="arm64",
        ),
        cpu=CpuReading(
            utilisation_percent=23.0, load_average=(1.4, 1.6, 1.9), interval_seconds=5.0
        ),
        memory=MemoryReading(
            total_bytes=16 * 1024**3,
            available_bytes=6 * 1024**3,
            used_bytes=10 * 1024**3,
            utilisation_percent=62.5,
        ),
        swap=SwapReading(
            total_bytes=2 * 1024**3, used_bytes=256 * 1024**2, utilisation_percent=12.5
        ),
        disks=(
            DiskReading(
                mount="/", total_bytes=494 * 10**9, free_bytes=132 * 10**9, used_percent=73.3
            ),
        ),
        network=NetworkReading(
            interfaces=("en0",), receive_bps=150_000.0, send_bps=52_000.0, interval_seconds=5.0
        ),
        battery=BatteryReading(present=True, percent=74.0, plugged_in=True),
        history=history,
        provenance="demo",
    )
