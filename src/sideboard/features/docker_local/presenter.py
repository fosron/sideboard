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
    ViewMode,
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
    """Health is only a live signal for a running container.

    A running container without a healthcheck is *unknown*, never "verified healthy".
    A stopped container reads "-": the state column already says it exited, and its last
    health status is history.
    """
    if not container.running:
        return "-"
    if container.health:
        return container.health
    return "unknown"


def severity_for(container: ContainerReading, expected: frozenset[str]) -> Severity:
    # Only a *running* unhealthy container is a live problem; a stopped one is history.
    if container.health_status == "unhealthy":
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
    unhealthy = sum(1 for c in payload.containers if c.health_status == "unhealthy")
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

    detail = inputs.context.mode is ViewMode.DETAIL
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
                basis="running containers with an explicit unhealthy status",
                available=True,
                note=(
                    f"{health_unknown} running without a healthcheck · "
                    f"{stopped} not running (their last status is history)"
                ),
                severity=Severity.WARNING if unhealthy or unexpected else Severity.OK,
            )
        )
    if settings.show_restarts:
        churn = sum(c.restarts_since_sample or 0 for c in payload.containers)
        known = any(c.restarts_since_sample is not None for c in payload.containers)
        if known or detail:
            # Without a previous sample there is no delta to show. On the card that means
            # no metric rather than one that says it cannot answer; the detail view keeps
            # the line and explains it.
            metrics.append(
                Metric(
                    id="restarts",
                    label="restarts",
                    value_text=str(churn),
                    basis=("since the previous sample; lifetime counts are in the container table"),
                    available=known,
                    note=(
                        None
                        if known
                        else "no baseline yet: the first sample after a start shows no delta"
                    ),
                    severity=Severity.WARNING if churn else Severity.OK,
                )
            )
    if settings.show_cpu:
        metrics.append(
            Metric(
                id="engine_cpu",
                label="cpu (engine)",
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

    if settings.show_storage:
        metrics.extend(_storage_metrics(payload, detail=detail))
    # The totals say the engine is busy; naming the heaviest containers says why. This is
    # the one part of the summary that answers a question, rather than reporting a state.
    heaviest = sorted(
        (c for c in payload.containers if c.running and c.memory_used_bytes is not None),
        key=lambda c: -(c.memory_used_bytes or 0),
    )
    limit = settings.top_containers if not detail else max(settings.top_containers, 8)
    for container in heaviest[: max(0, limit)]:
        metrics.append(
            Metric(
                id=f"mem_{container.name}",
                label=f"mem · {container.compose_service or container.name}",
                value_text=format_bytes(container.memory_used_bytes) or "—",
                basis=(
                    f"{container.name}"
                    + (
                        f" · {container.cpu_percent:.1f} % cpu"
                        if container.cpu_percent is not None
                        else ""
                    )
                ),
                privacy=Privacy.PRIVATE,
            )
        )

    # On the card a note has to be a problem. Warm-up and provenance notes ("rate baseline
    # reset for: …") are true and belong in the detail view; on the card they were a
    # sentence about measurement wrapped under a row of numbers.
    notes = list(payload.notes) if detail else []
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
        # Notes are problems ("expected running but stopped"), so they stay on the card;
        # on a card with nothing wrong they are absent rather than a reassurance line.
        footnote=" · ".join(notes) if notes else None,
    )


def _storage_metrics(payload: DockerPayload, *, detail: bool) -> list[Metric]:
    """What Docker is using on disk, from ``docker system df``.

    A container host fills up on images and build cache, which no container-level metric
    shows. Storage is optional on the source: when it is off, the card says so instead of
    rendering a zero.
    """
    if not payload.storage_available or not payload.storage:
        if not detail:
            # On the card, no storage means no storage line: "set include_storage" is a
            # configuration note that wrapped over two rows under a row of numbers.
            return []
        return [
            Metric.unavailable(
                "disk",
                "docker disk",
                "storage is off or unavailable: set include_storage on the docker source",
            )
        ]
    used = sum(s.size_bytes for s in payload.storage if s.size_bytes is not None)
    reclaimable = sum(
        s.reclaimable_bytes for s in payload.storage if s.reclaimable_bytes is not None
    )
    out = [
        Metric(
            id="disk",
            label="docker disk",
            value_text=format_bytes(used) or "—",
            basis="images, containers, volumes and build cache (docker system df)",
        ),
        Metric(
            id="reclaimable",
            label="reclaimable",
            value_text=format_bytes(reclaimable) or "—",
            basis="what a prune would free",
            severity=Severity.INFO if reclaimable else Severity.OK,
        ),
    ]
    if detail:
        for item in payload.storage:
            out.append(
                Metric(
                    id=f"storage_{item.kind}",
                    label=item.kind,
                    value_text=format_bytes(item.size_bytes) or "—",
                    basis=item.note or f"{item.total or 0} total · {item.active or 0} active",
                )
            )
    return out


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
