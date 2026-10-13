"""Presenter tests for the local snapshot widgets: absolute values, honest statuses."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sideboard.core.contracts import (
    Connectivity,
    Freshness,
    PresentInput,
    Privacy,
    Severity,
    SourceEnvelope,
    ViewContext,
    ViewMode,
    ViewModel,
)
from sideboard.core.errors import ErrorCategory, SourceErrorInfo
from sideboard.features.local_snapshot.config import (
    SnapshotItemsSettings,
    SnapshotMetricsSettings,
)
from sideboard.features.local_snapshot.models import SnapshotPayload
from sideboard.features.local_snapshot.presenter import present_items, present_metrics
from sideboard.features.local_snapshot.validate import validate_snapshot

FIXTURES = Path(__file__).parent / "fixtures"

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def fixture_payload() -> SnapshotPayload:
    document = json.loads((FIXTURES / "valid_snapshot.json").read_text(encoding="utf-8"))
    return validate_snapshot(document)


def envelope_for(
    payload: SnapshotPayload | None,
    *,
    connectivity: Connectivity = Connectivity.OK,
    error: SourceErrorInfo | None = None,
    valid_until: datetime | None = None,
) -> SourceEnvelope:
    return SourceEnvelope(
        source_id="backup_snapshot",
        payload_schema="local_snapshot.file",
        payload_schema_version=1,
        payload=payload,
        connectivity=connectivity,
        freshness=Freshness.FRESH if payload is not None else Freshness.NONE,
        last_success_at=NOW if payload is not None else None,
        as_of=payload.as_of if payload is not None else None,
        valid_until=valid_until
        if valid_until is not None
        else (payload.valid_until if payload else None),
        error=error,
    )


def metrics_view(
    payload: SnapshotPayload | None,
    *,
    settings: SnapshotMetricsSettings | None = None,
    mode: ViewMode = ViewMode.COMPACT,
    inputs: dict[str, SourceEnvelope | None] | None = None,
    valid_until: datetime | None = None,
) -> ViewModel:
    bound = envelope_for(payload, valid_until=valid_until)
    return present_metrics(
        PresentInput(
            settings=settings or SnapshotMetricsSettings(),
            inputs=inputs if inputs is not None else {"backup_snapshot": bound},
            context=ViewContext(mode=mode, now=NOW),
        )
    )


def items_view(
    payload: SnapshotPayload | None,
    *,
    settings: SnapshotItemsSettings | None = None,
    mode: ViewMode = ViewMode.COMPACT,
    inputs: dict[str, SourceEnvelope | None] | None = None,
) -> ViewModel:
    bound = envelope_for(payload)
    return present_items(
        PresentInput(
            settings=settings or SnapshotItemsSettings(),
            inputs=inputs if inputs is not None else {"backup_snapshot": bound},
            context=ViewContext(mode=mode, now=NOW),
        )
    )


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def test_metrics_are_rendered_with_units_and_basis() -> None:
    view = metrics_view(fixture_payload())
    assert view.title == "Example backup status"
    assert view.subtitle is not None and "example-backup" in view.subtitle
    by_id = {metric.id: metric for metric in view.metrics}
    assert set(by_id) == {"last_success", "duration_seconds", "verified", "note"}
    duration = by_id["duration_seconds"]
    assert duration.value_text == "42.5"
    assert duration.unit == "s"
    assert duration.basis == "measured"
    assert duration.available is True
    assert duration.as_of == datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    assert by_id["verified"].value_text == "true"
    assert by_id["last_success"].value_text == "2026-10-13T09:45:00Z"
    assert by_id["note"].value_text == "synthetic fixture, not a real backup"


def test_show_basis_false_hides_the_basis_column_value() -> None:
    view = metrics_view(fixture_payload(), settings=SnapshotMetricsSettings(show_basis=False))
    assert all(metric.basis == "" for metric in view.metrics)


def test_max_metrics_limits_the_compact_view() -> None:
    view = metrics_view(fixture_payload(), settings=SnapshotMetricsSettings(max_metrics=2))
    assert [metric.id for metric in view.metrics] == ["last_success", "duration_seconds"]
    assert view.footnote is not None and "showing 2 of 4 metrics" in view.footnote


def test_series_preserves_gaps_and_admits_it_has_no_timestamps() -> None:
    view = metrics_view(fixture_payload())
    assert len(view.series) == 1
    line = view.series[0]
    assert line.id == "snapshot:duration_seconds:history"
    assert [point.value for point in line.points] == [38.0, 40.5, None, 44.0, 42.5]
    assert line.unit == "s"
    assert line.note is not None and "no timestamps" in line.note


def test_sparkline_setting_disables_series() -> None:
    view = metrics_view(fixture_payload(), settings=SnapshotMetricsSettings(sparkline=False))
    assert view.series == ()


def test_private_snapshot_marks_metrics_and_view_private() -> None:
    view = metrics_view(fixture_payload())
    assert view.privacy is Privacy.PRIVATE
    assert all(metric.privacy is Privacy.PRIVATE for metric in view.metrics)


def test_public_snapshot_is_not_marked_private() -> None:
    payload = fixture_payload()
    public = payload.model_copy(
        update={"document": payload.document.model_copy(update={"privacy": Privacy.PUBLIC})}
    )
    view = metrics_view(public)
    assert view.privacy is Privacy.PUBLIC
    assert all(metric.privacy is Privacy.PUBLIC for metric in view.metrics)


def test_expired_snapshot_is_shown_and_marked_expired() -> None:
    view = metrics_view(fixture_payload(), valid_until=NOW - timedelta(hours=1))
    assert view.status is not None
    assert view.status.severity is Severity.WARNING
    assert view.status.headline == "Snapshot expired"
    assert view.metrics  # data is still shown


def test_absent_payload_gives_an_honest_status_instead_of_an_empty_card() -> None:
    view = metrics_view(None)
    assert view.status is not None
    assert view.status.headline == "No data yet"
    assert view.metrics == ()
    assert view.footnote == "no snapshot"
    unbound = present_metrics(
        PresentInput(
            settings=SnapshotMetricsSettings(),
            inputs={"backup_snapshot": None},
            context=ViewContext(now=NOW),
        )
    )
    assert unbound.status is not None
    assert unbound.status.headline == "Source not configured"


def test_failed_read_uses_the_shared_status() -> None:
    error = SourceErrorInfo(
        category=ErrorCategory.PARSE, message="snapshot is not valid JSON (line 1, column 1)"
    )
    bound = envelope_for(None, connectivity=Connectivity.ERROR, error=error)
    view = present_metrics(
        PresentInput(
            settings=SnapshotMetricsSettings(),
            inputs={"backup_snapshot": bound},
            context=ViewContext(now=NOW),
        )
    )
    assert view.status is not None
    assert view.status.headline == "No data yet"
    assert view.status.detail is not None and "not valid JSON" in view.status.detail


def test_detail_mode_lists_every_metric_in_a_table() -> None:
    view = metrics_view(
        fixture_payload(), settings=SnapshotMetricsSettings(max_metrics=2), mode=ViewMode.DETAIL
    )
    assert view.table is not None
    assert [column.id for column in view.table.columns] == ["metric", "value", "unit", "basis"]
    assert len(view.table.rows) == 4
    assert view.table.truncated is False


def test_use_snapshot_title_false_defers_to_the_widget_title() -> None:
    view = metrics_view(
        fixture_payload(), settings=SnapshotMetricsSettings(use_snapshot_title=False)
    )
    assert view.title is None


def test_inputs_keyed_by_binding_name_also_work() -> None:
    bound = envelope_for(fixture_payload())
    view = present_metrics(
        PresentInput(
            settings=SnapshotMetricsSettings(),
            inputs={"primary": bound},
            context=ViewContext(now=NOW),
        )
    )
    assert len(view.metrics) == 4


# --------------------------------------------------------------------------- #
# Items
# --------------------------------------------------------------------------- #


def test_items_have_stable_namespaced_ids_and_links() -> None:
    view = items_view(fixture_payload())
    assert [item.id for item in view.items] == [
        "snapshot:example-backup:run-001",
        "snapshot:example-backup:run-000",
    ]
    first = view.items[0]
    assert first.url == "https://example.invalid/backup/run-001"
    assert first.subtitle == "example.invalid · synthetic"
    assert first.published_at == datetime(2026, 10, 13, 9, 45, tzinfo=UTC)
    assert view.items[1].url is None
    assert view.footnote is not None and "example-backup" in view.footnote


def test_items_respect_max_items_and_detail_mode() -> None:
    compact = items_view(fixture_payload(), settings=SnapshotItemsSettings(max_items=1))
    assert len(compact.items) == 1
    assert compact.footnote is not None and "showing 1 of 2 items" in compact.footnote
    detail = items_view(
        fixture_payload(), settings=SnapshotItemsSettings(max_items=1), mode=ViewMode.DETAIL
    )
    assert len(detail.items) == 2


def test_items_mark_private_privacy() -> None:
    view = items_view(fixture_payload())
    assert view.privacy is Privacy.PRIVATE
    assert all(item.privacy is Privacy.PRIVATE for item in view.items)


def test_show_published_false_omits_the_timestamp() -> None:
    view = items_view(fixture_payload(), settings=SnapshotItemsSettings(show_published=False))
    assert all(item.published_at is None for item in view.items)


def test_items_footnote_records_the_producer_basis() -> None:
    view = items_view(fixture_payload())
    assert view.footnote is not None
    assert "producer-supplied snapshot · no live query" in view.footnote
    hidden = items_view(fixture_payload(), settings=SnapshotItemsSettings(show_basis=False))
    assert hidden.footnote is not None
    assert "producer-supplied snapshot" not in hidden.footnote


def test_empty_item_list_reports_itself() -> None:
    payload = fixture_payload()
    empty = payload.model_copy(
        update={"document": payload.document.model_copy(update={"items": ()})}
    )
    view = items_view(empty)
    assert view.items == ()
    assert view.status is not None
    assert view.status.headline == "No items in this snapshot"


def test_items_absent_payload_is_honest() -> None:
    view = items_view(None)
    assert view.status is not None
    assert view.items == ()
    assert view.footnote == "no snapshot"
