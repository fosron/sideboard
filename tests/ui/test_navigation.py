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
