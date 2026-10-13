"""Read-only Docker adapter.

One bounded acquisition per refresh:

1. revalidate the pinned context endpoint on every refresh (a later ``docker context
   update`` must not silently redirect collection)
2. ``container ls --all`` with a fixed narrow template for inventory
3. one batched ``container stats --no-stream`` call for the selected running IDs
4. one narrow ``container inspect --format`` call for restarts/exit/health status only
5. optional ``system df`` on a slow, separate cadence

Concurrency is one child process at a time (the host injects a single-slot
:class:`~sideboard.core.capabilities.CommandRunner`), every call has a timeout and an
output cap, and no call is ever left running.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ...core.contracts import (
    Cadence,
    Capability,
    Coverage,
    MetricReading,
    Precision,
    ProviderBuild,
    SourcePayload,
    SourceSpec,
)
from ...core.errors import UnavailableError
from .commands import (
    CONTEXT_INSPECT_TEMPLATE,
    ENGINE_INFO_TEMPLATE,
    HEALTH_TEMPLATE,
    INVENTORY_TEMPLATE,
    ContextEndpoint,
    build_argv,
    classify_cli_failure,
    deny_remote_endpoint,
    docker_env,
    parse_context_endpoint,
    parse_engine_info,
    parse_health_line,
    parse_inventory_line,
)
from .config import DockerSettings
from .models import ContainerReading, DockerPayload, EngineInfo, StorageUsage
from .units import parse_cpu_percent, parse_io_pair, parse_size, parse_size_pair

log = logging.getLogger("sideboard.docker")

STATS_TEMPLATE = "{{json .}}"
SHORT_ID_LENGTH = 12
STORAGE_TEMPLATE = "{{json .}}"

_HEALTH_VALUES = {"healthy", "unhealthy", "starting"}


@dataclass(slots=True)
class _RateBaseline:
    net_rx: int | None
    net_tx: int | None
    block_read: int | None
    block_write: int | None


@dataclass(frozen=True, slots=True)
class _StatFields:
    cpu_percent: float | None = None
    memory_used_bytes: int | None = None
    memory_limit_bytes: int | None = None
    memory_percent: float | None = None
    net_rx_bytes: int | None = None
    net_tx_bytes: int | None = None
    net_rx_bps: float | None = None
    net_tx_bps: float | None = None
    block_read_bytes: int | None = None
    block_write_bytes: int | None = None
    block_read_bps: float | None = None
    block_write_bps: float | None = None
    rate_gap: bool = False
    note: str | None = None


class DockerProvider:
    """Collects local engine state without ever mutating it."""

    def __init__(self, *, settings: DockerSettings) -> None:
        self.settings = settings
        self._endpoint: ContextEndpoint | None = None
        self._engine: EngineInfo | None = None
        self._engine_at: datetime | None = None
        self._rate_baselines: dict[str, _RateBaseline] = {}
        self._net_interval: float | None = None
        self._restart_baselines: dict[str, int] = {}
        self._storage: tuple[StorageUsage, ...] = ()
        self._storage_at: datetime | None = None
        self._stats_at: datetime | None = None
        self._metadata_at: datetime | None = None

    # ------------------------------------------------------------------ plumbing

    async def _run(self, context: ProviderBuild, *args: str, timeout: float | None = None) -> str:
        commands = context.commands
        if commands is None:
            raise UnavailableError("this collector requires the approved CLI capability")
        settings = context.settings
        assert isinstance(settings, DockerSettings)
        result = await commands.run(
            build_argv(settings.context, *args),
            timeout=timeout or settings.process_timeout_seconds,
            env=docker_env(),
        )
        if not result.ok:
            raise classify_cli_failure(result.stderr, result.returncode)
        if result.truncated:
            raise UnavailableError("docker output exceeded the configured cap", state="truncated")
        return result.stdout

    async def _validate_endpoint(self, context: ProviderBuild) -> ContextEndpoint:
        settings = context.settings
        assert isinstance(settings, DockerSettings)
        if not settings.context:
            raise UnavailableError(
                "no Docker context is pinned; run `sideboard doctor --docker` to list contexts, "
                "then set sources.docker.settings.context",
                state="no_context",
            )
        raw = await self._run(
            context,
            "context",
            "inspect",
            settings.context,
            "--format",
            CONTEXT_INSPECT_TEMPLATE,
        )
        endpoint = parse_context_endpoint(raw)
        # Remote engines are refused regardless of the local_only flag: a setting may
        # narrow scope, never widen it.
        deny_remote_endpoint(endpoint)
        self._endpoint = endpoint
        return endpoint

    # ------------------------------------------------------------------ fetch

    async def fetch(self, context: ProviderBuild) -> DockerPayload:
        settings = context.settings
        assert isinstance(settings, DockerSettings)
        endpoint = await self._validate_endpoint(context)
        now = context.clock.now()

        raw_inventory = await self._run(
            context, "container", "ls", "--all", "--format", INVENTORY_TEMPLATE
        )
        rows = [row for line in raw_inventory.splitlines() if (row := parse_inventory_line(line))]
        truncated = len(rows) > settings.inventory_limit
        rows = rows[: settings.inventory_limit]
        if settings.include_projects:
            allowed = set(settings.include_projects)
            rows = [row for row in rows if row.get("project") in allowed]

        containers = [self._base_container(row) for row in rows if row.get("id")]
        engine = await self._engine_info(context, endpoint, now)
        containers, stats_truncated = await self._with_stats(context, containers, now, engine)
        containers, metadata_note = await self._with_metadata(context, containers, now)

        storage: tuple[StorageUsage, ...] = ()
        if settings.include_storage:
            stale = (
                self._storage_at is None
                or (now - self._storage_at).total_seconds() >= settings.storage_seconds
            )
            if stale:
                # Optional, slow collection: never on the 15 s resource path.
                self._storage = await self._storage_usage(context)
                self._storage_at = now
            storage = self._storage

        notes: list[str] = []
        if stats_truncated:
            notes.append(f"stats limited to {settings.stats_batch_limit} running containers")
        if truncated:
            notes.append(f"inventory truncated to {settings.inventory_limit} containers")
        expected = set(settings.expected_running)
        unexpected = sorted(
            container.name
            for container in containers
            if container.is_unexpected_exit and container.name in expected
        )
        if unexpected:
            notes.append("expected running but stopped: " + ", ".join(unexpected))
        rate_gaps = sorted(container.name for container in containers if container.rate_gap)
        if rate_gaps:
            notes.append(f"rate baseline reset for: {', '.join(rate_gaps)}")
        if metadata_note:
            notes.append(metadata_note)

        running = [container for container in containers if container.running]
        return DockerPayload(
            engine=engine,
            containers=tuple(containers),
            storage=storage,
            stats_sampled_at=self._stats_at,
            metadata_sampled_at=self._metadata_at,
            stats_available=bool(running) and self._stats_at is not None,
            storage_available=bool(storage),
            truncated=truncated or stats_truncated,
            expected_running=tuple(settings.expected_running),
            notes=tuple(notes),
            as_of=now,
            precision=Precision.INSTANT,
            valid_until=now + timedelta(seconds=max(60.0, settings.stats_seconds * 3)),
            provenance=f"docker-cli:{endpoint.name}",
            coverage=Coverage(
                expected=len(containers),
                observed=len(running),
                missing=tuple(container.name for container in containers if not container.running),
                notes=f"{len(running)} running, {len(containers) - len(running)} not running",
            ),
        )

    @staticmethod
    def _base_container(row: dict[str, str]) -> ContainerReading:
        state = (row.get("state") or "unknown").lower()
        return ContainerReading(
            id=(row.get("id") or "")[:SHORT_ID_LENGTH],
            name=(row.get("name") or "unnamed")[:64],
            image=(row.get("image") or None),
            state=state,
            status_text=(row.get("status") or None),
            compose_project=row.get("project") or None,
            compose_service=row.get("service") or None,
            running=state == "running",
        )

    async def _engine_info(
        self, context: ProviderBuild, endpoint: ContextEndpoint, now: datetime
    ) -> EngineInfo:
        settings = context.settings
        assert isinstance(settings, DockerSettings)
        engine, engine_at = self._engine, self._engine_at
        if engine is not None and engine_at is not None:
            fresh = (now - engine_at).total_seconds() < settings.metadata_seconds
            if fresh:
                return engine
        raw = await self._run(context, "info", "--format", ENGINE_INFO_TEMPLATE)
        parsed = parse_engine_info(settings.context, raw)
        cpu_count = parsed.get("cpu_count")
        engine = EngineInfo(
            context=settings.context,
            endpoint=endpoint.endpoint,
            name=_str_or_none(parsed.get("name")),
            server_version=_str_or_none(parsed.get("server_version")),
            operating_system=_str_or_none(parsed.get("operating_system")),
            cpu_count=cpu_count if isinstance(cpu_count, int) else None,
            is_local=endpoint.is_local,
        )
        self._engine = engine
        self._engine_at = now
        return engine

    # ------------------------------------------------------------------ stats

    async def _with_stats(
        self,
        context: ProviderBuild,
        containers: list[ContainerReading],
        now: datetime,
        engine: EngineInfo,
    ) -> tuple[list[ContainerReading], bool]:
        settings = context.settings
        assert isinstance(settings, DockerSettings)
        running = [container for container in containers if container.running]
        selected = running[: settings.stats_batch_limit]
        truncated = len(running) > len(selected)
        if not selected:
            self._stats_at = now
            self._net_interval = None
            return containers, False

        raw = await self._run(
            context,
            "container",
            "stats",
            "--no-stream",
            "--format",
            STATS_TEMPLATE,
            *[container.id for container in selected],
        )
        elapsed = (now - self._stats_at).total_seconds() if self._stats_at else None
        by_key: dict[str, _StatFields] = {}
        for line in raw.splitlines():
            parsed = parse_inventory_line(line)
            if not parsed:
                continue
            key = parsed.get("Container") or ""
            if not key:
                continue
            container = next(
                (item for item in selected if item.id == key or item.name == key), None
            )
            if container is None:
                continue
            by_key[container.id] = self._stat_fields(parsed, container, elapsed, engine)
        self._stats_at = now
        self._metadata_at = self._metadata_at

        updated: list[ContainerReading] = []
        for container in containers:
            fields = by_key.get(container.id)
            if fields is None:
                updated.append(container)
                continue
            updated.append(
                container.model_copy(
                    update={
                        "cpu_percent": fields.cpu_percent,
                        "cpu_percent_of_engine": _engine_normalised(
                            fields.cpu_percent, engine.cpu_count
                        ),
                        "memory_used_bytes": fields.memory_used_bytes,
                        "memory_limit_bytes": fields.memory_limit_bytes,
                        "memory_percent": fields.memory_percent,
                        "net_rx_bytes": fields.net_rx_bytes,
                        "net_tx_bytes": fields.net_tx_bytes,
                        "net_rx_bps": fields.net_rx_bps,
                        "net_tx_bps": fields.net_tx_bps,
                        "block_read_bytes": fields.block_read_bytes,
                        "block_write_bytes": fields.block_write_bytes,
                        "block_read_bps": fields.block_read_bps,
                        "block_write_bps": fields.block_write_bps,
                        "rate_gap": fields.rate_gap,
                        "note": fields.note,
                    }
                )
            )
        return updated, truncated

    def _stat_fields(
        self,
        parsed: dict[str, str],
        container: ContainerReading,
        elapsed: float | None,
        engine: EngineInfo,
    ) -> _StatFields:
        cpu_raw = parse_cpu_percent(parsed.get("CPUPerc"))
        memory_used, memory_limit = parse_size_pair(parsed.get("MemUsage"))
        memory_percent = parse_cpu_percent(parsed.get("MemPerc"))
        net_rx, net_tx = parse_io_pair(parsed.get("NetIO"))
        block_read, block_write = parse_io_pair(parsed.get("BlockIO"))

        net_rx_bps = net_tx_bps = block_read_bps = block_write_bps = None
        rate_gap = False
        note = None
        previous = self._rate_baselines.get(container.id)
        if previous is None or elapsed is None or elapsed <= 0:
            rate_gap = True
            note = "warming up: first stats sample for this container"
        else:
            if net_rx is not None and previous.net_rx is not None and net_rx >= previous.net_rx:
                net_rx_bps = (net_rx - previous.net_rx) / elapsed
            if net_tx is not None and previous.net_tx is not None and net_tx >= previous.net_tx:
                net_tx_bps = (net_tx - previous.net_tx) / elapsed
            if (
                block_read is not None
                and previous.block_read is not None
                and block_read >= previous.block_read
            ):
                block_read_bps = (block_read - previous.block_read) / elapsed
            if (
                block_write is not None
                and previous.block_write is not None
                and block_write >= previous.block_write
            ):
                block_write_bps = (block_write - previous.block_write) / elapsed
            if net_rx_bps is None or net_tx_bps is None:
                rate_gap = True
                note = "counter reset or recreated container; rate baseline discarded"
        self._rate_baselines[container.id] = _RateBaseline(
            net_rx=net_rx, net_tx=net_tx, block_read=block_read, block_write=block_write
        )
        _ = engine
        return _StatFields(
            cpu_percent=cpu_raw,
            memory_used_bytes=memory_used,
            memory_limit_bytes=memory_limit,
            memory_percent=memory_percent,
            net_rx_bytes=net_rx,
            net_tx_bytes=net_tx,
            net_rx_bps=net_rx_bps,
            net_tx_bps=net_tx_bps,
            block_read_bytes=block_read,
            block_write_bytes=block_write,
            block_read_bps=block_read_bps,
            block_write_bps=block_write_bps,
            rate_gap=rate_gap,
            note=note,
        )

    # ------------------------------------------------------------------ metadata

    async def _with_metadata(
        self,
        context: ProviderBuild,
        containers: list[ContainerReading],
        now: datetime,
    ) -> tuple[list[ContainerReading], str | None]:
        settings = context.settings
        assert isinstance(settings, DockerSettings)
        if not containers:
            self._metadata_at = now
            return containers, None
        selected = containers[: settings.stats_batch_limit]
        raw = await self._run(
            context,
            "container",
            "inspect",
            "--format",
            HEALTH_TEMPLATE,
            *[container.id for container in selected],
        )
        inspected: dict[str, Any] = {}
        for line in raw.splitlines():
            parsed = parse_health_line(line)
            if parsed is not None and parsed.id:
                # The inventory prints the 12-character short id while inspect returns
                # the full 64-character one, so both are keyed by the short form.
                inspected[parsed.id[:SHORT_ID_LENGTH]] = parsed

        note: str | None = None
        changed = False
        updated: list[ContainerReading] = []
        for container in containers:
            record = inspected.get(container.id[:SHORT_ID_LENGTH])
            if record is None:
                updated.append(container)
                continue
            restarts = record.restarts
            delta = None
            if restarts is not None:
                previous = self._restart_baselines.get(container.id)
                if previous is not None and restarts > previous:
                    delta = restarts - previous
                    changed = True
                self._restart_baselines[container.id] = restarts
            health = record.health.lower() if record.health else None
            if health is not None and health not in _HEALTH_VALUES:
                health = None
            updated.append(
                container.model_copy(
                    update={
                        "health": health,
                        "health_defined": health is not None,
                        "restarts": restarts,
                        "restarts_since_sample": delta,
                        "exit_code": record.exit_code,
                        "state": (record.status or container.state).lower(),
                        "started_at": _parse_timestamp(record.started_at),
                    }
                )
            )
        self._metadata_at = now
        if len(containers) > len(selected):
            note = f"metadata limited to {settings.stats_batch_limit} containers"
        if changed:
            note = (
                note + "; " if note else ""
            ) + "restart count increased since the previous sample"
        return updated, note

    # ------------------------------------------------------------------ storage

    async def _storage_usage(self, context: ProviderBuild) -> tuple[StorageUsage, ...]:
        try:
            raw = await self._run(
                context, "system", "df", "--format", STORAGE_TEMPLATE, timeout=15.0
            )
        except UnavailableError as error:
            log.info("docker system df unavailable: %s", error.message)
            return (
                StorageUsage(kind="engine", note="storage usage is unavailable on this engine"),
            )
        out: list[StorageUsage] = []
        for line in raw.splitlines():
            parsed = parse_inventory_line(line)
            if not parsed:
                continue
            out.append(
                StorageUsage(
                    kind=(parsed.get("Type") or "unknown")[:24],
                    total=_int_or_none(parsed.get("TotalCount")),
                    active=_int_or_none(parsed.get("ActiveCount")),
                    size_bytes=parse_size(parsed.get("Size")),
                    reclaimable_bytes=parse_size((parsed.get("Reclaimable") or "").split(" ")[0]),
                    note="engine-reported usage; Docker Desktop stores this in a disk image",
                )
            )
        return tuple(out)

    async def aclose(self) -> None:
        return None


def _engine_normalised(cpu_percent: float | None, cpu_count: int | None) -> float | None:
    """Engine-capacity-normalised percentage, offered only when the engine CPU count is known."""
    if cpu_percent is None or not cpu_count:
        return None
    return cpu_percent / (cpu_count * 100.0) * 100.0


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _int_or_none(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def metrics_of(payload: SourcePayload) -> tuple[MetricReading, ...]:
    """Aggregate engine readings recorded in the host's bounded history.

    Per-container history is deliberately not persisted: it grows with the number of
    containers and is a debugging detail rather than an ambient signal.
    """
    if not isinstance(payload, DockerPayload):
        return ()
    running = [c for c in payload.containers if c.running]
    unhealthy = [c for c in payload.containers if c.health == "unhealthy"]
    cpu = [c.cpu_percent for c in running if c.cpu_percent is not None]
    memory = [c.memory_used_bytes for c in running if c.memory_used_bytes is not None]
    readings = [
        MetricReading(
            metric_id="containers_running", value=float(len(running)), label="running containers"
        ),
        MetricReading(
            metric_id="containers_unhealthy",
            value=float(len(unhealthy)),
            label="unhealthy containers",
        ),
    ]
    if cpu:
        readings.append(
            MetricReading(
                metric_id="engine_cpu_percent", value=sum(cpu), unit="%", label="engine CPU"
            )
        )
    if memory:
        readings.append(
            MetricReading(
                metric_id="engine_memory_bytes",
                value=float(sum(memory)),
                unit="B",
                label="engine memory",
            )
        )
    return tuple(readings)


def make_spec() -> SourceSpec:
    return SourceSpec(
        type_id="docker.containers",
        payload_model=DockerPayload,
        settings_model=DockerSettings,
        payload_schema="docker.containers",
        payload_schema_version=1,
        factory=lambda build: DockerProvider(settings=_settings_of(build)),
        capabilities=frozenset({Capability.CLI}),
        cadence=Cadence(active_seconds=15, ambient_seconds=15, background_seconds=60),
        retention_seconds=6 * 3600,
        description="Local Docker-compatible engine: containers, Compose projects, stats.",
        requires_setup=True,
        setup_hint=(
            "Run `sideboard doctor --docker` to list local contexts, then pin one in "
            "sources.docker.settings.context. Remote contexts are refused."
        ),
        metrics_extractor=metrics_of,
        demo_builder=demo_payload,
        setup_check=lambda settings: bool(settings.context),
    )


def _settings_of(build: ProviderBuild) -> DockerSettings:
    settings = build.settings
    assert isinstance(settings, DockerSettings)
    return settings


def demo_payload() -> SourcePayload:
    """Synthetic local engine for ``--demo``. No CLI is executed."""
    from datetime import UTC

    now = datetime.now(UTC)
    containers = (
        ContainerReading(
            id="demo-1",
            name="sideboard-web-1",
            image="nginx:alpine",
            state="running",
            status_text="Up 3 hours",
            health="healthy",
            health_defined=True,
            compose_project="sideboard",
            compose_service="web",
            running=True,
            cpu_percent=2.4,
            cpu_percent_of_engine=0.3,
            memory_used_bytes=48 * 1024**2,
            memory_limit_bytes=512 * 1024**2,
            memory_percent=9.4,
            net_rx_bytes=12 * 1024**2,
            net_tx_bytes=3 * 1024**2,
            net_rx_bps=18_000.0,
            net_tx_bps=6_000.0,
            restarts=0,
        ),
        ContainerReading(
            id="demo-2",
            name="sideboard-db-1",
            image="postgres:17-alpine",
            state="running",
            status_text="Up 3 hours",
            health=None,
            health_defined=False,
            compose_project="sideboard",
            compose_service="db",
            running=True,
            cpu_percent=124.5,
            cpu_percent_of_engine=15.6,
            memory_used_bytes=220 * 1024**2,
            memory_limit_bytes=1024**3,
            memory_percent=21.5,
            net_rx_bytes=64 * 1024**2,
            net_tx_bytes=40 * 1024**2,
            net_rx_bps=44_000.0,
            net_tx_bps=31_000.0,
            restarts=2,
            note="running / health unknown: no healthcheck defined",
        ),
        ContainerReading(
            id="demo-3",
            name="demo-batch-1",
            image="demo/batch:latest",
            state="exited",
            status_text="Exited (0) 2 hours ago",
            compose_project="sideboard",
            compose_service="batch",
            running=False,
            exit_code=0,
        ),
    )
    return DockerPayload(
        engine=EngineInfo(
            context="demo",
            endpoint="unix:///var/run/docker.sock",
            name="demo-engine",
            server_version="27.0.0",
            operating_system="Docker Desktop",
            cpu_count=8,
            is_local=True,
        ),
        containers=containers,
        storage=(
            StorageUsage(
                kind="Images",
                total=12,
                active=4,
                size_bytes=3 * 1024**3,
                reclaimable_bytes=1024**3,
            ),
            StorageUsage(
                kind="Volumes", total=5, active=3, size_bytes=512 * 1024**2, reclaimable_bytes=0
            ),
        ),
        stats_sampled_at=now,
        metadata_sampled_at=now,
        stats_available=True,
        storage_available=True,
        expected_running=("sideboard-web-1", "sideboard-db-1"),
        notes=("demo data: synthetic containers, no Docker CLI was executed",),
        as_of=now,
        precision=Precision.INSTANT,
        valid_until=now + timedelta(minutes=2),
        provenance="demo",
    )
