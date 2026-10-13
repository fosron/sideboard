"""Validated payload for the local Docker collector.

Metric interpretation lives here, not in the shared renderer: engine CPU is not Mac
host CPU, container memory is not host memory, and a running container without a
healthcheck is **running / health unknown**, not verified healthy.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ...core.contracts import SourcePayload

__all__ = ["ContainerReading", "DockerPayload", "EngineInfo", "StorageUsage"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EngineInfo(_Frozen):
    """Identity and capacity of the approved engine. Never a full ``docker info`` dump."""

    context: str
    endpoint: str | None = None
    name: str | None = None
    server_version: str | None = None
    operating_system: str | None = None
    cpu_count: int | None = None
    is_local: bool = False


class ContainerReading(_Frozen):
    id: str
    name: str
    image: str | None = None
    state: str = "unknown"
    status_text: str | None = None
    health: str | None = None  # healthy / unhealthy / starting / None (no healthcheck defined)
    health_defined: bool = False
    compose_project: str | None = None
    compose_service: str | None = None
    running: bool = False
    cpu_percent: float | None = None
    cpu_percent_of_engine: float | None = None
    memory_used_bytes: int | None = None
    memory_limit_bytes: int | None = None
    memory_percent: float | None = None
    net_rx_bytes: int | None = None
    net_tx_bytes: int | None = None
    net_rx_bps: float | None = None
    net_tx_bps: float | None = None
    block_read_bytes: int | None = None
    block_write_bytes: int | None = None
    block_read_bps: float | None = None
    block_write_bps: float | None = None
    restarts: int | None = None
    restarts_since_sample: int | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    rate_gap: bool = False
    note: str | None = None

    @property
    def is_unexpected_exit(self) -> bool:
        """Only an explicitly expected-running service can be an unexpected exit."""
        return not self.running and self.state in {"exited", "dead"}


class StorageUsage(_Frozen):
    kind: str
    total: int | None = None
    active: int | None = None
    size_bytes: int | None = None
    reclaimable_bytes: int | None = None
    note: str | None = None


class DockerPayload(SourcePayload):
    engine: EngineInfo
    containers: tuple[ContainerReading, ...] = ()
    storage: tuple[StorageUsage, ...] = ()
    stats_sampled_at: datetime | None = None
    metadata_sampled_at: datetime | None = None
    stats_available: bool = False
    storage_available: bool = False
    truncated: bool = False
    expected_running: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
