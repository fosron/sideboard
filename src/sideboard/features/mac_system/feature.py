"""Mac statistics feature registration.

Mac statistics are an ordinary feature module, not a special branch in ``app.py``.
The same registration path serves a simple feed, and the same path a future widget.
"""

from __future__ import annotations

from ...core.contracts import FeatureSpec, InputBinding, WidgetSpec
from .config import MacDetailsSettings, MacSummarySettings
from .presenter import present_details, present_summary
from .provider import make_spec

__all__ = ["FEATURE"]

_CADENCE_FIELDS = ("active_seconds", "ambient_seconds", "background_seconds")

SUMMARY_WIDGET = WidgetSpec(
    type_id="mac.summary",
    settings_model=MacSummarySettings,
    inputs=(InputBinding(name="primary", accepts=frozenset({"mac.metrics"})),),
    present=present_summary,
    description="CPU, RAM, disk free, network rates and battery, with a CPU sparkline.",
    default_category="infrastructure",
    detail="same metrics with short histories",
    min_width=30,
    min_height=4,
)

DETAILS_WIDGET = WidgetSpec(
    type_id="mac.details",
    settings_model=MacDetailsSettings,
    inputs=(InputBinding(name="primary", accepts=frozenset({"mac.metrics"})),),
    present=present_details,
    description="Full local metrics: swap, uptime, per-volume disk, disk I/O and histories.",
    default_category="infrastructure",
    detail="optional process panel when include_processes is enabled on the source",
    min_width=34,
    min_height=6,
)

FEATURE = FeatureSpec(
    id="mac_system",
    sources=(make_spec(),),
    widgets=(SUMMARY_WIDGET, DETAILS_WIDGET),
    default_widgets={
        "mac_summary": {"type": "mac.summary", "sources": {"primary": "mac"}},
    },
    description="Local Mac statistics from psutil, with field-level unavailable states.",
)
