"""Configuration for the GitHub Actions source and its widget."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GithubActionsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Repositories to watch, as ``owner/name``. Empty means the source is unconfigured.
    repos: list[str] = Field(default_factory=list)
    #: Runs fetched per repository, newest first. Bounded: the query is one call.
    run_limit: int = Field(default=12, ge=1, le=50)
    #: A failed run older than this is history, not a signal.
    failure_hours: float = Field(default=24.0, gt=0, le=168)
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    #: How long the payload is treated as current.
    valid_seconds: float = Field(default=180.0, gt=0)

    @field_validator("repos")
    @classmethod
    def _repos_are_owner_slash_name(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            repo = raw.strip()
            if not repo:
                continue
            if not REPO_PATTERN.match(repo):
                raise ValueError(f"repository must be owner/name, not {raw!r}")
            if repo not in cleaned:
                cleaned.append(repo)
        return cleaned


class GithubActionsWidgetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Failures shown after the running ones.
    show_failures: bool = True
    #: Rows shown in the compact card.
    limit: int = Field(default=8, ge=1, le=30)
