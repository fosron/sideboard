"""Pure presentation for Mac statistics.

Every value carries its basis. Unavailable fields render as unavailable, the first
sample renders as "warming up" rather than 0 %, and a RAM percentage is never
labelled as macOS memory pressure.
"""

from __future__ import annotations

from ...core.contracts import (
    Metric,
    PresentInput,
    Privacy,
    Series,
    SeriesPoint,
    Severity,
    StatusView,
    TableColumn,
    TableRow,
    TableView,
    ViewContext,
    ViewModel,
)
from ...core.present import status_from_envelope
from .config import MacDetailsSettings, MacSummarySettings
from .models import MacHistoryPoint, MacPayload

__all__ = ["format_bytes", "format_rate", "format_uptime", "present_details", "present_summary"]

_GIB = 1024.0**3
_MIB = 1024.0**2


def format_bytes(value: float | None, *, suffix: str = "") -> str | None:
    if value is None:
        return None
    sign = "-" if value < 0 else ""
    magnitude = abs(float(value))
    if magnitude >= _GIB:
        text = f"{magnitude / _GIB:.1f} GiB"
    elif magnitude >= _MIB:
        text = f"{magnitude / _MIB:.0f} MiB"
    else:
        text = f"{magnitude / 1024.0:.0f} KiB"
    return f"{sign}{text}{suffix}"


def format_rate(value: float | None) -> str | None:
    if value is None:
        return None
    return format_bytes(value, suffix="/s")


def format_uptime(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _series(
    points: tuple[MacHistoryPoint, ...],
    attribute: str,
    *,
    series_id: str,
    label: str,
    unit: str,
) -> Series:
    values = []
    for point in points:
        raw = getattr(point, attribute, None)
        values.append(SeriesPoint(t=point.at, value=float(raw) if raw is not None else None))
    return Series(id=series_id, label=label, unit=unit, points=tuple(values))


def _primary_disk(payload: MacPayload) -> tuple[float | None, str | None]:
    for disk in payload.disks:
        if disk.available and disk.free_bytes is not None:
            return float(disk.free_bytes), disk.mount
    return None, None


def _cpu_metric(payload: MacPayload) -> Metric:
    cpu = payload.cpu
    if cpu is None:
        return Metric.unavailable("cpu", "CPU", "not reported on this system")
    if cpu.warming_up:
        return Metric.unavailable("cpu", "CPU", cpu.note or "warming up")
    return Metric(
        id="cpu",
        label="CPU",
        value_text=f"{cpu.utilisation_percent:.0f}" if cpu.utilisation_percent is not None else "—",
        unit="%",
        basis="host-wide 0–100 %",
        available=cpu.utilisation_percent is not None,
        note=cpu.note,
        as_of=None,
    )


def _memory_metric(payload: MacPayload) -> Metric:
    memory = payload.memory
    if memory is None or memory.utilisation_percent is None:
        return Metric.unavailable("memory", "RAM", "memory information unavailable")
    return Metric(
        id="memory",
        label="RAM",
        value_text=f"{memory.utilisation_percent:.0f}",
        unit="%",
        basis="used = total − available (not memory pressure)",
        note=f"{format_bytes(memory.available_bytes)} available",
    )


def _disk_metric(payload: MacPayload) -> Metric:
    free, mount = _primary_disk(payload)
    if free is None:
        return Metric.unavailable("disk_free", "Disk free", "volume not readable")
    text = format_bytes(free) or "—"
    return Metric(
        id="disk_free",
        label="Disk free",
        value_text=text,
        basis=f"volume {mount}",
    )


def _net_metrics(payload: MacPayload) -> tuple[Metric, ...]:
    network = payload.network
    if network is None:
        return (Metric.unavailable("net_rx", "Net in", "no physical interface selected"),)
    if network.receive_bps is None and network.send_bps is None:
        return (
            Metric.unavailable("net_rx", "Net in", network.note or "waiting for a second sample"),
            Metric.unavailable("net_tx", "Net out", network.note or "waiting for a second sample"),
        )
    interfaces = ", ".join(network.interfaces) or "auto"
    return (
        Metric(
            id="net_rx",
            label="Net in",
            value_text=format_rate(network.receive_bps) or "—",
            available=network.receive_bps is not None,
            basis=f"delta over {network.interval_seconds:.1f}s on {interfaces}"
            if network.interval_seconds
            else f"interface {interfaces}",
        ),
        Metric(
            id="net_tx",
            label="Net out",
            value_text=format_rate(network.send_bps) or "—",
            available=network.send_bps is not None,
            basis=f"delta over {network.interval_seconds:.1f}s on {interfaces}"
            if network.interval_seconds
            else f"interface {interfaces}",
        ),
    )


def _battery_metric(payload: MacPayload) -> Metric:
    battery = payload.battery
    if battery is None or not battery.present:
        return Metric.unavailable("battery", "Battery", "no battery on this device")
    state = "charging" if battery.plugged_in else "on battery"
    return Metric(
        id="battery",
        label="Battery",
        value_text=f"{battery.percent:.0f}" if battery.percent is not None else "—",
        unit="%",
        basis=state,
        available=battery.percent is not None,
        note=battery.note,
    )


def _summary_metrics(
    payload: MacPayload, settings: MacSummarySettings, context: ViewContext
) -> tuple[Metric, ...]:
    metrics: list[Metric] = [_cpu_metric(payload), _memory_metric(payload)]
    if settings.show_disk:
        metrics.append(_disk_metric(payload))
    if settings.show_network:
        metrics.extend(_net_metrics(payload))
    if settings.show_battery:
        metrics.append(_battery_metric(payload))
    sw = payload.swap
    if sw is not None and sw.used_bytes is not None:
        metrics.append(
            Metric(
                id="swap",
                label="Swap",
                value_text=format_bytes(sw.used_bytes) or "—",
                basis="swap in use, not swapping activity",
            )
        )
    _ = context
    return tuple(metrics)


def present_summary(inputs: PresentInput) -> ViewModel:
    settings = inputs.settings
    assert isinstance(settings, MacSummarySettings)
    envelope = inputs.envelope("primary")
    payload = envelope.payload if envelope is not None else None
    status = status_from_envelope(
        envelope, offline=inputs.context.offline, demo=inputs.context.demo
    )
    if not isinstance(payload, MacPayload):
        return ViewModel(title="Mac", status=status or _empty_status())
    metrics = _summary_metrics(payload, settings, inputs.context)
    # No sparkline here: the CPU metric above it already states the value, and the series
    # adds a second CPU line plus a gap count. History is for the details view, where the
    # four sparklines have room to be read.
    return ViewModel(
        title="Mac",
        metrics=metrics,
        status=status,
    )


def present_details(inputs: PresentInput) -> ViewModel:
    settings = inputs.settings
    assert isinstance(settings, MacDetailsSettings)
    envelope = inputs.envelope("primary")
    payload = envelope.payload if envelope is not None else None
    status = status_from_envelope(
        envelope, offline=inputs.context.offline, demo=inputs.context.demo
    )
    if not isinstance(payload, MacPayload):
        return ViewModel(title="Mac details", status=status or _empty_status())

    metrics: list[Metric] = list(_summary_metrics(payload, MacSummarySettings(), inputs.context))
    if settings.show_swap and payload.swap is not None:
        metrics.append(
            Metric(
                id="swap_total",
                label="Swap total",
                value_text=format_bytes(payload.swap.total_bytes) or "—",
                basis=payload.swap.basis,
            )
        )
    metrics.append(
        Metric(
            id="uptime",
            label="Uptime",
            value_text=format_uptime(payload.host.uptime_seconds) or "—",
            available=payload.host.uptime_seconds is not None,
            basis=f"{payload.host.cpu_count_logical or '?'} logical CPUs · {payload.host.platform or '?'}",
        )
    )

    series: list[Series] = [
        _series(payload.history, "cpu_percent", series_id="cpu", label="CPU", unit="%"),
        _series(payload.history, "memory_percent", series_id="memory", label="RAM", unit="%"),
        _series(payload.history, "receive_bps", series_id="rx", label="Net in", unit="B/s"),
        _series(payload.history, "send_bps", series_id="tx", label="Net out", unit="B/s"),
    ]
    for disk in payload.disks:
        if disk.available:
            metrics.append(
                Metric(
                    id=f"disk_{disk.mount}",
                    label=f"Disk {disk.mount}",
                    value_text=format_bytes(disk.free_bytes) or "—",
                    available=disk.free_bytes is not None,
                    basis=f"{disk.used_percent:.0f}% used" if disk.used_percent is not None else "",
                )
            )
    table = None
    if settings.show_processes:
        table = _process_table(payload)
    if settings.show_disk_io and payload.disk_io:
        for key, value in payload.disk_io.items():
            metrics.append(
                Metric(
                    id=f"disk_io_{key}",
                    label=f"Disk {key.replace('_', ' ')}",
                    value_text=format_bytes(value) or "—",
                    available=value is not None,
                    basis="cumulative counter",
                )
            )
    unavailable = ", ".join(payload.unavailable) if payload.unavailable else None
    return ViewModel(
        title="Mac details",
        subtitle=(unavailable and f"unavailable: {unavailable}") or None,
        metrics=tuple(metrics),
        series=tuple(series),
        table=table,
        footnote=(
            "temperature, fans, GPU and power draw are not provided by the basic "
            "collector; no privileged helper is used"
        ),
    )


def _process_table(payload: MacPayload) -> TableView | None:
    if not payload.processes:
        return None
    return TableView(
        columns=(
            TableColumn(id="name", label="process", privacy=Privacy.PRIVATE),
            TableColumn(id="pid", label="pid", align="right"),
            TableColumn(id="cpu", label="cpu %", align="right"),
            TableColumn(id="rss", label="rss", align="right"),
        ),
        rows=tuple(
            TableRow(
                id=f"proc:{row.pid}",
                cells={
                    "name": row.name,
                    "pid": str(row.pid),
                    "cpu": f"{row.cpu_percent:.1f}" if row.cpu_percent is not None else "—",
                    "rss": format_bytes(row.rss_bytes) or "—",
                },
                privacy=Privacy.PRIVATE,
            )
            for row in payload.processes
        ),
        footnote="process names are masked in privacy mode",
    )


def _empty_status() -> StatusView:
    return StatusView(
        severity=Severity.INFO,
        headline="No sample yet",
        detail="Waiting for the first local sample.",
    )
