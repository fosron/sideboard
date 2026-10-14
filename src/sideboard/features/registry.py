"""Trusted feature modules.

This is the **one** repository-owned list of built-in features. Everything else —
registries, configuration validation, the widget catalogue, the palette, defaults —
is derived from these specs. There is no plugin discovery, no entry-point scanning and
no configuration-specified Python import path.

Feature manifests must be cheap and perform no I/O at import time. Optional heavy
libraries are imported inside the provider factory that needs them.
"""

from __future__ import annotations

from ..core.contracts import FeatureSpec
from ..core.registry import Registry, build_registry
from .diagnostics import FEATURE as DIAGNOSTICS
from .docker_local import FEATURE as DOCKER_LOCAL
from .fx import FEATURE as FX
from .git_local import FEATURE as GIT_LOCAL
from .github_actions import FEATURE as GITHUB_ACTIONS
from .hackernews import FEATURE as HACKERNEWS
from .local_snapshot import FEATURE as LOCAL_SNAPSHOT
from .mac_system import FEATURE as MAC_SYSTEM
from .markets import FEATURE as MARKETS
from .news import FEATURE as NEWS
from .portfolio import FEATURE as PORTFOLIO
from .reddit import FEATURE as REDDIT
from .weather import FEATURE as WEATHER
from .youtube import FEATURE as YOUTUBE

__all__ = ["FEATURES", "build_feature_registry"]

FEATURES: tuple[FeatureSpec, ...] = (
    GITHUB_ACTIONS,
    GIT_LOCAL,
    WEATHER,
    NEWS,
    HACKERNEWS,
    YOUTUBE,
    REDDIT,
    PORTFOLIO,
    FX,
    MARKETS,
    MAC_SYSTEM,
    DOCKER_LOCAL,
    LOCAL_SNAPSHOT,
    DIAGNOSTICS,
)


def build_feature_registry(extra: tuple[FeatureSpec, ...] = ()) -> Registry:
    """Validate and build the registry. Raises :class:`RegistryError` on conflicts."""
    return build_registry((*FEATURES, *extra))
