"""Rendered-screen tests.

These are the tests whose absence let two real display bugs ship:

* the tab body chain had no height, so every card was laid out at height 0 and the user
  saw only tab titles, border titles and the footer shortcut line;
* two Rich style *names* survived a refactor and raised ``MissingStyle`` during the
  layout pass, which aborted rendering.

Both bugs were invisible to tests that only inspected ``card.view`` (the data) instead
of the pixels. Everything here therefore asserts what is actually on the screen.
"""

from __future__ import annotations

import re
from pathlib import Path

from sideboard.app import SideboardApp, build_runtime
from sideboard.core.paths import default_paths

pytestmark = []

TARGET_SIZES = ((120, 40), (100, 32), (70, 45), (60, 24))

TEXT_ELEMENT = re.compile(r'<text[^>]*\sx="([^"]+)"[^>]*\sy="([^"]+)"[^>]*>(.*?)</text>', re.S)
ENTITIES = (
    ("&#160;", " "),
    ("&amp;", "&"),
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
    ("&#39;", "'"),
)


def screen_text(app: object) -> str:
    """Plain text of the composited screen, read back through Textual's own SVG export.

    ``Screen.render_line`` only renders the screen widget itself, not its children, so
    the screenshot export is the honest way to assert what a user would see.
    """
    svg = app.export_screenshot()  # type: ignore[attr-defined]
    rows: dict[float, list[tuple[float, str]]] = {}
    for match in TEXT_ELEMENT.finditer(svg):
        x, y, body = float(match.group(1)), float(match.group(2)), match.group(3)
        body = re.sub(r"<[^>]+>", "", body)
        for entity, character in ENTITIES:
            body = body.replace(entity, character)
        rows.setdefault(y, []).append((x, body))
    return "\n".join(
        "".join(piece for _, piece in sorted(pieces)) for _, pieces in sorted(rows.items())
    )


async def make_app(tmp_path: Path, *, demo: bool = True) -> SideboardApp:
    paths = default_paths(
        home=tmp_path,
        config_file=tmp_path / "config" / "config.toml",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    runtime = await build_runtime(paths=paths, demo=demo)
    return SideboardApp(runtime)


async def settle(app: SideboardApp, pilot: object) -> None:
    for source_id in list(app.runtime.scheduler.runtimes):
        await app.runtime.scheduler.refresh_now(source_id, force=True)
    app._present_everything()
    await pilot.pause()  # type: ignore[attr-defined]


async def test_overview_shows_real_card_content(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        text = screen_text(app)

        # The chrome that used to be everything the user could see.
        assert "Sideboard ·" in text
        assert "Overview" in text and "Systems" in text
        # The footer is one hint, not a keyboard reference: `?` opens the list.
        assert "? help" in text, "the footer must say where the keys are"
        assert "Space pause" not in text, "keys belong in the help screen, not the footer"

        # …and the card content that was missing.
        assert "Demo Equity" in text, "the portfolio table must render its rows"
        assert "DEMO: Klaipėda opens a new pier section" in text, "the digest must render items"
        assert "partly cloudy" in text, "the weather card must render its metrics"
        assert "RAM" in text and "CPU" in text, "the Mac card must render its metrics"
        assert "Tracked" not in text

        # A card that never got data would say so; nothing should be in a loading state.
        assert "loading…" not in text
        assert "Widget error" not in text
        # Demo data must be labelled as such, and must not claim to be stale.
        assert "DEMO DATA" in text
        assert "expired" not in text


async def test_every_card_gets_a_positive_height_at_every_target_size(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=TARGET_SIZES[0]) as pilot:
        await settle(app, pilot)
        for width, height in TARGET_SIZES:
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            await pilot.pause()
            assert app._overview is not None
            for slot_id, card in app._overview.cards.items():
                content = card.content_size
                assert content.height > 0, (
                    f"{width}x{height}: slot {slot_id} has no content height "
                    f"(size={card.size}); the tab body chain probably lost its height"
                )
                assert content.width > 0, f"{width}x{height}: slot {slot_id} has no width"


async def test_detail_tabs_render_content(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        for tab_id, expected in (
            ("money", "Demo Equity"),
            ("read", "DEMO:"),
            ("life", "partly cloudy"),
            ("systems", "Uptime"),
        ):
            app._run_command(f"tab:{tab_id}")
            await pilot.pause()
            text = screen_text(app)
            assert expected in text, f"the {tab_id} tab rendered nothing recognisable"
            # Every card the tab owns must be laid out with usable space, not only the
            # first one: the read tab used to give its whole height to the filter bar.
            pane = app._detail_panes.get(tab_id)
            assert pane is not None
            for widget_id in pane.widget_ids:
                card = pane.card(widget_id)
                assert card is not None
                assert card.size.height >= 3, f"{tab_id}: {widget_id} has no height"
                assert card.content_size.height >= 1, f"{tab_id}: {widget_id} has no content row"


async def test_a_tab_with_several_widgets_scrolls_to_reach_them_all(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app._run_command("tab:systems")
        await pilot.pause()
        pane = app._detail_panes["systems"]
        stack = pane.query_one("#sb-detail-stack")
        assert stack.scroll_offset.y == 0
        first = screen_text(app)
        assert "Disk free" in first  # the first card is on screen

        for _ in range(4):
            await pilot.press("pagedown")
            await pilot.pause()
        assert stack.scroll_offset.y > 0, "a tab taller than the viewport must scroll"
        last = screen_text(app)
        assert "active tab" in last, "the last card must be reachable by scrolling"


async def test_compact_size_still_renders_something_useful(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(58, 18)) as pilot:
        await settle(app, pilot)
        text = screen_text(app)
        assert app._overview is not None
        assert app._overview.mode == "compact"
        # The compact card is a one-liner, not an empty box.
        assert any(card.content_size.height > 0 for card in app._overview.cards.values())
        assert text.strip(), "the compact layout must still draw the chrome"


def test_no_rich_style_name_strings_remain_in_the_ui() -> None:
    """Rich style *names* are only defined in the Textual theme, not in Rich.

    Passing ``"sb-value"`` to a Rich renderable raises ``MissingStyle`` at layout time.
    Only ``ui/styles.py`` may spell these names, and only to publish the Style objects.
    """
    offenders: list[str] = []
    root = Path(__file__).resolve().parents[2] / "src" / "sideboard"
    pattern = re.compile(
        r'"sb-(?:dim|value|ok|info|warn|error|item|read|border|border-focus|held)"'
    )
    for path in root.rglob("*.py"):
        if path.name in {"styles.py", "theme.py"}:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line) and "set_class" not in line and "classes=" not in line:
                offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    assert not offenders, "Rich style names used as strings:\n" + "\n".join(offenders)


def test_a_card_line_says_one_thing_about_time() -> None:
    """The card line answers "when was this updated", and nothing else.

    It used to read "as of 14:15 · updated 14:22 · fresh · partial": two timestamps, a
    state that is the normal case, and a coverage word, with no way to tell which one
    mattered. The detail view keeps all of it (verbose=True).
    """
    from datetime import UTC, datetime

    from sideboard.core.contracts import Connectivity, Coverage, Freshness, SourceEnvelope
    from sideboard.ui.card import freshness_line

    now = datetime(2026, 10, 14, 14, 30, tzinfo=UTC)
    envelope = SourceEnvelope(
        source_id="news",
        payload_schema="news.feeds",
        payload_schema_version=1,
        connectivity=Connectivity.OK,
        freshness=Freshness.FRESH,
        as_of=datetime(2026, 10, 14, 14, 15, tzinfo=UTC),
        fetched_at=datetime(2026, 10, 14, 14, 22, tzinfo=UTC),
        coverage=Coverage(observed=3, expected=8),
    )
    compact = freshness_line(
        envelope, now=now, timezone="UTC", held_reasons=("hover", "interaction"), pinned=True
    )
    assert compact == "updated 14:22"
    verbose = freshness_line(
        envelope, now=now, timezone="UTC", held_reasons=("hover",), pinned=True, verbose=True
    )
    assert "as of 14:15" in verbose and "updated 14:22" in verbose
    assert "fresh" in verbose and "partial" in verbose and "held:hover" in verbose

    stale = SourceEnvelope(
        source_id="news",
        payload_schema="news.feeds",
        payload_schema_version=1,
        connectivity=Connectivity.OK,
        freshness=Freshness.STALE,
        fetched_at=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
    )
    assert freshness_line(stale, now=now, timezone="UTC") == "updated 12:00 · stale"


def test_focus_held_and_pinned_are_three_different_looks() -> None:
    """A card's state must be readable at a glance.

    Focus, "the pointer is over this" and "this is pinned" all drew the same amber border,
    so walking the grid with Tab left every visited card looking like the focused one. The
    three states now use three different colours, and pinned is marked by the diamond in
    the title instead of by a border colour.
    """
    from sideboard.ui.theme import SIDEBOARD_THEME

    variables = SIDEBOARD_THEME.variables
    focus = variables["sb-focus"]
    held = variables["sb-border-held"]
    plain = variables["sb-border"]
    assert len({focus, held, plain}) == 3, "focus, held and default must not share a colour"
    css = (Path(__file__).resolve().parents[2] / "src/sideboard/ui/theme.tcss").read_text()
    assert "CardShell:focus" in css and "background: $sb-focus-bg" in css
    pinned = css[css.index("CardShell.sb-pinned") :].split("}")[0]
    assert "sb-border;" in pinned, "a pinned card keeps the plain border"


async def test_help_lists_every_key_the_dashboard_has(tmp_path: Path) -> None:
    """`?` is the only place the keys are listed, so it has to be complete."""
    from sideboard.ui.screens import HelpScreen

    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        help_text = screen_text(app)
        for keys in ("Arrows", "Tab / Shift+Tab", "Enter", "Space", "Ctrl+P", "Ctrl+R", "q"):
            assert keys in help_text, f"the help screen must mention {keys!r}"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)
    await app.runtime.database.aclose()


def test_a_list_row_never_wraps() -> None:
    """One item is one row: the title is truncated so the stats fit beside it.

    The HN card rendered "Amazon pushes up prices across the Internet  pts 88 · c 56 ·
    age 2h" at a card width of 76 and the stats wrapped onto a second line, so half the
    rows read as a headline followed by a stray "age 2h".
    """
    from sideboard.core.contracts import Item, Privacy
    from sideboard.ui.renderers import render_items

    items = tuple(
        Item(
            id=f"item:{index}",
            title="Iosevka is an open-source, sans-serif and slab-serif, monospaced font",
            meta=(("pts", "220"), ("c", "77"), ("age", "3h")),
            privacy=Privacy.PUBLIC,
        )
        for index in range(4)
    )
    for width in (40, 60, 76, 100):
        group = render_items(items, width=width, privacy=False, selected_index=0)
        for line in group.renderables:  # type: ignore[attr-defined]
            assert len(line.plain) <= width, f"{width} columns: {line.plain!r} overflows"

    badged = (
        Item(
            id="item:badge",
            title="A headline that is deliberately much longer than the card is wide",
            badge="world",
            meta=(("cat", "personal finance"),),
            privacy=Privacy.PUBLIC,
        ),
    )
    group = render_items(badged, width=60, privacy=False)
    for line in group.renderables:  # type: ignore[attr-defined]
        assert len(line.plain) <= 60, line.plain
