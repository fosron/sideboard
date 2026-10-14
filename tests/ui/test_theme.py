"""Palette contracts: completeness, validation, and that a palette reaches the screen.

Two halves have to agree for a palette to work: Textual's CSS variables (borders, chrome)
and the Rich theme on the console (card text). A palette that only did one of them would
recolour half the interface, which is exactly the bug these tests exist to prevent.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path

import pytest

from sideboard.app import SideboardApp, build_runtime
from sideboard.core.config import ConfigError
from sideboard.core.paths import default_paths
from sideboard.ui.palettes import (
    DEFAULT_PALETTE,
    OVERRIDABLE,
    PALETTES,
    canonical_name,
    resolve_palette,
)
from sideboard.ui.theme import RICH_TOKENS, build_rich_theme, build_theme

HEX = "#0123456789abcdef"


def test_every_palette_defines_every_token_as_a_hex_colour() -> None:
    for key, palette in PALETTES.items():
        values = asdict(palette)
        for token in OVERRIDABLE:
            value = values[token]
            assert value.startswith("#") and len(value) == 7, f"{key}.{token} = {value!r}"
            assert all(char in HEX for char in value[1:].lower()), f"{key}.{token} = {value!r}"


def test_every_palette_has_readable_text_on_its_background() -> None:
    """A palette that puts text on its own background colour is unusable.

    Not a contrast ratio: the tokens are chosen by their authors. This only catches the
    mistake of copying the wrong field, which would make a card invisible.
    """

    def luminance(value: str) -> float:
        channels = [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        return sum(
            channel * weight for channel, weight in zip(channels, (0.21, 0.72, 0.07), strict=True)
        )

    for key, palette in PALETTES.items():
        background = luminance(palette.background)
        for token in ("foreground", "item", "dim", "ok", "warn", "error"):
            value = getattr(palette, token)
            assert value != palette.background, f"{key}.{token} equals the background"
            assert luminance(value) > background, (
                f"{key}.{token} ({value}) is darker than the background "
                f"({palette.background}); the text would be invisible"
            )


def test_both_halves_of_a_palette_are_built_from_it() -> None:
    palette = resolve_palette("dracula")
    theme = build_theme(palette, key="dracula")
    assert theme.name == "dracula"
    assert theme.variables["sb-dim"] == palette.dim
    assert theme.background == palette.background
    assert theme.panel == palette.panel
    rich = build_rich_theme(palette)
    for token in RICH_TOKENS:
        assert token in rich.styles, f"{token} must exist for Rich too"


def test_overrides_replace_single_tokens() -> None:
    palette = resolve_palette("nord", {"focus_bg": "#123456", "dim": "#ABCDEF"})
    assert palette.focus_bg == "#123456"
    assert palette.dim == "#abcdef", "colours are normalised"
    assert palette.background == PALETTES["nord"].background, "the rest is untouched"


def test_an_unknown_palette_or_token_is_an_error() -> None:
    with pytest.raises(ValueError) as info:
        resolve_palette("nope")
    assert "choose one of" in str(info.value)
    with pytest.raises(ValueError) as info:
        resolve_palette("nord", {"nope": "#ffffff"})
    assert "valid names are" in str(info.value)
    with pytest.raises(ValueError):
        resolve_palette("nord", {"dim": "cornflowerblue"})
    assert canonical_name("Tokyo-Night") == "tokyo-night", "names are case-insensitive"


def test_the_default_palette_is_the_documented_one() -> None:
    assert DEFAULT_PALETTE == "sideboard"
    assert resolve_palette("").label == PALETTES[DEFAULT_PALETTE].label


async def make_app(tmp_path: Path, *, theme: str = "sideboard", colours: dict | None = None):
    paths = default_paths(
        home=tmp_path,
        config_file=tmp_path / "config" / "config.toml",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    runtime = await build_runtime(paths=paths, demo=True)
    if theme != "sideboard" or colours:
        from dataclasses import replace

        runtime.config = replace(
            runtime.config,
            app=runtime.config.app.model_copy(
                update={"theme": theme, "theme_colors": colours or {}}
            ),
        )
    return SideboardApp(runtime)


async def test_the_configured_palette_reaches_css_and_card_text(tmp_path: Path) -> None:
    """The chrome *and* the card text follow the palette."""
    app = await make_app(tmp_path, theme="dracula")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.theme == "dracula"
        theme = app.get_theme("dracula")
        assert theme is not None and theme.variables["sb-dim"] == "#6272a4"
        # The Rich half: this is what the card renderers resolve at paint time.
        style = app.console.get_style("sb-dim", default=None)
        assert style is not None and style.color is not None
        assert style.color.get_truecolor().hex.lower() == PALETTES["dracula"].dim
        # …and it reaches the pixels: the palette background is in the painted output.
        assert PALETTES["dracula"].background in app.export_screenshot().lower()
    await app.runtime.database.aclose()


async def test_the_default_palette_keeps_the_original_colours(tmp_path: Path) -> None:
    """Nothing changes for anyone who does not choose a palette."""
    app = await make_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for token, expected in (("sb-dim", "#7e8a95"), ("sb-value", "#e2e7ec")):
            style = app.console.get_style(token, default=None)
            assert style is not None and style.color is not None
            assert style.color.get_truecolor().hex.lower() == expected
    await app.runtime.database.aclose()


def test_config_validation_rejects_an_unknown_palette(tmp_path: Path) -> None:
    """A typo in a palette name is a configuration error, not a silent default."""
    paths = default_paths(
        home=tmp_path,
        config_file=tmp_path / "config" / "config.toml",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)

    async def write_and_build(app_section: str):
        paths.config_file.write_text(
            f"config_version = 1\n[app]\n{app_section}\n", encoding="utf-8"
        )
        return await build_runtime(paths=paths, demo=True)

    with pytest.raises(ConfigError) as info:
        asyncio.run(write_and_build('theme = "no-such-palette"'))
    assert "unknown theme" in " ".join(info.value.problems)

    with pytest.raises(ConfigError) as info:
        asyncio.run(write_and_build('theme = "nord"\ntheme_colors = { nope = "#ffffff" }'))
    assert "unknown theme colour" in " ".join(info.value.problems)

    runtime = asyncio.run(write_and_build('theme = "gruvbox"'))
    assert runtime.config.app.theme == "gruvbox"


async def test_a_palette_change_on_reload_recolours_without_a_restart(tmp_path: Path) -> None:
    """Switching palette is a configuration change, like a tab or a dwell time.

    The theme name can stay the same while its colours change (overrides), which the
    reactive would not notice, so the reload re-parses the stylesheet either way.
    """
    app = await make_app(tmp_path, theme="nord")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.theme == "nord"
        nord = app.console.get_style("sb-dim", default=None)
        assert nord is not None and nord.color is not None
        assert nord.color.get_truecolor().hex.lower() == PALETTES["nord"].dim

        # A reload that changes only the overrides, keeping the palette name.
        from dataclasses import replace

        candidate = replace(
            app.runtime.config,
            app=app.runtime.config.app.model_copy(
                update={"theme": "nord", "theme_colors": {"dim": "#ff00ff"}}
            ),
        )
        await app._apply_config(candidate)
        await pilot.pause()
        changed = app.console.get_style("sb-dim", default=None)
        assert changed is not None and changed.color is not None
        assert changed.color.get_truecolor().hex.lower() == "#ff00ff"

        # …and switching to another palette keeps working after that.
        candidate = replace(
            app.runtime.config,
            app=app.runtime.config.app.model_copy(update={"theme": "gruvbox", "theme_colors": {}}),
        )
        await app._apply_config(candidate)
        await pilot.pause()
        assert app.theme == "gruvbox"
        gruvbox = app.console.get_style("sb-dim", default=None)
        assert gruvbox is not None and gruvbox.color is not None
        assert gruvbox.color.get_truecolor().hex.lower() == PALETTES["gruvbox"].dim
    await app.runtime.database.aclose()


async def test_t_cycles_palettes_for_the_session_only(tmp_path: Path) -> None:
    """Switching in the app must not rewrite the configuration.

    The palette is a session choice until the user decides otherwise, so the file they
    maintain by hand stays exactly as they wrote it.
    """
    app = await make_app(tmp_path, theme="sideboard")
    config_file = app.runtime.paths.config_file
    before = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        first = app.theme
        await pilot.press("t")
        await pilot.pause()
        second = app.theme
        assert second != first, "t must switch palette"
        assert app._session_theme == second
        style = app.console.get_style("sb-dim", default=None)
        assert style is not None and style.color is not None
        assert style.color.get_truecolor().hex.lower() == PALETTES[second].dim

        # Wrapping: one full cycle of presses returns to where it started.
        from sideboard.ui.palettes import palette_names

        for _ in range(len(palette_names()) - 1):
            await pilot.press("t")
            await pilot.pause()
        assert app._session_theme == "sideboard", "the cycle wraps around the list"

        after = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
        assert after == before, "a session switch must not touch the configuration file"
    await app.runtime.database.aclose()


async def test_ctrl_t_opens_a_picker_that_applies_a_palette(tmp_path: Path) -> None:
    from textual.widgets import OptionList

    from sideboard.ui.screens import ThemePickerScreen

    app = await make_app(tmp_path, theme="sideboard")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ThemePickerScreen)
        options = screen.query_one(OptionList)
        assert options.option_count == len(PALETTES)
        # Choose "dracula" the way the widget would.
        index = [str(option.id) for option in options.options].index("dracula")
        options.highlighted = index
        options.action_select()
        await pilot.pause()
        assert app.theme == "dracula"
        assert app._session_theme == "dracula"
        style = app.console.get_style("sb-value", default=None)
        assert style is not None and style.color is not None
        assert style.color.get_truecolor().hex.lower() == PALETTES["dracula"].value
    await app.runtime.database.aclose()


async def test_the_command_palette_can_switch_palettes_too(tmp_path: Path) -> None:
    app = await make_app(tmp_path, theme="sideboard")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        commands = dict(app._palette_commands())
        assert "action:theme_picker" in commands and "action:next_theme" in commands
        app._run_command("theme:gruvbox")
        await pilot.pause()
        assert app.theme == "gruvbox"
        # An unknown palette is reported, not applied.
        app._run_command("theme:no-such-palette")
        await pilot.pause()
        assert app.theme == "gruvbox"
    await app.runtime.database.aclose()
