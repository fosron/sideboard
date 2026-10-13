"""Portfolio feature: manual-first holdings, prices, dividend events and investor links.

Automated brokerage access is deliberately absent. Positions, quotes and FX are
separate stores; a manual or statement value is never silently overwritten by a quote,
and an unresolved instrument identity never acquires a guessed quote.
"""

from __future__ import annotations

from ...core.contracts import FeatureSpec, InputBinding, WidgetSpec
from .config import (
    PortfolioEventsSettings,
    PortfolioSummarySettings,
    PortfolioTableSettings,
    VercomSettings,
)
from .migrations import PORTFOLIO_MIGRATIONS
from .presenter import present_events, present_summary, present_table, present_vercom
from .provider import make_specs

__all__ = ["FEATURE"]

HOLDINGS_SOURCE, PRICES_SOURCE = make_specs()

SUMMARY_WIDGET = WidgetSpec(
    type_id="portfolio.summary",
    settings_model=PortfolioSummarySettings,
    inputs=(
        InputBinding(name="positions", accepts=frozenset({"portfolio.holdings"})),
        InputBinding(name="prices", accepts=frozenset({"portfolio.prices"}), required=False),
        InputBinding(name="fx", accepts=frozenset({"fx.rates"}), required=False),
        InputBinding(name="markets", accepts=frozenset({"markets.quotes"}), required=False),
    ),
    present=present_summary,
    description="Tracked value in the base currency, weights and price bases.",
    default_category="finance",
    detail="per-holding table with price, FX and reported-value bases",
    min_width=34,
    min_height=6,
)

TABLE_WIDGET = WidgetSpec(
    type_id="portfolio.holdings",
    settings_model=PortfolioTableSettings,
    inputs=(
        InputBinding(name="positions", accepts=frozenset({"portfolio.holdings"})),
        InputBinding(name="prices", accepts=frozenset({"portfolio.prices"}), required=False),
        InputBinding(name="fx", accepts=frozenset({"fx.rates"}), required=False),
        InputBinding(name="markets", accepts=frozenset({"markets.quotes"}), required=False),
    ),
    present=present_table,
    description="Every tracked position with its identifier, ISIN, quantity and value basis.",
    default_category="finance",
    detail="adds cost basis and reported statement values",
    min_width=36,
    min_height=6,
)

EVENTS_WIDGET = WidgetSpec(
    type_id="portfolio.events",
    settings_model=PortfolioEventsSettings,
    inputs=(InputBinding(name="positions", accepts=frozenset({"portfolio.holdings"})),),
    present=present_events,
    description="Declared/received dividends and official investor-relations links.",
    default_category="finance",
    detail="one row per dated event with its kind",
    min_width=30,
    min_height=5,
)

VERCOM_WIDGET = WidgetSpec(
    type_id="portfolio.instrument",
    settings_model=VercomSettings,
    inputs=(
        InputBinding(name="positions", accepts=frozenset({"portfolio.holdings"})),
        InputBinding(name="prices", accepts=frozenset({"portfolio.prices"}), required=False),
        InputBinding(name="fx", accepts=frozenset({"fx.rates"}), required=False),
        InputBinding(name="markets", accepts=frozenset({"markets.quotes"}), required=False),
    ),
    present=present_vercom,
    description="Concentration, dividends and report links for one configured holding.",
    default_category="finance",
    default_settings={"instrument_id": ""},
    detail="quantity, price, value, concentration and dated events",
    min_width=30,
    min_height=5,
)

FEATURE = FeatureSpec(
    id="portfolio",
    sources=(HOLDINGS_SOURCE, PRICES_SOURCE),
    widgets=(SUMMARY_WIDGET, TABLE_WIDGET, EVENTS_WIDGET, VERCOM_WIDGET),
    migrations=PORTFOLIO_MIGRATIONS,
    default_sources={
        "holdings": {
            "type": "portfolio.holdings",
            "enabled": True,
            "settings": {"mode": "manual_import", "base_currency": "EUR"},
        },
        "prices": {
            "type": "portfolio.prices",
            "enabled": True,
            "settings": {"mode": "manual_import"},
        },
    },
    default_widgets={
        "portfolio": {
            "type": "portfolio.summary",
            "sources": {"positions": "holdings", "prices": "prices", "fx": "fx"},
        },
    },
    description="Manual-first portfolio: positions, quotes, dividends and investor links.",
)
