"""Colour palettes for the dashboard.

One palette is a complete set of the semantic colours the interface uses: the surfaces,
the text tones and the status colours. The shipped default is ``sideboard`` (the colours
this dashboard has always used); the rest are well-known community palettes, mapped onto
the same names so nothing else in the code has to know which one is active.

Mapping rules, so a palette reads the way its author intended:

* ``background`` is the darkest surface, ``surface`` the card body, ``panel`` the chrome
  bars and ``boost`` one step above the panel (hover, selection);
* ``dim`` is the comment/muted tone, ``item`` the ordinary text, ``value`` the tone for
  numbers that should stand out and ``read`` the tone for already-read entries;
* ``border`` is the quiet card outline, ``border_held`` the pointer-held outline,
  ``focus`` the keyboard-focus outline and ``focus_bg`` the lifted card background that
  makes the focused card unmistakable;
* ``ok`` / ``info`` / ``warn`` / ``error`` are the status colours.

Every palette must define every field: a missing token would surface as a Rich
``MissingStyle`` error in the middle of a repaint, so the dataclass has no defaults and a
test checks each palette for completeness.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields

__all__ = [
    "DEFAULT_PALETTE",
    "OVERRIDABLE",
    "PALETTES",
    "Palette",
    "canonical_name",
    "palette_names",
    "resolve_palette",
    "validate_colour",
]

DEFAULT_PALETTE = "sideboard"

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True, slots=True)
class Palette:
    """A complete set of semantic colours. Field names are the override vocabulary."""

    label: str
    background: str
    surface: str
    panel: str
    boost: str
    foreground: str
    #: The tone for values that should stand out from ordinary item text (a price, a
    #: temperature). Equal to ``foreground`` unless a palette wants them to differ.
    value: str
    accent: str
    primary: str
    secondary: str
    ok: str
    info: str
    warn: str
    error: str
    dim: str
    item: str
    read: str
    border: str
    border_focus: str
    focus: str
    focus_bg: str
    held: str
    border_held: str

    def with_overrides(self, overrides: dict[str, str]) -> Palette:
        """A copy with individual colours replaced. Keys are field names."""
        return Palette(**{**asdict(self), **overrides})


#: Field names a configuration may override, in the order they are documented.
OVERRIDABLE: tuple[str, ...] = tuple(
    field.name for field in fields(Palette) if field.name != "label"
)


def validate_colour(value: str) -> str:
    """Accept ``#rrggbb`` only: a named or malformed colour would fail at repaint time."""
    text = value.strip()
    if not _HEX.match(text):
        raise ValueError(f"colour must be #rrggbb, not {value!r}")
    return text.lower()


def palette_names() -> tuple[str, ...]:
    return tuple(sorted(PALETTES))


def canonical_name(name: str) -> str:
    """The palette key for a configured name, or an error naming the alternatives."""
    key = (name or DEFAULT_PALETTE).strip().lower()
    if key not in PALETTES:
        raise ValueError(f"unknown theme {name!r}; choose one of: {', '.join(palette_names())}")
    return key


def resolve_palette(name: str, overrides: dict[str, str] | None = None) -> Palette:
    """The named palette, with optional per-token overrides applied.

    An unknown name is an error rather than a silent fallback: a typo in a colour choice
    should not quietly render the default theme.
    """
    palette = PALETTES[canonical_name(name)]
    if not overrides:
        return palette
    unknown = sorted(set(overrides) - set(OVERRIDABLE))
    if unknown:
        raise ValueError(
            f"unknown theme colour(s): {', '.join(unknown)}; "
            f"valid names are: {', '.join(OVERRIDABLE)}"
        )
    cleaned = {key_name: validate_colour(value) for key_name, value in overrides.items()}
    return palette.with_overrides(cleaned)


PALETTES: dict[str, Palette] = {
    # The dashboard's own restrained dark palette: thin borders, low chroma.
    "sideboard": Palette(
        label="Sideboard",
        background="#12161a",
        surface="#171c21",
        panel="#1d2329",
        boost="#20272e",
        foreground="#cfd6dd",
        value="#e2e7ec",
        accent="#a8b6c4",
        primary="#7aa2c8",
        secondary="#6d7f8f",
        ok="#7fae7a",
        info="#8fa8bd",
        warn="#d8b26a",
        error="#cf7b7b",
        dim="#7e8a95",
        item="#cfd6dd",
        read="#6f7a84",
        border="#2a323a",
        border_focus="#7aa2c8",
        focus="#a9cdf2",
        focus_bg="#1e2833",
        held="#d8b26a",
        border_held="#4c5866",
    ),
    # https://catppuccin.com/palette — Mocha.
    "catppuccin-mocha": Palette(
        label="Catppuccin Mocha",
        background="#11111b",
        surface="#181825",
        panel="#1e1e2e",
        boost="#313244",
        foreground="#cdd6f4",
        value="#cdd6f4",
        accent="#a6adc8",
        primary="#89b4fa",
        secondary="#7f849c",
        ok="#a6e3a1",
        info="#74c7ec",
        warn="#f9e2af",
        error="#f38ba8",
        dim="#6c7086",
        item="#bac2de",
        read="#6c7086",
        border="#45475a",
        border_focus="#89b4fa",
        focus="#89dceb",
        focus_bg="#313244",
        held="#fab387",
        border_held="#6c7086",
    ),
    # https://github.com/folke/tokyonight.nvim — Night.
    "tokyo-night": Palette(
        label="Tokyo Night",
        background="#16161e",
        surface="#1a1b26",
        panel="#1f2335",
        boost="#292e42",
        foreground="#c0caf5",
        value="#c0caf5",
        accent="#a9b1d6",
        primary="#7aa2f7",
        secondary="#565f89",
        ok="#9ece6a",
        info="#7dcfff",
        warn="#e0af68",
        error="#f7768e",
        dim="#565f89",
        item="#c0caf5",
        read="#565f89",
        border="#292e42",
        border_focus="#7aa2f7",
        focus="#7dcfff",
        focus_bg="#24283b",
        held="#ff9e64",
        border_held="#414868",
    ),
    # https://www.nordtheme.com — Polar Night / Snow Storm / Frost / Aurora.
    "nord": Palette(
        label="Nord",
        background="#2e3440",
        surface="#343b48",
        panel="#3b4252",
        boost="#434c5e",
        foreground="#d8dee9",
        value="#d8dee9",
        accent="#e5e9f0",
        primary="#88c0d0",
        secondary="#81a1c1",
        ok="#a3be8c",
        info="#8fbcbb",
        warn="#ebcb8b",
        error="#bf616a",
        dim="#616e88",
        item="#d8dee9",
        read="#7b88a1",
        border="#434c5e",
        border_focus="#88c0d0",
        focus="#8fbcbb",
        focus_bg="#3b4252",
        held="#ebcb8b",
        border_held="#4c566a",
    ),
    # https://github.com/morhetz/gruvbox — dark.
    "gruvbox": Palette(
        label="Gruvbox",
        background="#1d2021",
        surface="#282828",
        panel="#32302f",
        boost="#3c3836",
        foreground="#ebdbb2",
        value="#ebdbb2",
        accent="#d5c4a1",
        primary="#83a598",
        secondary="#a89984",
        ok="#b8bb26",
        info="#8ec07c",
        warn="#fabd2f",
        error="#fb4934",
        dim="#928374",
        item="#ebdbb2",
        read="#a89984",
        border="#3c3836",
        border_focus="#83a598",
        focus="#8ec07c",
        focus_bg="#32302f",
        held="#fe8019",
        border_held="#504945",
    ),
    # https://draculatheme.com/contribute — the official palette table.
    "dracula": Palette(
        label="Dracula",
        background="#191a21",
        surface="#21222c",
        panel="#282a36",
        boost="#44475a",
        foreground="#f8f8f2",
        value="#f8f8f2",
        accent="#d7d7d2",
        primary="#bd93f9",
        secondary="#6272a4",
        ok="#50fa7b",
        info="#8be9fd",
        warn="#f1fa8c",
        error="#ff5555",
        dim="#6272a4",
        item="#f8f8f2",
        read="#6272a4",
        border="#44475a",
        border_focus="#bd93f9",
        focus="#8be9fd",
        focus_bg="#282a36",
        held="#ffb86c",
        border_held="#6272a4",
    ),
    # https://github.com/atom/one-dark-syntax
    "one-dark": Palette(
        label="One Dark",
        background="#21252b",
        surface="#282c34",
        panel="#2c313a",
        boost="#3e4451",
        foreground="#abb2bf",
        value="#abb2bf",
        accent="#c8ccd4",
        primary="#61afef",
        secondary="#5c6370",
        ok="#98c379",
        info="#56b6c2",
        warn="#e5c07b",
        error="#e06c75",
        dim="#5c6370",
        item="#abb2bf",
        read="#5c6370",
        border="#3e4451",
        border_focus="#61afef",
        focus="#56b6c2",
        focus_bg="#2c313a",
        held="#d19a66",
        border_held="#5c6370",
    ),
    # https://ethanschoonover.com/solarized — dark.
    "solarized-dark": Palette(
        label="Solarized Dark",
        background="#002b36",
        surface="#073642",
        panel="#0b3b47",
        boost="#12414d",
        foreground="#93a1a1",
        value="#93a1a1",
        accent="#839496",
        primary="#268bd2",
        secondary="#586e75",
        ok="#859900",
        info="#2aa198",
        warn="#b58900",
        error="#dc322f",
        dim="#586e75",
        item="#93a1a1",
        read="#657b83",
        border="#12414d",
        border_focus="#268bd2",
        focus="#2aa198",
        focus_bg="#0b3b47",
        held="#cb4b16",
        border_held="#586e75",
    ),
    # https://rosepinetheme.com — main (dark).
    "rose-pine": Palette(
        label="Rosé Pine",
        background="#16141f",
        surface="#191724",
        panel="#1f1d2e",
        boost="#26233a",
        foreground="#e0def4",
        value="#e0def4",
        accent="#908caa",
        primary="#c4a7e7",
        secondary="#6e6a86",
        ok="#31748f",
        info="#9ccfd8",
        warn="#f6c177",
        error="#eb6f92",
        dim="#6e6a86",
        item="#e0def4",
        read="#6e6a86",
        border="#26233a",
        border_focus="#c4a7e7",
        focus="#9ccfd8",
        focus_bg="#21202e",
        held="#ebbcba",
        border_held="#524f67",
    ),
    # https://github.com/rebelot/kanagawa.nvim — wave.
    "kanagawa": Palette(
        label="Kanagawa",
        background="#16161d",
        surface="#1f1f28",
        panel="#2a2a37",
        boost="#363646",
        foreground="#dcd7ba",
        value="#dcd7ba",
        accent="#c8c093",
        primary="#7e9cd8",
        secondary="#727169",
        ok="#76946a",
        info="#6a9589",
        warn="#dca561",
        error="#c34043",
        dim="#727169",
        item="#dcd7ba",
        read="#727169",
        border="#2a2a37",
        border_focus="#7e9cd8",
        focus="#7e9cd8",
        focus_bg="#223249",
        held="#ff9e3b",
        border_held="#54546d",
    ),
    # https://github.com/sainnhe/everforest — dark, medium.
    "everforest": Palette(
        label="Everforest",
        background="#272e33",
        surface="#2d353b",
        panel="#343f44",
        boost="#3d484d",
        foreground="#d3c6aa",
        value="#d3c6aa",
        accent="#d3c6aa",
        primary="#7fbbb3",
        secondary="#859289",
        ok="#a7c080",
        info="#83c092",
        warn="#dbbc7f",
        error="#e67e80",
        dim="#859289",
        item="#d3c6aa",
        read="#7a8478",
        border="#3d484d",
        border_focus="#7fbbb3",
        focus="#83c092",
        focus_bg="#343f44",
        held="#e69875",
        border_held="#475258",
    ),
}
