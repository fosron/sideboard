"""Optional market quotes. Disabled by default; no mandatory credential or payment."""

from __future__ import annotations

from ...core.contracts import FeatureSpec
from .provider import make_spec

__all__ = ["FEATURE"]

FEATURE = FeatureSpec(
    id="markets",
    sources=(make_spec(),),
    widgets=(),
    default_sources={
        "markets": {
            "type": "markets.quotes",
            "enabled": False,
            "settings": {
                "mode": "disabled",
                "provider": "yahoo",
                "symbol_map": {},
                "refresh_seconds": 3600.0,
            },
        }
    },
    description="Optional, off-by-default market quotes with an explicit symbol mapping.",
)
