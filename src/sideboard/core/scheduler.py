"""Source-instance scheduling.

One task per configured source instance. Cadence is derived from demand (which tab
or rotation slot is actually visible), never from how many widgets are mounted.
Widgets never start timers.

Guarantees implemented here:

* one in-flight acquisition per source instance
* global HTTP concurrency enforced by the shared :class:`HttpLimits`
* one transient retry per refresh, then exponential backoff / circuit state
* ``Retry-After`` honoured; ``ETag``/``Last-Modified`` handed to the provider
* a failed acquisition never replaces a valid payload with an empty success
* import-driven sources poll nothing and update only on a local transaction
* an unreferenced source owns no recurring task
* sleep/wake and long sampling gaps stagger catch-up and mark a history gap
* removal/reconfiguration cancels and closes only the affected instance and
  increments its generation so late results are discarded
* shutdown drains pending writes and reaps owned subprocesses
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from random import Random
from typing import Any

from .capabilities import CommandRunner, FileReader, HttpLimits, HttpReader, HttpScope
from .clock import Clock
from .config import ResolvedConfig, SourceInstance
from .contracts import (
    Capability,
    Connectivity,
    Demand,
    Provider,
    ProviderBuild,
    RuntimeStatus,
    SourceEnvelope,
    SourcePayload,
    SourceStatusSummary,
    freshness_for,
    setup_required,
)
from .demo import synthetic_provider
from .errors import (
    BlockedError,
    ErrorCategory,
    ParseError,
    ProviderError,
    RateLimitedError,
    SideboardError,
    SourceErrorInfo,
    StorageError,
    TransientNetworkError,
    UnauthorizedError,
    UnavailableError,
)
from .history import BoundedHistory
from .storage import Database

__all__ = ["Scheduler", "SourceRuntime", "hash_payload", "map_error_categories"]

log = logging.getLogger("sideboard.scheduler")

TRANSIENT_RETRY_DELAY = 1.5
BACKOFF_BASE = 30.0
BACKOFF_CAP = 15 * 60.0
BLOCKED_BACKOFF = 60 * 60.0
MANUAL_REFRESH_MIN_INTERVAL = 5.0
MANUAL_REFRESH_BURST = 5
MANUAL_REFRESH_WINDOW = 60.0
STALE_INTERVAL_FACTOR = 2.5
DEMO_INTERVAL = 60.0
SLEEP_GAP_SECONDS = 90.0
IDLE_TICK = 5.0
# How long a loop waits when another attempt for the same instance is already in
# flight. A loop must never iterate without awaiting: an unyielding loop starves the
# event loop and therefore starves the very fetch it is waiting for.
SKIP_TICK = 0.5


def map_error_categories(error: BaseException) -> tuple[Connectivity, float]:
    """Map a typed failure to a connectivity state and a backoff in seconds."""
    if isinstance(error, BlockedError | UnauthorizedError):
        return Connectivity.BLOCKED, BLOCKED_BACKOFF
    if isinstance(error, UnavailableError):
        return Connectivity.UNAVAILABLE, BACKOFF_BASE
    if isinstance(error, RateLimitedError):
        return Connectivity.ERROR, max(error.retry_after_seconds or BACKOFF_BASE * 2, 5.0)
    if isinstance(error, ParseError):
        return Connectivity.ERROR, BACKOFF_BASE * 4
    if isinstance(error, TransientNetworkError):
        return Connectivity.ERROR, BACKOFF_BASE
    if isinstance(error, ProviderError):
        return Connectivity.ERROR, BACKOFF_BASE
    return Connectivity.ERROR, BACKOFF_BASE


@dataclass(slots=True)
class SourceRuntime:
    """Mutable runtime state for one configured source instance."""

    instance: SourceInstance
    envelope: SourceEnvelope
    demand: Demand = Demand.BACKGROUND
    generation: int = 0
    provider: Provider | None = None
    task: asyncio.Task[None] | None = None
    in_flight: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    failures: int = 0
    next_due: float = 0.0
    manual_times: list[float] = field(default_factory=list)
    refresh_requested: bool = False
    last_duration: float = 0.0
    random: Random = field(default_factory=Random)
    wake_reset_pending: bool = False

    @property
    def source_id(self) -> str:
        return self.instance.source_id

    @property
    def interval(self) -> float | None:
        return self.instance.cadence.seconds_for(self.demand)


class Scheduler:
    """Owns collection tasks at application scope."""

    def __init__(
        self,
        *,
        database: Database,
        clock: Clock,
        history: BoundedHistory,
        config: ResolvedConfig,
        commands: CommandRunner | None = None,
        file_roots: Mapping[str, tuple[str, ...]] | None = None,
        limits: HttpLimits | None = None,
        offline: bool = False,
        demo: bool = False,
        root_dir: str = "",
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.database = database
        self.clock = clock
        self.history = history
        self.config = config
        self.commands = commands
        self.file_roots = dict(file_roots or {})
        self.limits = limits or HttpLimits()
        self.offline = offline or config.offline
        self.demo = demo or config.demo
        self.root_dir = root_dir
        self._client_factory = client_factory
        self._runtimes: dict[str, SourceRuntime] = {}
        self._subscribers: list[Callable[[str], None]] = []
        self._active_tab: str | None = None
        self._closed = False
        self._shared_client: Any = None
        self._client_lock = asyncio.Lock()
        self._persist_tasks: set[asyncio.Task[None]] = set()
        self._last_tick: float | None = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def runtimes(self) -> Mapping[str, SourceRuntime]:
        return self._runtimes

    async def start(self, records: Mapping[str, SourceEnvelope] | None = None) -> None:
        """Seed from cache, then start tasks for instances that have demand."""
        self._seed_runtimes(records or {})
        for runtime in tuple(self._runtimes.values()):
            if runtime.instance.import_driven:
                # An import-driven source owns no recurring task, but it must publish the
                # current local state once so a first launch is not blank.
                if runtime.instance.enabled and runtime.demand is not Demand.NONE:
                    self._spawn_oneshot(runtime)
                continue
            self._ensure_task(runtime)

    def _seed_runtimes(self, records: Mapping[str, SourceEnvelope]) -> None:
        for source_id, instance in self.config.sources.items():
            if source_id in self._runtimes:
                continue
            cached = records.get(source_id)
            runtime = SourceRuntime(
                instance=instance,
                envelope=cached or self._empty_envelope(instance),
                random=Random(zlib.crc32(source_id.encode())),
                demand=self.config.demand_for_source(source_id, active_tab=self._active_tab),
            )
            runtime.next_due = self.clock.monotonic()
            self._runtimes[source_id] = runtime

    def _empty_envelope(self, instance: SourceInstance) -> SourceEnvelope:
        connectivity = Connectivity.UNAVAILABLE if instance.enabled else Connectivity.UNCONFIGURED
        return SourceEnvelope(
            source_id=instance.source_id,
            payload_schema=instance.payload_schema,
            payload_schema_version=instance.spec.payload_schema_version,
            connectivity=connectivity,
            stale_after_seconds=instance.cadence.seconds_for(Demand.AMBIENT),
        )

    # ------------------------------------------------------------------ demand

    def set_active_tab(self, tab_id: str | None) -> None:
        """Recompute demand from the visible view and (re)start tasks as needed."""
        self._active_tab = tab_id
        now = self.clock.monotonic()
        for runtime in tuple(self._runtimes.values()):
            demand = self.config.demand_for_source(runtime.source_id, active_tab=tab_id)
            if demand is not runtime.demand:
                runtime.demand = demand
                interval = runtime.interval
                if interval is not None:
                    runtime.next_due = min(runtime.next_due, now + interval)
            self._ensure_task(runtime)

    def set_offline(self, offline: bool) -> None:
        self.offline = offline
        for runtime in tuple(self._runtimes.values()):
            self._ensure_task(runtime)

    def suspend(self) -> None:
        """Stop recurring collection without discarding state, cache or providers.

        Used by non-interactive commands that drive acquisitions explicitly, and by
        tests that want a deterministic call count instead of a timer race.
        """
        for runtime in self._runtimes.values():
            self._cancel_task(runtime)

    def resume(self) -> None:
        """Restart recurring collection for instances that have demand."""
        for runtime in tuple(self._runtimes.values()):
            self._ensure_task(runtime)

    # ------------------------------------------------------------------ reads

    def snapshot(self, source_id: str) -> SourceEnvelope | None:
        runtime = self._runtimes.get(source_id)
        return self._live_envelope(runtime) if runtime is not None else None

    def snapshots(self) -> Mapping[str, SourceEnvelope]:
        return {source_id: self._live_envelope(rt) for source_id, rt in self._runtimes.items()}

    def _live_envelope(self, runtime: SourceRuntime) -> SourceEnvelope:
        now = self.clock.now()
        freshness = freshness_for(runtime.envelope, now)
        if freshness is runtime.envelope.freshness:
            return runtime.envelope
        return replace(runtime.envelope, freshness=freshness)

    def envelopes_for(self, source_ids: tuple[str, ...]) -> dict[str, SourceEnvelope | None]:
        return {source_id: self.snapshot(source_id) for source_id in source_ids}

    def subscribe(self, callback: Callable[[str], None]) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(callback)

        return unsubscribe

    def notify(self, source_id: str) -> None:
        for callback in tuple(self._subscribers):
            try:
                callback(source_id)
            except Exception:  # pragma: no cover - a UI callback must not kill collection
                log.exception("subscriber failed for %s", source_id)

    # ------------------------------------------------------------------ control

    def request_refresh(self, source_id: str, *, force: bool = False) -> bool:
        """Rate-limited manual refresh. Returns False when throttled or unknown."""
        runtime = self._runtimes.get(source_id)
        if runtime is None or not runtime.instance.enabled:
            return False
        now = self.clock.monotonic()
        runtime.manual_times = [t for t in runtime.manual_times if now - t < MANUAL_REFRESH_WINDOW]
        if not force:
            if (
                runtime.manual_times
                and now - runtime.manual_times[-1] < MANUAL_REFRESH_MIN_INTERVAL
            ):
                return False
            if len(runtime.manual_times) >= MANUAL_REFRESH_BURST:
                return False
        runtime.manual_times.append(now)
        runtime.refresh_requested = True
        runtime.next_due = now
        if runtime.instance.import_driven:
            self._spawn_oneshot(runtime)
        else:
            self._ensure_task(runtime)
        return True

    async def refresh_now(self, source_id: str, *, force: bool = False) -> bool:
        """Collect immediately (used by the CLI and by import-driven updates)."""
        runtime = self._runtimes.get(source_id)
        if runtime is None or not runtime.instance.enabled:
            return False
        if not force and not self.request_refresh(source_id):
            return False
        runtime.refresh_requested = False
        # Whether this call or a concurrent one performs the acquisition, the refresh
        # request itself was accepted.
        await self._fetch_once(runtime, manual=True)
        return True

    def mark_import_updated(self, source_id: str) -> None:
        """Import-driven sources update on a successful local transaction."""
        runtime = self._runtimes.get(source_id)
        if runtime is None:
            return
        runtime.refresh_requested = True
        runtime.next_due = self.clock.monotonic()
        if runtime.instance.import_driven:
            self._spawn_oneshot(runtime)
        else:
            self._ensure_task(runtime)

    def _spawn_oneshot(self, runtime: SourceRuntime) -> None:
        """One bounded acquisition without a recurring task (import-driven sources)."""
        if runtime.task is not None and not runtime.task.done():
            return
        runtime.refresh_requested = False
        runtime.task = asyncio.create_task(
            self._oneshot(runtime), name=f"source-once:{runtime.source_id}"
        )

    async def _oneshot(self, runtime: SourceRuntime) -> None:
        try:
            await self._fetch_once(runtime, manual=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            log.exception("one-shot collection failed for %s", runtime.source_id)
        finally:
            self._release_task(runtime)

    # ------------------------------------------------------------------ reconfigure

    async def reconfigure(self, config: ResolvedConfig) -> dict[str, str]:
        """Reconcile only changed instances. Returns source_id -> action."""
        self.config = config
        self.offline = config.offline or self.offline
        self.demo = config.demo or self.demo
        actions: dict[str, str] = {}

        for source_id in tuple(self._runtimes):
            instance = config.sources.get(source_id)
            runtime = self._runtimes[source_id]
            if instance is None:
                await self._stop_runtime(source_id)
                actions[source_id] = "removed"
                continue
            if self._instance_changed(runtime.instance, instance):
                await self._stop_runtime(source_id, keep_envelope=True)
                actions[source_id] = "updated"

        for source_id in config.sources:
            if source_id not in self._runtimes:
                actions.setdefault(source_id, "added")

        self._seed_runtimes({})
        for runtime in tuple(self._runtimes.values()):
            runtime.demand = config.demand_for_source(
                runtime.source_id, active_tab=self._active_tab
            )
            if runtime.instance.enabled:
                self._ensure_task(runtime)
            else:
                self._cancel_task(runtime)
                actions.setdefault(runtime.source_id, "disabled")
        return actions

    @staticmethod
    def _instance_changed(old: SourceInstance, new: SourceInstance) -> bool:
        return (
            old.type_id != new.type_id
            or old.enabled != new.enabled
            or old.settings != new.settings
            or old.cadence != new.cadence
            or old.retention_seconds != new.retention_seconds
        )

    async def _stop_runtime(self, source_id: str, *, keep_envelope: bool = False) -> None:
        runtime = self._runtimes.pop(source_id, None)
        if runtime is None:
            return
        runtime.generation += 1
        runtime.refresh_requested = False
        self._cancel_task(runtime)
        await self._close_provider(runtime)
        self.history.forget_source(source_id)
        if keep_envelope:
            restored = self._seed_one(source_id, runtime)
            if restored is not None:
                self._runtimes[source_id] = restored

    def _seed_one(self, source_id: str, previous: SourceRuntime) -> SourceRuntime | None:
        instance = self.config.sources.get(source_id)
        if instance is None:
            return None
        runtime = SourceRuntime(
            instance=instance,
            envelope=previous.envelope,
            generation=previous.generation,
            random=Random(zlib.crc32(source_id.encode())),
            demand=self.config.demand_for_source(source_id, active_tab=self._active_tab),
        )
        runtime.next_due = self.clock.monotonic()
        return runtime

    def _cancel_task(self, runtime: SourceRuntime) -> None:
        task, runtime.task = runtime.task, None
        if task is not None and not task.done():
            task.cancel()

    async def _close_provider(self, runtime: SourceRuntime) -> None:
        provider, runtime.provider = runtime.provider, None
        if provider is None:
            return
        with contextlib.suppress(Exception):
            await provider.aclose()

    def _ensure_task(self, runtime: SourceRuntime) -> None:
        if not runtime.instance.enabled or runtime.instance.import_driven:
            return
        if self.offline and self._requires_network(runtime.instance):
            runtime.envelope = replace(
                runtime.envelope,
                connectivity=Connectivity.OFFLINE,
                freshness=freshness_for(runtime.envelope, self.clock.now()),
            )
            self._cancel_task(runtime)
            return
        if runtime.demand is Demand.NONE and not runtime.refresh_requested:
            return
        if runtime.task is None or runtime.task.done():
            runtime.task = asyncio.create_task(
                self._loop(runtime), name=f"source:{runtime.source_id}"
            )

    @staticmethod
    def _requires_network(instance: SourceInstance) -> bool:
        return bool(instance.spec.capabilities & {Capability.HTTP_PUBLIC, Capability.HTTP_LOCAL})

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for runtime in tuple(self._runtimes.values()):
            runtime.generation += 1
            self._cancel_task(runtime)
        for runtime in tuple(self._runtimes.values()):
            await self._close_provider(runtime)
        if self._persist_tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.gather(*tuple(self._persist_tasks), return_exceptions=True), timeout=5.0
                )
        with contextlib.suppress(Exception):
            await self.history.flush()
        if self.commands is not None:
            await self.commands.aclose()
        async with self._client_lock:
            client, self._shared_client = self._shared_client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
        self._runtimes.clear()

    # ------------------------------------------------------------------ loop

    async def _loop(self, runtime: SourceRuntime) -> None:
        try:
            while True:
                now = self.clock.monotonic()
                await self._detect_tick_gap(now)
                if runtime.refresh_requested:
                    runtime.refresh_requested = False
                    await self._fetch_once(runtime, manual=True)
                    continue
                if runtime.demand is Demand.NONE:
                    self._release_task(runtime)
                    return
                wait = runtime.next_due - self.clock.monotonic()
                if wait > 0:
                    await self.clock.sleep(min(wait, IDLE_TICK))
                    continue
                if not await self._fetch_once(runtime):
                    # Another attempt for this instance is already running (a manual
                    # refresh, an import update, or a previous slow acquisition). Yield
                    # before re-checking: the in-flight attempt owns the reschedule.
                    await self.clock.sleep(SKIP_TICK)
        except asyncio.CancelledError:
            self._release_task(runtime)
            raise
        except Exception:  # pragma: no cover - defensive: a loop must never die silently
            log.exception("source loop crashed for %s", runtime.source_id)
            self._release_task(runtime)

    @staticmethod
    def _release_task(runtime: SourceRuntime) -> None:
        with contextlib.suppress(RuntimeError):
            if runtime.task is asyncio.current_task():
                runtime.task = None

    async def _detect_tick_gap(self, now: float) -> None:
        """Sleep/wake or a long sampling gap: stagger catch-up, do not replay ticks."""
        previous, self._last_tick = self._last_tick, now
        if previous is None:
            return
        gap = now - previous
        if gap <= SLEEP_GAP_SECONDS:
            return
        for runtime in self._runtimes.values():
            runtime.wake_reset_pending = True
            runtime.next_due = min(runtime.next_due, now + runtime.random.uniform(0.0, 5.0))

    async def _fetch_once(self, runtime: SourceRuntime, *, manual: bool = False) -> bool:
        """Run one acquisition if nothing else is already running for this instance.

        Returns whether this call performed the attempt. A ``False`` result means a
        concurrent acquisition owns the instance and is responsible for rescheduling it,
        and the caller must yield rather than re-check in a tight loop.
        """
        if runtime.in_flight:
            return False
        if runtime.interval is None and not manual:
            return False
        if runtime.demand is Demand.NONE and not manual:
            runtime.next_due = self.clock.monotonic() + IDLE_TICK
            return False
        async with runtime.lock:
            if runtime.in_flight:
                return False
            runtime.in_flight = True
            generation = runtime.generation
            started = self.clock.monotonic()
            try:
                await self._attempt(runtime, generation)
            finally:
                runtime.in_flight = False
                runtime.last_duration = self.clock.monotonic() - started
                self.notify(runtime.source_id)
        return True

    async def _attempt(self, runtime: SourceRuntime, generation: int) -> None:
        now_mono = self.clock.monotonic()
        now = self.clock.now()
        previous_failures = runtime.failures
        try:
            provider = runtime.provider
            if provider is None:
                provider = self._make_provider(runtime)
                runtime.provider = provider
            build = await self._build_context(runtime)
            payload = await self._fetch_with_retry(provider, build)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if generation != runtime.generation:
                return
            self._record_failure(runtime, error, now_mono, now)
            return
        if generation != runtime.generation:
            return
        if payload is None:
            self._record_failure(runtime, ParseError("provider returned no payload"), now_mono, now)
            return
        self._record_success(runtime, payload, now_mono, now, prior_failures=previous_failures)

    def _make_provider(self, runtime: SourceRuntime) -> Provider:
        spec = runtime.instance.spec
        if self.demo:
            synthetic = synthetic_provider(spec, runtime.instance.settings)
            if synthetic is None:
                raise UnavailableError(
                    f"{spec.type_id} has no synthetic demo collector; "
                    "the source is unavailable in demo mode"
                )
            return synthetic
        build = ProviderBuild(
            source_id=runtime.source_id,
            settings=runtime.instance.settings,
            clock=self.clock,
            offline=self.offline,
            demo=self.demo,
            root_dir=self.root_dir,
        )
        return spec.factory(build)

    async def _fetch_with_retry(self, provider: Provider, build: ProviderBuild) -> SourcePayload:
        try:
            return await provider.fetch(build)
        except TransientNetworkError:
            delay = TRANSIENT_RETRY_DELAY + build_delay_jitter(build.source_id)
            await self.clock.sleep(delay)
            return await provider.fetch(build)

    async def _build_context(self, runtime: SourceRuntime) -> ProviderBuild:
        instance = runtime.instance
        capabilities = instance.spec.capabilities
        return ProviderBuild(
            source_id=instance.source_id,
            settings=instance.settings,
            clock=self.clock,
            http=await self._http_for(instance),
            files=self._files_for(instance),
            commands=(self.commands if Capability.CLI in capabilities else None),
            database=(self.database if Capability.REPOSITORY in capabilities else None),
            previous=runtime.envelope if runtime.envelope.has_payload else None,
            offline=self.offline,
            demo=self.demo,
            root_dir=self.root_dir,
        )

    async def _http_for(self, instance: SourceInstance) -> HttpReader | None:
        capabilities = instance.spec.capabilities
        if Capability.HTTP_PUBLIC in capabilities:
            scope = HttpScope.PUBLIC
        elif Capability.HTTP_LOCAL in capabilities:
            scope = HttpScope.LOCAL
        else:
            return None
        allow_hosts = getattr(instance.settings, "allow_hosts", ()) or ()
        if not isinstance(allow_hosts, tuple | list):
            allow_hosts = ()
        return HttpReader(
            client=await self._client(),
            scope=scope,
            allow_hosts=frozenset(str(host) for host in allow_hosts),
            limits=self.limits,
        )

    async def _client(self) -> Any:
        async with self._client_lock:
            if self._shared_client is None:
                if self._client_factory is None:  # pragma: no cover - host injects one
                    raise SideboardError("no HTTP client factory configured")
                self._shared_client = self._client_factory()
            return self._shared_client

    def _files_for(self, instance: SourceInstance) -> FileReader | None:
        if Capability.FILE not in instance.spec.capabilities:
            return None
        return FileReader(allowed_roots=tuple(self.file_roots.get(instance.source_id, ())))

    # ------------------------------------------------------------------ record

    def _record_success(
        self,
        runtime: SourceRuntime,
        payload: SourcePayload,
        now_mono: float,
        now: datetime,
        *,
        prior_failures: int = 0,
    ) -> None:
        previous = runtime.envelope
        not_modified = payload is previous.payload
        content_hash = previous.content_hash if not_modified else hash_payload(payload)
        envelope = SourceEnvelope(
            source_id=runtime.source_id,
            payload_schema=runtime.instance.payload_schema,
            payload_schema_version=runtime.instance.spec.payload_schema_version,
            generation=runtime.generation,
            payload=payload,
            connectivity=Connectivity.OK,
            last_attempt_at=now,
            last_success_at=now,
            fetched_at=now,
            as_of=payload.as_of,
            precision=payload.precision,
            valid_until=payload.valid_until or previous.valid_until,
            stale_after_seconds=self._stale_after(runtime),
            content_hash=content_hash,
            validator_etag=previous.validator_etag,
            validator_last_modified=previous.validator_last_modified,
            provenance=payload.provenance,
            coverage=payload.coverage,
            error=None,
        )
        envelope = replace(envelope, freshness=freshness_for(envelope, now))
        gap = runtime.wake_reset_pending or prior_failures > 0
        runtime.wake_reset_pending = False
        runtime.envelope = envelope
        runtime.failures = 0
        self._record_metrics(runtime, payload, gap=gap)
        self._schedule_next(runtime, now_mono)
        self._spawn_persist(runtime, envelope)

    def _record_failure(
        self, runtime: SourceRuntime, error: BaseException, now_mono: float, now: datetime
    ) -> None:
        connectivity, backoff = map_error_categories(error)
        if self.offline and connectivity is Connectivity.ERROR:
            connectivity = Connectivity.OFFLINE
        info = _error_info(error, now)
        runtime.failures += 1
        if runtime.failures > 1:
            backoff = min(BACKOFF_CAP, backoff * (2 ** min(runtime.failures - 1, 6)))
        envelope = replace(
            runtime.envelope,
            connectivity=connectivity,
            generation=runtime.generation,
            last_attempt_at=now,
            error=info,
        )
        envelope = replace(envelope, freshness=freshness_for(envelope, now))
        runtime.envelope = envelope
        runtime.next_due = now_mono + backoff * runtime.random.uniform(0.9, 1.15)
        log.info("source %s failed (%s): %s", runtime.source_id, info.category, info.message)
        self._spawn_persist(runtime, envelope)

    def _schedule_next(self, runtime: SourceRuntime, now_mono: float) -> None:
        interval = runtime.interval
        if interval is None:
            runtime.next_due = now_mono + IDLE_TICK
            return
        if self.demo:
            interval = min(interval, DEMO_INTERVAL)
        runtime.next_due = now_mono + max(1.0, interval * runtime.random.uniform(0.9, 1.1))

    def _stale_after(self, runtime: SourceRuntime) -> float | None:
        interval = runtime.instance.cadence.seconds_for(runtime.demand)
        if interval is None:
            return None
        return max(interval * STALE_INTERVAL_FACTOR, 60.0)

    def _record_metrics(self, runtime: SourceRuntime, payload: SourcePayload, *, gap: bool) -> None:
        extractor = runtime.instance.spec.metrics_extractor
        if extractor is None:
            return
        try:
            readings = extractor(payload)
        except Exception:  # pragma: no cover - extractor is pure by contract
            log.exception("metrics extractor failed for %s", runtime.source_id)
            return
        wall = self.clock.now().timestamp()
        for reading in readings:
            self.history.record(
                runtime.source_id,
                reading.metric_id,
                wall,
                reading.value,
                unit=reading.unit,
                label=reading.label,
                gap=gap or reading.gap,
            )

    def _spawn_persist(self, runtime: SourceRuntime, envelope: SourceEnvelope) -> None:
        task = asyncio.create_task(
            self._persist(runtime, envelope), name=f"persist:{runtime.source_id}"
        )
        self._persist_tasks.add(task)
        task.add_done_callback(self._persist_tasks.discard)

    async def _persist(self, runtime: SourceRuntime, envelope: SourceEnvelope) -> None:
        try:
            await self.database.save_envelope(
                envelope,
                updated_at=self.clock.now().isoformat(),
                retention_seconds=runtime.instance.retention_seconds,
            )
        except ValueError, sqlite3.ProgrammingError, StorageError:
            # Shutdown closed the database under an in-flight write. The data was
            # collected and shown; there is nothing to report and nothing to retry.
            log.debug("database closed while persisting %s", runtime.source_id)
        except Exception:  # pragma: no cover - storage failure must not kill collection
            log.exception("could not persist envelope for %s", runtime.source_id)

    # ------------------------------------------------------------------ diagnostics

    def statuses(self) -> tuple[SourceStatusSummary, ...]:
        out: list[SourceStatusSummary] = []
        now_mono = self.clock.monotonic()
        for runtime in self._runtimes.values():
            envelope = self._live_envelope(runtime)
            out.append(
                SourceStatusSummary(
                    source_id=runtime.source_id,
                    type_id=runtime.instance.type_id,
                    enabled=runtime.instance.enabled,
                    demand=runtime.demand,
                    connectivity=envelope.connectivity,
                    freshness=envelope.freshness,
                    last_attempt_at=envelope.last_attempt_at,
                    last_success_at=envelope.last_success_at,
                    as_of=envelope.as_of,
                    precision=envelope.precision,
                    error_category=str(envelope.error.category) if envelope.error else None,
                    error_message=envelope.error.message[:200] if envelope.error else None,
                    interval_seconds=runtime.interval,
                    in_flight=runtime.in_flight,
                    next_due_seconds=max(0.0, runtime.next_due - now_mono),
                    failures=runtime.failures,
                    has_payload=envelope.has_payload,
                    partial=envelope.is_partial,
                    provenance=envelope.provenance,
                    setup_hint=runtime.instance.spec.setup_hint,
                    requires_setup=setup_required(runtime.instance.spec, runtime.instance.settings),
                    retention_seconds=runtime.instance.retention_seconds,
                )
            )
        return tuple(sorted(out, key=lambda s: s.source_id))

    def runtime_status(self) -> RuntimeStatus:
        summaries = self.statuses()
        stale = sum(1 for s in summaries if s.freshness.value in {"stale", "expired"})
        unavailable = sum(
            1 for s in summaries if s.connectivity.value in {"unavailable", "blocked", "error"}
        )
        return RuntimeStatus(
            sources=summaries,
            offline=self.offline,
            demo=self.demo,
            config_revision=self.config.revision,
            active_tab=self._active_tab,
            stale_count=stale,
            unavailable_count=unavailable,
            pending_writes=self.history.pending,
            config_path=str(self.config.paths.config_file),
            database_path=str(self.config.paths.database_file),
            log_path=str(self.config.paths.log_dir),
        )


def build_delay_jitter(source_id: str) -> float:
    return Random(zlib.crc32(f"retry:{source_id}".encode())).uniform(0.0, 1.0)


def hash_payload(payload: SourcePayload) -> str:
    encoded = json.dumps(payload.model_dump(mode="json"), sort_keys=True, default=str).encode()
    return f"{zlib.crc32(encoded):08x}"


def _error_info(error: BaseException, now: datetime) -> SourceErrorInfo:
    if isinstance(error, SideboardError):
        info = error.to_info()
        info.at = now.astimezone(UTC).isoformat()
        return info
    return SourceErrorInfo(
        category=ErrorCategory.INTERNAL,
        message=type(error).__name__,
        at=now.astimezone(UTC).isoformat(),
    )
