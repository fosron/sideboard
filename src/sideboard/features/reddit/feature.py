"""Registration for the Reddit feature.

One source type (``reddit.threads``) and one widget type (``reddit.list``). Reddit is
access-dependent, so the shipped default is link-only: it performs no network I/O and
renders deliberate community links plus an honest status. Enabling ``mode = "rss"``
requires the user to have validated that public feed form from this Mac first.
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
from .config import DEFAULT_COMMUNITIES, RedditListSettings, RedditSettings
from .models import REDDIT_PAYLOAD_SCHEMA, RedditPayload
from .presenter import present
from .provider import build_provider, demo_payload

__all__ = ["FEATURE", "REDDIT_SOURCE", "REDDIT_WIDGET"]

REDDIT_SOURCE = SourceSpec(
    type_id="reddit.threads",
    payload_model=RedditPayload,
    settings_model=RedditSettings,
    payload_schema=REDDIT_PAYLOAD_SCHEMA,
    payload_schema_version=1,
    factory=build_provider,
    capabilities=frozenset({Capability.HTTP_PUBLIC}),
    cadence=Cadence(active_seconds=3600.0, ambient_seconds=3600.0, background_seconds=7200.0),
    retention_seconds=48 * 3600,
    description="Permitted Reddit feeds: public RSS after validation, link-only otherwise.",
    # Access is conditional, so the spec carries a setup hint; link-only mode is a
    # deliberate, complete state rather than a configuration gap.
    requires_setup=True,
    setup_check=lambda settings: settings.mode == "links",
    setup_hint=(
        "Reddit access is conditional; validate a permitted feed or stay in link-only mode."
    ),
    demo_builder=demo_payload,
)

REDDIT_WIDGET = WidgetSpec(
    type_id="reddit.list",
    settings_model=RedditListSettings,
    inputs=(
        InputBinding(
            name="primary",
            accepts=frozenset({REDDIT_PAYLOAD_SCHEMA}),
            # Not required on purpose: the shipped link-only mode never produces a
            # payload, so the presenter must run and render the deliberate links.
            required=False,
        ),
    ),
    present=present,
    description="Reddit threads, or community links when the feed is not enabled.",
    detail="Scores and comment counts appear only when the permitted source reports them.",
    min_width=28,
    min_height=3,
)

FEATURE = FeatureSpec(
    id="reddit",
    sources=(REDDIT_SOURCE,),
    widgets=(REDDIT_WIDGET,),
    default_sources={
        "reddit": {
            "type": "reddit.threads",
            "enabled": True,
            "settings": {"mode": "links", "communities": list(DEFAULT_COMMUNITIES)},
        },
    },
    default_widgets={
        "reddit": {
            "type": "reddit.list",
            "sources": {"primary": "reddit"},
            "settings": {"communities": list(DEFAULT_COMMUNITIES)},
        },
    },
    description="Access-dependent Reddit threads with a link-only fallback.",
)
