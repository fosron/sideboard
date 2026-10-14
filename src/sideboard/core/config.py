"""Versioned TOML configuration: validation, merge and reload.

One canonical schema. ID-indexed tables (``sources``, ``widgets``, ``categories``)
merge by stable ID; ordered lists (``tabs``, an overview slot's widget queue) replace
atomically rather than concatenating.

A candidate configuration is fully validated *before* it can be applied; the app
keeps running the previous valid configuration if validation fails.
"""

from __future__ import annotations

import contextlib
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import (
    CONFIG_VERSION,
    Cadence,
    CategoryConfig,
    Demand,
    SourceSpec,
    TabConfig,
    WidgetSpec,
)
from .errors import ConfigError
from .paths import Paths
from .registry import Registry
from .storage import ensure_private_dir

__all__ = [
    "AppSettings",
    "OverviewSettings",
    "ResolvedConfig",
    "RootDocument",
    "SlotSettings",
    "SourceInstance",
    "WidgetInstance",
    "config_from_document",
    "document_from_text",
    "load_config_file",
    "merge_documents",
    "render_default_config",
    "resolve_config",
    "set_config_value",
    "write_config_text",
]

DEFAULT_CONFIG_TEXT_PATH = Path(__file__).resolve().parent.parent / "defaults.toml"

_SECRET_KEYS = ("token", "secret", "password", "api_key", "apikey", "feed_url", "ics_url")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppSettings(_Model):
    #: "local" is this Mac's own timezone; a named zone pins the clock instead.
    timezone: str = "local"
    base_currency: str = "EUR"
    #: Colour palette name (`sideboard themes list`). An unknown name is rejected at
    #: resolve time rather than silently rendering the default palette.
    theme: str = "sideboard"
    #: Per-token colour overrides as #rrggbb, keyed by palette field name
    #: (background, dim, ok, focus_bg, …): one palette, adjusted.
    theme_colors: dict[str, str] = Field(default_factory=dict)
    privacy_mode: bool = False
    mouse: bool = True
    compact_threshold_columns: int = 70
    min_columns: int = 60
    min_rows: int = 20


class SourceDocument(_Model):
    type: str
    enabled: bool = True
    active_seconds: float | None = None
    ambient_seconds: float | None = None
    background_seconds: float | None = None
    retention_seconds: int | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class WidgetDocument(_Model):
    type: str
    enabled: bool = True
    sources: dict[str, str] = Field(default_factory=dict)
    category: str | None = None
    title: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class CategoryDocument(_Model):
    label: str
    order: int = 100
    description: str = ""


class SlotSettings(_Model):
    widgets: list[str] = Field(default_factory=list)
    pinned: bool = False
    dwell_seconds: float = 40.0
    label: str | None = None


class OverviewSettings(_Model):
    rotation_enabled: bool = True
    hover_pause: bool = True
    keyboard_hold_seconds: float = 15.0
    resume_grace_seconds: float = 2.0
    slots: dict[str, SlotSettings] = Field(default_factory=dict)


class TabDocument(_Model):
    id: str
    label: str
    layout: str = "stack"
    widgets: list[str] = Field(default_factory=list)
    visible: bool = True
    category_filter: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class RootDocument(_Model):
    """The complete, validated configuration document."""

    config_version: int = CONFIG_VERSION
    app: AppSettings = Field(default_factory=AppSettings)
    categories: dict[str, CategoryDocument] = Field(default_factory=dict)
    sources: dict[str, SourceDocument] = Field(default_factory=dict)
    widgets: dict[str, WidgetDocument] = Field(default_factory=dict)
    tabs: list[TabDocument] = Field(default_factory=list)
    overview: OverviewSettings = Field(default_factory=OverviewSettings)


# --------------------------------------------------------------------------- #
# Resolved (typed) view
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SourceInstance:
    source_id: str
    spec: SourceSpec
    settings: BaseModel
    cadence: Cadence
    enabled: bool
    retention_seconds: int
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def type_id(self) -> str:
        return self.spec.type_id

    @property
    def payload_schema(self) -> str:
        return self.spec.payload_schema

    @property
    def import_driven(self) -> bool:
        return self.spec.import_driven or self.cadence.import_driven


@dataclass(frozen=True, slots=True)
class WidgetInstance:
    widget_id: str
    spec: WidgetSpec
    settings: BaseModel
    bindings: Mapping[str, str]
    category: str | None = None
    title: str | None = None
    enabled: bool = True

    def source_ids(self) -> tuple[str, ...]:
        return tuple(self.bindings.values())


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """Validated, resolved configuration. Immutable; the app swaps it atomically."""

    document: RootDocument
    app: AppSettings
    categories: Mapping[str, CategoryConfig]
    tabs: tuple[TabConfig, ...]
    slots: Mapping[str, SlotSettings]
    overview: OverviewSettings
    sources: Mapping[str, SourceInstance]
    widgets: Mapping[str, WidgetInstance]
    paths: Paths
    offline: bool = False
    demo: bool = False
    revision: int = 0

    def source(self, source_id: str) -> SourceInstance | None:
        return self.sources.get(source_id)

    def widget(self, widget_id: str) -> WidgetInstance | None:
        return self.widgets.get(widget_id)

    def widgets_for_tab(self, tab_id: str) -> tuple[str, ...]:
        for tab in self.tabs:
            if tab.id == tab_id:
                return tab.widgets
        return ()

    def tab(self, tab_id: str) -> TabConfig | None:
        for tab in self.tabs:
            if tab.id == tab_id:
                return tab
        return None

    def widgets_by_source(self) -> Mapping[str, tuple[str, ...]]:
        """Reverse index: which widgets consume a source instance."""
        out: dict[str, list[str]] = {source_id: [] for source_id in self.sources}
        for widget_id, widget in self.widgets.items():
            for source_id in widget.source_ids():
                out.setdefault(source_id, []).append(widget_id)
        return {key: tuple(sorted(value)) for key, value in out.items()}

    def sources_in_overview(self) -> frozenset[str]:
        """Source instances used by widgets placed in an overview rotation slot."""
        out: set[str] = set()
        for slot in self.slots.values():
            for widget_id in slot.widgets:
                widget = self.widgets.get(widget_id)
                if widget is not None:
                    out.update(widget.source_ids())
        return frozenset(out)

    def tabs_using_widget(self, widget_id: str) -> tuple[str, ...]:
        return tuple(tab.id for tab in self.tabs if widget_id in tab.widgets)

    def demand_for_source(self, source_id: str, *, active_tab: str | None = None) -> Demand:
        """Derive collection demand from configured consumers and the active view.

        A source no widget consumes owns no recurring task, so a stale configuration
        cannot keep polling in the background.
        """
        instance = self.sources.get(source_id)
        if instance is None or not instance.enabled:
            return Demand.NONE
        consumers = self.widgets_by_source().get(source_id, ())
        if not consumers:
            return Demand.NONE
        if active_tab is not None:
            for widget_id in self.widgets_for_tab(active_tab):
                widget = self.widgets.get(widget_id)
                if widget is not None and source_id in widget.source_ids():
                    return Demand.ACTIVE
        if source_id in self.sources_in_overview():
            return Demand.AMBIENT
        return Demand.BACKGROUND


def _settings_cadence(settings: BaseModel, spec: SourceSpec, document: SourceDocument) -> Cadence:
    values = dict(spec.cadence.model_dump())
    # ``refresh_seconds`` is the documented single-number form. It sets the active and
    # ambient cadence unless the more explicit fields override it.
    refresh = getattr(settings, "refresh_seconds", None)
    if isinstance(refresh, int | float) and refresh > 0:
        values["active_seconds"] = float(refresh)
        if values.get("ambient_seconds") is None:
            values["ambient_seconds"] = float(refresh)
    for field_name in ("active_seconds", "ambient_seconds", "background_seconds"):
        from_settings = getattr(settings, field_name, None)
        if isinstance(from_settings, int | float):
            values[field_name] = float(from_settings)
        from_document = getattr(document, field_name, None)
        if isinstance(from_document, int | float):
            values[field_name] = float(from_document)
    return Cadence(**values)


def merge_documents(
    base: Mapping[str, Any], override: Mapping[str, Any], *, _depth: int = 0
) -> dict[str, Any]:
    """Deep-merge two TOML documents.

    Tables merge key by key; lists and scalars from ``override`` replace the base
    value atomically. This is what makes "append a tab" mean *append*, not concatenate.
    """
    if _depth > 12:  # pragma: no cover - guard against pathological nesting
        return dict(override)
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        existing = out.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            out[key] = merge_documents(existing, value, _depth=_depth + 1)
        else:
            out[key] = value
    return out


#: A key line inside a table: indentation, the key, ``=``, then whatever follows.
_KEY_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_-]+)(?P<sep>\s*=\s*)(?P<value>.*)$")


def set_config_value(text: str, *, table: str, key: str, value: str) -> str:
    """Set one value in a hand-written configuration file, preserving everything else.

    The file belongs to the user: it has their comments, their ordering and their
    spacing. Re-rendering it from a template would throw all of that away, so this edits
    the one line and leaves the rest byte-identical. A trailing comment on that line
    survives too.

    The table header must match exactly (``[app]``), so a value is never written into a
    similarly named table.
    """
    lines = text.splitlines(keepends=True)
    header = f"[{table}]"
    start: int | None = None
    end = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == header and not stripped.startswith("#"):
            start = index
            continue
        if start is not None and stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    assignment = f'{key} = "{value}"'
    if start is None:
        # No such table: append one. Only reached for a file that never had the key.
        body = text if text.endswith("\n") else text + "\n"
        return f"{body}\n{header}\n{assignment}\n"
    for index in range(start + 1, end):
        match = _KEY_LINE.match(lines[index].rstrip("\n"))
        if match is None or match.group("key") != key:
            continue
        tail = match.group("value")
        comment = ""
        if tail.startswith('"'):
            closing = tail.find('"', 1)
            if closing != -1:
                comment = tail[closing + 1 :]
        lines[index] = f"{match.group('indent')}{assignment}{comment}\n"
        return "".join(lines)
    # The table exists without the key: put it first, where it is easy to find.
    lines.insert(start + 1, f"{assignment}\n")
    return "".join(lines)


def document_from_text(text: str) -> RootDocument:
    """Parse configuration text that has not been written to disk yet."""
    return config_from_document(tomllib.loads(text))


def write_config_text(path: Path, text: str) -> None:
    """Replace the configuration file atomically, keeping owner-only permissions.

    A half-written file is a configuration the next start cannot read, so the new text is
    written beside the original and renamed into place.
    """
    ensure_private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    with contextlib.suppress(OSError):
        temporary.chmod(0o600)
    temporary.replace(path)


def load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"configuration is not valid TOML: {exc}") from exc
    except PermissionError as exc:
        raise ConfigError(f"configuration file is not readable: {path}") from exc


def load_config_file(path: Path, *, registry: Registry | None = None) -> RootDocument:
    """Load a standalone configuration, optionally merged over shipped defaults."""
    document = load_toml(path)
    if registry is not None:
        document = merge_documents(shipped_defaults(), document)
    return config_from_document(document)


def shipped_defaults() -> dict[str, Any]:
    """The shipped ``defaults.toml`` as a document."""
    if not DEFAULT_CONFIG_TEXT_PATH.exists():  # pragma: no cover - packaging guard
        return {}
    return load_toml(DEFAULT_CONFIG_TEXT_PATH)


def config_from_document(document: Mapping[str, Any]) -> RootDocument:
    """Validate a merged document into :class:`RootDocument` with named problems."""
    version = document.get("config_version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise ConfigError(
            "configuration is invalid",
            [
                f"config_version {version!r} is not supported "
                f"(this build understands config_version {CONFIG_VERSION})"
            ],
        )
    try:
        parsed = RootDocument.model_validate(dict(document))
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
        raise ConfigError("configuration is invalid", problems) from exc
    _check_private_fields(parsed)
    return parsed


def _check_private_fields(document: RootDocument) -> None:
    """Reject obvious credentials in a file that is meant to stay shareable."""
    problems: list[str] = []
    for source_id, source in document.sources.items():
        for key, value in source.settings.items():
            lowered = key.lower()
            if (
                any(secret in lowered for secret in _SECRET_KEYS)
                and isinstance(value, str)
                and value
            ):
                problems.append(
                    f"sources.{source_id}.settings.{key}: looks like a credential; "
                    "Secrets belong in the macOS Keychain, not in config"
                )
    if problems:
        raise ConfigError("configuration contains credential-like values", problems)


def _theme_problems(app: AppSettings) -> list[str]:
    """Check the palette name and every override before anything is registered.

    Imported inside the function on purpose: the palette table lives in the UI layer, and
    core configuration must not import it at module load.
    """
    from ..ui.palettes import palette_names, resolve_palette

    try:
        resolve_palette(app.theme, app.theme_colors)
    except ValueError as error:
        return [f"app.theme: {error}"]
    _ = palette_names
    return []


def resolve_config(
    document: RootDocument,
    registry: Registry,
    *,
    paths: Paths,
    offline: bool = False,
    demo: bool = False,
    revision: int = 0,
) -> ResolvedConfig:
    """Cross-reference and type-check every registered reference. Raises ConfigError."""
    problems: list[str] = []
    problems.extend(_theme_problems(document.app))

    # ---- categories
    categories: dict[str, CategoryConfig] = {}
    for category in registry.categories():
        categories[category.id] = category
    for category_id, entry in document.categories.items():
        categories[category_id] = CategoryConfig(
            id=category_id, label=entry.label, order=entry.order, description=entry.description
        )

    # ---- sources
    sources: dict[str, SourceInstance] = {}
    for source_id, source_document in document.sources.items():
        spec = registry.source(source_document.type)
        if spec is None:
            problems.append(
                f"sources.{source_id}: unknown source type {source_document.type!r} "
                f"(registered: {', '.join(registry.source_types()) or 'none'})"
            )
            continue
        try:
            settings = spec.validate_settings(source_document.settings)
        except ValidationError as exc:
            for error in exc.errors():
                location = ".".join(str(part) for part in error["loc"])
                problems.append(f"sources.{source_id}.settings.{location}: {error['msg']}")
            continue
        cadence = _settings_cadence(settings, spec, source_document)
        if spec.cadence.import_driven and not source_document.enabled:
            pass
        sources[source_id] = SourceInstance(
            source_id=source_id,
            spec=spec,
            settings=settings,
            cadence=cadence,
            enabled=source_document.enabled,
            retention_seconds=(
                source_document.retention_seconds
                if source_document.retention_seconds is not None
                else spec.retention_seconds
            ),
            raw={"type": source_document.type, "enabled": source_document.enabled},
        )

    # ---- widgets
    widgets: dict[str, WidgetInstance] = {}
    for widget_id, widget_document in document.widgets.items():
        spec = registry.widget(widget_document.type)
        if spec is None:
            problems.append(
                f"widgets.{widget_id}: unknown widget type {widget_document.type!r} "
                f"(registered: {', '.join(registry.widget_types()) or 'none'})"
            )
            continue
        try:
            settings = spec.validate_settings(widget_document.settings)
        except ValidationError as exc:
            for error in exc.errors():
                location = ".".join(str(part) for part in error["loc"])
                problems.append(f"widgets.{widget_id}.settings.{location}: {error['msg']}")
            continue
        bindings: dict[str, str] = {}
        declared = set(widget_document.sources)
        known = {binding.name for binding in spec.inputs}
        for binding_name, source_id in widget_document.sources.items():
            binding = spec.binding(binding_name)
            if binding is None:
                problems.append(
                    f"widgets.{widget_id}.sources.{binding_name}: widget {spec.type_id!r} "
                    f"has no such input (declares: {', '.join(sorted(known)) or 'none'})"
                )
                continue
            instance = sources.get(source_id)
            if instance is None:
                if source_id in document.sources:
                    continue  # already reported as an invalid source
                problems.append(
                    f"widgets.{widget_id}.sources.{binding_name}: source instance "
                    f"{source_id!r} is not configured"
                )
                continue
            if instance.payload_schema not in binding.accepts:
                problems.append(
                    f"widgets.{widget_id}.sources.{binding_name}: {spec.type_id!r} accepts "
                    f"{sorted(binding.accepts)}, but {source_id!r} produces "
                    f"{instance.payload_schema!r}"
                )
                continue
            bindings[binding_name] = source_id
        for binding in spec.inputs:
            if binding.required and binding.name not in bindings and binding.name not in declared:
                problems.append(
                    f"widgets.{widget_id}.sources: missing required input {binding.name!r}"
                )
        if widget_document.category and widget_document.category not in categories:
            problems.append(
                f"widgets.{widget_id}.category: unknown category {widget_document.category!r}"
            )
        widgets[widget_id] = WidgetInstance(
            widget_id=widget_id,
            spec=spec,
            settings=settings,
            bindings=bindings,
            category=widget_document.category,
            title=widget_document.title,
            enabled=widget_document.enabled,
        )

    # ---- tabs (ordered list: replaced, never concatenated)
    tabs: list[TabConfig] = []
    seen_tabs: set[str] = set()
    for index, tab_document in enumerate(document.tabs):
        if tab_document.id in seen_tabs:
            problems.append(f"tabs: duplicate tab id {tab_document.id!r}")
            continue
        seen_tabs.add(tab_document.id)
        for widget_id in tab_document.widgets:
            if widget_id not in widgets:
                problems.append(
                    f"tabs[{index}] ({tab_document.id}): widget {widget_id!r} is not configured"
                )
        if (
            tab_document.category_filter
            and tab_document.category_filter not in categories
            and tab_document.category_filter != "all_registered"
        ):
            problems.append(
                f"tabs[{index}] ({tab_document.id}): unknown category filter "
                f"{tab_document.category_filter!r}"
            )
        tabs.append(
            TabConfig(
                id=tab_document.id,
                label=tab_document.label,
                layout=tab_document.layout,
                widgets=tuple(tab_document.widgets),
                visible=tab_document.visible,
                order=index,
                category_filter=tab_document.category_filter,
                settings=dict(tab_document.settings),
            )
        )
    if not tabs:
        problems.append("tabs: at least one tab is required")

    # ---- overview slots
    slots: dict[str, SlotSettings] = {}
    for slot_id, slot in document.overview.slots.items():
        for widget_id in slot.widgets:
            if widget_id not in widgets:
                problems.append(f"overview.slots.{slot_id}: widget {widget_id!r} is not configured")
        slots[slot_id] = slot

    if problems:
        raise ConfigError("configuration is invalid", problems)

    return ResolvedConfig(
        document=document,
        app=document.app,
        categories=categories,
        tabs=tuple(tabs),
        slots=slots,
        overview=document.overview,
        sources=sources,
        widgets=widgets,
        paths=paths,
        offline=offline,
        demo=demo,
        revision=revision,
    )


def validate_document(document: Mapping[str, Any], registry: Registry) -> RootDocument:
    """Validate a raw document without resolving against runtime paths."""
    return config_from_document(document)


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, Sequence) and not isinstance(value, str):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    return f'"{value!s}"'


def _toml_table(prefix: str, values: Mapping[str, Any], lines: list[str]) -> None:
    plain: list[tuple[str, Any]] = []
    nested: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in values.items():
        if isinstance(value, Mapping):
            nested.append((key, value))
        else:
            plain.append((key, value))
    if plain:
        lines.append(f"[{prefix}]")
        for key, value in plain:
            lines.append(f"{key} = {_toml_scalar(value)}")
        lines.append("")
    for key, value in nested:
        _toml_table(f"{prefix}.{key}", value, lines)


def render_default_config(registry: Registry, *, base_text: str | None = None) -> str:
    """Emit a complete, editable configuration.

    The shipped ``defaults.toml`` is the base so the six default tabs stay
    authoritative; registry default fragments for sources/widgets that are not
    already present are appended so a newly registered feature is configurable
    without hand-writing its section.
    """
    text = base_text if base_text is not None else _shipped_text()
    try:
        present = tomllib.loads(text) if text else {}
    except tomllib.TOMLDecodeError:  # pragma: no cover - shipped file is valid
        present = {}
    existing_sources = set(present.get("sources", {}))
    existing_widgets = set(present.get("widgets", {}))
    extra: list[str] = []
    for feature in registry.features:
        for source_id, fragment in feature.default_sources.items():
            if source_id in existing_sources:
                continue
            _toml_table(f"sources.{source_id}", fragment, extra)
            existing_sources.add(source_id)
        for widget_id, fragment in feature.default_widgets.items():
            if widget_id in existing_widgets:
                continue
            _toml_table(f"widgets.{widget_id}", fragment, extra)
            existing_widgets.add(widget_id)
    if not extra:
        return text
    separator = "" if text.endswith("\n") else "\n"
    if text and not text.endswith("\n\n"):
        separator += "\n"
    return text + separator + "\n".join(extra).rstrip() + "\n"


def _shipped_text() -> str:
    if not DEFAULT_CONFIG_TEXT_PATH.exists():  # pragma: no cover - packaging guard
        return ""
    return DEFAULT_CONFIG_TEXT_PATH.read_text(encoding="utf-8")
