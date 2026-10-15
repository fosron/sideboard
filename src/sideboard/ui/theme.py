"""Themes built from a palette.

Two themes come out of one palette, and both are needed:

* a Textual :class:`Theme`, whose variables the stylesheet resolves (``$sb-dim``,
  ``$panel``, ``$surface`` …);
* a Rich :class:`Theme`, pushed on the application console, which is what makes
  ``Text(style="sb-dim")`` resolve at *render* time. Rich cannot read Textual's CSS
  variables, so the same names are registered for it separately. That indirection is the
  reason a palette change recolours the card text as well as the borders.
"""

from __future__ import annotations

from rich.theme import Theme as RichTheme
from textual.theme import Theme

from .palettes import DEFAULT_PALETTE, Palette, canonical_name, resolve_palette

__all__ = [
    "SIDEBOARD_THEME",
    "THEME_NAME",
    "build_rich_theme",
    "build_theme",
    "theme_name",
]

#: The Rich style names the renderer layer uses. Kept in one place so a palette cannot
#: miss one without failing the completeness test.
RICH_TOKENS: tuple[str, ...] = (
    "sb-dim",
    "sb-value",
    "sb-ok",
    "sb-info",
    "sb-warn",
    "sb-error",
    "sb-item",
    "sb-read",
    "sb-border",
    "sb-border-focus",
    "sb-held",
)


def theme_name(key: str) -> str:
    """The Textual theme name for a palette key. Stable, so a reload can reuse it."""
    return canonical_name(key)


def build_theme(palette: Palette, *, key: str) -> Theme:
    """The Textual theme: chrome colours plus the ``sb-*`` CSS variables."""
    return Theme(
        name=theme_name(key),
        dark=True,
        primary=palette.primary,
        secondary=palette.secondary,
        accent=palette.accent,
        foreground=palette.foreground,
        background=palette.background,
        surface=palette.surface,
        panel=palette.panel,
        success=palette.ok,
        warning=palette.warn,
        error=palette.error,
        boost=palette.boost,
        luminosity_spread=0.12,
        text_alpha=0.92,
        variables={
            # One cell wide (the size is set in theme.tcss) and in palette colours: the
            # built-in default is a two-cell bright-blue bar that reads as a jagged stripe
            # on a dark dashboard.
            "scrollbar": palette.border_held,
            "scrollbar-hover": palette.border_focus,
            "scrollbar-active": palette.focus,
            "scrollbar-background": palette.surface,
            "scrollbar-background-hover": palette.panel,
            "scrollbar-background-active": palette.panel,
            "scrollbar-corner-color": palette.surface,
            "sb-dim": palette.dim,
            "sb-value": palette.value,
            "sb-ok": palette.ok,
            "sb-info": palette.info,
            "sb-warn": palette.warn,
            "sb-error": palette.error,
            "sb-item": palette.item,
            "sb-read": palette.read,
            "sb-border": palette.border,
            "sb-border-focus": palette.border_focus,
            "sb-held": palette.held,
            # sb-focus/sb-focus-bg are gone: focus is a marker plus a mid-tone border now,
            # and the bright colour survives only as the scrollbar's active tone.
            "sb-border-held": palette.border_held,
        },
    )


def build_rich_theme(palette: Palette) -> RichTheme:
    """The Rich theme for the card and renderer layer.

    ``sb-value`` is the tone for numbers that should stand out and ``sb-item`` the ordinary
    list-entry tone: a palette may make them differ, and the default one does.
    """
    return RichTheme(
        {
            "sb-dim": palette.dim,
            "sb-value": palette.value,
            "sb-ok": palette.ok,
            "sb-info": palette.info,
            "sb-warn": palette.warn,
            "sb-error": palette.error,
            "sb-item": palette.item,
            "sb-read": palette.read,
            "sb-border": palette.border,
            "sb-border-focus": palette.border_focus,
            "sb-held": palette.held,
        }
    )


#: Kept for the default case; the application builds from configuration instead.
THEME_NAME = theme_name(DEFAULT_PALETTE)
SIDEBOARD_THEME = build_theme(resolve_palette(DEFAULT_PALETTE), key=DEFAULT_PALETTE)
