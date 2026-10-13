"""Diagnostics: host-owned runtime status, rendered like any other widget."""

from __future__ import annotations

from datetime import datetime

from ...core.contracts import (
    Demand,
    InputBinding,
    Metric,
    PresentInput,
    Privacy,
    Severity,
    StatusView,
    TableColumn,
    TableRow,
    TableView,
    ViewContext,
    ViewModel,
)

__all__ = ["present_host", "present_sources"]


def _age(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    seconds = (now - moment).total_seconds()
    if seconds < 0:
        return "now"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    return f"{int(seconds / 3600)}h"


def present_sources(inputs: PresentInput) -> ViewModel:
    """One row per configured source instance: the honest status of the whole app."""
    runtime = inputs.context.runtime
    if runtime is None:
        return ViewModel(
            title="Sources",
            status=StatusView(
                severity=Severity.INFO,
                headline="Runtime status unavailable",
                detail="This widget reads host-owned collection status.",
            ),
        )
    rows: list[TableRow] = []
    problems: list[str] = []
    for status in runtime.sources:
        error = status.error_category or ""
        cells = {
            "source": status.source_id,
            "type": status.type_id,
            "demand": str(status.demand),
            "state": _state_label(status),
            "fresh": str(status.freshness),
            "age": _age(status.last_success_at, inputs.context.now),
            "every": f"{status.interval_seconds:.0f}s" if status.interval_seconds else "import",
            "error": error,
        }
        severity = Severity.OK
        if status.connectivity.value in {"error", "blocked", "unavailable"}:
            severity = Severity.WARNING
        if status.freshness.value in {"stale", "expired"}:
            severity = Severity.WARNING
        if not status.has_payload and status.connectivity.value == "unconfigured":
            severity = Severity.INFO
        rows.append(
            TableRow(
                id=f"source:{status.source_id}",
                cells=cells,
                privacy=Privacy.PUBLIC if _public_source(status.type_id) else Privacy.PRIVATE,
                severity=severity,
            )
        )
        if status.requires_setup and not status.has_payload:
            problems.append(f"{status.source_id}: {status.setup_hint or 'needs setup'}")

    stale = runtime.stale_count
    metrics = (
        Metric(
            id="sources", label="sources", value_text=str(len(runtime.sources)), basis="configured"
        ),
        Metric(
            id="stale",
            label="stale",
            value_text=str(stale),
            basis="beyond the freshness window",
            severity=Severity.WARNING if stale else Severity.OK,
        ),
        Metric(
            id="unavailable",
            label="unavailable",
            value_text=str(runtime.unavailable_count),
            basis="blocked or unreachable",
            severity=Severity.WARNING if runtime.unavailable_count else Severity.OK,
        ),
    )
    return ViewModel(
        title="Sources",
        subtitle=(
            f"config revision {runtime.config_revision}"
            + (" · offline" if runtime.offline else "")
            + (" · demo" if runtime.demo else "")
        ),
        metrics=metrics,
        table=TableView(
            columns=(
                TableColumn(id="source", label="source", privacy=Privacy.PRIVATE),
                TableColumn(id="type", label="type"),
                TableColumn(id="demand", label="demand"),
                TableColumn(id="state", label="state"),
                TableColumn(id="fresh", label="fresh"),
                TableColumn(id="age", label="age", align="right"),
                TableColumn(id="every", label="every", align="right"),
                TableColumn(id="error", label="error"),
            ),
            rows=tuple(rows),
            footnote="age is the last successful acquisition",
        ),
        footnote=" · ".join(problems[:3]) if problems else None,
    )


def _state_label(status: object) -> str:
    connectivity = str(getattr(status, "connectivity", "unknown"))
    if connectivity == "ok":
        return "collecting"
    return connectivity or "unknown"


def _public_source(type_id: str) -> bool:
    """Public feeds are not private identifiers; local data is."""
    return type_id.split(".", 1)[0] in {"news", "hackernews", "weather", "youtube", "reddit"}


def present_host(inputs: PresentInput) -> ViewModel:
    """Paths, mode flags and pending writes. Useful when the dashboard misbehaves."""
    runtime = inputs.context.runtime
    if runtime is None:
        return ViewModel(
            title="Host",
            status=StatusView(
                severity=Severity.INFO,
                headline="Runtime status unavailable",
                detail="This widget reads host-owned status.",
            ),
        )
    metrics = (
        Metric(
            id="mode",
            label="mode",
            value_text=("demo" if runtime.demo else "offline" if runtime.offline else "normal"),
            basis="launch flag",
        ),
        Metric(
            id="revision",
            label="config",
            value_text=f"revision {runtime.config_revision}",
            basis="applied configuration",
        ),
        Metric(
            id="pending",
            label="pending writes",
            value_text=str(runtime.pending_writes),
            basis="metric aggregates queued",
        ),
        Metric(
            id="tab",
            label="active tab",
            value_text=runtime.active_tab or "—",
            basis="collection demand follows this",
        ),
    )
    return ViewModel(
        title="Host",
        metrics=metrics,
        footnote=f"config {runtime.config_path} · db {runtime.database_path} · logs {runtime.log_path}",
    )


_ = (Demand, ViewContext, InputBinding)
