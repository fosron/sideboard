"""Typed settings for the local Docker collector."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["DockerSettings", "DockerSummarySettings", "DockerTableSettings"]

# Prefixes that mean "not the local engine". An approved local Unix socket is the
# only accepted endpoint: no SSH, no TCP, no npipe.
REMOTE_ENDPOINT_PREFIXES = ("ssh://", "tcp://", "http://", "https://", "npipe://")


class DockerSettings(BaseModel):
    """Connection scope and cadence. ``context`` has no guessed default."""

    model_config = ConfigDict(extra="forbid")

    context: str = ""
    local_only: bool = True
    active_seconds: float | None = 15.0
    ambient_seconds: float | None = 15.0
    background_seconds: float | None = 60.0
    stats_seconds: float = 15.0
    metadata_seconds: float = 30.0
    include_projects: list[str] = Field(default_factory=list)
    expected_running: list[str] = Field(default_factory=list)
    include_storage: bool = False
    storage_seconds: float = 1800.0
    inventory_limit: int = 100
    stats_batch_limit: int = 60
    process_timeout_seconds: float = 8.0
    allow_non_unix_endpoint: bool = False

    @field_validator("context")
    @classmethod
    def _no_endpoint_in_context(cls, value: str) -> str:
        lowered = value.lower()
        if lowered.startswith(REMOTE_ENDPOINT_PREFIXES):
            raise ValueError(
                "context must be a context name, not a remote endpoint; "
                "remote engines are not supported"
            )
        if lowered.startswith("unix://"):
            raise ValueError("pin a Docker context name; the endpoint is inspected instead")
        return value


class DockerSummarySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    show_unhealthy: bool = True
    show_cpu: bool = True
    show_memory: bool = True


class DockerTableSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_by: str = "compose_project"
    sort_by: str = "memory_bytes"
    limit: int = 40
    show_health: bool = True
    show_network: bool = True
    show_restarts: bool = True
    show_stopped: bool = True
