"""Tab panes and the filterable Read layout.

Layouts are configuration-driven strings (``stack``, ``detail``, ``grid``,
``filterable``); nothing here knows which feature a widget belongs to.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from .card import CardShell
from .styles import BORDER_FOCUS, DIM, VALUE
from .text import single_line

__all__ = ["CompactStatus", "DetailPane", "FilterBar"]


class FilterBar(Widget, can_focus=True):
    """Subfilter row used by the Read tab instead of four extra top-level tabs."""

    BINDINGS = [
        Binding("left", "previous", "previous", show=False),
        Binding("right", "next", "next", show=False),
    ]

    class FilterChanged(Message):
        def __init__(self, filter_id: str) -> None:
            self.filter_id = filter_id
            super().__init__()

    def __init__(self, *, filters: Sequence[tuple[str, str]], id: str | None = None) -> None:
        super().__init__(id=id)
        self.filters = tuple(filters)
        self.active = self.filters[0][0] if self.filters else ""

    def set_filters(self, filters: Sequence[tuple[str, str]], *, keep: bool = True) -> None:
        self.filters = tuple(filters)
        ids = {filter_id for filter_id, _ in self.filters}
        if not keep or self.active not in ids:
            self.active = self.filters[0][0] if self.filters else ""
        self.refresh()

    def render(self) -> Text:
        text = Text()
        for index, (filter_id, label) in enumerate(self.filters):
            if index:
                text.append("   ")
            selected = filter_id == self.active
            text.append(single_line(label, 24), style=VALUE if selected else DIM)
            if selected:
                text.append(" ●", style=BORDER_FOCUS)
        return text

    def action_next(self) -> None:
        self._shift(1)

    def action_previous(self) -> None:
        self._shift(-1)

    def _shift(self, delta: int) -> None:
        if not self.filters:
            return
        ids = [filter_id for filter_id, _ in self.filters]
        index = (ids.index(self.active) + delta) % len(ids) if self.active in ids else 0
        self._select(ids[index])

    def _select(self, filter_id: str) -> None:
        if filter_id == self.active:
            return
        self.active = filter_id
        self.refresh()
        self.post_message(FilterBar.FilterChanged(filter_id))

    def on_click(self, event: Click) -> None:
        """Select the label under the pointer."""
        column = 0
        for filter_id, label in self.filters:
            width = len(single_line(label, 24)) + (3 if column else 0)
            if column <= event.x < column + width:
                self._select(filter_id)
                return
            column += width


class CompactStatus(Static):
    """Below 60×20: a useful status list instead of clipped cards."""

    def set_lines(self, lines: Sequence[str], *, width: int) -> None:
        text = Text()
        for index, line in enumerate(lines):
            if index:
                text.append("\n")
            text.append(single_line(line, max(8, width)), style=DIM if index else VALUE)
        self.update(text)


class DetailPane(Widget):
    """A tab body with one card per configured widget, arranged by layout."""

    # A card needs room for its border, a few metric rows and at least one list row.
    MIN_CARD_ROWS = 8
    MAX_CARD_ROWS = 34
    LEAD_CARD_EXTRA_ROWS = 4

    def __init__(self, *, id: str | None = None, classes: str = "") -> None:
        super().__init__(id=id, classes=classes)
        self._cards: dict[str, CardShell] = {}
        self.widget_ids: tuple[str, ...] = ()
        # Not named ``layout``: Textual's Widget already has a ``layout`` property.
        self.layout_kind = "stack"

    @property
    def cards(self) -> Mapping[str, CardShell]:
        return self._cards

    def card(self, widget_id: str) -> CardShell | None:
        return self._cards.get(widget_id)

    async def rebuild(
        self, widget_ids: Sequence[str], layout: str, *, labels: Mapping[str, str] | None = None
    ) -> None:
        await self.remove_children()
        self._cards.clear()
        self.widget_ids = tuple(widget_ids)
        self.layout_kind = layout
        if not widget_ids:
            await self.mount(Static(Text("no widgets configured for this tab", style=DIM)))
            return
        if layout == "grid" and len(widget_ids) > 1:
            rows = [self.widget_ids[i : i + 2] for i in range(0, len(self.widget_ids), 2)]
            for index, chunk in enumerate(rows):
                row = Horizontal(classes="sb-overview-row", id=f"sb-detail-row-{index}")
                await self.mount(row)
                # Rows in a grid keep room for the cards they hold.
                row.styles.min_height = 12
                for widget_id in chunk:
                    shell = self._shell(widget_id, labels)
                    self._cards[widget_id] = shell
                    await row.mount(shell)
            return
        container = VerticalScroll(classes="sb-stack", id="sb-detail-stack")
        await self.mount(container)
        for widget_id in self.widget_ids:
            shell = self._shell(widget_id, labels)
            self._cards[widget_id] = shell
            await container.mount(shell)
        # Heights are set explicitly once the cards exist; see apply_heights.
        self.apply_heights()

    def on_resize(self, event: object) -> None:
        size = getattr(event, "size", None)
        if size is not None:
            self.apply_heights(viewport_height=size.height)

    def apply_heights(self, *, viewport_height: int | None = None) -> None:
        """Give every card an explicit, predictable height.

        Neither ``1fr`` nor ``auto`` works here. A fraction slices the viewport evenly,
        so a five-card tab gives each card three content rows. ``auto`` cannot measure a
        Rich ``Group`` that contains an expanding table, so it fills the viewport and a
        single card covers the whole tab. An explicit row count keeps every card usable
        and lets the pane scroll when they do not all fit.
        """
        if self.layout_kind == "grid":
            return
        count = len(self.widget_ids)
        available = viewport_height or self.size.height or 0
        if count == 0 or available <= 0:
            return
        even_split = max(self.MIN_CARD_ROWS, available // count)
        for index, widget_id in enumerate(self.widget_ids):
            card = self._cards.get(widget_id)
            if card is None:
                continue
            # Prefer the card's own estimate so a data-heavy card is not clipped, and
            # fall back to an even split before the first presentation.
            estimated = card.estimated_content_rows() + 2  # its own border
            lead = self.layout_kind == "detail" and index == 0
            height = min(
                self.MAX_CARD_ROWS,
                max(self.MIN_CARD_ROWS, estimated if card.view is not None else even_split)
                + (self.LEAD_CARD_EXTRA_ROWS if lead else 0),
            )
            if card.styles.height != height:
                card.styles.height = height

    @staticmethod
    def _shell(widget_id: str, labels: Mapping[str, str] | None) -> CardShell:
        label = (labels or {}).get(widget_id, "")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in widget_id)
        return CardShell(
            card_key=widget_id,
            title=label,
            id=f"sb-card-{safe}",
        )
