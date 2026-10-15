"""Modal screens must be dismissible and cheap to update.

The diagnostic screen is a development tool: it is opened from the command palette or by
`sideboard doctor --mouse`, and it can also be opened by a stray Ctrl+D byte if a terminal
sends one. A modal that swallows every key and offers no visible way out looks exactly
like a broken application.
"""

from __future__ import annotations

from pathlib import Path


async def test_mouse_diagnostic_cannot_trap_the_app(tmp_path: Path) -> None:
    """Any key closes the diagnostic screen.

    Ctrl+D is also a terminal's end-of-input key, so a stray byte can open this modal. It
    must not be possible to be stuck on it: every key except r returns to the dashboard,
    and the screen says so.
    """
    from textual.app import App

    from sideboard.ui.palettes import resolve_palette
    from sideboard.ui.screens import MouseDiagnosticScreen
    from sideboard.ui.theme import build_rich_theme

    class Host(App[None]):
        def on_load(self) -> None:
            # The dashboard's renderers resolve palette names (``sb-dim`` …) through the
            # console's Rich theme, so any host that renders them registers it first.
            self.console.push_theme(build_rich_theme(resolve_palette("sideboard")))

        def on_mount(self) -> None:
            self.push_screen(MouseDiagnosticScreen(terminal="test"))

    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, MouseDiagnosticScreen)
        await pilot.press("x")
        await pilot.pause()
        assert not isinstance(app.screen, MouseDiagnosticScreen), "x must close the screen"
        app.push_screen(MouseDiagnosticScreen(terminal="test"))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, MouseDiagnosticScreen), "escape must close it"
        app.push_screen(MouseDiagnosticScreen(terminal="test"))
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, MouseDiagnosticScreen), "r releases holds, it closes nothing"


async def test_clicking_outside_a_modal_closes_it(tmp_path: Path) -> None:
    """The dimmed margin around a modal is a close target, and the modal is not one.

    Clicks bubble from children to the screen, so the handler tests the click against the
    content region instead of trusting whichever widget received the event. Uses the real
    application because the margin comes from its stylesheet: the modal is 90% x 80%.
    """
    from sideboard.ui.screens import DetailScreen
    from tests.ui.test_app import make_app, settle  # type: ignore[import-not-found]

    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await settle(app, pilot)
        card = next(card for card in app._all_cards() if card.view is not None)
        card.focus()
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DetailScreen)
        wrapper = screen.query_one("#sb-detail")
        assert wrapper.region.width < app.size.width, "the modal must leave a margin"

        # A click inside the modal keeps it open.
        inside = wrapper.region
        await pilot.click(offset=(inside.x + 2, inside.y + 2))
        await pilot.pause()
        assert isinstance(app.screen, DetailScreen), "a click inside must not close it"

        # A click on the margin closes it. The margin exists on every side because the
        # modal is centred; pick a corner that is definitely outside it.
        margin = (1, 1)
        if inside.x <= 1 and inside.y <= 1:
            margin = (app.size.width - 1, app.size.height - 1)
        await pilot.click(offset=margin)
        await pilot.pause()
        assert not isinstance(app.screen, DetailScreen), "an outside click must close it"
    await app.runtime.database.aclose()


async def test_clicking_a_row_opens_that_row_link(tmp_path: Path) -> None:
    """A click lands on a row, not on a block of text, and opens that row's link."""
    from sideboard.app import SideboardApp
    from tests.ui.test_app import make_app, settle  # type: ignore[import-not-found]

    app = await make_app(tmp_path)
    opened: list[str] = []
    app.open_validated_url = lambda url: opened.append(url)  # type: ignore[method-assign]
    async with app.run_test(size=(140, 44)) as pilot:
        await settle(app, pilot)
        card = next(
            card
            for card in app._all_cards()
            if card.view is not None and card.view.items and card.view.items[0].url
        )
        card.focus()
        card.select_index(0)
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        from sideboard.ui.screens import DetailRow

        rows = list(screen.query(DetailRow))  # type: ignore[attr-defined]
        assert rows, "the detail view renders one widget per item"
        rows[0].on_click()
        await pilot.pause()
        assert card.view is not None
        assert opened == [card.view.items[0].url], opened
        assert not isinstance(app.screen, type(screen)), "opening closes the modal"
        _ = SideboardApp
    await app.runtime.database.aclose()


async def test_enter_opens_the_selected_row_in_the_detail_view(tmp_path: Path) -> None:
    from tests.ui.test_app import make_app, settle  # type: ignore[import-not-found]

    app = await make_app(tmp_path)
    opened: list[str] = []
    app.open_validated_url = lambda url: opened.append(url)  # type: ignore[method-assign]
    async with app.run_test(size=(140, 44)) as pilot:
        await settle(app, pilot)
        card = next(
            card
            for card in app._all_cards()
            if card.view is not None and len(card.view.items) > 1 and card.view.items[1].url
        )
        assert card.view is not None
        card.focus()
        card.select_index(0)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert opened == [card.view.items[1].url], opened
    await app.runtime.database.aclose()


async def test_a_row_without_a_link_does_not_open_another_row(tmp_path: Path) -> None:
    """The wrong article is worse than none."""
    from textual.app import App

    from sideboard.core.contracts import Item, Privacy, ViewModel
    from sideboard.ui.palettes import resolve_palette
    from sideboard.ui.screens import DetailScreen
    from sideboard.ui.theme import build_rich_theme

    class Host(App[None]):
        def on_load(self) -> None:
            self.console.push_theme(build_rich_theme(resolve_palette("sideboard")))

        def on_mount(self) -> None:
            view = ViewModel(
                title="Mixed",
                items=(
                    Item(id="a:1", title="No link here", privacy=Privacy.PUBLIC),
                    Item(
                        id="a:2",
                        title="A link",
                        url="https://example.com/second",
                        privacy=Privacy.PUBLIC,
                    ),
                ),
            )
            self.push_screen(
                DetailScreen(
                    title="Mixed",
                    view=view,
                    privacy=False,
                    url_provider=lambda: None,
                    item_provider=lambda: None,
                )
            )

    app = Host()
    opened: list[str] = []
    app.open_validated_url = lambda url: opened.append(url)  # type: ignore[method-assign]
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert opened == [], "a row without a link must not open another row's"
        notices = [str(note.message) for note in app._notifications]
        assert any("No link on this row" in note for note in notices), notices


async def test_one_cursor_reaches_items_and_table_rows_and_marks_one(tmp_path: Path) -> None:
    """A view with both sections is one list, and only one row carries the chevron.

    The same index was applied to the item list and the table, so two rows were marked at
    once, and `row_count` counted only the items, so the last date could not be reached.
    """
    from textual.app import App

    from sideboard.core.contracts import (
        Item,
        Privacy,
        TableColumn,
        TableRow,
        TableView,
        ViewModel,
    )
    from sideboard.ui.card import cursor_layout, cursor_target
    from sideboard.ui.palettes import resolve_palette
    from sideboard.ui.screens import DetailRow, DetailScreen
    from sideboard.ui.theme import build_rich_theme

    view = ViewModel(
        title="Weather",
        items=tuple(
            Item(
                id=f"hour-{hour}",
                title=f"{hour}:00",
                url=f"https://example.com/{hour}",
                privacy=Privacy.PUBLIC,
            )
            for hour in (18, 19)
        ),
        table=TableView(
            columns=(TableColumn(id="date", label="Date"),),
            rows=tuple(
                TableRow(
                    id=f"2026-10-{day}",
                    cells={"date": f"Oct {day}"},
                    url=f"https://example.com/d{day}",
                )
                for day in (14, 15, 16)
            ),
        ),
    )
    assert cursor_layout(view) == (2, 3)
    assert cursor_target(view, 0) == ("item", 0)
    assert cursor_target(view, 1) == ("item", 1)
    assert cursor_target(view, 2) == ("row", 0)
    assert cursor_target(view, 4) == ("row", 2)
    assert cursor_target(view, 5) is None

    class Host(App[None]):
        def on_load(self) -> None:
            self.console.push_theme(build_rich_theme(resolve_palette("sideboard")))

        def on_mount(self) -> None:
            self.push_screen(
                DetailScreen(
                    title="Weather",
                    view=view,
                    privacy=False,
                    url_provider=lambda: None,
                    item_provider=lambda: None,
                )
            )

    app = Host()
    opened: list[str] = []
    app.open_validated_url = lambda url: opened.append(url)  # type: ignore[method-assign]
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DetailScreen)
        assert screen._rows() == 5, "the cursor must reach the last date"
        for _ in range(4):
            await pilot.press("down")
            await pilot.pause()
        assert screen.index == 4, "four presses reach the last table row"

        def plain(widget) -> str:  # type: ignore[no-untyped-def]
            # A Static wraps what it was given in a Visual; the Rich renderable is inside.
            renderable = getattr(widget.visual, "_renderable", widget.visual)
            return "".join(text for text, _style, _control in app.console.render(renderable))

        rows = list(screen.query(DetailRow))
        chevrons = sum(plain(row).count("›") for row in rows)
        table_text = plain(screen.query_one("#sb-detail-table"))
        assert chevrons + table_text.count("›") == 1, (chevrons, table_text)
        await pilot.press("enter")
        await pilot.pause()
        assert opened == ["https://example.com/d16"], opened


async def test_arrows_walk_rows_in_a_long_modal_and_page_keys_scroll() -> None:
    """A scroll container's bindings beat the screen's, so the chevron never moved.

    With the wrapper focused, `up`/`down` scrolled the box and the cursor stayed where it
    was. The wrapper now walks the cursor; PgDn still scrolls, and the row the cursor lands
    on is brought into view.
    """
    from textual.app import App

    from sideboard.core.contracts import Item, Privacy, ViewModel
    from sideboard.ui.palettes import resolve_palette
    from sideboard.ui.screens import DetailRow, DetailScreen
    from sideboard.ui.theme import build_rich_theme

    view = ViewModel(
        title="News",
        items=tuple(
            Item(
                id=f"news:{index}",
                title=f"Headline {index}",
                url=f"https://example.com/{index}",
                privacy=Privacy.PUBLIC,
            )
            for index in range(30)
        ),
    )

    class Host(App[None]):
        def on_load(self) -> None:
            self.console.push_theme(build_rich_theme(resolve_palette("sideboard")))

        def on_mount(self) -> None:
            self.push_screen(
                DetailScreen(
                    title="News",
                    view=view,
                    privacy=False,
                    url_provider=lambda: None,
                    item_provider=lambda: None,
                )
            )

    app = Host()
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DetailScreen)
        wrapper = screen.query_one("#sb-detail")
        assert len(list(screen.query(DetailRow))) == 30
        assert wrapper.scroll_offset.y == 0

        await pilot.press("down")
        await pilot.pause()
        assert screen.index == 1, "down must move the cursor"
        assert wrapper.scroll_offset.y == 0, "…and not scroll the box by itself"

        # Rows 0..19 fit the viewport, so nothing scrolls until the cursor passes them.
        for _ in range(24):
            await pilot.press("down")
            await pilot.pause()
        assert screen.index == 25
        assert wrapper.scroll_offset.y > 0, "the view follows the cursor past the fold"

        offset = wrapper.scroll_offset.y
        await pilot.press("pagedown")
        await pilot.pause()
        assert wrapper.scroll_offset.y > offset, "PgDn scrolls"
        assert screen.index == 25, "PgDn does not move the cursor"

        # The last row is reachable, and only it carries the chevron.
        for _ in range(10):
            await pilot.press("down")
            await pilot.pause()
        assert screen.index == 29
        marked = [
            row
            for row in screen.query(DetailRow)
            if "›" in str(getattr(row.visual, "_renderable", row.visual))
        ]
        assert len(marked) == 1, "exactly one row is marked"


def test_widget_labels_read_like_names() -> None:
    """A filter called "List" and a card called "Hn Top" are identifiers, not labels."""
    from sideboard.app import widget_label

    assert widget_label("reddit", None, "reddit.list") == "Reddit"
    assert widget_label("hn_top", None, "hackernews.list") == "HN Top"
    assert widget_label("weather", None, "weather.summary") == "Weather"
    assert widget_label("mac_summary", None, "mac.summary") == "Mac Summary"
    assert widget_label("docker_containers", None, "docker.table") == "Docker Containers"
    assert widget_label("news", "My News", "news.digest") == "My News", "a configured title wins"


def test_border_chrome_cannot_be_parsed_as_markup() -> None:
    """Textual parses border titles as markup; provider text must not reach it raw.

    Measured before the fix: `[live] News` silently lost text, `[red]BBC[/red]` was styled,
    `[link=evil.example]click[/link]` became a terminal hyperlink, and `[/]` raised
    MarkupError — a crash path from a feed's own title.
    """
    from sideboard.ui.text import markup_free

    assert markup_free("[live] News") == "(live) News"
    assert markup_free("[red]BBC[/red]") == "(red)BBC(/red)"
    assert markup_free("[link=evil.example]click[/link]") == "(link=evil.example)click(/link)"
    assert markup_free("[/]") == "(/)"
    assert markup_free("\x1b[31mred\x1b[0m") == "red", "control stripping still applies"


async def test_a_stale_card_is_drawn_as_stale(tmp_path: Path) -> None:
    """Colour, not only one dim word: the values dim and the timestamp warns."""
    from sideboard.core.contracts import Freshness
    from tests.ui.test_app import make_app, settle  # type: ignore[import-not-found]

    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await settle(app, pilot)
        card = app._card_for_widget("mac_summary")
        assert card is not None and card.envelope is not None
        assert card.is_stale is False
        from dataclasses import replace

        card.envelope = replace(card.envelope, freshness=Freshness.EXPIRED)
        card.set_card(
            widget_id=card.widget_id,
            title=card.card_title,
            view=card.view,
            envelope=card.envelope,
            freshness=card.freshness,
        )
        await pilot.pause()
        assert card.is_stale is True
        assert card.has_class("sb-stale"), "the stylesheet needs the class to tone it"
    await app.runtime.database.aclose()
