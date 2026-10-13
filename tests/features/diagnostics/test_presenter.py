"""Diagnostics presentation: host-owned runtime status rendered by a widget."""

from __future__ import annotations

from datetime import UTC, datetime

from sideboard.core.contracts import (
    Demand,
    EmptySettings,
    PresentInput,
    RuntimeStatus,
    SourceStatusSummary,
    ViewContext,
)
from sideboard.features.diagnostics.presenter import present_host, present_sources

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def test_diagnostics_lists_source_states_and_setup_problems() -> None:
    runtime = RuntimeStatus(
        sources=(
            SourceSummary("mac", "mac.metrics", Demand.ACTIVE, "ok", "fresh", has_payload=True),
            SourceSummary(
                "docker",
                "docker.containers",
                Demand.NONE,
                "unavailable",
                "none",
                requires_setup=True,
                setup_hint="pin a local context",
            ),
        ),
        stale_count=1,
        unavailable_count=1,
        config_revision=3,
        active_tab="systems",
        config_path="/tmp/config.toml",
        database_path="/tmp/sideboard.sqlite3",
        log_path="/tmp/logs",
    )
    view = present_sources(
        PresentInput(
            settings=EmptySettings(), inputs={}, context=ViewContext(now=NOW, runtime=runtime)
        )
    )
    assert view.table is not None
    assert len(view.table.rows) == 2
    assert "pin a local context" in (view.footnote or "")
    assert view.metric("stale") is not None

    host = present_host(
        PresentInput(
            settings=EmptySettings(), inputs={}, context=ViewContext(now=NOW, runtime=runtime)
        )
    )
    assert host.metric("mode") is not None
    assert "/tmp/config.toml" in (host.footnote or "")


def SourceSummary(
    source_id: str,
    type_id: str,
    demand: object,
    connectivity: str,
    freshness: str,
    *,
    has_payload: bool = False,
    requires_setup: bool = False,
    setup_hint: str = "",
) -> SourceStatusSummary:
    from sideboard.core.contracts import Connectivity as ConnEnum
    from sideboard.core.contracts import Demand as DemandEnum
    from sideboard.core.contracts import Freshness as FreshEnum

    assert isinstance(demand, DemandEnum)
    return SourceStatusSummary(
        source_id=source_id,
        type_id=type_id,
        demand=demand,
        connectivity=ConnEnum(connectivity),
        freshness=FreshEnum(freshness),
        has_payload=has_payload,
        requires_setup=requires_setup,
        setup_hint=setup_hint,
    )
