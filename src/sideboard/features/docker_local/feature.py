"""Local Docker feature registration."""

from __future__ import annotations

from ...core.contracts import FeatureSpec, InputBinding, WidgetSpec
from .config import DockerSummarySettings, DockerTableSettings
from .presenter import present_summary, present_table
from .provider import make_spec

__all__ = ["FEATURE"]

SUMMARY_WIDGET = WidgetSpec(
    type_id="docker.summary",
    settings_model=DockerSummarySettings,
    inputs=(InputBinding(name="primary", accepts=frozenset({"docker.containers"})),),
    present=present_summary,
    description="Context label, running/stopped/unhealthy counts and engine CPU/memory.",
    default_category="infrastructure",
    detail="counts, engine identity and collected notes",
    min_width=32,
    min_height=5,
)

TABLE_WIDGET = WidgetSpec(
    type_id="docker.table",
    settings_model=DockerTableSettings,
    inputs=(InputBinding(name="primary", accepts=frozenset({"docker.containers"})),),
    present=present_table,
    description="Containers grouped by Compose project with state, health, CPU, memory and I/O.",
    default_category="infrastructure",
    detail="grouped table with restart counts and collected notes",
    min_width=40,
    min_height=6,
)

FEATURE = FeatureSpec(
    id="docker_local",
    sources=(make_spec(),),
    widgets=(SUMMARY_WIDGET, TABLE_WIDGET),
    default_sources={
        "docker": {
            "type": "docker.containers",
            "enabled": False,
            "settings": {
                "context": "",
                "local_only": True,
                "stats_seconds": 15.0,
                "metadata_seconds": 30.0,
                "background_seconds": 60.0,
                "include_storage": False,
                "expected_running": [],
            },
        }
    },
    default_widgets={
        "docker_summary": {"type": "docker.summary", "sources": {"primary": "docker"}},
    },
    description="Read-only local Docker-compatible engine monitoring.",
)
