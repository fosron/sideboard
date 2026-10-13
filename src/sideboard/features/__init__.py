"""Feature packages.

The trusted feature list lives in :mod:`sideboard.features.registry`.
"""

from .registry import FEATURES, build_feature_registry

__all__ = ["FEATURES", "build_feature_registry"]
