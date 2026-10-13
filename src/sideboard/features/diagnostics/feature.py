"""Diagnostics feature: host-owned runtime status, no collector and no privileges."""

from __future__ import annotations

from ...core.contracts import EmptySettings, FeatureSpec, WidgetSpec
from .presenter import present_host, present_sources

__all__ = ["FEATURE"]

SOURCES_WIDGET = WidgetSpec(
    type_id="diagnostics.sources",
    settings_model=EmptySettings,
    inputs=(),
    present=present_sources,
    description="One row per configured source instance with demand, freshness and error state.",
    default_category="infrastructure",
    detail="full source table plus setup problems",
    min_width=34,
    min_height=6,
)

HOST_WIDGET = WidgetSpec(
    type_id="diagnostics.host",
    settings_model=EmptySettings,
    inputs=(),
    present=present_host,
    description="Launch mode, applied configuration revision and private data locations.",
    default_category="infrastructure",
    min_width=30,
    min_height=4,
)

FEATURE = FeatureSpec(
    id="diagnostics",
    sources=(),
    widgets=(SOURCES_WIDGET, HOST_WIDGET),
    default_widgets={
        "sources": {"type": "diagnostics.sources"},
    },
    description="Host-owned runtime diagnostics rendered through the standard card shell.",
)
