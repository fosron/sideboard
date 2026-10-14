"""Metric, item, table, status and sparkline renderers.

All renderers build :class:`rich.text.Text` / ``Table`` objects programmatically and
never enable markup parsing, so untrusted content cannot inject style or terminal
control sequences. Every renderer accepts the available width and degrades to a
narrower form rather than clipping.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ...core.contracts import (
    Metric,
    Privacy,
    Series,
    Severity,
    StatusView,
    TableView,
)
from ..styles import BORDER, BORDER_FOCUS, DIM, INFO, ITEM, READ, VALUE, WARN
from ..text import SEVERITY_STYLE, control_free, single_line

__all__ = [
    "render_item_row",
    "render_items",
    "render_metrics",
    "render_series",
    "render_status",
    "render_table",
]


def _unavailable_note(metric: Metric) -> str:
    if metric.note:
        return single_line(metric.note, 40)
    return "unavailable"


def render_metrics(
    metrics: Sequence[Metric],
    *,
    width: int,
    privacy: bool,
    columns: int = 2,
) -> RenderableType:
    """Label/value grid. Unavailable fields stay visibly unavailable."""
    if not metrics:
        return Text("")
    usable = max(1, width - 2)
    if columns <= 1 or usable < 46:
        per_row = 1
    elif columns >= 3 and usable >= 90:
        per_row = 3
    else:
        per_row = 2
    cell = usable // per_row
    rows: list[Text] = []
    for start in range(0, len(metrics), per_row):
        line = Text()
        for index, metric in enumerate(metrics[start : start + per_row]):
            if index:
                line.append("  ")
            hide = privacy and metric.privacy is Privacy.PRIVATE
            label = single_line(metric.label, 22)
            if not metric.available:
                value = _unavailable_note(metric)
                style = DIM
            elif hide:
                value = "••••"
                style = DIM
            else:
                value = single_line(metric.value_text, max(6, cell // 2))
                if metric.unit:
                    value = f"{value} {control_free(metric.unit)}".strip()
                style = VALUE
            line.append(label, style=DIM)
            line.append(" ")
            line.append(value, style=style)
            pad = cell - (len(label) + 1 + len(value))
            if pad > 1:
                line.append(" " * pad)
        line.rstrip()
        rows.append(line)
    return Group(*rows)


def render_status(status: StatusView, *, width: int) -> RenderableType:
    style = SEVERITY_STYLE.get(status.severity, DIM)
    parts: list[Text] = []
    headline = Text()
    headline.append(f"{status.severity.value.upper()}  ", style=style)
    headline.append(single_line(status.headline, max(10, width - 12)), style=VALUE)
    parts.append(headline)
    if status.detail:
        body = Text()
        body.append(single_line(status.detail, max(20, width * 2)), style=DIM)
        parts.append(body)
    for hint in status.hints:
        hint_text = Text()
        hint_text.append("· ", style=DIM)
        hint_text.append(single_line(hint, max(20, width * 2)), style=DIM)
        parts.append(hint_text)
    return Group(*parts)


def render_item_row(raw: object, *, width: int, privacy: bool, selected: bool = False) -> Text:
    """One item row: cursor, saved marker, title, badge and stats — never two lines.

    Extracted so the detail view can render rows as individual widgets: a click then lands
    on a row rather than on a block of text, and the same code decides what a row looks
    like in both places.
    """
    from ...core.contracts import Item

    assert isinstance(raw, Item)
    line = Text()
    if selected:
        line.append("› ", style=BORDER_FOCUS)
    else:
        line.append("  ")
    line.append("◆ " if raw.saved else "  ", style=WARN)
    hide = privacy and raw.privacy is Privacy.PRIVATE
    style = READ if raw.read else ITEM
    badge = f" [{single_line(raw.badge, 20)}]" if raw.badge else ""
    meta_text = ""
    if raw.meta and width >= 60:
        meta_parts = []
        for key, value in raw.meta[:3]:
            text = "••••" if hide else single_line(value, 18)
            meta_parts.append(f"{single_line(key, 8)} {text}")
        meta_text = " · ".join(meta_parts)
    # One item, one row. The title is truncated to what the badge and the stats do not
    # need, and the stats are capped at half the row: a headline plus a long
    # "pts 220 · c 77 · age 3h" used to overflow and wrap.
    room = max(12, width - 4)  # cursor and saved marker
    if meta_text:
        meta_text = single_line(meta_text, max(8, room // 2))
        room -= len(meta_text) + 2
    title_room = max(12, room - len(badge))
    line.append(single_line(raw.title, title_room), style=style)
    if badge:
        line.append(badge, style=DIM)
    if meta_text:
        line.append("  ")
        line.append(meta_text, style=DIM)
    return line


def render_items(
    items: Sequence[object],
    *,
    width: int,
    privacy: bool,
    selected_index: int = 0,
    now: datetime | None = None,
    max_rows: int | None = None,
) -> RenderableType:
    """One row per item with a cursor. Long titles are truncated, never wrapped."""
    from ...core.contracts import Item

    if not items:
        return Text("No items.", style=DIM)
    rows: list[Text] = []
    limit = len(items) if max_rows is None else min(len(items), max_rows)
    for index, raw in enumerate(items[:limit]):
        assert isinstance(raw, Item)
        line = render_item_row(raw, width=width, privacy=privacy, selected=index == selected_index)
        rows.append(line)
        hide = privacy and raw.privacy is Privacy.PRIVATE
        if raw.subtitle and width >= 60:
            sub = Text("     ")
            sub.append("…" if hide else single_line(raw.subtitle, max(20, width - 8)), style=DIM)
            rows.append(sub)
    extra = len(items) - limit
    if extra > 0:
        more = Text(f"  +{extra} more", style=DIM)
        rows.append(more)
    if selected_index >= limit and rows:
        rows[-1].style = BORDER_FOCUS
    return Group(*rows)


def render_table(
    view: TableView,
    *,
    width: int,
    privacy: bool,
    selected_index: int = 0,
) -> RenderableType:
    """Compact table. Grouped rows carry a group header line."""
    table = Table(
        show_header=True,
        header_style=DIM,
        border_style=BORDER,
        box=None,
        pad_edge=False,
        expand=True,
        padding=(0, 1),
    )
    for column in view.columns:
        table.add_column(
            single_line(column.label, 18),
            justify=column.align,  # type: ignore[arg-type]
            no_wrap=True,
            overflow="ellipsis",
        )
    grouped: dict[str, list[object]] = {}
    for group_name, row_ids in view.groups:
        grouped[group_name] = [row for row in view.rows if row.id in set(row_ids)]
    rendered: set[str] = set()
    index = 0
    for group_name, rows in grouped.items():
        if group_name:
            header_cell = Text(single_line(group_name, max(10, width - 6)), style=WARN)
            table.add_row(*_pad_cells([header_cell], len(view.columns)), style="")
        for row in rows:
            _add_row(table, view, row, privacy, selected_index, index)
            rendered.add(row.id)  # type: ignore[attr-defined]
            index += 1
    ungrouped = [row for row in view.rows if row.id not in rendered]
    if grouped and ungrouped and any(name for name, _ in view.groups):
        table.add_row(*_pad_cells([Text("ungrouped", style=DIM)], len(view.columns)))
    for row in ungrouped:
        _add_row(table, view, row, privacy, selected_index, index)
        index += 1
    return table


def _pad_cells(cells: list[Text], count: int) -> list[Text]:
    for _ in range(count - len(cells)):
        cells.append(Text(""))
    return cells


def _add_row(
    table: Table,
    view: TableView,
    row: object,
    privacy: bool,
    selected_index: int,
    index: int,
) -> None:
    cells: list[Text] = []
    hide = privacy and getattr(row, "privacy", Privacy.PUBLIC) is Privacy.PRIVATE
    for column in view.columns:
        raw = getattr(row, "cells", {}).get(column.id, "")
        text = Text(
            "••••" if (hide and column.privacy is Privacy.PRIVATE) else single_line(raw, 28)
        )
        severity = getattr(row, "severity", Severity.UNKNOWN)
        known_severity = severity if isinstance(severity, Severity) else Severity.UNKNOWN
        if known_severity is not Severity.UNKNOWN and column.id == "state":
            text.stylize(SEVERITY_STYLE.get(known_severity, VALUE))
        cells.append(text)
    prefix = "›" if index == selected_index else " "
    cells[0] = Text.assemble(Text(prefix + " "), cells[0])
    table.add_row(*cells, style=READ if index != selected_index else VALUE)


_SPARK = "▁▂▃▄▅▆▇█"


def render_series(series: Series, *, width: int, height: int = 1) -> RenderableType:
    """Sparkline that draws a break where the data has a gap."""
    points = list(series.points)[-max(8, width - 4) :]
    values = [p.value for p in points if p.value is not None]
    label = Text()
    label.append(single_line(series.label, max(8, width // 3)), style=DIM)
    if not values:
        label.append("  no samples", style=DIM)
        return label
    low, high = min(values), max(values)
    span = high - low
    line = Text()
    for point in points:
        if point.value is None:
            line.append(" ", style=DIM)
            continue
        level = 4 if span <= 0 else int((point.value - low) / span * (len(_SPARK) - 1))
        line.append(_SPARK[max(0, min(len(_SPARK) - 1, level))], style=INFO)
    summary = Text("  ")
    summary.append(f"{values[-1]:.1f}", style=VALUE)
    if series.unit:
        summary.append(f" {series.unit}", style=DIM)
    if len(values) < len(points):
        summary.append(f"  ({len(points) - len(values)} gap)", style=DIM)
    out = Text()
    out.append_text(label)
    out.append("  ")
    out.append_text(line)
    out.append_text(summary)
    if series.note:
        out.append("\n")
        out.append(single_line(series.note, max(20, width * 2)), style=DIM)
    return out
