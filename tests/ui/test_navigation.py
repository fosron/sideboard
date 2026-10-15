"""Keyboard navigation between cards.

The arrow keys are the primary way around the dashboard on a remote display, so the
contract is: inside a card they walk its rows, at the edge they move to the card in that
direction, left and right wrap, and the card that receives focus is scrolled into view.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sideboard.app import SideboardApp, build_runtime
from sideboard.core.paths import default_paths
from sideboard.ui.navigation import Box, neighbour


def _grid() -> list[Box]:
    """Two rows of two cards, 70 columns wide and 20 rows tall, as the Overview lays out."""
    return [
        Box("a", 0, 2, 70, 20),
        Box("b", 70, 2, 70, 20),
        Box("c", 0, 22, 70, 20),
        Box("d", 70, 22, 70, 20),
    ]


def test_neighbour_follows_the_grid() -> None:
    grid = _grid()
    assert neighbour(grid, "a", "right") == "b"
    assert neighbour(grid, "b", "left") == "a"
    assert neighbour(grid, "a", "down") == "c"
    assert neighbour(grid, "c", "up") == "a"
    assert neighbour(grid, "d", "up") == "b"


def test_neighbour_wraps_left_and_right_but_not_up_and_down() -> None:
    grid = _grid()
    assert neighbour(grid, "b", "right") == "a"  # end of the row: back to the start
    assert neighbour(grid, "a", "left") == "d"  # backwards from the first card
    assert neighbour(grid, "a", "up") is None
    assert neighbour(grid, "c", "down") is None


def test_neighbour_prefers_the_card_beside_it_over_the_one_diagonally_away() -> None:
    """A stack of unequal cards: the tie-break must not jump to another column."""
    boxes = [
        Box("left-top", 0, 0, 60, 10),
        Box("left-bottom", 0, 10, 60, 30),
        Box("right", 60, 0, 60, 40),
    ]
    assert neighbour(boxes, "left-top", "right") == "right"
    assert neighbour(boxes, "left-bottom", "right") == "right"
    # Going left, the tall card is vertically closer to the lower one.
    assert neighbour(boxes, "right", "left") == "left-bottom"


def test_neighbour_ignores_an_unknown_card_and_direction() -> None:
    assert neighbour(_grid(), "missing", "right") is None
    assert neighbour(_grid(), "a", "sideways") is None
    assert neighbour([Box("only", 0, 0, 10, 10)], "only", "right") is None


async def make_app(tmp_path: Path) -> SideboardApp:
    paths = default_paths(
        home=tmp_path,
        config_file=tmp_path / "config" / "config.toml",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    return SideboardApp(await build_runtime(paths=paths, demo=True))


async def test_arrow_keys_walk_between_cards(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await asyncio.sleep(1.0)
        cards = app._active_cards()
        assert len(cards) >= 2, "the Overview must offer more than one card"
        cards[0].focus()
        await pilot.press("right")
        await pilot.pause()
        assert app.focused_card() is cards[1], "right must move to the next card"
        await pilot.press("down")
        await pilot.pause()
        assert app.focused_card() is not cards[1], "down must leave the top row"
        await pilot.press("up")
        await pilot.pause()
        assert app.focused_card() is cards[1], "up must come back"
        await pilot.press("left")
        await pilot.pause()
        assert app.focused_card() is cards[0]
        await pilot.press("left")
        await pilot.pause()
        assert app.focused_card() is cards[-1], "left from the first card wraps"
        await pilot.press("right")
        await pilot.pause()
        assert app.focused_card() is cards[0], "right from the last card wraps"
    await app.runtime.database.aclose()


async def test_a_list_card_walks_its_rows_before_leaving(tmp_path: Path) -> None:
    """Up and down belong to the list until the list is finished."""
    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await asyncio.sleep(1.0)
        card = next(card for card in app._active_cards() if card.row_count > 1)
        card.focus()
        card.select_index(0)
        await pilot.press("down")
        await pilot.pause()
        assert app.focused_card() is card, "the first press must move the row cursor"
        assert card.cursor_index == 1
        for _ in range(card.row_count):
            await pilot.press("down")
            await pilot.pause()
        assert app.focused_card() is not card, "the last press must leave the card"
    await app.runtime.database.aclose()


async def test_ctrl_d_is_not_a_dashboard_control(tmp_path: Path) -> None:
    """The pointer/focus diagnostic is a development tool, not a key binding.

    Ctrl+D is end-of-input in a terminal: a stray byte used to open a modal that swallowed
    every key. It now lives in the palette and in `sideboard doctor --mouse`.
    """
    from sideboard.ui.screens import MouseDiagnosticScreen

    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.sleep(0.5)
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert not isinstance(app.screen, MouseDiagnosticScreen)
        commands = dict(app._palette_commands())
        assert "action:mouse" in commands, "the diagnostic must stay reachable by name"
    await app.runtime.database.aclose()


async def test_tab_cycles_the_cards_of_the_visible_tab(tmp_path: Path) -> None:
    """Tab must not wander onto a filter bar or the tab strip."""
    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await asyncio.sleep(1.0)
        cards = app._active_cards()
        assert len(cards) >= 3
        cards[0].focus()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused_card() is cards[1]
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.focused_card() is cards[0]
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.focused_card() is cards[-1], "backwards from the first card wraps"
    await app.runtime.database.aclose()


async def test_the_detail_view_can_be_walked_with_arrows(tmp_path: Path) -> None:
    """Enter opens the card's view on its cursor, and the arrows keep walking there."""
    from sideboard.ui.screens import DetailScreen

    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await asyncio.sleep(1.0)
        card = next(card for card in app._active_cards() if card.row_count > 2)
        card.focus()
        card.select_index(0)
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DetailScreen)
        assert screen.index == 0
        await pilot.press("down")
        await pilot.pause()
        assert screen.index == 1, "down must move the cursor inside the detail view"
        await pilot.press("j")
        await pilot.pause()
        assert screen.index == 2
        await pilot.press("up")
        await pilot.pause()
        assert screen.index == 1
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused_card() is card, "closing returns to the card"
    await app.runtime.database.aclose()


async def test_focus_moves_the_hold_instead_of_accumulating_one(tmp_path: Path) -> None:
    """Only the card the keyboard is on is held.

    Each card used to keep its interaction hold for fifteen seconds after being visited,
    so a walk with Tab left every card drawn as if it were the focused one.
    """
    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await asyncio.sleep(1.0)
        rotation = app._rotation
        assert rotation is not None
        cards = app._active_cards()
        cards[0].focus()
        app._apply_interaction_hold(cards[0].card_key)
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        assert app.focused_card() is cards[1]
        assert rotation.held("a") is False, "the card left behind must not stay held"
        assert rotation.held("b") is True, "the card in focus stays put"
    await app.runtime.database.aclose()


async def test_the_read_subfilter_can_be_switched_without_a_mouse(tmp_path: Path) -> None:
    """`[` and `]` walk the subfilter row, and every entry says how many rows it has.

    The row could only be clicked: `Tab` walks cards by contract, so `←` and `→` never
    reached it and four of the five Read views were unreachable from the keyboard. The
    entries were also rendered in `sb-dim`, which the contrast audit measured at 4.4966:1
    against the panel — below AA, and on the iPad the unselected views faded out of the
    row, so the Saved view looked like it did not exist.
    """
    from sideboard.ui.shell import FilterBar

    app = await make_app(tmp_path)
    async with app.run_test(size=(150, 44)) as pilot:
        await asyncio.sleep(1.2)
        await pilot.press("3")
        await pilot.pause()
        await asyncio.sleep(0.6)
        assert app._active_tab == "read"
        bar = app.query_one("#sb-filter-read", FilterBar)
        assert [filter_id for filter_id, _ in bar.filters] == [
            "news",
            "hn_top",
            "hn_new",
            "reddit",
            "saved",
        ]
        # Counts come from the presented views, so the Saved view is visibly not empty
        # once anything is saved.
        news_card = app._detail_panes["read"].card("news")
        assert news_card is not None and news_card.view is not None and news_card.view.items
        await app._toggle_saved(news_card.view.items[0].id)
        await pilot.pause()
        assert bar.label_for("saved") == "Saved 1", bar.counts

        await pilot.press("]")
        await pilot.pause()
        assert bar.active == "hn_top"
        card = app._detail_panes["read"].card(bar.active)
        assert card is not None and app.focused_card() is card, "focus follows the switch"
        await pilot.press("[")
        await pilot.pause()
        assert bar.active == "news"
        await pilot.press("[")
        await pilot.pause()
        assert bar.active == "saved", "backwards from the first entry wraps to the last"
        card = app._detail_panes["read"].card("saved")
        assert card is not None
        assert app._detail_panes["read"].card("news") is None, "one view at a time"
    await app.runtime.database.aclose()


async def test_a_click_on_a_counted_entry_selects_that_entry(tmp_path: Path) -> None:
    """The hit test measures the text that is drawn, count included.

    Widths were computed from the bare label while the row drew `Saved 1`, so every entry
    after a counted one was clickable one column short of where it appeared.
    """
    from sideboard.ui.shell import FilterBar

    app = await make_app(tmp_path)
    async with app.run_test(size=(150, 44)) as pilot:
        await asyncio.sleep(1.2)
        await pilot.press("3")
        await pilot.pause()
        await asyncio.sleep(0.6)
        bar = app.query_one("#sb-filter-read", FilterBar)
        bar.set_counts({filter_id: 9 for filter_id, _ in bar.filters})
        await pilot.pause()
        x = 0
        for filter_id, _ in bar.filters:
            if filter_id == "saved":
                break
            x += len(bar.label_for(filter_id)) + 3
        await pilot.click("#sb-filter-read", offset=(x + 1, 0))
        await pilot.pause()
        assert bar.active == "saved", f"clicked column {x + 1} and got {bar.active}"
    await app.runtime.database.aclose()


async def test_the_subfilter_keys_say_so_on_a_tab_that_has_none(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await asyncio.sleep(0.8)
        await pilot.press("6")
        await pilot.pause()
        assert app._active_tab == "systems"
        await pilot.press("]")
        await pilot.pause()
        notices = [str(note.message) for note in app._notifications]
        assert any("subfilter" in notice for notice in notices), notices
    await app.runtime.database.aclose()
