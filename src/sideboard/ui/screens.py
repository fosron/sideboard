"""Modal screens: detail view, help, command palette, mouse diagnostic.

While a modal is open the app applies a single global ``modal`` hold, so all visual
rotation pauses without touching per-card hold reasons.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence

from rich.console import Group, RenderableType
from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.events import Click, Key, MouseMove
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ..core.contracts import ViewModel
from .card import cursor_layout, cursor_target
from .renderers import (
    render_item_row,
    render_items,
    render_metrics,
    render_series,
    render_status,
    render_table,
)
from .styles import BORDER_FOCUS, DIM, VALUE
from .text import markup_free, single_line

__all__ = [
    "CommandPaletteScreen",
    "DetailRow",
    "DetailScreen",
    "DetailScroll",
    "DismissOnOutsideClick",
    "HelpScreen",
    "MouseDiagnosticScreen",
    "RowActivated",
    "ThemePickerScreen",
]


def render_view_body(
    view: ViewModel,
    *,
    width: int,
    privacy: bool,
    selected_index: int = 0,
    items_as_rows: bool = False,
    include_table: bool = True,
) -> RenderableType:
    """Detail-mode rendering of a whole standard view model.

    ``selected_index`` marks the row the cursor is on, so the detail view can be walked
    with the arrow keys and opened from there. ``items_as_rows`` and ``include_table`` let
    the modal place those two sections itself: its rows are widgets, and the table belongs
    *after* them so hours read before days.
    """
    sections: list[RenderableType] = []
    if view.title:
        sections.append(Text(single_line(view.title, width), style=VALUE))
    if view.subtitle:
        sections.append(Text(single_line(view.subtitle, width), style=DIM))
    if view.status is not None:
        sections.append(render_status(view.status, width=width))
    if view.metrics:
        sections.append(render_metrics(view.metrics, width=width, privacy=privacy, columns=2))
    for series in view.series:
        sections.append(render_series(series, width=width))
    if view.items and not items_as_rows:
        # The modal renders each item as its own widget so a click can land on it; every
        # other caller gets the whole list as one renderable.
        sections.append(
            render_items(
                view.items,
                width=width,
                privacy=privacy,
                selected_index=selected_index,
                max_rows=200,
            )
        )
    if include_table and view.table is not None and view.table.columns:
        sections.append(
            render_table(view.table, width=width, privacy=privacy, selected_index=selected_index)
        )
    # Deliberately no footnote line: explanations and credits are not drawn anywhere.
    if not sections:
        sections.append(Text("no data", style=DIM))
    return Group(*sections)


class RowActivated(Message):
    """A row inside a modal was clicked. The modal decides what that means."""

    def __init__(self, index: int) -> None:
        self.index = index
        super().__init__()


class DismissOnOutsideClick:
    """Clicking the dimmed area around a modal closes it.

    The click has to be tested against the content region rather than the widget that
    received it: clicks bubble up from children, so a click on a row inside the modal also
    reaches the screen and would otherwise close it.
    """

    #: Selectors for the widgets that count as "inside" the modal.
    content_selectors: tuple[str, ...] = ()

    def on_click(self, event: Click) -> None:
        if not self.content_selectors:
            return
        x = event.screen_x if event.screen_x is not None else event.x
        y = event.screen_y if event.screen_y is not None else event.y
        for selector in self.content_selectors:
            with contextlib.suppress(Exception):
                widget = self.query_one(selector)  # type: ignore[attr-defined]
                if widget.region.contains(x, y):
                    return
        self.dismiss()  # type: ignore[attr-defined]


class DetailRow(Static):
    """One clickable row of a detail view.

    A row is a widget rather than a line of a single renderable so a click lands on it:
    a block of text cannot tell which row was hit.
    """

    can_focus = False

    def __init__(self, index: int, text: Text) -> None:
        super().__init__(text, id=f"sb-detail-row-{index}")
        self.index = index

    def on_click(self) -> None:
        self.post_message(RowActivated(self.index))


class DetailScroll(VerticalScroll):
    """The modal's scroll container.

    The arrows walk the rows rather than scrolling the box: a scroll container's own
    bindings are checked before the screen's, so with the wrapper focused the chevron
    stayed put and the list moved under it. Page keys and the wheel still scroll, and the
    row the cursor lands on is brought into view.
    """

    BINDINGS = [
        Binding("up", "cursor(-1)", "up", show=False),
        Binding("k", "cursor(-1)", "up", show=False),
        Binding("down", "cursor(1)", "down", show=False),
        Binding("j", "cursor(1)", "down", show=False),
    ]

    def action_cursor(self, delta: str) -> None:
        # Textual passes an action's parameters as strings: "cursor(-1)" arrives as "-1".
        screen = self.screen
        move = getattr(screen, "_move_cursor", None)
        if move is not None:
            move(int(delta))


class DetailScreen(DismissOnOutsideClick, ModalScreen[None]):
    """Stable detail view for one card."""

    content_selectors = ("#sb-detail",)

    BINDINGS = [
        Binding("escape", "dismiss_screen", "close"),
        Binding("enter", "open_url", "open link", show=False),
        Binding("o", "open_url", "open link"),
        Binding("c", "open_discussion", "comments"),
        Binding("s", "save", "save"),
        Binding("up", "cursor_up", "up", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("j", "cursor_down", "down", show=False),
    ]

    class SaveRequested(Message):
        def __init__(self, item_id: str) -> None:
            self.item_id = item_id
            super().__init__()

    def __init__(
        self,
        *,
        title: str,
        view: ViewModel,
        privacy: bool,
        url_provider: Callable[[], str | None],
        item_provider: Callable[[], str | None],
        selected_index: int = 0,
        freshness: str = "",
    ) -> None:
        super().__init__()
        self.view_title = title
        self.view = view
        self.privacy = privacy
        self.index = selected_index
        self.freshness = freshness
        self._url_provider = url_provider
        self._item_provider = item_provider

    def compose(self) -> object:
        wrapper = DetailScroll(id="sb-detail")
        wrapper.border_title = single_line(markup_free(self.view_title), 60)
        # The same one timestamp the card shows, in the same place a card puts it.
        wrapper.border_subtitle = single_line(markup_free(self.freshness), 60) or None
        yield wrapper

    def on_mount(self) -> None:
        wrapper = self.query_one("#sb-detail", VerticalScroll)
        wrapper.mount(Static(id="sb-detail-head"))
        for index in range(len(self.view.items)):
            wrapper.mount(DetailRow(index, Text("")))
        wrapper.mount(Static(id="sb-detail-table"))
        self._refresh_body()

    def _row_widgets(self) -> list[DetailRow]:
        return list(self.query(DetailRow))

    def _refresh_body(self) -> None:
        """Head, rows and foot, with the cursor row marked.

        Only the rows whose selection changed are rewritten: a detail view can hold
        hundreds of rows and this runs on every arrow press.
        """
        width = max(40, self.size.width - 8)
        head = self.query_one("#sb-detail-head", Static)
        sections: list[RenderableType] = []
        sections.append(
            render_view_body(
                self.view,
                width=width,
                privacy=self.privacy,
                selected_index=self.index,
                items_as_rows=True,
                include_table=False,
            )
        )
        head.update(Group(*sections), layout=False)
        table = self.query_one("#sb-detail-table", Static)
        item_count, _ = cursor_layout(self.view)
        table.update(
            render_table(
                self.view.table,
                width=width,
                privacy=self.privacy,
                selected_index=(self.index - item_count if self.index >= item_count else -1),
            )
            if self.view.table is not None and self.view.table.columns
            else Text(""),
            layout=False,
        )
        rows = self._row_widgets()
        for row in rows:
            if row.index >= len(self.view.items):
                continue
            row.update(
                render_item_row(
                    self.view.items[row.index],
                    width=width,
                    privacy=self.privacy,
                    selected=row.index == self.index,
                ),
                layout=False,
            )

    def _rows(self) -> int:
        """Rows the cursor can reach: the items, then the table rows.

        One list, so the cursor reaches the last row of either. Treating them separately
        also meant the same index marked a row in both sections at once — two chevrons.
        """
        items, rows = cursor_layout(self.view)
        return items + rows

    def _selected_url(self) -> str | None:
        target = cursor_target(self.view, self.index)
        if target is None:
            return None
        kind, index = target
        if kind == "item":
            return self.view.items[index].url
        assert self.view.table is not None
        return self.view.table.rows[index].url

    def _selected_id(self) -> str | None:
        target = cursor_target(self.view, self.index)
        if target is None:
            return None
        kind, index = target
        if kind == "item":
            return self.view.items[index].id
        assert self.view.table is not None
        return self.view.table.rows[index].id

    def action_cursor_up(self) -> None:
        self._move_cursor(-1)

    def action_cursor_down(self) -> None:
        self._move_cursor(1)

    def _move_cursor(self, delta: int) -> None:
        rows = self._rows()
        if rows <= 0:
            return
        before = self.index
        self.index = max(0, min(rows - 1, self.index + delta))
        if self.index != before:
            self._refresh_body()
            widgets = self._row_widgets()
            if 0 <= self.index < len(widgets):
                # Ask the container, not the widget: `scroll_visible` goes through the
                # screen's scrolling, which does not move this modal's own box.
                with contextlib.suppress(Exception):
                    self.query_one("#sb-detail", DetailScroll).scroll_to_widget(
                        widgets[self.index], animate=False, immediate=True
                    )

    def on_row_activated(self, event: RowActivated) -> None:
        """A clicked row is the selected row, and a row with a link opens it."""
        rows = self._rows()
        if rows <= 0:
            return
        before = self.index
        self.index = max(0, min(rows - 1, event.index))
        if self.index != before:
            self._refresh_body()
        self.action_open_url()

    def action_dismiss_screen(self) -> None:
        self.dismiss()

    def action_open_url(self) -> None:
        # The row under the cursor decides. If it has no link, say so rather than opening
        # a different article's: a view can hold many links and the wrong one is worse than
        # none. Only a view with no rows at all falls back to its first link.
        if self._rows() > 0:
            url = self._selected_url()
            if url is None:
                self.notify("No link on this row")  # type: ignore[attr-defined]
                return
        else:
            url = self._url_provider() or self.view.first_url()
        if url is not None:
            self.dismiss()
            self.app.open_validated_url(url)  # type: ignore[attr-defined]

    def action_open_discussion(self) -> None:
        """`c`: the second link of the row under the cursor (the comment thread)."""
        target = cursor_target(self.view, self.index)
        url = None
        if target is not None:
            kind, index = target
            if kind == "item":
                url = self.view.items[index].alt_url
        if url is None:
            self.notify("No discussion link on this row")  # type: ignore[attr-defined]
            return
        self.dismiss()
        self.app.open_validated_url(url)  # type: ignore[attr-defined]

    def action_save(self) -> None:
        item_id = self._selected_id() or self._item_provider()
        if item_id:
            self.post_message(DetailScreen.SaveRequested(item_id))


class HelpScreen(DismissOnOutsideClick, ModalScreen[None]):
    content_selectors = ("#sb-help",)

    BINDINGS = [
        Binding("escape", "dismiss_screen", "close"),
        Binding("?", "dismiss_screen", "close"),
    ]

    HELP_ROWS = (
        ("1–9", "select the first nine visible tabs"),
        (
            "Arrows",
            "walk cards: left/right step across and wrap, up/down follow the grid; "
            "inside a card the arrows move its rows first",
        ),
        ("Tab / Shift+Tab", "next / previous card of the visible tab"),
        (
            "Enter / click",
            "open the detail view; inside it, Enter or a click on a row opens that "
            "row's link, and a click outside closes it",
        ),
        ("Space", "toggle global visual pause (collection continues)"),
        ("p", "pin / unpin the focused overview card"),
        ("n", "advance the focused rotating card, even while paused"),
        ("o", "open the selected validated link in the browser"),
        ("c", "open the discussion link of the selected row (Hacker News, Reddit)"),
        ("s", "save / unsave the selected item"),
        ("r", "request a rate-limited refresh of the selected provider"),
        ("v", "privacy mode: hide amounts and private identifiers"),
        ("Ctrl+Y", "release every hold"),
        ("t / Ctrl+T", "next colour palette / pick one (kept in the configuration)"),
        ("Ctrl+P", "command palette: any tab, any widget, any action"),
        ("Ctrl+R", "validate and reload configuration"),
        ("?", "this help"),
        ("q", "quit"),
    )

    def compose(self) -> object:
        lines = Text()
        lines.append("Sideboard controls\n\n", style=VALUE)
        for keys, description in self.HELP_ROWS:
            lines.append(f"  {keys:<16}", style=BORDER_FOCUS)
            lines.append(f"{description}\n", style=DIM)
        lines.append("\nHover pauses only the hovered card. Data keeps collecting.\n", style=DIM)
        lines.append(
            "The pointer/focus diagnostic is a development tool: it is in the palette "
            "(Ctrl+P) and in `sideboard doctor --mouse`.\n",
            style=DIM,
        )
        lines.append("q or Esc closes this help.\n", style=DIM)
        yield Static(lines, id="sb-help")

    def action_dismiss_screen(self) -> None:
        self.dismiss()


class PaletteCommand(Message):
    def __init__(self, command_id: str) -> None:
        self.command_id = command_id
        super().__init__()


class CommandPaletteScreen(DismissOnOutsideClick, ModalScreen[None]):
    """Ctrl+P palette: switch tabs, focus widgets, run actions."""

    content_selectors = ("#sb-help", "#sb-palette-input")

    BINDINGS = [Binding("escape", "dismiss_screen", "close")]

    def __init__(self, commands: Sequence[tuple[str, str]]) -> None:
        super().__init__()
        self._commands = tuple(commands)

    def compose(self) -> object:
        container = VerticalScroll(id="sb-help")
        container.border_title = "Command palette"
        yield container
        yield Input(placeholder="filter…", id="sb-palette-input")

    def on_mount(self) -> None:
        options = OptionList(id="sb-palette-list")
        self.query_one("#sb-help", VerticalScroll).mount(options)
        self._fill(self._commands)
        self.query_one("#sb-palette-input", Input).focus()

    def _fill(self, commands: Sequence[tuple[str, str]]) -> None:
        options = self.query_one("#sb-palette-list", OptionList)
        options.clear_options()
        for command_id, label in commands:
            options.add_option(Option(single_line(label, 60), id=command_id))

    def on_input_changed(self, event: Input.Changed) -> None:
        needle = event.value.strip().lower()
        if not needle:
            self._fill(self._commands)
            return
        filtered = [
            (command_id, label)
            for command_id, label in self._commands
            if needle in label.lower() or needle in command_id.lower()
        ]
        self._fill(filtered)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        options = self.query_one("#sb-palette-list", OptionList)
        if options.option_count:
            options.action_select()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        command_id = str(event.option.id or "")
        self.dismiss()
        if command_id:
            self.app.post_message(PaletteCommand(command_id))  # type: ignore[attr-defined]

    def action_dismiss_screen(self) -> None:
        self.dismiss()


class ThemePickerScreen(DismissOnOutsideClick, ModalScreen[None]):
    """Ctrl+T: pick a palette.

    Choosing one applies it and writes ``app.theme`` into the configuration, editing that
    single line so the comments around it survive.
    """

    content_selectors = ("#sb-help",)

    BINDINGS = [Binding("escape", "dismiss_screen", "close")]

    def __init__(self, palettes: Sequence[tuple[str, str]], current: str) -> None:
        super().__init__()
        self._palettes = tuple(palettes)
        self._current = current

    def compose(self) -> object:
        wrapper = VerticalScroll(id="sb-help")
        wrapper.border_title = "Colour palette"
        yield wrapper

    def on_mount(self) -> None:
        options = OptionList(id="sb-palette-list")
        self.query_one("#sb-help", VerticalScroll).mount(options)
        for key, label in self._palettes:
            marker = "● " if key == self._current else "  "
            options.add_option(Option(single_line(f"{marker}{label}", 48), id=key))
        self.query_one("#sb-help", VerticalScroll).mount(
            Static(
                Text(
                    "Choosing a palette applies it now and writes app.theme into the "
                    "configuration,\nediting that one line and leaving your comments "
                    "alone.\n",
                    style=DIM,
                ),
                id="sb-theme-note",
            )
        )
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        key = str(event.option.id or "")
        self.dismiss()
        if key:
            self.app.post_message(PaletteCommand(f"theme:{key}"))  # type: ignore[attr-defined]

    def action_dismiss_screen(self) -> None:
        self.dismiss()


class MouseDiagnosticScreen(ModalScreen[None]):
    """Mouse/focus diagnostic. Hover support cannot be verified from headless tests."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "close"),
        Binding("q", "dismiss_screen", "close", show=False),
        Binding("enter", "dismiss_screen", "close", show=False),
        Binding("r", "reset_holds", "reset holds"),
    ]

    class ResetHolds(Message):
        """Request the app to release every hold."""

    def __init__(self, *, terminal: str, mouse_enabled: bool | None = None) -> None:
        super().__init__()
        self.terminal = terminal
        self.mouse_enabled = mouse_enabled
        self.moves = 0
        self.enters = 0
        self.leaves = 0
        self.clicks = 0
        self.blurs = 0
        self.last: str = "—"

    def compose(self) -> object:
        wrapper = VerticalScroll(id="sb-help")
        wrapper.border_title = "Mouse / focus diagnostic"
        yield wrapper

    def on_mount(self) -> None:
        self.query_one("#sb-help", VerticalScroll).mount(
            Static(self._report(), id="sb-mouse-report")
        )

    def _report(self) -> Text:
        text = Text()
        text.append("Pointer and focus events seen in this session\n\n", style=VALUE)
        rows = (
            ("terminal", self.terminal),
            ("mouse motion", "reporting" if self.moves else "not seen yet"),
            ("MouseMove events", str(self.moves)),
            ("Enter events", str(self.enters)),
            ("Leave events", str(self.leaves)),
            ("Clicks", str(self.clicks)),
            ("App blur events", str(self.blurs)),
            ("last event", self.last),
        )
        for label, value in rows:
            text.append(f"  {label:<20}", style=DIM)
            text.append(f"{value}\n", style=VALUE)
        text.append(
            "\nMove the pointer over this screen, then over a card on the Overview.\n"
            "If MouseMove stays at 0 the terminal is not reporting motion; hover pause\n"
            "and mouse selection will not work there. Keyboard controls still do.\n\n",
            style=DIM,
        )
        text.append("r  release every hold (visible reset-holds action)\n", style=DIM)
        text.append("Esc, q, or any other key  close this screen\n", style=DIM)
        return text

    def _touch(self, kind: str, x: int | None = None, y: int | None = None) -> None:
        self.last = f"{kind} {x},{y}" if x is not None else kind
        with contextlib.suppress(Exception):  # pragma: no cover - may not be mounted yet
            # layout=False: the report is a fixed block of lines, so recounting events must
            # repaint its own rows instead of asking the whole screen to re-lay out. With
            # the default, every fifth mouse move repainted the entire screen.
            self.query_one("#sb-mouse-report", Static).update(self._report(), layout=False)

    def on_key(self, event: Key) -> None:
        """Any key other than r returns to the dashboard.

        This screen exists to explain why hover is not working, so being stuck on it is
        worse than not having it: a terminal that sends an unexpected byte (Ctrl+D is also
        EOF) must not be able to trap the app here. ``r`` still releases holds.
        """
        if event.key == "r":
            return
        event.stop()
        self.dismiss()

    def on_mouse_move(self, event: MouseMove) -> None:
        self.moves += 1
        if self.moves % 5 == 0 or self.moves < 5:
            self._touch("MouseMove", event.x, event.y)

    def on_click(self, event: Click) -> None:
        self.clicks += 1
        self._touch("Click", event.x, event.y)

    def on_enter(self) -> None:
        self.enters += 1
        self._touch("Enter")

    def on_leave(self) -> None:
        self.leaves += 1
        self._touch("Leave")

    def note_blur(self) -> None:
        self.blurs += 1
        self._touch("AppBlur")

    def action_reset_holds(self) -> None:
        self.post_message(MouseDiagnosticScreen.ResetHolds())

    def action_dismiss_screen(self) -> None:
        self.dismiss()
