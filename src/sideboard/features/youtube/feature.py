"""Feature registration for the YouTube uploads slice.

One source type (``youtube.uploads``) and one widget type (``youtube.uploads``).
The registry entry is the only core-facing wiring; all behavior stays inside this
package.
"""

from __future__ import annotations

from ...core.contracts import (
    Cadence,
    Capability,
    FeatureSpec,
    InputBinding,
    Provider,
    ProviderBuild,
    SourceSpec,
    WidgetSpec,
)
from .config import YouTubeSettings, YouTubeWidgetSettings
from .models import YouTubePayload
from .presenter import present
from .provider import YouTubeProvider, demo_payload

__all__ = ["FEATURE", "UPLOADS_SOURCE", "UPLOADS_WIDGET"]

YOUTUBE_UPLOADS = "youtube.uploads"


def _provider_factory(build: ProviderBuild) -> Provider:
    settings = build.settings
    if not isinstance(settings, YouTubeSettings):
        settings = YouTubeSettings.model_validate(settings)
    return YouTubeProvider(settings)


UPLOADS_SOURCE = SourceSpec(
    type_id=YOUTUBE_UPLOADS,
    payload_model=YouTubePayload,
    settings_model=YouTubeSettings,
    payload_schema=YOUTUBE_UPLOADS,
    payload_schema_version=1,
    factory=_provider_factory,
    capabilities=frozenset({Capability.HTTP_PUBLIC, Capability.FILE}),
    cadence=Cadence(active_seconds=1800.0, ambient_seconds=1800.0, background_seconds=7200.0),
    description=(
        "Recent uploads from configured YouTube channels via the public Atom feed, "
        "with optional Google Takeout subscriptions and watch-history imports."
    ),
    requires_setup=True,
    setup_check=lambda settings: bool(
        settings.channels
        or settings.favorite_channel_ids
        or settings.takeout_subscriptions_csv
        or settings.takeout_watch_history_json
        or settings.imported_channel_ids
    ),
    setup_hint=(
        "Add a channel ID (UC…, or a URL containing /channel/UC…) or import a "
        "Google Takeout subscriptions CSV; handles are not resolved by scraping."
    ),
    demo_builder=demo_payload,
)

UPLOADS_WIDGET = WidgetSpec(
    type_id=YOUTUBE_UPLOADS,
    settings_model=YouTubeWidgetSettings,
    inputs=(InputBinding(name="primary", accepts=frozenset({YOUTUBE_UPLOADS})),),
    present=present,
    description="Recent channel uploads, ordered by favorites and recent viewing frequency.",
    default_category="media",
    min_width=26,
    min_height=4,
)

FEATURE = FeatureSpec(
    id="youtube",
    sources=(UPLOADS_SOURCE,),
    widgets=(UPLOADS_WIDGET,),
    description=(
        "YouTube channel uploads with optional local Google Takeout subscription "
        "and watch-history imports."
    ),
)
