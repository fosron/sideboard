"""Directed FX rates with an optional ECB reference updater."""

from __future__ import annotations

from ...core.contracts import EmptySettings, FeatureSpec, InputBinding, WidgetSpec
from ..portfolio.presenter import present_fx
from .provider import make_spec

__all__ = ["FEATURE"]

FX_WIDGET = WidgetSpec(
    type_id="fx.summary",
    settings_model=EmptySettings,
    inputs=(InputBinding(name="primary", accepts=frozenset({"fx.rates"})),),
    present=present_fx,
    description="Directed FX rates with their reference dates and direction.",
    default_category="finance",
    detail="every stored direction, plus a warning when a direction is missing",
    min_width=28,
    min_height=3,
)

FEATURE = FeatureSpec(
    id="fx",
    sources=(make_spec(),),
    widgets=(FX_WIDGET,),
    default_widgets={
        "fx": {"type": "fx.summary", "sources": {"primary": "fx"}},
    },
    description="Directed manual/imported FX rates; optional ECB reference refresh.",
)
