"""Validated payload for the FX source.

The model is defined once, in the money domain (``portfolio.models``), so the fx
collector and the portfolio presenter validate against exactly the same schema. The
dependency direction stays one-way: ``fx`` already depends on ``portfolio`` for its
settings and store, never the reverse.
"""

from __future__ import annotations

from ..portfolio.models import FxPayload, FxRateRecord

__all__ = ["FxPayload", "FxRateRecord"]
