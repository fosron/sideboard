"""Reddit: access-dependent, link-only by default.

The package re-exports its :class:`FeatureSpec` so the trusted registry can name it
without importing implementation modules directly.
"""

from __future__ import annotations

from .feature import FEATURE

__all__ = ["FEATURE"]
