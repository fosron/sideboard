"""v1 extension contracts.

This module is the whole public surface a feature module needs. It is deliberately
small: typed protocols, validated immutable payloads, no inheritance hierarchy.

Concepts kept separate (see the plan, section 7):

* :class:`FeatureSpec` – a domain module (``weather``, ``docker_local``, ``portfolio``)
* :class:`SourceSpec` – how a collector validates settings and produces typed data
* source instance – one configured collection/cache identity (``mac``, ``weather_home``)
* :class:`WidgetSpec` – maps bound source data into a presentation
* :class:`CategoryConfig` / :class:`TabConfig` – open string IDs, not enums
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .clock import Clock
from .errors import SourceErrorInfo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .capabilities import CommandRunner, FileReader, HttpReader
    from .storage import Database

__all__ = [
    "API_VERSION",
    "CONFIG_VERSION",
    "Cadence",
    "Capability",
    "CategoryConfig",
    "Connectivity",
    "Coverage",
    "Demand",
    "EmptySettings",
    "FeatureSpec",
    "Freshness",
    "InputBinding",
    "Item",
    "ItemState",
    "Metric",
    "MetricReading",
    "Migration",
    "Precision",
    "Presence",
    "PresentInput",
    "Privacy",
    "Provider",
    "ProviderBuild",
    "RuntimeStatus",
    "Series",
    "SeriesPoint",
    "Severity",
    "SnapshotStore",
    "SourceEnvelope",
    "SourcePayload",
    "SourceSpec",
    "SourceStatusSummary",
    "StatusView",
    "TabConfig",
    "TableColumn",
    "TableRow",
    "TableView",
    "ViewContext",
    "ViewMode",
    "ViewModel",
    "WidgetSpec",
    "compact_metric",
    "freshness_for",
    "setup_required",
    "to_jsonable",
]

API_VERSION = 1
CONFIG_VERSION = 1


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EmptySettings(BaseModel):
    """Settings model for widgets and sources that take no options."""

    model_config = ConfigDict(extra="forbid")


class Precision(StrEnum):
    """Temporal precision the source actually supplied."""

    INSTANT = "instant"
    DATE = "date"
    MONTH = "month"
    UNKNOWN = "unknown"


class Freshness(StrEnum):
    """Independent of connectivity. Recomputed by the host on every read."""

    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"
    NONE = "none"


class Connectivity(StrEnum):
    """Access/transport outcome. Independent of freshness."""

    OK = "ok"
    OFFLINE = "offline"
    ERROR = "error"
    BLOCKED = "blocked"
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"


class Presence(StrEnum):
    """Third dimension: is the UI holding an older rendered snapshot?"""

    LIVE = "live"
    HELD = "held"


class Privacy(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class Severity(StrEnum):
    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    UNKNOWN = "unknown"


class Capability(StrEnum):
    """Approved access boundaries. Configuration can only narrow these."""

    HTTP_PUBLIC = "http_public"
    HTTP_LOCAL = "http_local"
    FILE = "file"
    LOCAL_METRICS = "local_metrics"
    CLI = "cli"
    REPOSITORY = "repository"


class Demand(StrEnum):
    """Derived by the host from configured consumers and active views."""

    ACTIVE = "active"
    AMBIENT = "ambient"
    BACKGROUND = "background"
    NONE = "none"


class ViewMode(StrEnum):
    COMPACT = "compact"
    DETAIL = "detail"


class Coverage(_Frozen):
    """Honest reporting of partial data: one field can be missing while the fetch succeeds."""

    expected: int | None = None
    observed: int | None = None
    missing: tuple[str, ...] = ()
    notes: str | None = None

    @property
    def is_partial(self) -> bool:
        if self.missing:
            return True
        return (
            self.expected is not None
            and self.observed is not None
            and self.observed < self.expected
        )


class SourcePayload(_Frozen):
    """Base class for validated feature payloads.

    Subclasses add domain fields. ``as_of`` is the source's own timestamp and
    ``valid_until`` the point after which the host should mark it stale.
    """

    as_of: datetime | None = None
    precision: Precision = Precision.INSTANT
    valid_until: datetime | None = None
    provenance: str = ""
    coverage: Coverage = Field(default_factory=Coverage)

    @classmethod
    def schema_id(cls) -> str:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class SourceEnvelope:
    """Host-owned wrapper around a payload.

    Adds attempt/success metadata and bounded error information. The host always
    overwrites ``freshness`` when loading from cache, so it cannot go stale in memory.
    """

    source_id: str
    payload_schema: str
    payload_schema_version: int
    generation: int = 0
    payload: SourcePayload | None = None
    connectivity: Connectivity = Connectivity.UNCONFIGURED
    freshness: Freshness = Freshness.NONE
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    fetched_at: datetime | None = None
    as_of: datetime | None = None
    precision: Precision = Precision.UNKNOWN
    valid_until: datetime | None = None
    stale_after_seconds: float | None = None
    content_hash: str | None = None
    validator_etag: str | None = None
    validator_last_modified: str | None = None
    provenance: str = ""
    coverage: Coverage = field(default_factory=Coverage)
    error: SourceErrorInfo | None = None

    @property
    def has_payload(self) -> bool:
        return self.payload is not None

    @property
    def is_partial(self) -> bool:
        return self.coverage.is_partial


def _compute_freshness(
    *,
    now: datetime,
    last_success_at: datetime | None,
    valid_until: datetime | None,
    stale_after_seconds: float | None,
) -> Freshness:
    if last_success_at is None:
        return Freshness.NONE
    if valid_until is not None and now >= valid_until:
        return Freshness.EXPIRED
    if stale_after_seconds is not None and stale_after_seconds > 0:
        age = (now - last_success_at).total_seconds()
        if age > stale_after_seconds * 3:
            return Freshness.EXPIRED
        if age > stale_after_seconds:
            return Freshness.STALE
    return Freshness.FRESH


def freshness_for(envelope: SourceEnvelope, now: datetime) -> Freshness:
    """Recompute freshness for a loaded envelope."""
    if envelope.connectivity is Connectivity.UNCONFIGURED:
        return Freshness.NONE
    return _compute_freshness(
        now=now,
        last_success_at=envelope.last_success_at,
        valid_until=envelope.valid_until,
        stale_after_seconds=envelope.stale_after_seconds,
    )


@runtime_checkable
class Provider(Protocol):
    """One bounded acquisition, never its own polling loop."""

    async def fetch(self, context: ProviderBuild) -> SourcePayload:
        """Acquire and validate one payload. Raise a typed error on failure."""
        ...

    async def aclose(self) -> None:
        """Release owned resources. Must be safe to call more than once."""
        ...


class Cadence(_Frozen):
    """Default sampling policy. Feature settings or instance config may override."""

    active_seconds: float = 300.0
    ambient_seconds: float | None = None
    background_seconds: float | None = None
    import_driven: bool = False

    def seconds_for(self, demand: Demand) -> float | None:
        if self.import_driven:
            return None
        if demand is Demand.ACTIVE:
            return self.active_seconds
        if demand is Demand.AMBIENT:
            return (
                self.ambient_seconds
                if self.ambient_seconds is not None
                else self.active_seconds * 2
            )
        if demand is Demand.BACKGROUND:
            return (
                self.background_seconds
                if self.background_seconds is not None
                else self.active_seconds * 4
            )
        return None


@dataclass(frozen=True, slots=True)
class ProviderBuild:
    """Injected services handed to a provider factory.

    Only capabilities declared by the :class:`SourceSpec` are populated. A narrow
    typed service beats an unrestricted global application object.
    """

    source_id: str
    settings: BaseModel
    clock: Clock
    http: HttpReader | None = None
    files: FileReader | None = None
    commands: CommandRunner | None = None
    database: Database | None = None
    previous: SourceEnvelope | None = None
    offline: bool = False
    demo: bool = False
    root_dir: str = ""


@dataclass(frozen=True, slots=True)
class MetricReading:
    """One numeric sample a feature wants recorded in bounded history."""

    metric_id: str
    value: float
    unit: str = ""
    label: str = ""
    gap: bool = False


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """How one source type validates configuration and produces typed data."""

    type_id: str
    payload_model: type[SourcePayload]
    settings_model: type[BaseModel]
    payload_schema: str
    payload_schema_version: int
    factory: Callable[[ProviderBuild], Provider]
    capabilities: frozenset[Capability] = frozenset()
    cadence: Cadence = field(default_factory=Cadence)
    retention_seconds: int = 7 * 24 * 3600
    description: str = ""
    requires_setup: bool = False
    setup_hint: str = ""
    import_driven: bool = False
    metrics_extractor: Callable[[SourcePayload], Sequence[MetricReading]] | None = None
    demo_builder: Callable[[], SourcePayload] | None = None
    # The predicate receives the instance's own settings model. It is typed loosely
    # because each feature validates its own concrete type; the host treats an
    # exception as "setup still required" rather than trusting a broken predicate.
    setup_check: Callable[[Any], bool] | None = None

    def validate_settings(self, raw: Mapping[str, Any]) -> BaseModel:
        return self.settings_model.model_validate(dict(raw))


@dataclass(frozen=True, slots=True)
class InputBinding:
    """One named input a widget expects, and the payload schemas it accepts."""

    name: str
    accepts: frozenset[str]
    required: bool = True
    many: bool = False


# --------------------------------------------------------------------------- #
# View models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Metric:
    """A single labelled measurement. ``available`` is False for a missing field."""

    id: str
    label: str
    value_text: str
    unit: str = ""
    basis: str = ""
    available: bool = True
    note: str | None = None
    privacy: Privacy = Privacy.PUBLIC
    as_of: datetime | None = None
    numeric: float | None = None
    severity: Severity = Severity.UNKNOWN

    @classmethod
    def unavailable(cls, metric_id: str, label: str, note: str | None = None) -> Metric:
        return cls(
            id=metric_id,
            label=label,
            value_text="—",
            available=False,
            note=note,
            severity=Severity.UNKNOWN,
        )


@dataclass(frozen=True, slots=True)
class Item:
    """A list entry: reading item, upload, thread, snapshot row."""

    id: str
    title: str
    subtitle: str | None = None
    url: str | None = None
    category: str | None = None
    published_at: datetime | None = None
    meta: tuple[tuple[str, str], ...] = ()
    privacy: Privacy = Privacy.PUBLIC
    source_id: str | None = None
    saved: bool = False
    read: bool = False
    badge: str | None = None


@dataclass(frozen=True, slots=True)
class TableColumn:
    id: str
    label: str
    align: str = "left"
    width: int | None = None
    privacy: Privacy = Privacy.PUBLIC


@dataclass(frozen=True, slots=True)
class TableRow:
    id: str
    cells: Mapping[str, str]
    privacy: Privacy = Privacy.PUBLIC
    url: str | None = None
    severity: Severity = Severity.UNKNOWN


@dataclass(frozen=True, slots=True)
class TableView:
    columns: tuple[TableColumn, ...]
    rows: tuple[TableRow, ...]
    groups: tuple[tuple[str, tuple[str, ...]], ...] = ()
    truncated: bool = False
    footnote: str | None = None


@dataclass(frozen=True, slots=True)
class StatusView:
    severity: Severity
    headline: str
    detail: str | None = None
    hints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """``value`` is None for a gap: sleep, counter reset or a long sampling gap."""

    t: float
    value: float | None


@dataclass(frozen=True, slots=True)
class Series:
    id: str
    label: str
    unit: str = ""
    points: tuple[SeriesPoint, ...] = ()
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ViewModel:
    """One validated standard presentation shape.

    A presenter fills only the sections it has. ``status`` is how a presenter says
    "unconfigured", "blocked" or "no data yet" honestly.
    """

    title: str | None = None
    subtitle: str | None = None
    status: StatusView | None = None
    metrics: tuple[Metric, ...] = ()
    items: tuple[Item, ...] = ()
    table: TableView | None = None
    series: tuple[Series, ...] = ()
    footnote: str | None = None
    privacy: Privacy = Privacy.PUBLIC

    @property
    def is_empty(self) -> bool:
        return not (
            self.metrics
            or self.items
            or self.series
            or (self.table is not None and self.table.rows)
        )

    def metric(self, metric_id: str) -> Metric | None:
        for metric in self.metrics:
            if metric.id == metric_id:
                return metric
        return None

    def first_url(self) -> str | None:
        if self.table is not None:
            for row in self.table.rows:
                if row.url:
                    return row.url
        for item in self.items:
            if item.url:
                return item.url
        return None


def compact_metric(
    metric_id: str,
    label: str,
    value: float | int | str | None,
    *,
    unit: str = "",
    basis: str = "",
    digits: int = 0,
    suffix: str = "",
    note: str | None = None,
    privacy: Privacy = Privacy.PUBLIC,
    as_of: datetime | None = None,
) -> Metric:
    """Helper for presenters: format a number or fall back to an unavailable metric."""
    if value is None:
        return Metric.unavailable(metric_id, label, note)
    if isinstance(value, str):
        text = value
        numeric = None
    else:
        text = f"{value:,.{digits}f}{suffix}"
        numeric = float(value)
    return Metric(
        id=metric_id,
        label=label,
        value_text=text,
        unit=unit,
        basis=basis,
        note=note,
        privacy=privacy,
        as_of=as_of,
        numeric=numeric,
    )


@dataclass(frozen=True, slots=True)
class ItemState:
    """Local, UI-owned read/saved state."""

    saved: bool = False
    read: bool = False
    dismissed: bool = False


@dataclass(frozen=True, slots=True)
class SourceStatusSummary:
    """Runtime diagnostics for one source instance. Built by the host, read by presenters."""

    source_id: str
    type_id: str
    enabled: bool = True
    demand: Demand = Demand.NONE
    connectivity: Connectivity = Connectivity.UNCONFIGURED
    freshness: Freshness = Freshness.NONE
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    as_of: datetime | None = None
    precision: Precision = Precision.UNKNOWN
    error_category: str | None = None
    error_message: str | None = None
    interval_seconds: float | None = None
    in_flight: bool = False
    next_due_seconds: float | None = None
    failures: int = 0
    has_payload: bool = False
    partial: bool = False
    provenance: str = ""
    setup_hint: str = ""
    requires_setup: bool = False
    retention_seconds: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """Host-owned runtime snapshot handed to diagnostics presenters."""

    sources: tuple[SourceStatusSummary, ...] = ()
    offline: bool = False
    demo: bool = False
    config_revision: int = 0
    active_tab: str | None = None
    stale_count: int = 0
    unavailable_count: int = 0
    pending_writes: int = 0
    config_path: str = ""
    database_path: str = ""
    log_path: str = ""


@dataclass(frozen=True, slots=True)
class ViewContext:
    """Every input a presenter may use besides its bound snapshots and settings."""

    mode: ViewMode = ViewMode.COMPACT
    width: int = 80
    height: int = 24
    privacy: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    timezone: str = "UTC"
    selected_id: str | None = None
    held: bool = False
    offline: bool = False
    demo: bool = False
    saved_ids: frozenset[str] = frozenset()
    read_ids: frozenset[str] = frozenset()
    item_states: Mapping[str, ItemState] = field(default_factory=dict)
    saved_items: tuple[Item, ...] = ()
    runtime: RuntimeStatus | None = None

    @property
    def compact(self) -> bool:
        return self.mode is ViewMode.COMPACT


@dataclass(frozen=True, slots=True)
class PresentInput:
    """Arguments passed to a widget presenter. Pure: no I/O, no timers, no DB."""

    settings: BaseModel
    inputs: Mapping[str, SourceEnvelope | None]
    context: ViewContext

    def envelope(self, binding: str) -> SourceEnvelope | None:
        return self.inputs.get(binding)

    def payload(self, binding: str) -> SourcePayload | None:
        envelope = self.inputs.get(binding)
        return envelope.payload if envelope is not None else None

    def payloads(self, binding: str) -> tuple[SourcePayload, ...]:
        envelope = self.inputs.get(binding)
        if envelope is None or envelope.payload is None:
            return ()
        return (envelope.payload,)


@dataclass(frozen=True, slots=True)
class WidgetSpec:
    """Maps bound source data into a presentation. Owns no polling timer."""

    type_id: str
    settings_model: type[BaseModel]
    inputs: tuple[InputBinding, ...]
    present: Callable[[PresentInput], ViewModel]
    description: str = ""
    default_category: str | None = None
    default_settings: Mapping[str, Any] = field(default_factory=dict)
    detail: str | None = None
    interactions: tuple[str, ...] = ()
    min_width: int = 24
    min_height: int = 3

    def binding(self, name: str) -> InputBinding | None:
        for binding in self.inputs:
            if binding.name == name:
                return binding
        return None

    def validate_settings(self, raw: Mapping[str, Any]) -> BaseModel:
        return self.settings_model.model_validate(dict(raw))


@dataclass(frozen=True, slots=True)
class Migration:
    """Namespaced, ordered persistence change owned by a feature or core."""

    version: int
    name: str
    statements: tuple[str, ...]
    owner: str = "core"


@dataclass(frozen=True, slots=True)
class CategoryConfig:
    """Open metadata for filtering and digest/queue selection."""

    id: str
    label: str
    order: int = 100
    description: str = ""


@dataclass(frozen=True, slots=True)
class TabConfig:
    """Configured layout and widget placement. No closed enum of today's tabs."""

    id: str
    label: str
    layout: str = "stack"
    widgets: tuple[str, ...] = ()
    visible: bool = True
    order: int = 0
    category_filter: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """A domain module. Registered in one trusted, repository-owned list."""

    id: str
    sources: tuple[SourceSpec, ...] = ()
    widgets: tuple[WidgetSpec, ...] = ()
    migrations: tuple[Migration, ...] = ()
    categories: tuple[CategoryConfig, ...] = ()
    default_sources: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    default_widgets: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    description: str = ""
    api_version: int = API_VERSION

    def source(self, type_id: str) -> SourceSpec | None:
        for spec in self.sources:
            if spec.type_id == type_id:
                return spec
        return None

    def widget(self, type_id: str) -> WidgetSpec | None:
        for spec in self.widgets:
            if spec.type_id == type_id:
                return spec
        return None


class SnapshotStore(Protocol):
    """Read-only view of collected data, handed to presenters by the host."""

    def envelope(self, source_id: str) -> SourceEnvelope | None: ...

    def envelopes(self, source_ids: Iterable[str]) -> Mapping[str, SourceEnvelope]: ...


def setup_required(spec: SourceSpec, settings: BaseModel) -> bool:
    """Whether a source instance still needs the user's configuration.

    A spec-level ``requires_setup`` flag alone is not enough: a weather source with
    coordinates, or a news source with validated feeds, is configured even though the
    spec needs setup for a new instance. Features may therefore provide a predicate.
    """
    if not spec.requires_setup:
        return False
    check = spec.setup_check
    if check is None:
        return True
    try:
        return not bool(check(settings))
    except Exception:  # pragma: no cover - a predicate must be pure and total
        return True


def to_jsonable(value: Any) -> Any:
    """Best-effort conversion used before persisting envelopes."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [to_jsonable(v) for v in value]
    return value
