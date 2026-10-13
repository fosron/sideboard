"""Registration for the generic local snapshot feature.

One source type (``local_snapshot.file``) serves two widgets
(``local_snapshot.metrics`` and ``local_snapshot.items``); both read the same validated
payload, so mounting them together needs no second file read. ``path`` must point at an
explicitly selected, allowlisted regular file that matches
``src/sideboard/schemas/local-snapshot.schema.json``.
"""

from __future__ import annotations

from ...core.contracts import (
    Cadence,
    Capability,
    FeatureSpec,
    InputBinding,
    SourceSpec,
    WidgetSpec,
)
from .config import LocalSnapshotSettings, SnapshotItemsSettings, SnapshotMetricsSettings
from .models import LOCAL_SNAPSHOT_SCHEMA, SnapshotPayload
from .presenter import present_items, present_metrics
from .provider import build_provider, demo_payload

__all__ = ["FEATURE", "FILE_SOURCE", "ITEMS_WIDGET", "METRICS_WIDGET"]

FILE_SOURCE = SourceSpec(
    type_id="local_snapshot.file",
    payload_model=SnapshotPayload,
    settings_model=LocalSnapshotSettings,
    payload_schema=LOCAL_SNAPSHOT_SCHEMA,
    payload_schema_version=1,
    factory=build_provider,
    capabilities=frozenset({Capability.FILE}),
    cadence=Cadence(active_seconds=30.0, ambient_seconds=60.0, background_seconds=120.0),
    description="A bounded, validated JSON snapshot written by a local producer.",
    requires_setup=True,
    setup_check=lambda settings: bool(settings.path),
    setup_hint="Point this source at an explicitly selected JSON snapshot file.",
    demo_builder=demo_payload,
)

METRICS_WIDGET = WidgetSpec(
    type_id="local_snapshot.metrics",
    settings_model=SnapshotMetricsSettings,
    inputs=(
        InputBinding(
            name="primary",
            accepts=frozenset({LOCAL_SNAPSHOT_SCHEMA}),
            # Not required: a missing/expired file should reach the presenter, which
            # renders the honest status, instead of the generic unconfigured card.
            required=False,
        ),
    ),
    present=present_metrics,
    description="Snapshot metrics with optional producer histories as sparklines.",
    detail="Detail mode lists every metric in a table; compact mode shows the first few.",
    min_width=28,
    min_height=3,
)

ITEMS_WIDGET = WidgetSpec(
    type_id="local_snapshot.items",
    settings_model=SnapshotItemsSettings,
    inputs=(
        InputBinding(
            name="primary",
            accepts=frozenset({LOCAL_SNAPSHOT_SCHEMA}),
            required=False,
        ),
    ),
    present=present_items,
    description="Short list from a local JSON snapshot (jobs, reports, backup runs).",
    detail="Detail mode shows more items than the compact card.",
    min_width=28,
    min_height=3,
)

FEATURE = FeatureSpec(
    id="local_snapshot",
    sources=(FILE_SOURCE,),
    widgets=(METRICS_WIDGET, ITEMS_WIDGET),
    description="Generic consumer for produced JSON snapshots (plan section 6.7).",
)
