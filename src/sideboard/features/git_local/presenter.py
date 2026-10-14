"""Presentation for local checkouts.

The card is a working-set view, not a git client: which checkout has uncommitted work,
which is behind the remote, and how long ago each was touched. Anything deeper is one
key press away, and the file list is deliberately not shown — it is the part that grows
without bound and cannot be read in a card row.
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
from .config import GitStatusWidgetSettings
from .models import GitPayload, RepoStatus

__all__ = ["present_status"]


def _age(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "—"
    seconds = max(0.0, (now - moment).total_seconds())
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    if seconds < 48 * 3600:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / (24 * 3600))}d"


def _local_changes(repo: RepoStatus) -> str:
    """One number for "work not committed", because that is the decision it drives."""
    return str(repo.changed + repo.untracked + repo.unmerged)


def _order(repo: RepoStatus) -> tuple[int, float]:
    """Dirty first, then most recently touched."""
    stamp = repo.last_commit_at.timestamp() if repo.last_commit_at is not None else 0.0
    return (0 if repo.dirty else 1, -stamp)


def _item(repo: RepoStatus, now: datetime) -> Item:
    meta: list[tuple[str, str]] = []
    changes = _local_changes(repo)
    if changes != "0":
        meta.append(("changed", changes))
    if repo.ahead:
        meta.append(("ahead", str(repo.ahead)))
    if repo.behind:
        meta.append(("behind", str(repo.behind)))
    return Item(
        id=f"repo:{repo.path}",
        title=repo.last_commit_subject or repo.name,
        subtitle=repo.note,
        category="git",
        published_at=repo.last_commit_at,
        meta=tuple(meta),
        badge=repo.branch or repo.name,
        privacy=Privacy.PRIVATE,
        source_id=repo.name,
    )


def present_status(inputs: PresentInput) -> ViewModel:
    settings = inputs.settings
    assert isinstance(settings, GitStatusWidgetSettings)
    envelope = inputs.envelope("primary")
    payload = envelope.payload if envelope is not None else None
    status = status_from_envelope(
        envelope, offline=inputs.context.offline, demo=inputs.context.demo
    )
    if not isinstance(payload, GitPayload):
        return ViewModel(title="Git", status=status or _unconfigured())

    now = inputs.context.now
    dirty = [repo for repo in payload.repos if repo.dirty]
    behind = [repo for repo in payload.repos if repo.behind]
    metrics: list[Metric] = [
        Metric(
            id="repos",
            label="checkouts",
            value_text=str(len(payload.repos)),
            basis="local paths watched",
            privacy=Privacy.PRIVATE,
        ),
        Metric(
            id="dirty",
            label="uncommitted",
            value_text=str(len(dirty)),
            basis="checkouts with staged, modified, untracked or conflicted files",
            privacy=Privacy.PRIVATE,
            severity=Severity.WARNING if dirty else Severity.OK,
        ),
        Metric(
            id="behind",
            label="behind",
            value_text=str(len(behind)),
            basis="checkouts with commits to pull",
            privacy=Privacy.PRIVATE,
            severity=Severity.WARNING if behind else Severity.OK,
        ),
    ]

    if inputs.context.mode is ViewMode.DETAIL:
        return ViewModel(
            title="Git",
            metrics=tuple(metrics),
            table=_table(payload, now),
            status=status,
            footnote=" · ".join(payload.notes) if payload.notes else None,
            privacy=Privacy.PRIVATE,
        )

    repos = [repo for repo in payload.repos if not (settings.hide_clean and not repo.dirty)]
    repos.sort(key=_order)
    if not repos:
        return ViewModel(
            title="Git",
            metrics=tuple(metrics),
            status=StatusView(
                severity=Severity.OK,
                headline="Nothing uncommitted",
                detail="Every watched checkout is clean.",
            ),
            privacy=Privacy.PRIVATE,
        )
    return ViewModel(
        title="Git",
        metrics=tuple(metrics),
        items=tuple(_item(repo, now) for repo in repos[: settings.limit]),
        status=status,
        privacy=Privacy.PRIVATE,
    )


def _table(payload: GitPayload, now: datetime) -> TableView:
    rows: list[TableRow] = []
    for repo in sorted(payload.repos, key=_order):
        rows.append(
            TableRow(
                id=f"repo:{repo.path}",
                cells={
                    "repo": repo.name,
                    "branch": repo.branch or "detached",
                    "changed": str(repo.changed + repo.untracked + repo.unmerged),
                    "ahead": str(repo.ahead),
                    "behind": str(repo.behind),
                    "last": _age(repo.last_commit_at, now),
                    "subject": repo.last_commit_subject or repo.note or "—",
                },
                privacy=Privacy.PRIVATE,
                severity=Severity.WARNING if repo.dirty else Severity.OK,
            )
        )
    return TableView(
        columns=(
            TableColumn(id="repo", label="checkout", privacy=Privacy.PRIVATE),
            TableColumn(id="branch", label="branch", privacy=Privacy.PRIVATE),
            TableColumn(id="changed", label="changed", align="right", privacy=Privacy.PRIVATE),
            TableColumn(id="ahead", label="ahead", align="right", privacy=Privacy.PRIVATE),
            TableColumn(id="behind", label="behind", align="right", privacy=Privacy.PRIVATE),
            TableColumn(id="last", label="last", align="right", privacy=Privacy.PRIVATE),
            TableColumn(id="subject", label="last commit", privacy=Privacy.PRIVATE),
        ),
        rows=tuple(rows),
        footnote="read with git status --porcelain=v2 and git log -1; nothing is fetched or staged",
    )


def _unconfigured() -> StatusView:
    return StatusView(
        severity=Severity.INFO,
        headline="No checkouts configured",
        detail="List local checkout paths to see branch state and uncommitted work.",
        hints=('sources.git.settings.repos = ["~/Documents/repos/demo-app"]',),
    )
