"""Print the dashboard as plain text at a given size.

    uv run python tools/preview.py [columns] [rows] [tab]

Useful for checking what a card actually says — titles, columns, footnotes — without
reading escape sequences or taking a screenshot. It reads the same configuration and the
same cached data the application does, presents every widget, and prints the composited
screen: exactly the characters a terminal would show, with the styling dropped.

The text comes from Textual's own screenshot export rather than from ``Screen.render_line``,
which renders the screen widget alone and would miss every child card.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sideboard.app import SideboardApp, build_runtime  # noqa: E402
from sideboard.core.paths import default_paths  # noqa: E402

TEXT_ELEMENT = re.compile(r'<text[^>]*x="([\d.]+)"[^>]*y="([\d.]+)"[^>]*>(.*?)</text>', re.S)
ENTITIES = (
    ("&amp;", "&"),
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
    ("&#39;", "'"),
    ("&#160;", " "),
)


def screen_text(app: SideboardApp) -> str:
    """Plain text of the composited screen, in reading order.

    Rich's screenshot decorates the export with the application title and traffic-light
    circles above the screen. They are not rows of the terminal, so the title element is
    dropped: a preview that showed them would invent a line the user never sees.
    """
    svg = app.export_screenshot()
    svg = re.sub(r'<text class="[^"]*-title"[^>]*>.*?</text>', "", svg, flags=re.S)
    rows: dict[float, list[tuple[float, str]]] = {}
    for match in TEXT_ELEMENT.finditer(svg):
        x, y, body = float(match.group(1)), float(match.group(2)), match.group(3)
        body = re.sub(r"<[^>]+>", "", body)
        for entity, character in ENTITIES:
            body = body.replace(entity, character)
        rows.setdefault(y, []).append((x, body))
    lines = []
    for y in sorted(rows):
        line = "".join(body for _, body in sorted(rows[y]))
        lines.append(line.rstrip())
    return "\n".join(lines)


async def main() -> int:
    columns = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    rows = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    tab = sys.argv[3] if len(sys.argv) > 3 else None
    runtime = await build_runtime(paths=default_paths(), offline=False, demo=False)
    app = SideboardApp(runtime)
    async with app.run_test(size=(columns, rows)) as pilot:
        for source_id in list(app.runtime.scheduler.runtimes):
            await app.runtime.scheduler.refresh_now(source_id, force=True)
        app._present_everything()
        await pilot.pause()
        await asyncio.sleep(1.0)
        if tab:
            app._run_command(f"tab:{tab}")
            await pilot.pause()
            await asyncio.sleep(0.5)
        print(screen_text(app))
    await runtime.database.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
