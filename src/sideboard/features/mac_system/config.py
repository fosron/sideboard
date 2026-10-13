"""Typed settings for the Mac statistics collector."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["MacDetailsSettings", "MacSettings", "MacSummarySettings"]

# Interfaces that duplicate traffic or never carry real host traffic. Kept as a
# documented default so `network_interfaces = []` means "auto-select", not "sum everything".
VIRTUAL_INTERFACE_PREFIXES = (
    "lo",
    "gif",
    "stf",
    "bridge",
    "ap",
    "awdl",
    "llw",
    "utun",
    "ipsec",
    "vmenet",
    "anpi",
    "p2p",
    "tap",
    "tun",
    "vmnet",
)


class MacSettings(BaseModel):
    """Sampling policy. Cadence fields are optional instance-level overrides."""

    model_config = ConfigDict(extra="forbid")

    active_seconds: float | None = 5.0
    ambient_seconds: float | None = 15.0
    background_seconds: float | None = 60.0
    volumes: list[str] = Field(default_factory=lambda: ["/"])
    network_interfaces: list[str] = Field(default_factory=list)
    include_processes: bool = False
    process_limit: int = 5
    include_per_core: bool = False
    disk_refresh_seconds: float = 60.0
    uptime_refresh_seconds: float = 300.0


class MacSummarySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    show_network: bool = True
    show_battery: bool = True
    show_disk: bool = True
    sparkline: bool = True


class MacDetailsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    show_per_core: bool = True
    show_swap: bool = True
    show_processes: bool = False
    show_disk_io: bool = True
