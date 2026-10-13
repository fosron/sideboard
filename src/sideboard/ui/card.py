"""The shared card shell.

Every card — Overview, detail tabs and composite widgets — uses this widget. It owns
loading/stale/blocked/error presentation, hover and keyboard holds, privacy masking,
resize behaviour, pinning and detail navigation. A feature must not reimplement any
of that; a rare custom renderer still runs inside this shell.

Rendering is signature-gated so an unchanged card does not repaint every tick, which
keeps idle CPU low.
"""

from __future__ import annotations

import zlib
from datetime import datetime

from rich.console import Group, RenderableType
from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget

from ..core.clock import display_timezone, to_local
from ..core.contracts import Connectivity, SourceEnvelope, ViewMode, ViewModel
from .renderers import render_items, render_metrics, render_series, render_status, render_table
from .styles import DIM
from .text import control_free, single_line

__all__ = ["CardShell", "freshness_line"]


def freshness_line(
    envelope: SourceEnvelope | None,
    *,
    now: datetime,
    timezone: str,
    held_reasons: tuple[str, ...] = (),
    demo: bool = False,
    offline: bool = False,
    dwell_remaining: float | None = None,
    pinned: bool = False,
) -> str:
    """Honest one-line provenance: source date, fetch time, freshness."""
    parts: list[str] = []
    if demo:
        parts.append("demo")
    if offline:
        parts.append("offline")
    if pinned:
        parts.append("pinned")
    if held_reasons:
        parts.append("held:" + "+".join(held_reasons))
    elif dwell_remaining is not None and dwell_remaining > 0:
        parts.append(f"{int(dwell_remaining)}s")
    if envelope is None:
        parts.append("no source")
        return " · ".join(parts)
    if envelope.as_of is not None:
        parts.append(f"as of {_local(envelope.as_of, timezone, now)}")
    elif envelope.fetched_at is not None:
        parts.append("no source date")
    if envelope.fetched_at is not None:
        parts.append(f"updated {_local(envelope.fetched_at, timezone, now)}")
    parts.append(str(envelope.freshness))
    if envelope.is_partial:
        parts.append("partial")
    if envelope.connectivity is not Connectivity.OK and envelope.has_payload:
        parts.append(str(envelope.connectivity))
    return " · ".join(parts)


def _local(moment: datetime, timezone: str, now: datetime) -> str:
    zone = display_timezone(timezone)
    local = to_local(moment, zone)
    reference = to_local(now, zone)
    if local.date() == reference.date():
        return local.strftime("%H:%M")
    return local.strftime("%m-%d %H:%M")


class CardShell(Widget, can_focus=True):
    """Shared card behaviour: status, holds, selection, resize, detail navigation."""

    BINDINGS = [
        Binding("up", "cursor_up", "up", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("enter", "activate", "open", show=False),
    ]

    class HoldChanged(Message):
        """A hold was requested by this card (hover or explicit interaction)."""

        def __init__(self, card_key: str, reason: str, active: bool) -> None:
            self.card_key = card_key
            self.reason = reason
            self.active = active
            super().__init__()

    class Activated(Message):
        """The user asked for this card's detail view."""

        def __init__(self, card_key: str, widget_id: str | None) -> None:
            self.card_key = card_key
            self.widget_id = widget_id
            super().__init__()

    class SelectionChanged(Message):
        def __init__(self, card_key: str, widget_id: str | None, index: int) -> None:
            self.card_key = card_key
            self.widget_id = widget_id
            self.index = index
            super().__init__()

    def __init__(
        self,
        *,
        card_key: str,
        title: str = "",
        slot_label: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.card_key = card_key
        self.widget_id: str | None = None
        self.card_title = title
        self.slot_label = slot_label
        self.view: ViewModel | None = None
        self.envelope: SourceEnvelope | None = None
        self.freshness = ""
        self.hold_reasons: tuple[str, ...] = ()
        self.privacy = False
        self.mode = ViewMode.COMPACT
        self.cursor_index = 0
        self.is_pinned = False
        self.is_held = False
        self.offline = False
        self.demo = False
        self.timezone = "UTC"
        self.now: datetime | None = None
        self._pointer_inside = False
        self._min_width = 24
        self._min_height = 3
        self._signature: tuple[object, ...] | None = None

    # ------------------------------------------------------------------ content

    def set_card(
        self,
        *,
        widget_id: str | None,
        title: str,
        view: ViewModel | None,
        envelope: SourceEnvelope | None,
        freshness: str,
        held: bool = False,
        hold_reasons: tuple[str, ...] = (),
        pinned: bool = False,
        privacy: bool = False,
        mode: ViewMode = ViewMode.COMPACT,
        timezone: str = "UTC",
        now: datetime | None = None,
        offline: bool = False,
        demo: bool = False,
        slot_label: str | None = None,
        min_width: int = 24,
        min_height: int = 3,
    ) -> None:
        self.widget_id = widget_id
        self.card_title = title
        self.slot_label = slot_label
        self.view = view
        self.envelope = envelope
        self.freshness = freshness
        self.is_held = held
        self.hold_reasons = hold_reasons
        self.is_pinned = pinned
        self.privacy = privacy
        self.mode = mode
        self.timezone = timezone
        self.now = now
        self.offline = offline
        self.demo = demo
        self._min_width = min_width
        self._min_height = min_height
        self._clamp_cursor()
        self._apply_chrome()
        signature = self._build_signature()
        if signature != self._signature:
            self._signature = signature
            self.refresh(layout=True)

    def _apply_chrome(self) -> None:
        marker = "◆ " if self.is_pinned else ""
        label = f"{marker}{control_free(self.card_title) or 'card'}"
        if self.slot_label:
            label = f"[{control_free(self.slot_label)}] {label}"
        self.border_title = label
        self.border_subtitle = single_line(self.freshness, 80) if self.freshness else None
        self.set_class(self.is_held, "sb-held")
        self.set_class(self.is_pinned, "sb-pinned")

    def _build_signature(self) -> tuple[object, ...]:
        view = self.view
        return (
            self.widget_id,
            self.card_title,
            self.slot_label,
            self.freshness,
            self.is_held,
            self.is_pinned,
            self.hold_reasons,
            self.privacy,
            self.cursor_index,
            self.size.width,
            self.size.height,
            None
            if view is None
            else (
                view.title,
                view.subtitle,
                len(view.metrics),
                len(view.items),
                None if view.table is None else len(view.table.rows),
                len(view.series),
                view.footnote,
                None if view.status is None else view.status.headline,
            ),
        )

    # ------------------------------------------------------------------ selection

    @property
    def row_count(self) -> int:
        view = self.view
        if view is None:
            return 0
        if view.table is not None and view.table.rows:
            return len(view.table.rows)
        return len(view.items)

    def _clamp_cursor(self) -> None:
        rows = self.row_count
        self.cursor_index = max(0, min(self.cursor_index, rows - 1)) if rows else 0

    def selected_id(self) -> str | None:
        view = self.view
        if view is None:
            return None
        if view.table is not None and view.table.rows:
            rows = view.table.rows
            return rows[self.cursor_index].id if 0 <= self.cursor_index < len(rows) else None
        if view.items and 0 <= self.cursor_index < len(view.items):
            return view.items[self.cursor_index].id
        return None

    def selected_url(self) -> str | None:
        view = self.view
        if view is None:
            return None
        if view.table is not None and view.table.rows:
            rows = view.table.rows
            return rows[self.cursor_index].url if 0 <= self.cursor_index < len(rows) else None
        if view.items and 0 <= self.cursor_index < len(view.items):
            return view.items[self.cursor_index].url
        return None

    def action_cursor_up(self) -> None:
        self._move_cursor(-1)

    def action_cursor_down(self) -> None:
        self._move_cursor(1)

    def _move_cursor(self, delta: int) -> None:
        rows = self.row_count
        if rows <= 0:
            return
        before = self.cursor_index
        self.cursor_index = max(0, min(rows - 1, self.cursor_index + delta))
        if before != self.cursor_index:
            self.post_message(
                CardShell.SelectionChanged(self.card_key, self.widget_id, self.cursor_index)
            )
            self.refresh(layout=True)

    def action_activate(self) -> None:
        self.post_message(CardShell.Activated(self.card_key, self.widget_id))

    def select_index(self, index: int) -> None:
        rows = self.row_count
        if rows <= 0:
            return
        self.cursor_index = max(0, min(rows - 1, index))
        self.refresh(layout=True)

    def find_item(self, item_id: str) -> int | None:
        view = self.view
        if view is None:
            return None
        for index, item in enumerate(view.items):
            if item.id == item_id:
                return index
        if view.table is not None:
            for index, row in enumerate(view.table.rows):
                if row.id == item_id:
                    return index
        return None

    # ------------------------------------------------------------------ mouse

    def on_mouse_move(self) -> None:
        self._acquire_pointer()

    def on_enter(self) -> None:
        self._acquire_pointer()

    def _acquire_pointer(self) -> None:
        if self._pointer_inside:
            return
        self._pointer_inside = True
        self.post_message(CardShell.HoldChanged(self.card_key, "hover", True))

    def on_leave(self) -> None:
        if not self._pointer_inside:
            return
        self._pointer_inside = False
        self.post_message(CardShell.HoldChanged(self.card_key, "hover", False))

    def on_click(self) -> None:
        self.focus()
        self.action_activate()

    def clear_pointer(self) -> None:
        """Terminal blur: forget the pointer so a fresh event is needed to reacquire."""
        self._pointer_inside = False

    @property
    def pointer_inside(self) -> bool:
        return self._pointer_inside

    # ------------------------------------------------------------------ render

    def render(self) -> RenderableType:
        width = max(8, self.size.width - 2)
        height = max(1, self.size.height)
        view = self.view
        if view is None:
            return Text("loading…", style=DIM)
        if self.size.width < self._min_width or height < self._min_height:
            return self._render_compact(view, width)
        return self._render_full(view, width, height)

    def _render_compact(self, view: ViewModel, width: int) -> RenderableType:
        parts = [control_free(self.card_title) or "card"]
        for metric in view.metrics[:3]:
            if not metric.available:
                continue
            value = (
                "••••" if self.privacy and str(metric.privacy) == "private" else metric.value_text
            )
            unit = f" {metric.unit}" if metric.unit else ""
            parts.append(f"{metric.label} {value}{unit}")
        if not view.metrics and view.items:
            parts.append(control_free(view.items[0].title))
        if not view.metrics and not view.items and view.status is not None:
            parts.append(control_free(view.status.headline))
        return Text(single_line(" · ".join(parts), width), style=DIM)

    def _render_full(self, view: ViewModel, width: int, height: int) -> RenderableType:
        sections: list[RenderableType] = []
        has_body = bool(view.metrics or view.items or view.series)
        if view.status is not None and not has_body:
            sections.append(render_status(view.status, width=width))
        if view.metrics:
            sections.append(
                render_metrics(
                    view.metrics,
                    width=width,
                    privacy=self.privacy,
                    columns=3 if width >= 96 else 2,
                )
            )
        for series in view.series:
            sections.append(render_series(series, width=width))
        if view.items:
            budget = max(1, height - 3 - (2 if view.metrics else 0))
            sections.append(
                render_items(
                    view.items,
                    width=width,
                    privacy=self.privacy,
                    selected_index=self.cursor_index,
                    max_rows=budget,
                )
            )
        table = view.table
        if table is not None and table.columns:
            sections.append(
                render_table(
                    table, width=width, privacy=self.privacy, selected_index=self.cursor_index
                )
            )
            footnote = _table_footnote(table, width)
            if footnote.plain:
                sections.append(footnote)
        if view.status is not None and has_body:
            sections.append(Text(""))
            sections.append(render_status(view.status, width=width))
        if view.footnote:
            sections.append(Text(""))
            sections.append(Text(single_line(view.footnote, width), style=DIM))
        if not sections:
            sections.append(Text("no data", style=DIM))
        return Group(*sections)


def _table_footnote(table: object, width: int) -> Text:
    parts: list[str] = []
    if getattr(table, "truncated", False):
        parts.append("truncated")
    note = getattr(table, "footnote", None)
    if note:
        parts.append(control_free(note))
    if not parts:
        return Text("")
    return Text(single_line(" · ".join(parts), width), style=DIM)


def signature_of(*values: object) -> int:
    """Cheap stable signature helper for renderers that need memoisation."""
    return zlib.crc32(repr(values).encode())
