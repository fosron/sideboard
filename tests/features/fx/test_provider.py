"""FX source: directed rate handling, mode selection and the rate widget."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import respx

from sideboard.core.capabilities import HttpReader, HttpScope
from sideboard.core.clock import FakeClock
from sideboard.core.contracts import (
    Connectivity,
    EmptySettings,
    Freshness,
    PresentInput,
    SourceEnvelope,
    ViewContext,
    ViewMode,
)
from sideboard.core.storage import CORE_MIGRATIONS, Database, Migration
from sideboard.features.fx.models import FxPayload, FxRateRecord
from sideboard.features.fx.provider import FxProvider, _extract_rate
from sideboard.features.portfolio.config import FxSettings
from sideboard.features.portfolio.presenter import present_fx

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

FX_MIGRATION = Migration(
    version=20,
    name="fx_rates",
    owner="fx",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS fx_rates (
            base TEXT NOT NULL, quote TEXT NOT NULL, rate TEXT NOT NULL, as_of TEXT,
            source TEXT NOT NULL DEFAULT 'manual', fetched_at TEXT NOT NULL,
            PRIMARY KEY (base, quote, as_of)
        )
        """,
    ),
)


async def test_manual_mode_reads_the_store_and_needs_no_network(tmp_path) -> None:
    database = await Database.open(tmp_path / "db.sqlite3")
    await database.initialize((*CORE_MIGRATIONS, FX_MIGRATION))
    try:
        from sideboard.features.portfolio.repo import upsert_rate

        await upsert_rate(
            database,
            base="PLN",
            quote="EUR",
            rate=Decimal("0.2320"),
            as_of=date(2026, 10, 12),
            at=NOW,
        )
        settings = FxSettings(mode="manual_import", currencies=["PLN"])
        provider = FxProvider(settings=settings)
        payload = await provider.fetch(
            ProviderBuild_like(settings=settings, database=database, clock=FakeClock(NOW))
        )
        assert isinstance(payload, FxPayload)
        assert payload.rates[0].base == "PLN"
        assert payload.rates[0].quote == "EUR"
        assert payload.rates[0].source == "manual"
        assert "no network request was made" in (payload.coverage.notes or "")
    finally:
        await database.aclose()


def ProviderBuild_like(**kwargs: object):
    from sideboard.core.contracts import ProviderBuild

    return ProviderBuild(
        source_id="fx",
        settings=kwargs["settings"],  # type: ignore[arg-type]
        clock=kwargs["clock"],  # type: ignore[arg-type]
        database=kwargs.get("database"),  # type: ignore[arg-type]
        http=kwargs.get("http"),  # type: ignore[arg-type]
    )


@respx.mock
async def test_reference_mode_parses_the_documented_v2_shape() -> None:
    respx.get("https://api.frankfurter.dev/v2/providers/ecb/rate/PLN/EUR").mock(
        return_value=httpx.Response(
            200, json={"date": "2026-10-12", "base": "PLN", "quote": "EUR", "rate": 0.22805}
        )
    )
    # The v1 route is only a fallback; it must not be requested when v2 answers.
    respx.get("https://api.frankfurter.dev/v1/latest?base=PLN&symbols=EUR").mock(
        return_value=httpx.Response(500)
    )
    settings = FxSettings(mode="reference", currencies=["PLN"])
    provider = FxProvider(settings=settings)
    http = HttpReader(client=httpx.AsyncClient(), scope=HttpScope.PUBLIC)
    payload = await provider.fetch(
        ProviderBuild_like(settings=settings, clock=FakeClock(NOW), http=http)  # type: ignore[call-arg]
    )
    assert isinstance(payload, FxPayload)
    assert payload.rates[0].base == "PLN" and payload.rates[0].quote == "EUR"
    assert payload.rates[0].as_of == date(2026, 10, 12)
    assert "v2" in payload.provenance
    assert "not execution rates" in (payload.coverage.notes or "")
    await http.aclose()


@respx.mock
async def test_reference_mode_falls_back_to_the_v1_shape() -> None:
    respx.get("https://api.frankfurter.dev/v2/providers/ecb/rate/PLN/EUR").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.frankfurter.dev/v1/latest?base=PLN&symbols=EUR").mock(
        return_value=httpx.Response(
            200,
            json={"amount": 1.0, "base": "PLN", "date": "2026-10-12", "rates": {"EUR": 0.22805}},
        )
    )
    settings = FxSettings(mode="reference", currencies=["PLN"])
    provider = FxProvider(settings=settings)
    http = HttpReader(client=httpx.AsyncClient(), scope=HttpScope.PUBLIC)
    payload = await provider.fetch(
        ProviderBuild_like(settings=settings, clock=FakeClock(NOW), http=http)  # type: ignore[call-arg]
    )
    assert isinstance(payload, FxPayload)
    assert "v1" in payload.provenance
    await http.aclose()


def test_rate_extraction_is_defensive() -> None:
    assert _extract_rate({"rate": 0.5, "date": "2026-10-12"}, "EUR")[0] == 0.5
    assert _extract_rate({"rates": {"EUR": 0.25}}, "EUR")[0] == 0.25
    assert _extract_rate({"rates": {}}, "EUR")[0] is None
    assert _extract_rate("not a dict", "EUR")[0] is None
    assert _extract_rate({"rate": True}, "EUR")[0] is None


def test_fx_widget_states_the_direction_and_missing_reverse() -> None:
    payload = FxPayload(
        rates=(
            FxRateRecord(base="PLN", quote="EUR", rate="0.2320", as_of=date(2026, 10, 12)),
            FxRateRecord(base="GBP", quote="EUR", rate="1.1650", as_of=date(2026, 10, 12)),
        )
    )
    envelope = SourceEnvelope(
        source_id="fx",
        payload_schema="fx.rates",
        payload_schema_version=1,
        payload=payload,
        connectivity=Connectivity.OK,
        freshness=Freshness.FRESH,
    )
    view = present_fx(
        PresentInput(
            settings=EmptySettings(),
            inputs={"primary": envelope},
            context=ViewContext(now=NOW, mode=ViewMode.DETAIL),
        )
    )
    assert view.table is not None
    assert "PLN/EUR" in view.table.rows[0].cells["pair"]
    assert "EUR/PLN" in (view.footnote or "")
