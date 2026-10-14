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
from ..core.contracts import Connectivity, Freshness, SourceEnvelope, ViewMode, ViewModel
from .renderers import render_items, render_metrics, render_series, render_status, render_table
from .styles import DIM
from .text import control_free, single_line

__all__ = ["CardShell", "cursor_layout", "cursor_target", "freshness_line"]


def freshness_line(
    envelope: SourceEnvelope | None,
    *,
    now: datetime,
    timezone: str,
    demo: bool = False,
    offline: bool = False,
) -> str:
    """One timestamp, and a state only when it is not fresh.

    Deliberately silent about three things that used to be printed here and read as
    noise: interaction state ("held:hover+interaction", "pinned"), which the card's own
    border already shows, coverage ("partial"), which belongs next to the numbers it
    qualifies, and a second timestamp. A card and its full view show the same line, so
    they can never disagree about when the data arrived.

    There is no dwell countdown either: a string that changes every second forced a
    repaint of every rotating card every second.
    """
    parts: list[str] = []
    if demo:
        parts.append("demo")
    if offline:
        parts.append("offline")
    if envelope is None:
        parts.append("no source")
        return " · ".join(parts)
    moment = envelope.fetched_at or envelope.as_of
    if moment is not None:
        label = "updated" if envelope.fetched_at is not None else "as of"
        parts.append(f"{label} {_local(moment, timezone, now)}")
    if envelope.freshness not in {Freshness.FRESH, Freshness.NONE}:
        parts.append(str(envelope.freshness))
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
        Binding("left", "card_left", "left", show=False),
        Binding("h", "card_left", "left", show=False),
        Binding("right", "card_right", "right", show=False),
        Binding("l", "card_right", "right", show=False),
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

    class FocusRequest(Message):
        """The user asked to move on: this card has nothing left in that direction."""

        def __init__(self, direction: str) -> None:
            self.direction = direction
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
        self._chrome_title: str | None = None
        self._chrome_subtitle: str | None = None

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
            # A repaint, not a layout: the card's box is set by CSS or by
            # DetailPane.apply_heights, so new content cannot change its geometry.
            # refresh(layout=True) here marked the whole screen for re-layout, and a
            # pointer sweep across four cards did that dozens of times a second.
            self.refresh()

    def estimated_content_rows(self) -> int:
        """Rows this card's current view needs, for a scrollable stack of cards.

        The estimate is deliberately simple: it decides a *height*, and the renderers
        still truncate whatever does not fit. It is stable for the same view, so a card
        does not oscillate between two heights.
        """
        view = self.view
        if view is None:
            return self._min_height
        width = self.content_size.width or self.size.width or 60
        columns = 3 if width >= 96 else 2
        rows = 0
        if view.status is not None:
            rows += 3
        if view.metrics:
            rows += (len(view.metrics) + columns - 1) // columns
        rows += len(view.series)
        if view.items:
            rows += min(len(view.items) * 2, 40)
        if view.table is not None and view.table.rows:
            rows += len(view.table.rows) + 2
        if view.footnote:
            rows += 1
        return max(3, rows)

    def _apply_chrome(self) -> None:
        """Update the border title and subtitle, but only when they actually change.

        Textual's ``border_title``/``border_subtitle`` reject an unchanged value by
        repainting anyway, and this ran on every present — twice a second per card while
        idle, which repainted the whole dashboard for nothing. ``set_class`` already
        checks for an unchanged class, so it needs no guard.
        """
        marker = "◆ " if self.is_pinned else ""
        label = f"{marker}{control_free(self.card_title) or 'card'}"
        if self.slot_label:
            label = f"[{control_free(self.slot_label)}] {label}"
        if label != self._chrome_title:
            self._chrome_title = label
            self.border_title = label
        subtitle = single_line(self.freshness, 80) if self.freshness else None
        if subtitle != self._chrome_subtitle:
            self._chrome_subtitle = subtitle
            self.border_subtitle = subtitle
        self.set_class(self.is_held, "sb-held")
        self.set_class(self.is_pinned, "sb-pinned")

    def _build_signature(self) -> tuple[object, ...]:
        """Everything that affects what this card looks like.

        The view's *content* is included, not just its shape: a Mac card whose CPU value
        changes, or a digest that rotates to another article with the same item count,
        must repaint. Comparing only counts meant such updates were silently dropped.
        The digest is bounded so a long list cannot make this expensive.
        """
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
            None if self.view is None else _view_digest(self.view),
        )

    # ------------------------------------------------------------------ selection

    @property
    def row_count(self) -> int:
        view = self.view
        if view is None:
            return 0
        items, rows = cursor_layout(view)
        return items + rows

    def _clamp_cursor(self) -> None:
        rows = self.row_count
        self.cursor_index = max(0, min(self.cursor_index, rows - 1)) if rows else 0

    def selected_id(self) -> str | None:
        view = self.view
        if view is None:
            return None
        target = cursor_target(view, self.cursor_index)
        if target is None:
            return None
        kind, index = target
        if kind == "item":
            return view.items[index].id
        assert view.table is not None
        return view.table.rows[index].id

    def selected_url(self) -> str | None:
        view = self.view
        if view is None:
            return None
        target = cursor_target(view, self.cursor_index)
        if target is None:
            return None
        kind, index = target
        if kind == "item":
            return view.items[index].url
        assert view.table is not None
        return view.table.rows[index].url

    def action_cursor_up(self) -> None:
        self._move_cursor(-1)

    def action_cursor_down(self) -> None:
        self._move_cursor(1)

    def action_card_left(self) -> None:
        self.post_message(CardShell.FocusRequest("left"))

    def action_card_right(self) -> None:
        self.post_message(CardShell.FocusRequest("right"))

    def _move_cursor(self, delta: int) -> None:
        """Walk the rows, then leave the card.

        A card with a list uses up/down for the list; at either end the same key moves to
        the card above or below, which is what makes the arrow keys usable for getting
        around the dashboard instead of stopping at every card's edge.
        """
        rows = self.row_count
        if rows > 1:
            before = self.cursor_index
            self.cursor_index = max(0, min(rows - 1, self.cursor_index + delta))
            if before != self.cursor_index:
                self.post_message(
                    CardShell.SelectionChanged(self.card_key, self.widget_id, self.cursor_index)
                )
                self.refresh(layout=True)
                return
        self.post_message(CardShell.FocusRequest("down" if delta > 0 else "up"))

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
        # ``content_size`` is the region available inside the border and padding, which
        # is what a renderable is drawn into. A zero-height region means the layout has
        # not given this card any space yet: render nothing rather than a clipped line,
        # and let the next layout pass drive a real render.
        content = self.content_size
        height = content.height
        width = max(8, content.width)
        if height <= 0 or self.size.height <= 2:
            return Text("")
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
            # How many rows the list actually gets: everything above it in the card, plus
            # one spare. The old formula subtracted a fixed five rows as if every item had
            # a subtitle, so a card with room for ten headlines showed six and looked
            # cropped.
            overhead = 0
            if view.metrics:
                per_row = 3 if width >= 96 else 2
                overhead += (len(view.metrics) + per_row - 1) // per_row
            overhead += len(view.series)
            if view.status is not None:
                overhead += 3
            if view.footnote:
                overhead += 1
            budget = max(1, height - overhead - 1)
            item_count, _ = cursor_layout(view)
            sections.append(
                render_items(
                    view.items,
                    width=width,
                    privacy=self.privacy,
                    selected_index=(self.cursor_index if self.cursor_index < item_count else -1),
                    max_rows=budget,
                )
            )
        table = view.table
        if table is not None and table.columns:
            item_count, _ = cursor_layout(view)
            sections.append(
                render_table(
                    table,
                    width=width,
                    privacy=self.privacy,
                    selected_index=(
                        self.cursor_index - item_count if self.cursor_index >= item_count else -1
                    ),
                )
            )
            footnote = _table_footnote(table, width)
            if footnote.plain:
                sections.append(footnote)
        if view.status is not None and has_body:
            sections.append(Text(""))
            sections.append(render_status(view.status, width=width))
        # No footnote line: the dashboard does not print explanations, caveats or credits.
        # A presenter may still record one (the field is part of the widget contract), but
        # a card is read at a glance and a sentence about methodology is not part of it.
        if not sections:
            sections.append(Text("no data", style=DIM))
        return Group(*sections)


def cursor_layout(view: ViewModel) -> tuple[int, int]:
    """How a view's rows are split for the cursor: items first, then table rows.

    A view can hold both (weather: hours then days). Treating them as one list is what
    lets the cursor reach the last row of either, and each section is then marked with its
    own relative index — the same index applied to both drew two chevrons at once.
    """
    items = len(view.items)
    rows = len(view.table.rows) if view.table is not None else 0
    return items, rows


def cursor_target(view: ViewModel, index: int) -> tuple[str, int] | None:
    """Resolve a cursor index into ``("item", i)`` or ``("row", i)``."""
    items, rows = cursor_layout(view)
    if 0 <= index < items:
        return ("item", index)
    if items <= index < items + rows:
        return ("row", index - items)
    return None


def _table_footnote(table: object, width: int) -> Text:
    """Only the one thing a table cannot show by itself: that it is cut short."""
    if not getattr(table, "truncated", False):
        return Text("")
    return Text(single_line("truncated", width), style=DIM)


def signature_of(*values: object) -> int:
    """Cheap stable signature helper for renderers that need memoisation."""
    return zlib.crc32(repr(values).encode())


def _view_digest(view: ViewModel) -> tuple[object, ...]:
    """A cheap, bounded summary of everything a renderer will draw.

    Used to decide whether a card must repaint. Bounded so a hundred-row table cannot
    turn the comparison into the expensive part of a frame.
    """
    digest: list[object] = [
        view.title,
        view.subtitle,
        view.footnote,
        None
        if view.status is None
        else (str(view.status.severity), view.status.headline, view.status.detail),
    ]
    for metric in view.metrics:
        digest.append((metric.id, metric.value_text, metric.unit, metric.available, metric.note))
    for item in view.items[:80]:
        digest.append((item.id, item.title, item.subtitle, item.badge, item.saved, item.read))
    if view.table is not None:
        digest.append(("cols", *(column.id for column in view.table.columns)))
        for row in view.table.rows[:80]:
            digest.append((row.id, tuple(sorted(row.cells.items())), str(row.severity)))
        digest.append(("truncated", view.table.truncated, view.table.footnote))
    for series in view.series:
        digest.append(
            (
                series.id,
                series.unit,
                series.label,
                *((point.t, point.value) for point in series.points[-120:]),
            )
        )
    return tuple(digest)
