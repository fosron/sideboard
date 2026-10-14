"""Measure terminal output deterministically, before and after the behaviour fixes.

Textual's headless driver receives the exact byte stream a terminal would receive, so
counting at its ``write`` boundary measures terminal cost without depending on a
pseudo-terminal. One scenario per process, because Textual's application context and the
scenario patches must not outlive a single run:

    uv run python tools/ui_cost.py            # runs every scenario and prints a table
    uv run python tools/ui_cost.py <name>     # one scenario, used by the table itself
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from rich.console import Console  # noqa: E402
from textual._compositor import CompositorUpdate  # noqa: E402
from textual.app import App  # noqa: E402
from textual.screen import Screen  # noqa: E402

from sideboard.app import SideboardApp, build_runtime  # noqa: E402
from sideboard.core.paths import default_paths  # noqa: E402
from sideboard.ui import card as card_module  # noqa: E402
from sideboard.ui.card import CardShell  # noqa: E402

SCENARIO_SECONDS = 5.0
SCENARIO_TIMEOUT = 90.0

METER = {"bytes": 0, "layouts": 0.0, "layout_ms": 0.0}

_ORIGINAL_DISPLAY = App._display
_ORIGINAL_LAYOUT = Screen._refresh_layout
_MEASURE_CONSOLE = Console(width=140)


def install_meters() -> None:
    """Count what a terminal would receive.

    A headless app never writes: ``App._display`` returns early when ``is_headless``.
    The bytes are therefore taken from the compositor update itself, through the same
    ``render_segments`` call the real display path uses, so the number is the terminal
    output that would be produced.
    """

    def counting_display(self, screen, renderable):  # type: ignore[no-untyped-def]
        """Count only the updates Textual would actually write.

        ``App._display`` is where a compositor update becomes terminal output: it skips
        the write while a batch is open or when the app is closing. Counting earlier than
        this over-reports work that never reaches a terminal.
        """
        if isinstance(renderable, CompositorUpdate) and not self._batch_count and not self._closed:
            with contextlib.suppress(Exception):
                METER["bytes"] += len(renderable.render_segments(_MEASURE_CONSOLE))
        return _ORIGINAL_DISPLAY(self, screen, renderable)

    def counting_layout(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        start = time.perf_counter()
        try:
            return _ORIGINAL_LAYOUT(self, *args, **kwargs)
        finally:
            METER["layouts"] += 1
            METER["layout_ms"] += (time.perf_counter() - start) * 1000

    App._display = counting_display  # type: ignore[method-assign]
    Screen._refresh_layout = counting_layout  # type: ignore[method-assign]


SCENARIOS: dict[str, dict[str, object]] = {
    "idle-current": {"hover_rate": 0.0},
    "idle-no-timers": {"hover_rate": 0.0, "no_timers": True},
    "hover-2s-current": {"hover_rate": 0.5},
    "hover-20s-current": {"hover_rate": 0.05},
    "idle-old": {"hover_rate": 0.0, "old": True},
    "hover-2s-old": {"hover_rate": 0.5, "old": True},
    "hover-20s-old": {"hover_rate": 0.05, "old": True},
}

LABELS = {
    "idle-current": "idle (current)",
    "idle-no-timers": "idle, timers disabled (control)",
    "hover-2s-current": "hover sweep 2/s (current)",
    "hover-20s-current": "hover sweep 20/s (current)",
    "idle-old": "idle (old behaviour)",
    "hover-2s-old": "hover sweep 2/s (old behaviour)",
    "hover-20s-old": "hover sweep 20/s (old behaviour)",
}


def emulate_old_behaviour() -> None:
    """The three behaviours that made the dashboard expensive."""
    original_freshness = card_module.freshness_line

    def with_countdown(*args, **kwargs):  # type: ignore[no-untyped-def]
        return f"{original_freshness(*args, **kwargs)} · {int(time.monotonic()) % 60}s"

    card_module.freshness_line = with_countdown

    original_handler = SideboardApp.on_card_shell_hold_changed

    def whole_grid(self, event):  # type: ignore[no-untyped-def]
        original_handler(self, event)
        self._present_overview()

    SideboardApp.on_card_shell_hold_changed = whole_grid  # type: ignore[method-assign]


def disable_timers() -> None:
    SideboardApp._on_tick = lambda self: None  # type: ignore[method-assign]
    SideboardApp._on_status_tick = lambda self: None  # type: ignore[method-assign]


async def scenario(name: str, *, seconds: float = SCENARIO_SECONDS) -> tuple[float, float, float]:
    options = SCENARIOS[name]
    hover_rate = float(options.get("hover_rate", 0.0))
    runtime = await build_runtime(paths=default_paths(), offline=False, demo=False)
    app = SideboardApp(runtime)
    if options.get("old"):
        emulate_old_behaviour()
        app.set_interval(1.0, lambda: app._update_chrome(force=True))
    if options.get("no_timers"):
        disable_timers()

    slots = ("a", "b", "c", "d")
    state = {"i": 0, "held": None}

    def drive() -> None:
        if state["held"] is not None:
            app.on_card_shell_hold_changed(CardShell.HoldChanged(state["held"], "hover", False))
            state["held"] = None
        slot = slots[state["i"] % 4]
        state["i"] += 1
        app.on_card_shell_hold_changed(CardShell.HoldChanged(slot, "hover", True))
        state["held"] = slot

    async with app.run_test(size=(140, 44)) as pilot:
        await asyncio.sleep(2.0)  # first paint and first data
        METER["bytes"] = 0
        METER["layouts"] = 0
        METER["layout_ms"] = 0.0
        started = time.perf_counter()
        while time.perf_counter() - started < seconds:
            if hover_rate:
                drive()
                await asyncio.sleep(hover_rate)
            else:
                await asyncio.sleep(0.05)
        elapsed = time.perf_counter() - started
        _ = pilot
    await runtime.database.aclose()
    cost = METER["layout_ms"] / max(1.0, METER["layouts"])
    return METER["bytes"] / elapsed / 1024, METER["layouts"] / elapsed, cost


def main() -> int:
    if len(sys.argv) > 1:
        name = sys.argv[1]
        install_meters()
        kib, layouts, cost = asyncio.run(scenario(name))
        print(f"{kib:.4f} {layouts:.4f} {cost:.4f}")
        return 0

    print(f"{'scenario':<36}{'terminal output':>16}{'layout passes':>15}{'ms/layout':>12}")
    for name in SCENARIOS:
        try:
            completed = subprocess.run(
                [sys.executable, str(Path(__file__)), name],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=SCENARIO_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print(
                f"{LABELS[name]:<36}{'did not finish':>16}  "
                f"(a {SCENARIO_SECONDS:.0f} s run did not complete in "
                f"{SCENARIO_TIMEOUT:.0f} s: the behaviour is that expensive)"
            )
            continue
        if completed.returncode != 0:
            print(
                f"{LABELS[name]:<36}{'failed':>16}  {completed.stderr.strip().splitlines()[-1][:40]}"
            )
            continue
        kib, layouts, cost = (float(value) for value in completed.stdout.split())
        print(f"{LABELS[name]:<36}{kib:>13.1f} KiB/s{layouts:>15.1f}{cost:>12.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
