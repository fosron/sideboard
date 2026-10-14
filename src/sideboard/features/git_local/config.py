"""Configuration for the local git source and its widget."""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Local checkout paths. A path that is not a checkout is reported, not followed.
    repos: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    valid_seconds: float = Field(default=600.0, gt=0)

    @field_validator("repos")
    @classmethod
    def _paths_are_paths(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            path = raw.strip()
            if not path:
                continue
            if path.startswith("-"):
                raise ValueError(f"a repository path may not start with a dash: {raw!r}")
            # Expanded once, here: the provider is async and must not touch the filesystem
            # through pathlib, and every later consumer wants a real path anyway.

            # these two rules contradict each other here; the async one is the real risk.
            expanded = os.path.expanduser(path)  # noqa: PTH111 - no pathlib in async
            if expanded not in cleaned:
                cleaned.append(expanded)
        return cleaned


class GitStatusWidgetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Rows shown in the compact card.
    limit: int = Field(default=8, ge=1, le=30)
    #: Clean checkouts are usually not worth a row; the metric still counts them.
    hide_clean: bool = False
