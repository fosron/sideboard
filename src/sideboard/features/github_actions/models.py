"""Validated GitHub Actions payloads."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ...core.contracts import SourcePayload

RUNNING_STATUSES = frozenset({"queued", "in_progress", "requested", "waiting", "pending"})
FAILED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "startup_failure", "cancelled", "action_required"}
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunReading(_Frozen):
    """One workflow run, as the API describes it. No derived health is stored."""

    repo: str
    run_id: int = 0
    workflow: str = "unnamed workflow"
    status: str = "unknown"
    conclusion: str | None = None
    branch: str | None = None
    event: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    url: str | None = None

    @property
    def running(self) -> bool:
        """Queued counts as running: the user is waiting for it either way."""
        return self.status in RUNNING_STATUSES

    @property
    def failed(self) -> bool:
        return (self.conclusion or "").lower() in FAILED_CONCLUSIONS

    @property
    def short_repo(self) -> str:
        return self.repo.split("/", 1)[-1]


class GithubActionsPayload(SourcePayload):
    repos: tuple[str, ...] = ()
    runs: tuple[RunReading, ...] = ()
    #: Kept explicit rather than derived on every read: the window is a settings choice.
    running: tuple[RunReading, ...] = ()
    failures: tuple[RunReading, ...] = ()
    sampled_at: datetime | None = None
    notes: tuple[str, ...] = Field(default_factory=tuple)
