"""Profile a real run: terminal output, responsiveness, CPU, and source health.

    uv run python tools/profile_run.py [seconds]

Runs the application exactly as ``uv run sideboard`` does — real terminal driver, the
configured data sources, the user's own configuration — and reports what the run actually
cost. Use it when the dashboard *feels* slow but nothing obvious is wrong: it separates
"the app floods the terminal" from "the app blocks its event loop" from "a source is
retrying forever", which need different fixes.

Output goes to stdout at the end of the run, so it can be pasted into an issue. The
report is written from the ``finally`` block rather than from a timer, because a timer
inside a busy app is exactly what might not run.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console
from textual._compositor import CompositorUpdate
from textual.app import App

from sideboard.app import SideboardApp, build_runtime
from sideboard.core.paths import default_paths

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
PROBE_INTERVAL = 0.02

_MEASURE_CONSOLE = Console(width=140)
_ORIGINAL_DISPLAY = App._display

WRITES: list[tuple[float, int]] = []


def install_meter() -> None:
    """Record every update Textual would write, with the time it was written."""

    def counting_display(self: App[object], screen: object, renderable: object) -> None:
        if isinstance(renderable, CompositorUpdate) and not self._batch_count and not self._closed:
            with contextlib.suppress(Exception):
                WRITES.append(
                    (time.perf_counter(), len(renderable.render_segments(_MEASURE_CONSOLE)))
                )
        return _ORIGINAL_DISPLAY(self, screen, renderable)  # type: ignore[arg-type]

    App._display = counting_display  # type: ignore[method-assign]


async def main() -> int:
    install_meter()
    runtime = await build_runtime(paths=default_paths(), offline=False, demo=False)
    app = SideboardApp(runtime)

    stalls: list[float] = []
    cpu_start = time.process_time()
    statuses: list[object] = []

    async def probe() -> None:
        """A task that should wake every 20 ms; lateness is UI latency."""
        while True:
            started = time.perf_counter()
            await asyncio.sleep(PROBE_INTERVAL)
            stalls.append((time.perf_counter() - started - PROBE_INTERVAL) * 1000)

    async def sample_statuses() -> None:
        """Keep the last status snapshot: the scheduler is closed by the time we report."""
        while True:
            statuses.clear()
            statuses.extend(runtime.scheduler.runtime_status().sources)
            await asyncio.sleep(1.0)

    async def stop_after(seconds: float) -> None:
        await asyncio.sleep(seconds)
        app.exit()

    probe_task = asyncio.create_task(probe())
    status_task = asyncio.create_task(sample_statuses())
    stop_task = asyncio.create_task(stop_after(SECONDS))
    started = time.perf_counter()
    try:
        await app.run_async()
    finally:
        probe_task.cancel()
        status_task.cancel()
        stop_task.cancel()
        await runtime.database.aclose()
    elapsed = max(0.001, time.perf_counter() - started)
    cpu = time.process_time() - cpu_start

    print()
    print(f"sideboard profile — {elapsed:.1f} s of a real run, {app.size.width}x{app.size.height}")
    print()
    if not WRITES:
        print("terminal: no writes at all (the app never painted)")
    else:
        first = WRITES[0]
        # The first paint is everything written before the first gap longer than a second:
        # after that the screen is up and later writes are data refreshes.
        gap_index = next(
            (index for index, (when, _) in enumerate(WRITES[1:], start=1) if when - first[0] > 1.0),
            len(WRITES),
        )
        first_paint = sum(size for _, size in WRITES[:gap_index])
        total = sum(size for _, size in WRITES)
        largest = max(size for _, size in WRITES)
        quiet_tail = [size for when, size in WRITES if when > started + elapsed - 10.0]
        print(f"terminal: {len(WRITES)} writes, {total / 1024:.0f} KiB total")
        print(f"  first paint (first {gap_index} writes): {first_paint / 1024:.0f} KiB")
        print(f"  largest single write: {largest / 1024:.0f} KiB")
        print(
            f"  last 10 s: {sum(quiet_tail) / 1024 / min(10.0, elapsed):.1f} KiB/s "
            f"over {len(quiet_tail)} writes"
        )
    ordered = sorted(stalls)
    if ordered:
        median = ordered[len(ordered) // 2]
        p95 = ordered[int(len(ordered) * 0.95)] if len(ordered) > 20 else ordered[-1]
        print(
            f"event loop: {len(stalls)} probes, median stall {median:.1f} ms, "
            f"p95 {p95:.1f} ms, worst {ordered[-1]:.1f} ms"
        )
    print(f"cpu: {cpu:.1f} s over {elapsed:.1f} s ({100 * cpu / elapsed:.0f}% of one core)")
    print()
    print("sources:")
    for status in sorted(statuses, key=lambda item: item.source_id):  # type: ignore[attr-defined]
        state = "off" if not status.enabled else str(status.connectivity)
        flags = []
        if status.in_flight:
            flags.append("in-flight")
        if status.failures:
            flags.append(f"{status.failures} failure(s)")
        if status.partial:
            flags.append("partial")
        if status.error_category:
            flags.append(str(status.error_category))
        print(f"  {status.source_id:<18}{state:<14}{status.freshness!s:<9}{' '.join(flags) or '-'}")
    print()
    print("If the numbers look calm but the screen still feels slow, the delay is in the")
    print("terminal or the link to it: try --no-mouse, a smaller window, and a local")
    print("terminal on the Mac to compare.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
