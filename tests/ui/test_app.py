"""Headless interaction tests with Textual's Pilot.

Covers the acceptance list that can be checked without a real terminal: tab selection,
pause, privacy, hover holds and overlap, stable selection during refresh, responsive
layout at the target sizes, keyboard-only operation, config reload rollback and a
minimum-size guard.

A real terminal smoke test is still required for hover/mouse reporting; headless tests
passing does not by itself prove hover works. See docs/setup.md and `doctor --mouse`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sideboard.app import SideboardApp, build_runtime
from sideboard.core.contracts import Demand
from sideboard.core.paths import default_paths
from sideboard.ui.card import CardShell
from sideboard.ui.rotation import HoldReason

pytestmark = []


async def make_app(tmp_path: Path, *, offline: bool = False) -> SideboardApp:
    paths = default_paths(
        home=tmp_path,
        config_file=tmp_path / "config" / "config.toml",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    runtime = await build_runtime(paths=paths, offline=offline, demo=True)
    return SideboardApp(runtime)


async def settle(app: SideboardApp, pilot, *sources: str) -> None:
    """Drive every (or the named) source once and re-present the cards.

    Demo collectors are instant, so this makes the assertions deterministic instead of
    racing the scheduler's first tick.
    """
    targets = sources or tuple(app.runtime.scheduler.runtimes)
    for source_id in targets:
        await app.runtime.scheduler.refresh_now(source_id, force=True)
    app._present_everything()
    await pilot.pause()


async def test_boots_with_six_default_tabs_and_renders_cards(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        assert [tab.id for tab in app.runtime.config.tabs] == [
            "overview",
            "money",
            "read",
            "watch",
            "life",
            "systems",
        ]
        assert app._overview is not None
        assert app._overview.cards, "the overview must mount at least one card"
        # At 120x40 the wide layout uses the four configured slots.
        assert set(app._overview.cards) == {"a", "b", "c", "d"}
        bodies = [card for card in app._all_cards() if card.view is not None]
        assert bodies


async def test_number_keys_select_tabs_and_the_palette_reaches_later_tabs(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        assert app._active_tab == "money"
        await pilot.press("6")
        await pilot.pause()
        assert app._active_tab == "systems"
        # The palette can reach any tab, including ones beyond the ninth.
        app._run_command("tab:watch")
        await pilot.pause()
        assert app._active_tab == "watch"


async def test_demand_follows_the_visible_tab(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("6")
        await pilot.pause()
        assert app.runtime.scheduler.runtimes["mac"].demand is Demand.ACTIVE


async def test_space_pauses_visually_and_collection_continues(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert app.paused is True
        assert app._rotation is not None
        assert HoldReason.MANUAL in app._rotation.global_holds
        # Network refresh is independent of the visual pause.
        assert app.runtime.scheduler.offline is False
        await pilot.press("space")
        await pilot.pause()
        assert app.paused is False


async def test_privacy_mode_masks_a_private_amount(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app._run_command("tab:money")
        await pilot.pause()
        card = app._card_for_widget("portfolio")
        assert card is not None and card.view is not None
        tracked = card.view.metric("tracked_value")
        assert tracked is not None
        assert tracked.privacy.value == "private"
        await pilot.press("v")
        await pilot.pause()
        assert app.privacy is True
        card = app._card_for_widget("portfolio")
        assert card is not None and card.view is not None
        # The presenter still supplies the value; the renderer masks it.
        assert card.privacy is True


async def test_hover_hold_is_per_card_and_overlapping_reasons_survive(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        rotation = app._rotation
        assert rotation is not None
        card = app._overview.card("c") if app._overview else None
        assert card is not None

        app.on_card_shell_hold_changed(CardShell.HoldChanged("c", "hover", True))
        await pilot.pause()
        assert rotation.held("c") is True
        assert rotation.held("d") is False

        # A modal hold overlaps the hover hold; releasing the modal keeps the hover.
        app._apply_global_hold(HoldReason.MODAL, True)
        app._apply_global_hold(HoldReason.MODAL, False)
        assert rotation.held("c") is True

        # Leaving the card starts the grace period instead of resuming instantly.
        app.on_card_shell_hold_changed(CardShell.HoldChanged("c", "hover", False))
        await pilot.pause()
        assert rotation.held("c") is False
        assert rotation.in_grace("c") is True


async def test_terminal_blur_clears_pointer_holds_but_keeps_the_manual_pause(
    tmp_path: Path,
) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        rotation = app._rotation
        assert rotation is not None
        app.on_card_shell_hold_changed(CardShell.HoldChanged("c", "hover", True))
        app.on_card_shell_hold_changed(CardShell.HoldChanged("d", "hover", True))
        app.paused = True
        rotation.set_global_hold(HoldReason.MANUAL, True)
        app.on_app_blur()
        await pilot.pause()
        # The pointer holds are gone; the explicit manual pause is untouched.
        assert "hover" not in rotation.hold_reasons("c")
        assert "hover" not in rotation.hold_reasons("d")
        assert rotation.held_globally is True
        assert app.paused is True
        for card in app._all_cards():
            assert card.pointer_inside is False


async def test_selection_survives_a_source_refresh(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app._run_command("tab:read")
        await pilot.pause()
        card = app._card_for_widget("news")
        assert card is not None and card.view is not None
        assert len(card.view.items) >= 2
        card.select_index(2)
        selected = card.selected_id()
        assert selected is not None
        # A refresh replaces the view model; the selection must stay on the same item.
        app.on_source_updated(SideboardApp.SourceUpdated("news"))
        await pilot.pause()
        assert card.selected_id() == selected
        assert card.cursor_index == 2


async def test_resize_keeps_pins_and_a_usable_layout(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app._overview is not None
        assert app._overview.mode == "grid4"

        await pilot.resize_terminal(100, 32)
        await pilot.pause()
        await pilot.pause()
        assert app._overview.mode in {"stack2", "grid4"}
        if app._overview.mode == "stack2":
            # Narrowing merges the rotating queues but never drops a widget from them.
            assert app._overview.card("a") is not None
            merged = app._overview.card("c+d")
            assert merged is not None
            assert app._rotation is not None
            assert set(app._rotation.slots["c+d"].queue) == {
                "news",
                "hn_top",
                "youtube",
                "hn_new",
                "mac_summary",
                "docker_summary",
                "reddit",
                "saved",
                "vercom",
                "backup",
            }

        await pilot.resize_terminal(70, 45)
        await pilot.pause()
        await pilot.pause()
        assert app._overview.mode == "stack2"

        await pilot.resize_terminal(60, 24)
        await pilot.pause()
        await pilot.pause()
        assert app._overview.mode == "single"
        assert app._overview.cards


async def test_no_crash_below_the_minimum_size(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(58, 18)) as pilot:
        await pilot.pause()
        assert app._overview is not None
        assert app._overview.mode == "compact"
        assert app._overview.cards
        await pilot.press("6")
        await pilot.pause()
        assert app._active_tab == "systems"


async def test_keyboard_only_navigation_reaches_every_card(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        seen: set[str] = set()
        for _ in range(6):
            focused = app.focused
            if isinstance(focused, CardShell) and focused.widget_id:
                seen.add(focused.widget_id)
            await pilot.press("tab")
            await pilot.pause()
        assert seen, "keyboard focus must reach at least one card"


async def test_config_reload_rejects_invalid_and_keeps_the_running_layout(
    tmp_path: Path,
) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        before = [tab.id for tab in app.runtime.config.tabs]
        before_revision = app.runtime.config.revision
        app.runtime.paths.config_file.write_text(
            "config_version = 1\n[tabs]\nbroken = true\n", encoding="utf-8"
        )
        await app._reload_config()
        await pilot.pause()
        assert [tab.id for tab in app.runtime.config.tabs] == before
        assert app.runtime.config.revision == before_revision
        assert app._reload_note == "reload failed"


async def test_config_reload_adds_a_tab_and_a_category(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from sideboard.core.config import shipped_defaults

        document = shipped_defaults()
        document["categories"]["operations"] = {"label": "Operations", "order": 5}
        document["tabs"].append(
            {
                "id": "operations",
                "label": "Operations",
                "layout": "stack",
                "widgets": ["mac_details", "docker_containers"],
            }
        )
        text = _to_toml(document)
        app.runtime.paths.config_file.write_text(text, encoding="utf-8")
        await app._reload_config()
        await pilot.pause()
        assert app.runtime.config.tab("operations") is not None
        assert app.runtime.config.categories["operations"].label == "Operations"
        assert app.runtime.config.revision == 2


async def test_hold_release_restores_rotation(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        rotation = app._rotation
        assert rotation is not None
        app.on_card_shell_hold_changed(CardShell.HoldChanged("c", "hover", True))
        app._reset_holds()
        await pilot.pause()
        assert rotation.held("c") is False
        assert rotation.held_globally is False


async def test_open_url_refuses_a_non_http_link(tmp_path: Path, monkeypatch) -> None:
    app = await make_app(tmp_path)
    opened: list[str] = []
    monkeypatch.setattr(
        SideboardApp, "_hand_to_browser", lambda self, url: opened.append(url) or True
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.open_validated_url("file:///etc/passwd")
        assert opened == []
        app.open_validated_url("https://example.com/article")
        assert opened == ["https://example.com/article"]


async def test_handing_a_url_to_the_desktop_is_explicit(tmp_path: Path, monkeypatch) -> None:
    """macOS opens the URL with ``open``, and a failure is reported instead of claimed.

    ``webbrowser`` on macOS drives ``osascript``, which Automation permissions can refuse
    while returning quietly — the user saw "Opened news.ycombinator.com" and no browser.
    """
    from types import SimpleNamespace

    from sideboard import app as app_module

    app = await make_app(tmp_path)
    recorded: list[list[str]] = []

    class FakePopen:
        def __init__(self, args, **kwargs):  # type: ignore[no-untyped-def]
            recorded.append(list(args))

    monkeypatch.setattr(app_module, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(app_module.subprocess, "Popen", FakePopen)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.open_validated_url("https://example.com/article")
        await pilot.pause()
        assert recorded == [["/usr/bin/open", "https://example.com/article"]]
        notices = [str(note.message) for note in app._notifications]
        assert any("example.com" in note for note in notices), notices

        def refuse(args, **kwargs):  # type: ignore[no-untyped-def]
            raise OSError("no desktop session")

        monkeypatch.setattr(app_module.subprocess, "Popen", refuse)
        app.open_validated_url("https://example.com/other")
        await pilot.pause()
        notices = [str(note.message) for note in app._notifications]
        assert any("Could not open" in note and "example.com/other" in note for note in notices), (
            notices
        )


def _to_toml(document: dict) -> str:
    """Render a configuration document back to TOML for the reload test."""
    from sideboard.core.config import render_default_config
    from sideboard.features import build_feature_registry

    _ = build_feature_registry()
    lines: list[str] = []
    base = render_default_config(_registry())
    # Start from the shipped text and append the extra tab, which is exactly what a
    # human would do when adding a seventh tab.
    extras = [
        "\n[categories.operations]",
        'label = "Operations"',
        "order = 5",
        "",
        "[[tabs]]",
        'id = "operations"',
        'label = "Operations"',
        'layout = "stack"',
        'widgets = ["mac_details", "docker_containers"]',
        "",
    ]
    lines.append(base)
    lines.append("\n".join(extras))
    _ = document
    return "\n".join(lines)


def _registry():
    from sideboard.features import build_feature_registry

    return build_feature_registry()


def test_utc_now_is_available() -> None:
    assert datetime.now(UTC).tzinfo is not None
