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

    It used to read "as of 14:15 · updated 14:22 · fresh · partial" plus the hold reasons:
    two timestamps, a state that is the normal case, coverage, and interaction state, with
    no way to tell which one mattered.
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
    assert freshness_line(envelope, now=now, timezone="UTC") == "updated 14:22"

    stale = SourceEnvelope(
        source_id="news",
        payload_schema="news.feeds",
        payload_schema_version=1,
        connectivity=Connectivity.OK,
        freshness=Freshness.STALE,
        fetched_at=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
    )
    assert freshness_line(stale, now=now, timezone="UTC") == "updated 12:00 · stale"

    # No fetch time at all: the source's own date is the only timestamp there is.
    undated = SourceEnvelope(
        source_id="news",
        payload_schema="news.feeds",
        payload_schema_version=1,
        connectivity=Connectivity.OK,
        freshness=Freshness.FRESH,
        as_of=datetime(2026, 10, 14, 14, 15, tzinfo=UTC),
    )
    assert freshness_line(undated, now=now, timezone="UTC") == "as of 14:15"
    # A degraded connection is a problem, so it is stated.
    degraded = SourceEnvelope(
        source_id="news",
        payload_schema="news.feeds",
        payload_schema_version=1,
        connectivity=Connectivity.UNAVAILABLE,
        freshness=Freshness.STALE,
        fetched_at=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
        payload=object(),  # type: ignore[arg-type]
    )
    assert "unavailable" in freshness_line(degraded, now=now, timezone="UTC")


def _rendered_text(renderable: object) -> str:
    """Everything Rich would draw for a renderable, as plain text.

    The palette is registered on this console exactly as the application registers it:
    the renderers pass style *names*, and Rich resolves them against a theme.
    """
    from rich.console import Console

    from sideboard.ui.palettes import resolve_palette
    from sideboard.ui.theme import build_rich_theme

    console = Console(width=200, record=True, legacy_windows=False)
    console.push_theme(build_rich_theme(resolve_palette("sideboard")))
    return "".join(
        text
        for text, _style, _control in console.render(renderable)  # type: ignore[arg-type]
    )


#: Phrases that used to be printed somewhere in the dashboard and are not welcome there:
#: credits, methodology, coverage caveats, warm-up notes and interaction state. A card
#: shows values; a full view shows more values. Nothing explains itself.
BANNED_TEXT = (
    "model-derived",
    "open-meteo",
    "valid until",
    "share is a share",
    "price change is not",
    "deposits and withdrawals",
    "full articles open",
    "publisher excerpts",
    "rate baseline",
    "partial fetch",
    "no article for",
    "no forecast is invented",
    "reported market values",
    "rates are directed",
    "nothing is fetched",
    "cannot cancel or retry",
    "last successful acquisition",
    "all metrics reported by",
    "privileged helper",
    "saved links are kept locally",
    "synthetic demo data",
    "held:",
    "pinned ·",
    "as of 1",  # a second timestamp next to "updated"
    "queued or in progress",
    "not produced a payload yet",
    "waiting for the next successful refresh",
    "add a position manually",
    "run `sideboard init`",
    "relevance filtered",
    "generated ",
    "unavailable: ",
)


async def test_no_screen_prints_explanations_or_credits(tmp_path: Path) -> None:
    """Every tab, and a full view of two cards, say nothing but values.

    The dashboard is read at a glance from across a room: attribution, methodology notes,
    coverage caveats and collection warm-up lines are noise there, and they had collected
    all over the interface.
    """
    app = await make_app(tmp_path)
    async with app.run_test(size=(150, 44)) as pilot:
        await settle(app, pilot)
        screens: dict[str, str] = {"overview": screen_text(app)}
        for tab_id in ("money", "read", "dev", "watch", "life", "systems"):
            app._run_command(f"tab:{tab_id}")
            await pilot.pause()
            screens[tab_id] = screen_text(app)

        # …and the full view of two very different cards.
        for tab_id, widget_id in (("life", "weather"), ("money", "portfolio")):
            app._run_command(f"tab:{tab_id}")
            await pilot.pause()
            card = app._card_for_widget(widget_id)
            assert card is not None, f"{widget_id} is not on the {tab_id} tab"
            card.focus()
            await pilot.press("enter")
            await pilot.pause()
            screens[f"modal:{widget_id}"] = screen_text(app)
            await pilot.press("escape")
            await pilot.pause()

        for name, text in screens.items():
            lowered = text.lower()
            for phrase in BANNED_TEXT:
                assert phrase not in lowered, f"{name} still prints {phrase!r}"


def test_a_status_is_a_state_not_a_paragraph() -> None:
    """`detail` and `hints` are recorded for diagnostics and never drawn.

    They were printed under the headline, which put sentences like "No workflow is queued
    or in progress, and nothing failed recently." on a card that already said
    "running 0 · failed 0".
    """
    from sideboard.core.contracts import Severity, StatusView
    from sideboard.ui.renderers import render_status

    status = StatusView(
        severity=Severity.WARNING,
        headline="Source unreachable",
        detail="The last three attempts failed with a timeout after twenty seconds.",
        hints=("Check the network", "Run sideboard doctor"),
    )
    rendered = _rendered_text(render_status(status, width=80))
    assert "Source unreachable" in rendered
    assert "WARNING" in rendered
    assert "timeout" not in rendered, rendered
    assert "doctor" not in rendered, rendered


def test_hours_come_before_days_in_a_full_view() -> None:
    """The hourly list is above the daily table, as it already was on the card.

    Below it, a bare "09:00" read as though it belonged to the last date in the table, and
    the reader had to work out which section the times were in.
    """
    from sideboard.core.contracts import (
        Item,
        Metric,
        Privacy,
        TableColumn,
        TableRow,
        TableView,
        ViewModel,
    )
    from sideboard.ui.screens import render_view_body

    view = ViewModel(
        title="Weather",
        metrics=(Metric(id="temp", label="Temp", value_text="13.3", unit="°C"),),
        items=(
            Item(
                id="hour-1",
                title="18:00 · 13.3 °C",
                url="https://example.com/forecast",
                privacy=Privacy.PUBLIC,
            ),
        ),
        table=TableView(
            columns=(
                TableColumn(id="date", label="Date"),
                TableColumn(id="min", label="Min °C", align="right"),
            ),
            rows=(TableRow(id="2026-10-14", cells={"date": "Wed 14 Oct", "min": "10°"}),),
        ),
    )
    rendered = _rendered_text(render_view_body(view, width=100, privacy=False, selected_index=0))
    hours = rendered.index("18:00")
    days = rendered.index("Wed 14 Oct")
    assert hours < days, rendered
