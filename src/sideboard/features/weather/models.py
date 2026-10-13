"""Validated payload models for the weather feature.

Every measurement is optional. A field the API did not return stays ``None`` so a
presenter can report it as unavailable: a temperature of ``0.0`` and "the API did
not return a temperature" must never be indistinguishable.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from ...core.contracts import SourcePayload

__all__ = ["CurrentConditions", "DailyPoint", "HourlyPoint", "WeatherPayload"]


class _Frozen(BaseModel):
    """Immutable, strict base for the payload parts."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CurrentConditions(_Frozen):
    """Model-derived current conditions for the configured place."""

    temperature_c: float | None = None
    apparent_c: float | None = None
    precipitation_mm: float | None = None
    weather_code: int | None = None
    wind_speed_kmh: float | None = None
    is_day: bool | None = None
    observed_at: datetime | None = None


class HourlyPoint(_Frozen):
    """One forecast hour. ``at`` is required: a point without a time is useless."""

    at: datetime
    temperature_c: float | None = None
    precipitation_probability: int | None = None
    precipitation_mm: float | None = None
    weather_code: int | None = None


class DailyPoint(_Frozen):
    """One forecast day."""

    date: date
    tmin_c: float | None = None
    tmax_c: float | None = None
    precipitation_mm: float | None = None
    precipitation_probability: int | None = None
    sunrise: datetime | None = None
    sunset: datetime | None = None
    weather_code: int | None = None


class WeatherPayload(SourcePayload):
    """One Open-Meteo forecast for a single configured place.

    ``model_note`` keeps the presentation honest: these are model-derived values,
    not a station observation, and the UI labels them accordingly.
    """

    # ``model_note`` is part of the documented payload contract, so the pydantic
    # "model_" protected namespace must be relaxed for this model.
    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    location_label: str
    timezone: str
    latitude: float | None = None
    longitude: float | None = None
    current: CurrentConditions | None = None
    hourly: tuple[HourlyPoint, ...] = ()
    daily: tuple[DailyPoint, ...] = ()
    model_note: str = "model-derived forecast"

    @classmethod
    def schema_id(cls) -> str:
        """Stable payload schema id; mirrors ``SourceSpec.payload_schema``."""
        return "weather.forecast"
