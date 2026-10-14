"""Validated local git payloads."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ...core.contracts import SourcePayload


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RepoStatus(_Frozen):
    """One checkout: branch state, local changes and the last commit."""

    path: str
    name: str
    branch: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    changed: int = 0
    untracked: int = 0
    unmerged: int = 0
    last_commit_at: datetime | None = None
    last_commit_sha: str | None = None
    last_commit_subject: str | None = None
    note: str | None = None

    @property
    def dirty(self) -> bool:
        """Anything uncommitted, including untracked files and conflicts."""
        return bool(self.changed or self.untracked or self.unmerged)

    @property
    def diverged(self) -> bool:
        return bool(self.ahead or self.behind)


class GitPayload(SourcePayload):
    repos: tuple[RepoStatus, ...] = ()
    sampled_at: datetime | None = None
    notes: tuple[str, ...] = Field(default_factory=tuple)
