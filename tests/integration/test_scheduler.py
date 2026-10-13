"""Scheduling behaviour: cadence, demand, retries, backoff, cancellation and cleanup."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from sideboard.core.clock import FakeClock
from sideboard.core.config import config_from_document, resolve_config
from sideboard.core.contracts import (
    Cadence,
    Capability,
    Connectivity,
    Coverage,
    Demand,
    FeatureSpec,
    InputBinding,
    Precision,
    ProviderBuild,
    SourcePayload,
    SourceSpec,
    ViewModel,
    WidgetSpec,
)
from sideboard.core.errors import (
    BlockedError,
    RateLimitedError,
    TransientNetworkError,
    UnavailableError,
)
from sideboard.core.history import BoundedHistory
from sideboard.core.paths import default_paths
from sideboard.core.registry import build_registry
from sideboard.core.scheduler import BACKOFF_BASE, Scheduler
from sideboard.core.storage import CORE_MIGRATIONS, Database


class Settings(BaseModel):
    fail_with: str = ""
    value: float = 1.0
    active_seconds: float | None = None


class Payload(SourcePayload):
    value: float = 0.0


class StubProvider:
    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings
        self.calls = 0
        self.closed = 0
        self.payload: Payload | None = None

    async def fetch(self, context: ProviderBuild) -> Payload:
        self.calls += 1
        _ = context
        mode = self.settings.fail_with
        if mode == "transient":
            raise TransientNetworkError("boom")
        if mode == "rate":
            raise RateLimitedError("slow down", retry_after_seconds=90.0)
        if mode == "blocked":
            raise BlockedError("not allowed")
        if mode == "unavailable":
            raise UnavailableError("missing tool")
        if mode == "once_and_transient" and self.calls == 1:
            raise TransientNetworkError("boom")
        self.payload = Payload(
            value=self.settings.value,
            as_of=context.clock.now(),
            precision=Precision.INSTANT,
            provenance="stub",
            coverage=Coverage(observed=1),
        )
        return self.payload

    async def aclose(self) -> None:
        self.closed += 1


def _spec(type_id: str = "stub.value", *, import_driven: bool = False) -> SourceSpec:
    return SourceSpec(
        type_id=type_id,
        payload_model=Payload,
        settings_model=Settings,
        payload_schema=type_id,
        payload_schema_version=1,
        factory=lambda build: StubProvider(settings=_settings(build)),
        capabilities=frozenset({Capability.LOCAL_METRICS}),
        cadence=Cadence(
            import_driven=import_driven,
            active_seconds=30,
            ambient_seconds=60,
            background_seconds=120,
        ),
        demo_builder=lambda: Payload(value=99.0, provenance="demo"),
    )


def _settings(build: ProviderBuild) -> Settings:
    settings = build.settings
    assert isinstance(settings, Settings)
    return settings


def _widget(type_id: str = "stub.widget", source_type: str = "stub.value") -> WidgetSpec:
    return WidgetSpec(
        type_id=type_id,
        settings_model=Settings,
        inputs=(InputBinding(name="primary", accepts=frozenset({source_type})),),
        present=lambda inputs: ViewModel(),
    )


FETURE = FeatureSpec(id="stub", sources=(_spec(),), widgets=(_widget(),))


def _document(**source_settings: object) -> dict:
    settings = {"fail_with": "", "value": 1.0, "active_seconds": 30}
    settings.update(source_settings)
    return {
        "config_version": 1,
        "sources": {"stub": {"type": "stub.value", "enabled": True, "settings": settings}},
        "widgets": {"stub_widget": {"type": "stub.widget", "sources": {"primary": "stub"}}},
        "tabs": [{"id": "systems", "label": "Systems", "widgets": ["stub_widget"]}],
        "overview": {"slots": {}},
    }


async def _make(tmp_path: Path, document: dict, feature: FeatureSpec = FETURE):
    registry = build_registry((feature,))
    paths = default_paths(home=tmp_path, config_file=tmp_path / "config.toml")
    config = resolve_config(config_from_document(document), registry, paths=paths)
    database = await Database.open(tmp_path / "db.sqlite3")
    await database.initialize(CORE_MIGRATIONS + registry.migrations())
    clock = FakeClock(datetime(2026, 10, 13, 12, 0, tzinfo=UTC))
    scheduler = Scheduler(
        database=database,
        clock=clock,
        history=BoundedHistory(database=database),
        config=config,
        client_factory=None,
    )
    return scheduler, database, clock, config


class FailingProvider:
    """Always raises, to prove the last good payload survives a later failure."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def fetch(self, context: ProviderBuild) -> Payload:
        _ = context
        raise self.error

    async def aclose(self) -> None:
        return None


async def test_one_task_and_one_interval_per_instance(tmp_path: Path) -> None:
    scheduler, database, clock, config = await _make(tmp_path, _document())
    try:
        await scheduler.start({})
        scheduler.set_active_tab("systems")
        runtime = scheduler.runtimes["stub"]
        assert runtime.interval == 30.0
        assert runtime.task is not None
        scheduler.suspend()
        await scheduler.refresh_now("stub", force=True)
        envelope = scheduler.snapshot("stub")
        assert envelope is not None and envelope.payload is not None
        assert 27.0 <= runtime.next_due - clock.monotonic() <= 33.0
        # A second widget bound to the same instance would not add a task or a request.
        assert len(config.widgets_by_source()["stub"]) == 1
        assert (
            len([rt for rt in scheduler.runtimes.values() if rt.instance.source_id == "stub"]) == 1
        )
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_unreferenced_source_owns_no_task(tmp_path: Path) -> None:
    document = _document()
    document["widgets"] = {}
    document["tabs"][0]["widgets"] = []
    scheduler, database, _clock, _config = await _make(tmp_path, document)
    try:
        await scheduler.start({})
        runtime = scheduler.runtimes["stub"]
        assert runtime.demand is Demand.NONE
        assert runtime.task is None
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_visible_tab_raises_demand_without_a_second_task(tmp_path: Path) -> None:
    scheduler, database, _clock, _config = await _make(tmp_path, _document())
    try:
        await scheduler.start({})
        runtime = scheduler.runtimes["stub"]
        # Referenced only by an inactive tab: background cadence, one task.
        assert runtime.demand is Demand.BACKGROUND
        assert runtime.interval == 120.0
        first_task = runtime.task
        assert first_task is not None
        scheduler.set_active_tab("systems")
        assert runtime.demand is Demand.ACTIVE
        assert runtime.interval == 30.0
        # Switching tabs never creates a second collection task for the same instance.
        assert runtime.task is first_task
        scheduler.set_active_tab("money")
        assert runtime.demand is Demand.BACKGROUND
        assert runtime.interval == 120.0
        assert runtime.task is first_task
        scheduler.suspend()
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_import_driven_source_polls_nothing(tmp_path: Path) -> None:
    feature = FeatureSpec(
        id="stub",
        sources=(
            SourceSpec(
                type_id="stub.value",
                payload_model=Payload,
                settings_model=Settings,
                payload_schema="stub.value",
                payload_schema_version=1,
                factory=lambda build: StubProvider(settings=_settings(build)),
                cadence=Cadence(import_driven=True, active_seconds=60),
            ),
        ),
        widgets=(_widget(),),
    )
    scheduler, database, _clock, _config = await _make(tmp_path, _document(), feature=feature)
    try:
        await scheduler.start({})
        runtime = scheduler.runtimes["stub"]
        # No cadence at all: an import-driven source has no interval to poll on.
        assert runtime.interval is None
        # It still publishes the current local state once, so a first launch is not blank.
        assert runtime.task is not None
        assert runtime.task.get_name().startswith("source-once")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert runtime.task is None
        assert scheduler.snapshot("stub").payload is not None  # type: ignore[union-attr]

        # A local transaction is the update event.
        scheduler.mark_import_updated("stub")
        assert runtime.task is not None
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert runtime.task is None
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_transient_failure_is_retried_once_then_succeeds(tmp_path: Path) -> None:
    scheduler, database, _clock, _config = await _make(
        tmp_path, _document(fail_with="once_and_transient")
    )
    try:
        await scheduler.start({})
        scheduler.set_active_tab("systems")
        scheduler.suspend()
        await scheduler.refresh_now("stub", force=True)
        envelope = scheduler.snapshot("stub")
        assert envelope is not None
        assert envelope.connectivity is Connectivity.OK
        runtime = scheduler.runtimes["stub"]
        provider = runtime.provider
        assert isinstance(provider, StubProvider)
        assert provider.calls == 2  # one failure, one retry
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_rate_limit_backoff_uses_retry_after(tmp_path: Path) -> None:
    scheduler, database, clock, _config = await _make(tmp_path, _document(fail_with="rate"))
    try:
        await scheduler.start({})
        scheduler.set_active_tab("systems")
        scheduler.suspend()
        await scheduler.refresh_now("stub", force=True)
        envelope = scheduler.snapshot("stub")
        assert envelope is not None
        assert envelope.connectivity is Connectivity.ERROR
        assert envelope.error is not None and envelope.error.category == "rate_limited"
        runtime = scheduler.runtimes["stub"]
        assert runtime.next_due - clock.monotonic() >= 80.0
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_blocked_source_keeps_the_last_good_payload(tmp_path: Path) -> None:
    scheduler, database, _clock, _config = await _make(tmp_path, _document(fail_with=""))
    try:
        await scheduler.start({})
        scheduler.set_active_tab("systems")
        scheduler.suspend()
        await scheduler.refresh_now("stub", force=True)
        good = scheduler.snapshot("stub")
        assert good is not None and good.payload is not None

        runtime = scheduler.runtimes["stub"]
        runtime.provider = FailingProvider(BlockedError("not allowed"))
        await scheduler.refresh_now("stub", force=True)
        after = scheduler.snapshot("stub")
        assert after is not None
        # The valid payload survives: a failed acquisition never replaces data with nothing.
        assert after.payload is not None
        assert after.connectivity is Connectivity.BLOCKED
        assert after.error is not None
        assert after.error.category == "blocked"
        assert after.freshness.value in {"fresh", "stale"}
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_backoff_grows_with_consecutive_failures(tmp_path: Path) -> None:
    scheduler, database, clock, _config = await _make(tmp_path, _document(fail_with="transient"))
    try:
        await scheduler.start({})
        scheduler.set_active_tab("systems")
        runtime = scheduler.runtimes["stub"]
        scheduler.suspend()
        await scheduler.refresh_now("stub", force=True)
        first = runtime.next_due - clock.monotonic()
        clock.advance(first + 1)
        await scheduler.refresh_now("stub", force=True)
        second = runtime.next_due - clock.monotonic()
        assert first >= BACKOFF_BASE * 0.9
        assert second > first
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_offline_suppresses_network_sources(tmp_path: Path) -> None:
    feature = FeatureSpec(
        id="stub",
        sources=(
            SourceSpec(
                type_id="stub.value",
                payload_model=Payload,
                settings_model=Settings,
                payload_schema="stub.value",
                payload_schema_version=1,
                factory=lambda build: StubProvider(settings=_settings(build)),
                capabilities=frozenset({Capability.HTTP_PUBLIC}),
                cadence=Cadence(active_seconds=30),
            ),
        ),
        widgets=(_widget(),),
    )
    scheduler, database, _clock, _config = await _make(tmp_path, _document(), feature=feature)
    scheduler.offline = True
    try:
        await scheduler.start({})
        scheduler.set_active_tab("systems")
        runtime = scheduler.runtimes["stub"]
        scheduler.suspend()
        assert runtime.task is None
        envelope = scheduler.snapshot("stub")
        assert envelope is not None
        assert envelope.connectivity is Connectivity.OFFLINE
        assert runtime.provider is None
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_manual_refresh_is_rate_limited(tmp_path: Path) -> None:
    scheduler, database, _clock, _config = await _make(tmp_path, _document())
    try:
        await scheduler.start({})
        scheduler.set_active_tab("systems")
        scheduler.suspend()
        assert scheduler.request_refresh("stub") is True
        assert scheduler.request_refresh("stub") is False  # throttled within 5 s
        assert scheduler.request_refresh("missing") is False
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_cancelled_state_is_not_written_by_late_results(tmp_path: Path) -> None:
    scheduler, database, _clock, _config = await _make(tmp_path, _document())
    try:
        await scheduler.start({})
        scheduler.set_active_tab("systems")
        runtime = scheduler.runtimes["stub"]
        scheduler.suspend()
        stale_generation = runtime.generation
        runtime.generation += 1
        await scheduler._attempt(runtime, stale_generation)
        envelope = scheduler.snapshot("stub")
        assert envelope is not None
        assert envelope.last_success_at is None
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_persisted_envelope_round_trips_and_recomputes_freshness(tmp_path: Path) -> None:
    scheduler, database, clock, _config = await _make(tmp_path, _document())
    try:
        await scheduler.start({})
        scheduler.set_active_tab("systems")
        scheduler.suspend()
        await scheduler.refresh_now("stub", force=True)
        await scheduler.aclose()
        clock.advance(3600 * 5)
        loaded = await database.load_envelopes({"stub": Payload}, now=clock.now())
        assert "stub" in loaded
        envelope = loaded["stub"]
        assert envelope.payload is not None
        assert envelope.freshness.value in {"stale", "expired"}
    finally:
        await database.aclose()
