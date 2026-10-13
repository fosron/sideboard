"""Pure presentation for local Docker state.

Metric interpretation stays here, next to the feature: engine CPU is not Mac host CPU,
container memory plus Mac memory would double-count nothing but must not be added,
and a running container without a healthcheck is "running / health unknown".
"""

from __future__ import annotations

from ...core.contracts import (
    Metric,
    PresentInput,
    Privacy,
    Severity,
    StatusView,
    TableColumn,
    TableRow,
    TableView,
    ViewModel,
)
from ...core.present import status_from_envelope
from .config import DockerSummarySettings, DockerTableSettings
from .models import ContainerReading, DockerPayload

__all__ = ["format_bytes", "present_summary", "present_table"]


def format_bytes(value: float | int | None) -> str | None:
    if value is None:
        return None
    magnitude = float(value)
    for unit, factor in (("GiB", 1024.0**3), ("MiB", 1024.0**2), ("KiB", 1024.0)):
        if abs(magnitude) >= factor:
            return f"{magnitude / factor:.1f} {unit}"
    return f"{magnitude:.0f} B"


def health_label(container: ContainerReading) -> str:
    """A running container without a healthcheck is not verified healthy."""
    if container.health:
        return container.health
    if container.running:
        return "unknown"
    return container.state


def severity_for(container: ContainerReading, expected: frozenset[str]) -> Severity:
    if container.health == "unhealthy":
        return Severity.WARNING
    if container.running:
        return Severity.OK
    if container.name in expected:
        return Severity.WARNING
    if container.exit_code not in (None, 0):
        return Severity.INFO
    return Severity.UNKNOWN


def _counts(payload: DockerPayload) -> tuple[int, int, int, int]:
    running = sum(1 for c in payload.containers if c.running)
    stopped = sum(1 for c in payload.containers if not c.running)
    unhealthy = sum(1 for c in payload.containers if c.health == "unhealthy")
    health_unknown = sum(1 for c in payload.containers if c.running and not c.health_defined)
    return running, stopped, unhealthy, health_unknown


def present_summary(inputs: PresentInput) -> ViewModel:
    settings = inputs.settings
    assert isinstance(settings, DockerSummarySettings)
    envelope = inputs.envelope("primary")
    payload = envelope.payload if envelope is not None else None
    status = status_from_envelope(
        envelope, offline=inputs.context.offline, demo=inputs.context.demo
    )
    if not isinstance(payload, DockerPayload):
        return ViewModel(title="Docker", status=status or _empty())

    running, stopped, unhealthy, health_unknown = _counts(payload)
    expected = frozenset(payload.expected_running)
    unexpected = [c.name for c in payload.containers if c.name in expected and c.is_unexpected_exit]
    cpu_total = sum(c.cpu_percent for c in payload.containers if c.cpu_percent is not None)
    mem_total = sum(
        c.memory_used_bytes for c in payload.containers if c.memory_used_bytes is not None
    )
    metrics: list[Metric] = [
        Metric(
            id="context",
            label="context",
            value_text=payload.engine.context or "unset",
            basis="pinned local context",
            privacy=Privacy.PRIVATE,
        ),
        Metric(
            id="containers",
            label="running",
            value_text=f"{running}/{len(payload.containers)}",
            basis=f"{stopped} not running",
        ),
    ]
    if settings.show_unhealthy:
        metrics.append(
            Metric(
                id="unhealthy",
                label="unhealthy",
                value_text=str(unhealthy),
                basis="explicit health status only",
                available=True,
                note=None if health_unknown == 0 else f"{health_unknown} without a healthcheck",
                severity=Severity.WARNING if unhealthy or unexpected else Severity.OK,
            )
        )
    if settings.show_cpu:
        metrics.append(
            Metric(
                id="engine_cpu",
                label="cpu (engine scale)",
                value_text=f"{cpu_total:.1f}",
                unit="%",
                basis="sum of container CPU on the engine scale, not Mac host CPU",
                available=bool(payload.stats_available),
            )
        )
    if settings.show_memory:
        metrics.append(
            Metric(
                id="engine_memory",
                label="mem (containers)",
                value_text=format_bytes(mem_total) or "—",
                basis="container memory reported by the engine; not added to Mac RAM",
                available=bool(payload.stats_available),
            )
        )

    notes = list(payload.notes)
    if unexpected:
        notes.insert(0, "expected running but stopped: " + ", ".join(sorted(unexpected)))
    subtitle = None
    if payload.engine.server_version:
        subtitle = f"{payload.engine.name or 'engine'} {payload.engine.server_version}"
        if payload.engine.cpu_count:
            subtitle += f" · {payload.engine.cpu_count} CPUs"
    return ViewModel(
        title="Docker",
        subtitle=subtitle,
        metrics=tuple(metrics),
        status=status,
        footnote=" · ".join(notes) if notes else None,
    )


def present_table(inputs: PresentInput) -> ViewModel:
    settings = inputs.settings
    assert isinstance(settings, DockerTableSettings)
    envelope = inputs.envelope("primary")
    payload = envelope.payload if envelope is not None else None
    status = status_from_envelope(
        envelope, offline=inputs.context.offline, demo=inputs.context.demo
    )
    if not isinstance(payload, DockerPayload):
        return ViewModel(title="Containers", status=status or _empty())

    rows = [
        container for container in payload.containers if settings.show_stopped or container.running
    ]
    rows = _sorted(rows, settings.sort_by)[: settings.limit]
    expected = frozenset(payload.expected_running)

    columns = [
        TableColumn(id="name", label="container", privacy=Privacy.PRIVATE),
        TableColumn(id="state", label="state"),
    ]
    if settings.show_health:
        columns.append(TableColumn(id="health", label="health"))
    columns.append(TableColumn(id="cpu", label="cpu %", align="right"))
    columns.append(TableColumn(id="mem", label="mem", align="right"))
    if settings.show_network:
        columns.append(TableColumn(id="net", label="net in/out", align="right"))
    if settings.show_restarts:
        columns.append(TableColumn(id="restarts", label="↻", align="right"))

    table_rows: list[TableRow] = []
    for container in rows:
        project = container.compose_project
        service = container.compose_service
        label = container.name
        if service:
            label = f"{service} ({container.name})"
        cells = {
            "name": label,
            "state": container.state,
            "health": health_label(container),
            "cpu": f"{container.cpu_percent:.1f}" if container.cpu_percent is not None else "—",
            "mem": format_bytes(container.memory_used_bytes) or "—",
            "net": (
                f"{format_bytes(container.net_rx_bps) or '—'}/s "
                f"{format_bytes(container.net_tx_bps) or '—'}/s"
            )
            if container.net_rx_bps is not None
            else "—",
            "restarts": (
                f"{container.restarts}"
                + (
                    f" (+{container.restarts_since_sample})"
                    if container.restarts_since_sample
                    else ""
                )
                if container.restarts is not None
                else "—"
            ),
            "_project": project or "",
        }
        table_rows.append(
            TableRow(
                id=f"docker:{container.id}",
                cells=cells,
                privacy=Privacy.PRIVATE,
                severity=severity_for(container, expected),
            )
        )

    groups: list[tuple[str, tuple[str, ...]]] = []
    if settings.group_by == "compose_project" and any(
        row.cells.get("_project") for row in table_rows
    ):
        by_project: dict[str, list[str]] = {}
        for row in table_rows:
            by_project.setdefault(row.cells.get("_project") or "ungrouped", []).append(row.id)
        groups = [(f"project {name}", tuple(ids)) for name, ids in sorted(by_project.items())]

    footnote_parts = [f"{len(rows)} containers"]
    if payload.truncated:
        footnote_parts.append("truncated")
    if payload.stats_sampled_at is not None:
        footnote_parts.append("cursor: ↻ = lifetime restarts")
    return ViewModel(
        title="Containers",
        table=TableView(
            columns=tuple(columns),
            rows=tuple(table_rows),
            groups=tuple(groups),
            truncated=payload.truncated,
            footnote=" · ".join(footnote_parts),
        ),
        status=status,
        footnote=" · ".join(payload.notes) if payload.notes else None,
    )


def _sorted(rows: list[ContainerReading], key: str) -> list[ContainerReading]:
    if key == "cpu_percent":
        return sorted(rows, key=lambda c: -(c.cpu_percent or 0.0))
    if key == "name":
        return sorted(rows, key=lambda c: c.name)
    if key == "project":
        return sorted(rows, key=lambda c: (c.compose_project or "", c.compose_service or ""))
    if key == "state":
        return sorted(rows, key=lambda c: (c.running, c.name))
    return sorted(rows, key=lambda c: -(c.memory_used_bytes or 0))


def _empty() -> StatusView:
    return StatusView(
        severity=Severity.INFO,
        headline="No engine data yet",
        detail="Waiting for the first successful local collection.",
    )
