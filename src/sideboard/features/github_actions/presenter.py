"""Presentation for GitHub Actions runs.

The card answers one question first — is anything running right now — and then the
second one that matters on a working day: did anything fail while I was not looking.
Failures inside the configured window are listed with their age; older ones are
history and stay in the detail view.
"""

from __future__ import annotations

from datetime import datetime

from ...core.contracts import (
    Item,
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
from .config import GithubActionsWidgetSettings
from .models import GithubActionsPayload, RunReading

__all__ = ["present_actions"]


def _age(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "undated"
    seconds = max(0.0, (now - moment).total_seconds())
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    if seconds < 48 * 3600:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / (24 * 3600))}d"


def _status_text(run: RunReading) -> str:
    if run.running:
        return run.status.replace("_", " ")
    return (run.conclusion or run.status).replace("_", " ")


def _item(run: RunReading, now: datetime, *, badge: str | None = None) -> Item:
    """One run: what it is, where, and how long it has been going or been broken."""
    return Item(
        id=f"run:{run.repo}:{run.run_id}",
        title=f"{run.workflow} · {run.branch or 'no branch'}",
        url=run.url,
        category="github",
        published_at=run.created_at,
        meta=(("age", _age(run.updated_at or run.created_at, now)),),
        badge=badge or run.short_repo,
        privacy=Privacy.PRIVATE,
        source_id=run.repo,
    )


def _severity(running: int, failures: int) -> Severity:
    if failures:
        return Severity.WARNING
    if running:
        return Severity.INFO
    return Severity.OK


def present_actions(inputs: PresentInput) -> ViewModel:
    settings = inputs.settings
    assert isinstance(settings, GithubActionsWidgetSettings)
    envelope = inputs.envelope("primary")
    payload = envelope.payload if envelope is not None else None
    status = status_from_envelope(
        envelope, offline=inputs.context.offline, demo=inputs.context.demo
    )
    if not isinstance(payload, GithubActionsPayload):
        return ViewModel(title="Actions", status=status or _unconfigured())

    now = inputs.context.now
    running = payload.running
    failures = payload.failures
    metrics: list[Metric] = [
        Metric(
            id="running",
            label="running",
            value_text=str(len(running)),
            basis="workflow runs queued or in progress",
            privacy=Privacy.PRIVATE,
        ),
        Metric(
            id="failed",
            label="failed",
            value_text=str(len(failures)),
            basis="failed runs inside the configured window",
            privacy=Privacy.PRIVATE,
            severity=Severity.WARNING if failures else Severity.OK,
        ),
    ]
    if len(payload.repos) != 1:
        metrics.append(
            Metric(
                id="repos",
                label="repos",
                value_text=str(len(payload.repos)),
                basis="repositories watched",
                privacy=Privacy.PRIVATE,
            )
        )

    if inputs.context.mode is ViewMode.DETAIL:
        return ViewModel(
            title="Actions",
            metrics=tuple(metrics),
            table=_table(payload, now, limit=settings.limit * 3),
            status=status,
            footnote=" · ".join(payload.notes) if payload.notes else None,
            privacy=Privacy.PRIVATE,
        )

    items: list[Item] = [_item(run, now) for run in running]
    if settings.show_failures:
        items.extend(_item(run, now, badge=f"{run.short_repo} ✗") for run in failures)
    if not items:
        # Nothing running, nothing failed: the two metrics already say exactly that, and a
        # reassurance line under them is text a glance does not need.
        return ViewModel(
            title="Actions",
            metrics=tuple(metrics),
            privacy=Privacy.PRIVATE,
        )
    return ViewModel(
        title="Actions",
        metrics=tuple(metrics),
        items=tuple(items[: settings.limit]),
        status=status,
        privacy=Privacy.PRIVATE,
    )


def _table(payload: GithubActionsPayload, now: datetime, *, limit: int) -> TableView:
    rows: list[TableRow] = []
    for run in payload.runs[:limit]:
        rows.append(
            TableRow(
                id=f"run:{run.repo}:{run.run_id}",
                url=run.url,
                cells={
                    "workflow": run.workflow,
                    "repo": run.short_repo,
                    "status": _status_text(run),
                    "branch": run.branch or "—",
                    "event": run.event or "—",
                    "age": _age(run.updated_at or run.created_at, now),
                },
                privacy=Privacy.PRIVATE,
                severity=Severity.WARNING if run.failed else Severity.OK,
            )
        )
    return TableView(
        columns=(
            TableColumn(id="workflow", label="workflow", privacy=Privacy.PRIVATE),
            TableColumn(id="repo", label="repo", privacy=Privacy.PRIVATE),
            TableColumn(id="status", label="status", privacy=Privacy.PRIVATE),
            TableColumn(id="branch", label="branch", privacy=Privacy.PRIVATE),
            TableColumn(id="event", label="event", privacy=Privacy.PRIVATE),
            TableColumn(id="age", label="age", align="right", privacy=Privacy.PRIVATE),
        ),
        rows=tuple(rows),
        truncated=len(payload.runs) > limit,
        footnote="runs are read with `gh api graphql`; nothing here can cancel or retry one",
    )


def _unconfigured() -> StatusView:
    return StatusView(
        severity=Severity.INFO,
        headline="GitHub Actions not configured",
        detail="Authenticate the GitHub CLI and list repositories as owner/name.",
        hints=("gh auth login", 'sources.github.settings.repos = ["owner/repo"]'),
    )
