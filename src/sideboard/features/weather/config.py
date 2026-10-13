"""Typed settings for the weather feature.

Location is explicit configuration, never inferred. ``timezone`` is the place's
IANA name: it is used to display times and to interpret the local wall-clock
timestamps Open-Meteo returns, but it is *not* treated as evidence of where the
user is, so it never selects or guesses a location.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["WeatherSettings", "WeatherWidgetSettings"]


class WeatherSettings(BaseModel):
    """Source instance settings for one configured place."""

    model_config = ConfigDict(extra="forbid")

    label: str = "Home"
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    timezone: str = "Europe/Madrid"
    units: Literal["metric"] = "metric"
    # ``refresh_seconds`` is the documented one-number form; the host resolves it to the
    # active/ambient cadence. The explicit overrides below take precedence.
    refresh_seconds: float | None = None
    active_seconds: float | None = None
    ambient_seconds: float | None = None
    background_seconds: float | None = None
    forecast_days: int = Field(default=7, ge=1, le=7)


class WeatherWidgetSettings(BaseModel):
    """Widget settings: how much of the collected forecast a card shows.

    ``show_hours``/``show_days`` bound the rows a small card renders; the payload
    always carries 24 hours and up to 7 days, so raising these values needs no
    new collection.
    """

    model_config = ConfigDict(extra="forbid")

    show_hours: int = Field(default=4, ge=1, le=24)
    show_days: int = Field(default=5, ge=1, le=7)
    show_rain: bool = True
