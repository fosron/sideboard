"""Performance contracts for the dashboard.

These tests measure what a terminal receives and how much layout work is done, because
the failure they guard against is invisible to functional tests: the dashboard worked
correctly and repainted everything, twice a second, forever.

Measured before the fixes these tests encode:

===========================================  ==============  =============
scenario                                     terminal output  layout passes
===========================================  ==============  =============
idle                                         24.7 KiB/s       0.4 /s
pointer sweep at 20 transitions/second       255.6 KiB/s      up to 4 each

Measured after them (140x44, demo data, collecting stopped):

===========================================  ==============  =============
scenario                                     terminal output  writes
===========================================  ==============  =============
idle                                         0.0 KiB/s        0
pointer sweep at 20 transitions/second       17 KiB/s         coalesced
one tab switch                               about 195 KiB    5
start-up (before the animation fix)          222 KiB          24
start-up (after it)                          47 KiB           6
===========================================  ==============  =============

Start-up was dominated by Textual's tab underline, which animates for 0.3 s at 60 fps
and repainted the whole screen on every frame: nineteen full-screen paints before the
dashboard was usable. ``App.__init__`` assigns ``animation_level`` from the environment,
so it has to be set afterwards — a class attribute is silently shadowed.

Bytes are counted where Textual decides to write (``App._display``), which is also where
a batch suppresses the write, rather than where the compositor builds an update: counting
earlier reports work that never reaches a terminal.
===========================================  ==============  =============
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from rich.console import Console
from textual._compositor import CompositorUpdate
from textual.app import App
from textual.screen import Screen
from textual.widget import Widget

from sideboard.app import SideboardApp, build_runtime
from sideboard.core.paths import default_paths
from sideboard.ui.card import CardShell

# Generous bounds: the point is to catch a per-second or per-event repaint, not to pin
# an exact byte count that would differ on another terminal width.
IDLE_BYTES_LIMIT = 4_000
HOVER_PAIR_BYTES_LIMIT = 25_000
SWEEP_BYTES_PER_TRANSITION_LIMIT = 6_000
# A tab switch paints the newly visible pane, and Textual lays that out more than once
# (pane display, tab underline, scrollbar). It follows a deliberate key press, so the
# bound is loose: it exists to catch a switch that re-presents every tab at once.
SWITCH_BYTES_LIMIT = 260_000
SWITCH_WRITES_LIMIT = 8


class Meter:
    """Counts terminal bytes and layout passes through Textual's own renderers."""

    def __init__(self) -> None:
        self.bytes = 0
        self.writes = 0
        self.layouts = 0
        self.card_refreshes = 0
        self._console = Console(width=140)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original_display = App._display
        original_layout = Screen._refresh_layout
        original_refresh = Widget.refresh

        def display(app: App[object], screen: object, renderable: object) -> None:
            if (
                isinstance(renderable, CompositorUpdate)
                and not app._batch_count
                and not app._closed
            ):
                with contextlib.suppress(Exception):  # measurement only
                    METER[0].bytes += len(renderable.render_segments(METER[0]._console))
                    METER[0].writes += 1
            return original_display(app, screen, renderable)  # type: ignore[arg-type]

        def layout(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            METER[0].layouts += 1
            return original_layout(self, *args, **kwargs)

        def refresh(widget, *args, **kwargs):  # type: ignore[no-untyped-def]
            if isinstance(widget, CardShell):
                METER[0].card_refreshes += 1
            return original_refresh(widget, *args, **kwargs)

        monkeypatch.setattr(App, "_display", display)
        monkeypatch.setattr(Screen, "_refresh_layout", layout)
        monkeypatch.setattr(Widget, "refresh", refresh)

    def reset(self) -> None:
        self.bytes = 0
        self.writes = 0
        self.layouts = 0
        self.card_refreshes = 0


METER: list[Meter] = []


@pytest.fixture
def meter(monkeypatch: pytest.MonkeyPatch) -> Iterator[Meter]:
    instance = Meter()
    instance.install(monkeypatch)
    METER.clear()
    METER.append(instance)
    yield instance
    METER.clear()


async def make_app(tmp_path: Path) -> SideboardApp:
    paths = default_paths(
        home=tmp_path,
        config_file=tmp_path / "config" / "config.toml",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    runtime = await build_runtime(paths=paths, demo=True)
    return SideboardApp(runtime)


async def settle(app: SideboardApp, pilot: object, *, freeze: bool = True) -> None:
    """Fill every card, then stop collecting.

    Freezing makes the measurement about the dashboard's own painting: a real data
    update is *supposed* to repaint the card it belongs to, and would otherwise be
    indistinguishable from a wasted repaint in these bounds.
    """
    for source_id in list(app.runtime.scheduler.runtimes):
        await app.runtime.scheduler.refresh_now(source_id, force=True)
    app._present_everything()
    await pilot.pause()  # type: ignore[attr-defined]
    await asyncio.sleep(1.5)
    if freeze:
        app.runtime.scheduler.suspend()


async def test_idle_dashboard_does_not_repaint(tmp_path: Path, meter: Meter) -> None:
    """An idle dashboard must not paint at all.

    Everything a card shows is either fixed (title, layout) or changes at a data
    cadence. A per-second subtitle, an unguarded border assignment or a forced chrome
    update each turned that into a continuous repaint.
    """
    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await settle(app, pilot)
        meter.reset()
        await asyncio.sleep(4.0)
        per_second = meter.bytes / 4.0
        assert meter.bytes < IDLE_BYTES_LIMIT, (
            f"idle dashboard wrote {meter.bytes:,} bytes in 4 s "
            f"({per_second / 1024:.1f} KiB/s); nothing on screen was changing"
        )
        assert meter.layouts <= 1, f"{meter.layouts} layout passes while idle"
    await app.runtime.database.aclose()


async def test_hover_repaints_only_the_affected_card(tmp_path: Path, meter: Meter) -> None:
    """Entering and leaving one card repaints that card, not the grid."""
    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await settle(app, pilot)
        meter.reset()
        app.on_card_shell_hold_changed(CardShell.HoldChanged("a", "hover", True))
        await asyncio.sleep(0.5)
        app.on_card_shell_hold_changed(CardShell.HoldChanged("a", "hover", False))
        await asyncio.sleep(0.5)
        # Each transition changes a CSS class and the border subtitle, and Textual calls
        # refresh for both; the compositor coalesces them into one paint, which is what
        # the byte count below verifies. Before the fix a single transition re-presented
        # all four slots and painted the whole grid.
        assert meter.card_refreshes <= 6, (
            f"{meter.card_refreshes} refresh calls for one hovered card"
        )
        assert meter.bytes < HOVER_PAIR_BYTES_LIMIT, (
            f"one hover enter/leave wrote {meter.bytes:,} bytes "
            f"(one full-width card is about 6 KiB)"
        )
        # …and the held indicator must still be visible: a fix that painted nothing
        # would pass the bound above while making hover invisible.
        assert meter.bytes > 0, "the hovered card must show that it is held"
        assert meter.layouts == 0, "a hold change must not change any geometry"
    await app.runtime.database.aclose()


async def test_a_pointer_sweep_is_bounded(tmp_path: Path, meter: Meter) -> None:
    """A fast sweep costs a bounded amount per transition, whatever the event rate."""
    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await settle(app, pilot)
        meter.reset()
        slots = ("a", "b", "c", "d")
        transitions = 20
        held = None
        for index in range(transitions):
            if held is not None:
                app.on_card_shell_hold_changed(CardShell.HoldChanged(held, "hover", False))
            held = slots[index % len(slots)]
            app.on_card_shell_hold_changed(CardShell.HoldChanged(held, "hover", True))
            await asyncio.sleep(0.05)
        per_transition = meter.bytes / transitions
        assert per_transition < SWEEP_BYTES_PER_TRANSITION_LIMIT, (
            f"{per_transition / 1024:.1f} KiB per transition "
            f"(a whole-screen repaint is about 5 KiB per card)"
        )
        assert meter.layouts == 0, "hovering must not trigger layout"
    await app.runtime.database.aclose()


async def test_changed_values_reach_the_screen(tmp_path: Path, meter: Meter) -> None:
    """The repaint gate must not swallow real updates.

    The gate compares a digest of the view, so a card whose values change with an
    unchanged item count still repaints — the reason a stale dashboard was possible.
    """
    from dataclasses import replace

    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await settle(app, pilot)
        card = app._card_for_widget("mac_summary")
        assert card is not None and card.view is not None and card.view.metrics
        meter.reset()
        metric = card.view.metrics[0]
        changed = replace(
            card.view, metrics=(replace(metric, value_text="99"), *card.view.metrics[1:])
        )
        card.set_card(
            widget_id=card.widget_id,
            title=card.card_title,
            view=changed,
            envelope=card.envelope,
            freshness=card.freshness,
        )
        await pilot.pause()
        assert meter.card_refreshes >= 1, "a changed value must repaint the card"
    await app.runtime.database.aclose()


async def test_switching_tabs_costs_one_pane(tmp_path: Path, meter: Meter) -> None:
    """A tab switch paints the pane that appears, not every tab's cards.

    Start-up used to present all six tabs, and each presentation assigned card heights,
    so a switch and a start-up both reflowed and repainted the whole screen several times.
    Presentation is now batched and limited to what is visible.
    """
    app = await make_app(tmp_path)
    async with app.run_test(size=(140, 44)) as pilot:
        await settle(app, pilot)
        meter.reset()
        await pilot.press("6")
        await pilot.pause()
        await asyncio.sleep(0.3)
        assert meter.bytes < SWITCH_BYTES_LIMIT, (
            f"one tab switch wrote {meter.bytes:,} bytes in {meter.writes} writes"
        )
        assert meter.writes <= SWITCH_WRITES_LIMIT, (
            f"one tab switch produced {meter.writes} terminal writes"
        )
    await app.runtime.database.aclose()


async def test_startup_does_not_animate(tmp_path: Path, meter: Meter) -> None:
    """Start-up must not spend its first third of a second animating.

    Textual's tab underline animates for 0.3 s at 60 fps, and every frame repainted the
    whole screen: nineteen full-screen paints before the dashboard was usable. The
    animation level has to be set after ``App.__init__``, which assigns it from the
    environment, so a class attribute alone silently does nothing.
    """
    app = await make_app(tmp_path)
    assert app.animation_level == "none", (
        f"animation level is {app.animation_level!r}: App.__init__ overwrote the class "
        f"attribute, so every Textual animation runs"
    )
    async with app.run_test(size=(140, 44)) as pilot:
        await asyncio.sleep(1.5)
        assert meter.writes <= 10, (
            f"{meter.writes} terminal writes in the first 1.5 s "
            f"(an animating tab underline produces about twenty)"
        )
        _ = pilot
    await app.runtime.database.aclose()
