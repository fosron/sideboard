"""Typed settings for the portfolio, prices and FX sources."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "FxSettings",
    "FxWidgetSettings",
    "HoldingsSettings",
    "PortfolioEventsSettings",
    "PortfolioSummarySettings",
    "PortfolioTableSettings",
    "PricesSettings",
    "VercomSettings",
]


class HoldingsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "manual_import"
    base_currency: str = "EUR"
    show_zero_positions: bool = False

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        if value not in {"manual_import"}:
            raise ValueError(
                "only manual_import is supported; automated brokerage access is not required"
            )
        return value


class PricesSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "manual_import"
    provider: str = "none"

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        if value not in {"manual_import"}:
            raise ValueError("only manual_import is supported; enable the markets feature instead")
        return value


class FxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "manual_import"
    provider: str = "frankfurter-ecb"
    base_currency: str = "EUR"
    currencies: list[str] = Field(default_factory=lambda: ["PLN"])
    refresh_seconds: float | None = None
    active_seconds: float | None = None
    ambient_seconds: float | None = None
    background_seconds: float | None = None
    allow_hosts: list[str] = Field(default_factory=lambda: ["api.frankfurter.dev"])

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        if value not in {"manual_import", "reference"}:
            raise ValueError("mode must be manual_import or reference")
        return value

    @field_validator("currencies")
    @classmethod
    def _upper(cls, value: list[str]) -> list[str]:
        return [item.strip().upper() for item in value if item.strip()]


class PortfolioSummarySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    show_weights: bool = True
    show_change: bool = True
    max_holdings: int = 6
    show_concentration: bool = True
    concentration_instrument: str = ""
    prefer_manual_quotes: bool = True


class PortfolioTableSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = 20
    show_basis: bool = True
    show_cost_basis: bool = False
    show_reported_values: bool = True


class PortfolioEventsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = 10
    show_links: bool = True


class VercomSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument_id: str = ""
    label: str = "Vercom"
    show_weight: bool = True
    show_dividends: bool = True
    show_links: bool = True


class FxWidgetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pairs: list[str] = Field(default_factory=list)
    show_source_date: bool = True
