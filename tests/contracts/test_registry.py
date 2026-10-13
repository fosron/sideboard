"""Contract tests for the feature registry."""

from __future__ import annotations

import pytest

from sideboard.core.contracts import (
    API_VERSION,
    Cadence,
    FeatureSpec,
    ProviderBuild,
    SourcePayload,
    SourceSpec,
    WidgetSpec,
)
from sideboard.core.registry import RegistryError, build_registry
from sideboard.features import build_feature_registry


def _source(type_id: str = "demo.thing") -> SourceSpec:
    from pydantic import BaseModel

    from sideboard.core.contracts import SourcePayload

    class Settings(BaseModel):
        pass

    class Payload(SourcePayload):
        pass

    return SourceSpec(
        type_id=type_id,
        payload_model=Payload,
        settings_model=Settings,
        payload_schema=type_id,
        payload_schema_version=1,
        factory=lambda build: _NoopProvider(),
    )


class _NoopProvider:
    """A provider stub used only to build registry entries in tests."""

    async def fetch(self, context: ProviderBuild) -> SourcePayload:
        _ = context
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def _widget(type_id: str = "demo.widget") -> WidgetSpec:
    from pydantic import BaseModel

    from sideboard.core.contracts import InputBinding, ViewModel

    class Settings(BaseModel):
        pass

    return WidgetSpec(
        type_id=type_id,
        settings_model=Settings,
        inputs=(InputBinding(name="primary", accepts=frozenset({"demo.thing"})),),
        present=lambda inputs: ViewModel(),
    )


def test_registry_indexes_types() -> None:
    registry = build_registry([FeatureSpec(id="demo", sources=(_source(),), widgets=(_widget(),))])
    assert registry.source_types() == ("demo.thing",)
    assert registry.widget_types() == ("demo.widget",)
    assert registry.source("demo.thing") is not None
    assert registry.widget("demo.widget") is not None


def test_duplicate_ids_are_named_errors() -> None:
    with pytest.raises(RegistryError) as info:
        build_registry(
            [
                FeatureSpec(id="demo", sources=(_source(),), widgets=(_widget(),)),
                FeatureSpec(id="demo", sources=(), widgets=()),
            ]
        )
    assert any("duplicate feature id" in problem for problem in info.value.problems)

    with pytest.raises(RegistryError) as info2:
        build_registry(
            [
                FeatureSpec(id="one", sources=(_source(),), widgets=(_widget(),)),
                FeatureSpec(id="two", sources=(_source(),), widgets=(_widget(),)),
            ]
        )
    assert any("duplicate source type id" in problem for problem in info2.value.problems)
    assert any("duplicate widget type id" in problem for problem in info2.value.problems)


def test_incompatible_api_version_is_rejected() -> None:
    with pytest.raises(RegistryError) as info:
        build_registry([FeatureSpec(id="demo", api_version=API_VERSION + 1)])
    assert any("api_version" in problem for problem in info.value.problems)


def test_unnamespaced_type_ids_are_rejected() -> None:
    with pytest.raises(RegistryError) as info:
        build_registry([FeatureSpec(id="demo", sources=(_source("unscoped"),), widgets=())])
    assert any("namespaced" in problem for problem in info.value.problems)


def test_registered_features_are_self_consistent() -> None:
    """Every shipped feature must satisfy the shared contract vocabulary."""
    registry = build_feature_registry()
    assert len(registry.features) >= 10
    for spec in registry.sources():
        assert spec.payload_schema_version >= 1
        assert spec.retention_seconds > 0
        assert isinstance(spec.cadence, Cadence)
        assert spec.payload_schema
        if spec.import_driven:
            # An import-driven source must not own a recurring poll of a provider.
            assert spec.cadence.import_driven
    for widget in registry.widgets():
        for binding in widget.inputs:
            assert binding.accepts
    versions: dict[int, str] = {}
    for migration in registry.migrations():
        previous = versions.get(migration.version)
        assert previous is None or previous == migration.name, (
            f"conflicting migrations for version {migration.version}"
        )
        versions[migration.version] = migration.name
