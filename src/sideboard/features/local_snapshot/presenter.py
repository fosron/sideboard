"""Pure presentation for the two local snapshot widgets.

``local_snapshot.metrics`` renders producer metrics (with bounded histories as
sparklines, gaps preserved) and ``local_snapshot.items`` renders the producer's short
list. Neither performs I/O.

Honesty rules implemented here:

* a producer snapshot past ``expires_at`` is shown **and** marked expired
* a missing snapshot or a failed read uses the shared honest status instead of a
  fabricated empty card
* history values are index-ordered because the format supplies no per-sample
  timestamps; the sparkline note says so, and gaps are drawn as gaps
* when the snapshot declares ``privacy = "private"``, every metric and item is marked
  private so the shell can mask it in privacy mode
"""

from __future__ import annotations

from datetime import UTC, datetime

from ...core.contracts import (
    Item,
    Metric,
    PresentInput,
    Privacy,
    Series,
    SeriesPoint,
    Severity,
    SourceEnvelope,
    StatusView,
    TableColumn,
    TableRow,
    TableView,
    ViewModel,
)
from ...core.present import status_from_envelope
from .config import SnapshotItemsSettings, SnapshotMetricsSettings
from .models import SnapshotDocument, SnapshotItem, SnapshotMetric, SnapshotPayload

__all__ = ["present_items", "present_metrics"]

DETAIL_METRIC_LIMIT = 24

DETAIL_ITEM_LIMIT = 32

VALUE_TEXT_LIMIT = 160

SPARKLINE_NOTE = "producer history · no timestamps supplied"


def present_metrics(requested: PresentInput) -> ViewModel:
    """Map a bound ``local_snapshot.file`` snapshot into a metrics view."""
    settings = _metrics_settings(requested.settings)
    envelope = _envelope(requested)
    payload = _payload(envelope)
    if payload is None:
        return ViewModel(status=_status(envelope, requested), footnote="no snapshot")
    document = payload.document
    private = _privacy(document)
    limit = settings.max_metrics if requested.context.compact else DETAIL_METRIC_LIMIT
    selected = document.metrics[:limit]
    sparklines: list[Series] = []
    if settings.sparkline:
        for metric in selected:
            line = _series(metric)
            if line is not None:
                sparklines.append(line)
    return ViewModel(
        title=_title(document, settings.use_snapshot_title),
        subtitle=_subtitle(document),
        status=_status(envelope, requested),
        metrics=tuple(_metric(metric, document, private, settings) for metric in selected),
        series=tuple(sparklines),
        table=None if requested.context.compact else _metrics_table(document, selected),
        footnote=_footnote(
            document,
            requested,
            shown=len(selected),
            total=len(document.metrics),
            label="metrics",
        ),
        privacy=private,
    )


def present_items(requested: PresentInput) -> ViewModel:
    """Map a bound ``local_snapshot.file`` snapshot into an item list view."""
    settings = _items_settings(requested.settings)
    envelope = _envelope(requested)
    payload = _payload(envelope)
    if payload is None:
        return ViewModel(status=_status(envelope, requested), footnote="no snapshot")
    document = payload.document
    private = _privacy(document)
    limit = settings.max_items if requested.context.compact else DETAIL_ITEM_LIMIT
    selected = document.items[:limit]
    items = tuple(_item(entry, document, private, settings) for entry in selected)
    status = _status(envelope, requested)
    if status is None and not items:
        status = StatusView(
            severity=Severity.INFO,
            headline="No items in this snapshot",
            detail="The producer reported metrics only.",
        )
    return ViewModel(
        title=_title(document, settings.use_snapshot_title),
        subtitle=_subtitle(document),
        status=status,
        items=items,
        footnote=_footnote(
            document,
            requested,
            shown=len(selected),
            total=len(document.items),
            label="items",
            extra=("producer-supplied snapshot · no live query" if settings.show_basis else None),
        ),
        privacy=private,
    )


# --------------------------------------------------------------------------- #
# Pieces
# --------------------------------------------------------------------------- #


def _metric(
    metric: SnapshotMetric,
    document: SnapshotDocument,
    privacy: Privacy,
    settings: SnapshotMetricsSettings,
) -> Metric:
    return Metric(
        id=metric.id,
        label=metric.label,
        value_text=_value_text(metric.value),
        unit=metric.unit,
        basis=metric.basis if settings.show_basis else "",
        available=True,
        privacy=privacy,
        as_of=document.generated_at,
    )


def _series(metric: SnapshotMetric) -> Series | None:
    if len(metric.history) < 2:
        return None
    return Series(
        id=f"snapshot:{metric.id}:history",
        label=metric.label,
        unit=metric.unit,
        points=tuple(
            SeriesPoint(t=float(index), value=point) for index, point in enumerate(metric.history)
        ),
        note=SPARKLINE_NOTE,
    )


def _item(
    entry: SnapshotItem,
    document: SnapshotDocument,
    privacy: Privacy,
    settings: SnapshotItemsSettings,
) -> Item:
    return Item(
        id=f"snapshot:{document.source_id}:{entry.id}",
        title=entry.title,
        subtitle=entry.subtitle,
        url=entry.url,
        published_at=entry.published_at if settings.show_published else None,
        privacy=privacy,
    )


def _metrics_table(document: SnapshotDocument, selected: tuple[SnapshotMetric, ...]) -> TableView:
    columns = (
        TableColumn(id="metric", label="Metric"),
        TableColumn(id="value", label="Value", align="right"),
        TableColumn(id="unit", label="Unit", width=10),
        TableColumn(id="basis", label="Basis", width=12),
    )
    rows = tuple(
        TableRow(
            id=f"snapshot:{document.source_id}:metric:{metric.id}",
            cells={
                "metric": metric.label,
                "value": _value_text(metric.value),
                "unit": metric.unit or "—",
                "basis": metric.basis or "—",
            },
        )
        for metric in selected
    )
    return TableView(
        columns=columns,
        rows=rows,
        truncated=len(document.metrics) > len(selected),
        footnote=f"all metrics reported by {document.source_id}",
    )


def _status(envelope: SourceEnvelope | None, requested: PresentInput) -> StatusView | None:
    if envelope is None:
        return StatusView(
            severity=Severity.WARNING,
            headline="Source not configured",
            detail="No local snapshot source is bound to this card.",
            hints=("Configure a local_snapshot.file source with an absolute path.",),
        )
    valid_until = envelope.valid_until
    if (
        envelope.payload is not None
        and valid_until is not None
        and requested.context.now >= valid_until
    ):
        return StatusView(
            severity=Severity.WARNING,
            headline="Snapshot expired",
            detail=(
                f"The producer marked this snapshot valid until {_short(valid_until)}. "
                "Showing it anyway."
            ),
            hints=("Sideboard keeps the last good snapshot instead of hiding data.",),
        )
    return status_from_envelope(
        envelope, offline=requested.context.offline, demo=requested.context.demo
    )


def _footnote(
    document: SnapshotDocument,
    requested: PresentInput,
    *,
    shown: int,
    total: int,
    label: str,
    extra: str | None = None,
) -> str:
    parts = [f"{document.source_id} · generated {_short(document.generated_at)}"]
    if document.expires_at is not None:
        expired = requested.context.now >= document.expires_at
        parts.append(("expired " if expired else "valid until ") + _short(document.expires_at))
    if total > shown:
        parts.append(f"showing {shown} of {total} {label}")
    if extra:
        parts.append(extra)
    if document.privacy is Privacy.PRIVATE:
        parts.append("private snapshot")
    return " · ".join(parts)


def _value_text(value: bool | int | float | str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.3f}".rstrip("0").rstrip(".") or "0"
    return _clamp(value, VALUE_TEXT_LIMIT)


def _clamp(text: str, limit: int) -> str:
    single = " ".join(text.split())
    if len(single) > limit:
        single = single[: max(1, limit - 1)].rstrip() + "…"
    return single


def _short(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%d %b %H:%M")


def _subtitle(document: SnapshotDocument) -> str:
    """Who produced it. When it was generated is the timestamp line's job."""
    return document.source_id


def _title(document: SnapshotDocument, use_snapshot_title: bool) -> str | None:
    if use_snapshot_title and document.title:
        return document.title
    return None


def _privacy(document: SnapshotDocument) -> Privacy:
    return Privacy.PRIVATE if document.privacy is Privacy.PRIVATE else Privacy.PUBLIC


# --------------------------------------------------------------------------- #
# Input resolution
# --------------------------------------------------------------------------- #


def _metrics_settings(raw: object) -> SnapshotMetricsSettings:
    if isinstance(raw, SnapshotMetricsSettings):  # pragma: no cover - host guarantees
        return raw
    return SnapshotMetricsSettings()


def _items_settings(raw: object) -> SnapshotItemsSettings:
    if isinstance(raw, SnapshotItemsSettings):  # pragma: no cover - host guarantees
        return raw
    return SnapshotItemsSettings()


def _payload(envelope: SourceEnvelope | None) -> SnapshotPayload | None:
    payload = envelope.payload if envelope is not None else None
    return payload if isinstance(payload, SnapshotPayload) else None


def _envelope(requested: PresentInput) -> SourceEnvelope | None:
    """Resolve the bound envelope.

    ``PresentInput.envelope``/``payload`` look up by *binding name*, while the host
    currently hands presenters a mapping keyed by *source instance id*. Accept either
    shape so these presenters are correct under both conventions.
    """
    envelope = requested.envelope("primary")
    if envelope is not None:
        return envelope
    for candidate in requested.inputs.values():
        if candidate is not None:
            return candidate
    return None
