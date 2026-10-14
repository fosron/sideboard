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
from .renderers import render_items, render_metrics, render_series, render_status, render_table
from .styles import BORDER_FOCUS, DIM, VALUE
from .text import single_line

__all__ = [
    "CommandPaletteScreen",
    "DetailScreen",
    "HelpScreen",
    "MouseDiagnosticScreen",
]


def render_view_body(
    view: ViewModel, *, width: int, privacy: bool, selected_index: int = 0
) -> RenderableType:
    """Detail-mode rendering of a whole standard view model.

    ``selected_index`` marks the row the cursor is on, so the detail view can be walked
    with the arrow keys and opened from there.
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
    if view.table is not None and view.table.columns:
        sections.append(
            render_table(view.table, width=width, privacy=privacy, selected_index=selected_index)
        )
        if view.table.footnote:
            sections.append(Text(single_line(view.table.footnote, width), style=DIM))
    if view.items:
        sections.append(
            render_items(
                view.items,
                width=width,
                privacy=privacy,
                selected_index=selected_index,
                max_rows=200,
            )
        )
    if view.footnote:
        sections.append(Text(single_line(view.footnote, width), style=DIM))
    if not sections:
        sections.append(Text("no data", style=DIM))
    return Group(*sections)


class DetailScreen(ModalScreen[None]):
    """Stable detail view for one card."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "close"),
        Binding("o", "open_url", "open link"),
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
        provenance: str = "",
    ) -> None:
        super().__init__()
        self.view_title = title
        self.view = view
        self.privacy = privacy
        self.index = selected_index
        self.provenance = provenance
        self._url_provider = url_provider
        self._item_provider = item_provider

    def compose(self) -> object:
        body = Static(id="sb-detail-body")
        wrapper = VerticalScroll(body, id="sb-detail")
        wrapper.border_title = single_line(self.view_title, 60)
        wrapper.border_subtitle = single_line(self.view.footnote or "", 60) or None
        yield wrapper

    def on_mount(self) -> None:
        self._refresh_body()

    def _refresh_body(self) -> None:
        width = max(40, self.size.width - 8)
        body = self.query_one("#sb-detail-body", Static)
        sections: list[RenderableType] = []
        if self.provenance:
            # The card shows one timestamp; where it came from, when it was fetched and
            # whether it is partial belong here, next to the numbers they qualify.
            sections.append(Text(single_line(self.provenance, width), style=DIM))
        sections.append(
            render_view_body(
                self.view, width=width, privacy=self.privacy, selected_index=self.index
            )
        )
        body.update(Group(*sections), layout=False)

    def _rows(self) -> int:
        """Rows the cursor can reach: the table when there is one, otherwise the items."""
        if self.view.table is not None and self.view.table.rows:
            return len(self.view.table.rows)
        return len(self.view.items)

    def _selected_url(self) -> str | None:
        table = self.view.table
        if table is not None and table.rows and 0 <= self.index < len(table.rows):
            return table.rows[self.index].url
        if 0 <= self.index < len(self.view.items):
            return self.view.items[self.index].url
        return None

    def _selected_id(self) -> str | None:
        table = self.view.table
        if table is not None and table.rows and 0 <= self.index < len(table.rows):
            return table.rows[self.index].id
        if 0 <= self.index < len(self.view.items):
            return self.view.items[self.index].id
        return None

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

    def action_dismiss_screen(self) -> None:
        self.dismiss()

    def action_open_url(self) -> None:
        # The row under the cursor wins; then the card's own selection, then the first
        # link in the view. Opening a link is explicit: nothing is opened on arrival.
        url = self._selected_url() or self._url_provider() or self.view.first_url()
        if url is not None:
            self.dismiss()
            self.app.open_validated_url(url)  # type: ignore[attr-defined]

    def action_save(self) -> None:
        item_id = self._selected_id() or self._item_provider()
        if item_id:
            self.post_message(DetailScreen.SaveRequested(item_id))


class HelpScreen(ModalScreen[None]):
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
        ("Enter / click", "open the detail view (arrows walk it, Esc closes it)"),
        ("Space", "toggle global visual pause (collection continues)"),
        ("p", "pin / unpin the focused overview card"),
        ("n", "advance the focused rotating card, even while paused"),
        ("o", "open the selected validated link in the browser"),
        ("s", "save / unsave the selected item"),
        ("r", "request a rate-limited refresh of the selected provider"),
        ("v", "privacy mode: hide amounts and private identifiers"),
        ("Ctrl+Y", "release every hold"),
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


class CommandPaletteScreen(ModalScreen[None]):
    """Ctrl+P palette: switch tabs, focus widgets, run actions."""

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
