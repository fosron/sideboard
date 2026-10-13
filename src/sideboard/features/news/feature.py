"""News feature registration."""

from __future__ import annotations

from ...core.contracts import FeatureSpec, InputBinding, WidgetSpec
from .config import NewsWidgetSettings, SavedWidgetSettings
from .presenter import present_news, present_saved
from .provider import make_spec

__all__ = ["FEATURE"]

NEWS_WIDGET = WidgetSpec(
    type_id="news.digest",
    settings_model=NewsWidgetSettings,
    inputs=(InputBinding(name="primary", accepts=frozenset({"news.feeds"})),),
    present=present_news,
    description="Curated news digest with deterministic category diversity.",
    default_category="world",
    default_settings={"limit": 6},
    detail="bucket list, feed health and article counts",
    min_width=30,
    min_height=5,
)

SAVED_WIDGET = WidgetSpec(
    type_id="news.saved",
    settings_model=SavedWidgetSettings,
    inputs=(),
    present=present_saved,
    description="Locally saved reading and video links. Local state only, no collector.",
    default_category="world",
    min_width=26,
    min_height=3,
)

FEATURE = FeatureSpec(
    id="news",
    sources=(make_spec(),),
    widgets=(NEWS_WIDGET, SAVED_WIDGET),
    description="Publisher RSS/Atom news with a deterministic diversity digest.",
)
