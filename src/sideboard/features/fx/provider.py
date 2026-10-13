"""Directed reference FX rates.

Two modes:

``manual_import`` (default)
    read manual/imported rates from the local store; no network at all.

``reference``
    refresh daily ECB reference rates through Frankfurter. A reference rate is not an
    execution or live trading rate, and it is stored as a **directed** record: PLN/EUR
    and EUR/PLN are different rows.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from ...core.capabilities import HttpReader
from ...core.contracts import (
    Cadence,
    Capability,
    Coverage,
    Precision,
    ProviderBuild,
    SourcePayload,
    SourceSpec,
)
from ...core.errors import CapabilityError, ParseError
from ..portfolio.config import FxSettings
from ..portfolio.models import FxPayload, FxRateRecord
from ..portfolio.repo import load_fx

log = logging.getLogger("sideboard.fx")

__all__ = ["RATE_ROUTES", "FxProvider", "demo_rates", "make_spec"]

# Documented route first, then the widely used v1 route as a fallback. Both are value
# routes; the resolved one is recorded in provenance.
RATE_ROUTES = (
    "https://api.frankfurter.dev/v2/providers/ecb/rate/{base}/{quote}",
    "https://api.frankfurter.dev/v1/latest?base={base}&symbols={quote}",
)

REFERENCE_REFRESH_SECONDS = 12 * 3600


class FxProvider:
    """Reads local rates, or refreshes dated ECB reference rates."""

    def __init__(self, *, settings: FxSettings) -> None:
        self.settings = settings

    async def fetch(self, context: ProviderBuild) -> FxPayload:
        settings = context.settings
        assert isinstance(settings, FxSettings)
        if settings.mode == "manual_import":
            return await self._from_store(context, settings)
        return await self._from_reference(context, settings)

    async def _from_store(self, context: ProviderBuild, settings: FxSettings) -> FxPayload:
        database = context.database
        if database is None:
            raise CapabilityError("the FX store requires read-only repository access")
        rates = await load_fx(database)
        base = settings.base_currency.upper()
        selected = [
            rate
            for (fx_base, _), rate in rates.items()
            if not settings.currencies or fx_base in settings.currencies or fx_base == base
        ]
        now = context.clock.now()
        stale_after_days = 7
        return FxPayload(
            rates=tuple(
                FxRateRecord(
                    base=rate.base,
                    quote=rate.quote,
                    rate=str(rate.rate),
                    as_of=rate.as_of,
                    source=rate.source,
                )
                for rate in sorted(selected, key=lambda r: (r.base, r.quote))
            ),
            as_of=None,
            precision=Precision.DATE if selected else Precision.UNKNOWN,
            valid_until=now + timedelta(days=stale_after_days),
            provenance="local directed FX store",
            coverage=Coverage(
                observed=len(selected),
                notes=(
                    "manual and imported rates only; no network request was made"
                    if selected
                    else "no rates stored yet; add one manually or import a rate file"
                ),
            ),
        )

    async def _from_reference(self, context: ProviderBuild, settings: FxSettings) -> FxPayload:
        http = context.http
        if http is None:
            raise CapabilityError("reference FX requires HTTP access")
        base = settings.base_currency.upper()
        now = context.clock.now()
        records: list[FxRateRecord] = []
        notes: list[str] = []
        missing: list[str] = []
        route_used: str | None = None
        for currency in settings.currencies:
            if currency == base:
                continue
            rate, observed, route = await self._one_rate(http, currency, base)
            if rate is None:
                missing.append(f"{currency}/{base}")
                continue
            route_used = route_used or route
            records.append(
                FxRateRecord(
                    base=currency,
                    quote=base,
                    rate=str(rate),
                    as_of=observed,
                    source="frankfurter-ecb",
                )
            )
        if not records:
            raise ParseError(
                "no reference rate was returned for the configured currencies; "
                "the route or its response shape may have changed"
            )
        if missing:
            notes.append("no reference rate for: " + ", ".join(missing))
        return FxPayload(
            rates=tuple(records),
            as_of=None,
            precision=Precision.DATE,
            valid_until=now + timedelta(seconds=REFERENCE_REFRESH_SECONDS * 2),
            provenance=f"frankfurter-ecb ({route_used or 'v1'})",
            coverage=Coverage(
                expected=len([c for c in settings.currencies if c != base]),
                observed=len(records),
                missing=tuple(missing),
                notes=" · ".join(notes) or "ECB daily reference rates, not execution rates",
            ),
        )

    @staticmethod
    async def _one_rate(
        http: HttpReader, currency: str, base: str
    ) -> tuple[float | None, date | None, str | None]:
        last_error: Exception | None = None
        for index, template in enumerate(RATE_ROUTES):
            # Currency codes are sent uppercase, as the publisher documents them.
            url = template.format(base=currency.upper(), quote=base.upper())
            try:
                response = await http.get_json(url)
            except Exception as error:  # a route may simply not exist yet
                last_error = error
                continue
            rate, observed = _extract_rate(response.json(), base)
            if rate is not None:
                return float(rate), observed, "v2" if index == 0 else "v1"
        if last_error is not None:
            raise last_error
        return None, None, None

    async def aclose(self) -> None:
        return None


def _extract_rate(document: object, base: str) -> tuple[float | None, date | None]:
    """Extract a rate from either documented response shape, defensively."""
    if not isinstance(document, dict):
        return None, None
    observed = _parse_date(document.get("date") or document.get("as_of"))
    rate = document.get("rate") or document.get("value")
    if isinstance(rate, int | float) and not isinstance(rate, bool):
        return float(rate), observed
    rates = document.get("rates")
    if isinstance(rates, dict):
        candidate = rates.get(base) or rates.get(base.upper())
        if isinstance(candidate, int | float) and not isinstance(candidate, bool):
            return float(candidate), observed
    return None, observed


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def make_spec() -> SourceSpec:
    return SourceSpec(
        type_id="fx.rates",
        payload_model=FxPayload,
        settings_model=FxSettings,
        payload_schema="fx.rates",
        payload_schema_version=1,
        factory=lambda build: FxProvider(settings=_settings_of(build)),
        capabilities=frozenset({Capability.REPOSITORY, Capability.HTTP_PUBLIC, Capability.FILE}),
        cadence=Cadence(
            import_driven=True,
            active_seconds=REFERENCE_REFRESH_SECONDS,
            ambient_seconds=REFERENCE_REFRESH_SECONDS,
            background_seconds=24 * 3600,
        ),
        retention_seconds=365 * 24 * 3600,
        description="Directed manual/imported FX rates, optionally refreshed from ECB reference",
        setup_hint=(
            "Enter a rate manually or import a file (sideboard import fx). "
            "Reference mode fetches ECB daily rates; it is not a live trading rate."
        ),
        demo_builder=demo_rates,
    )


def _settings_of(build: ProviderBuild) -> FxSettings:
    settings = build.settings
    assert isinstance(settings, FxSettings)
    return settings


def demo_rates() -> SourcePayload:
    """Synthetic directed rates. Both directions are stored as separate records."""
    return FxPayload(
        rates=(
            FxRateRecord(
                base="PLN", quote="EUR", rate="0.2320", as_of=date(2026, 10, 12), source="demo"
            ),
            FxRateRecord(
                base="EUR", quote="PLN", rate="4.3100", as_of=date(2026, 10, 12), source="demo"
            ),
            FxRateRecord(
                base="GBP", quote="EUR", rate="1.1650", as_of=date(2026, 10, 12), source="demo"
            ),
        ),
        precision=Precision.DATE,
        provenance="demo",
        coverage=Coverage(observed=3, notes="synthetic directed reference rates"),
    )
