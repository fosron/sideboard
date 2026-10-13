"""Semantic theme tokens for Sideboard.

Dark, restrained, thin borders, generous whitespace. No feature-specific colours:
a feature may only use the semantic styles declared here.
"""

from __future__ import annotations

from textual.theme import Theme

__all__ = ["SIDEBOARD_THEME", "THEME_NAME"]

THEME_NAME = "sideboard-dark"

SIDEBOARD_THEME = Theme(
    name=THEME_NAME,
    dark=True,
    primary="#7aa2c8",
    secondary="#6d7f8f",
    accent="#a8b6c4",
    foreground="#cfd6dd",
    background="#12161a",
    surface="#171c21",
    panel="#1d2329",
    success="#7fae7a",
    warning="#d8b26a",
    error="#cf7b7b",
    boost="#20272e",
    luminosity_spread=0.12,
    text_alpha=0.92,
    variables={
        "sb-dim": "#7e8a95",
        "sb-value": "#e2e7ec",
        "sb-ok": "#7fae7a",
        "sb-info": "#8fa8bd",
        "sb-warn": "#d8b26a",
        "sb-error": "#cf7b7b",
        "sb-item": "#cfd6dd",
        "sb-read": "#6f7a84",
        "sb-border": "#2a323a",
        "sb-border-focus": "#7aa2c8",
        "sb-held": "#d8b26a",
    },
)
