"""Registry of trusted feature modules.

Validates and looks up specs. Contains **no feature-specific branches**: the only
knowledge here is the contract vocabulary from :mod:`sideboard.core.contracts`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .contracts import API_VERSION, CategoryConfig, FeatureSpec, Migration, SourceSpec, WidgetSpec
from .errors import ConfigError

__all__ = ["Registry", "RegistryError", "build_registry"]


class RegistryError(ConfigError):
    """A registry-level problem: duplicate or incompatible registrations."""

    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        super().__init__(message, list(problems))


@dataclass(frozen=True, slots=True)
class Registry:
    """Immutable lookup over validated feature specs."""

    features: tuple[FeatureSpec, ...]
    _sources: Mapping[str, SourceSpec] = field(default_factory=dict, repr=False)
    _widgets: Mapping[str, WidgetSpec] = field(default_factory=dict, repr=False)
    _features_by_id: Mapping[str, FeatureSpec] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ lookups

    def feature(self, feature_id: str) -> FeatureSpec | None:
        return self._features_by_id.get(feature_id)

    def source(self, type_id: str) -> SourceSpec | None:
        return self._sources.get(type_id)

    def widget(self, type_id: str) -> WidgetSpec | None:
        return self._widgets.get(type_id)

    def sources(self) -> tuple[SourceSpec, ...]:
        return tuple(self._sources.values())

    def widgets(self) -> tuple[WidgetSpec, ...]:
        return tuple(self._widgets.values())

    def source_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._sources))

    def widget_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._widgets))

    def migrations(self) -> tuple[Migration, ...]:
        out: list[Migration] = []
        for feature in self.features:
            out.extend(feature.migrations)
        return tuple(out)

    def categories(self) -> tuple[CategoryConfig, ...]:
        seen: dict[str, CategoryConfig] = {}
        for feature in self.features:
            for category in feature.categories:
                seen.setdefault(category.id, category)
        return tuple(sorted(seen.values(), key=lambda c: (c.order, c.id)))

    def default_config_fragments(self) -> tuple[Mapping[str, Mapping[str, object]], ...]:
        """Per-feature ``(sources, widgets)`` default fragments for ``init``."""
        return tuple(
            {"sources": dict(f.default_sources), "widgets": dict(f.default_widgets)}
            for f in self.features
        )


def build_registry(features: Iterable[FeatureSpec]) -> Registry:
    """Validate specs and build a registry. Raises :class:`RegistryError`."""
    problems: list[str] = []
    feature_list: list[FeatureSpec] = []
    sources: dict[str, SourceSpec] = {}
    widgets: dict[str, WidgetSpec] = {}
    features_by_id: dict[str, FeatureSpec] = {}

    for feature in features:
        if not feature.id:
            problems.append("feature with empty id")
            continue
        if feature.id in features_by_id:
            problems.append(f"duplicate feature id: {feature.id}")
            continue
        if feature.api_version != API_VERSION:
            problems.append(
                f"feature {feature.id!r} declares api_version "
                f"{feature.api_version}, this build supports {API_VERSION}"
            )
            continue
        features_by_id[feature.id] = feature
        feature_list.append(feature)
        for spec in feature.sources:
            if (
                "." not in spec.type_id
                or spec.type_id.startswith(".")
                or spec.type_id.endswith(".")
            ):
                problems.append(
                    f"{feature.id}: source type {spec.type_id!r} must be namespaced "
                    f"as <namespace>.<name>"
                )
            if spec.type_id in sources:
                problems.append(
                    f"duplicate source type id: {spec.type_id} "
                    f"({sources[spec.type_id]!r} and {feature.id})"
                )
                continue
            if spec.payload_schema_version < 1:
                problems.append(f"{spec.type_id}: payload_schema_version must be >= 1")
            sources[spec.type_id] = spec
        for widget in feature.widgets:
            if (
                "." not in widget.type_id
                or widget.type_id.startswith(".")
                or widget.type_id.endswith(".")
            ):
                problems.append(
                    f"{feature.id}: widget type {widget.type_id!r} must be namespaced "
                    f"as <namespace>.<name>"
                )
            if widget.type_id in widgets:
                problems.append(f"duplicate widget type id: {widget.type_id}")
                continue
            for binding in widget.inputs:
                if not binding.accepts:
                    problems.append(
                        f"{widget.type_id}: binding {binding.name!r} accepts no payload schemas"
                    )
            widgets[widget.type_id] = widget

    if problems:
        raise RegistryError("feature registry is invalid", problems)
    return Registry(
        features=tuple(feature_list),
        _sources=sources,
        _widgets=widgets,
        _features_by_id=features_by_id,
    )
