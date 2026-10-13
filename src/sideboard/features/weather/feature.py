"""Weather feature registration.

Exactly one trusted registration entry: a source type that collects an
Open-Meteo forecast for one explicitly configured place, and a summary widget
that renders it compactly or in detail. Instances, tabs and categories are
configuration, not code.
"""

from __future__ import annotations

from ...core.contracts import (
    Cadence,
    Capability,
    FeatureSpec,
    InputBinding,
    SourceSpec,
    WidgetSpec,
)
from .config import WeatherSettings, WeatherWidgetSettings
from .models import WeatherPayload
from .presenter import present
from .provider import build_provider, demo_builder

__all__ = ["FEATURE", "WEATHER_SOURCE", "WEATHER_SUMMARY"]

WEATHER_SOURCE = SourceSpec(
    type_id="weather.forecast",
    payload_model=WeatherPayload,
    settings_model=WeatherSettings,
    payload_schema="weather.forecast",
    payload_schema_version=1,
    factory=build_provider,
    capabilities=frozenset({Capability.HTTP_PUBLIC}),
    cadence=Cadence(
        active_seconds=1200.0,
        ambient_seconds=1200.0,
        background_seconds=3600.0,
    ),
    description="Current conditions and forecast for one configured place (Open-Meteo).",
    requires_setup=True,
    setup_check=lambda settings: settings.latitude is not None and settings.longitude is not None,
    setup_hint="Set latitude, longitude and an IANA timezone for this place.",
    demo_builder=demo_builder,
)

WEATHER_SUMMARY = WidgetSpec(
    type_id="weather.summary",
    settings_model=WeatherWidgetSettings,
    inputs=(InputBinding(name="primary", accepts=frozenset({"weather.forecast"})),),
    present=present,
    description="Current conditions and forecast for one configured place.",
    default_category="life",
    min_width=28,
    min_height=4,
)

FEATURE = FeatureSpec(
    id="weather",
    sources=(WEATHER_SOURCE,),
    widgets=(WEATHER_SUMMARY,),
    description="Open-Meteo current conditions and forecast for one configured place.",
)
