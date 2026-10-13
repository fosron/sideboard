"""Contract tests for configuration validation, merging and reload semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from sideboard.core.config import (
    config_from_document,
    merge_documents,
    render_default_config,
    resolve_config,
    shipped_defaults,
)
from sideboard.core.errors import ConfigError
from sideboard.core.paths import default_paths
from sideboard.features import build_feature_registry


@pytest.fixture(scope="module")
def registry():
    return build_feature_registry()


@pytest.fixture
def paths(tmp_path: Path):
    return default_paths(home=tmp_path, config_file=tmp_path / "config.toml")


def test_shipped_defaults_resolve(registry, paths) -> None:
    resolved = resolve_config(config_from_document(shipped_defaults()), registry, paths=paths)
    ids = [tab.id for tab in resolved.tabs]
    assert ids == ["overview", "money", "read", "watch", "life", "systems"]
    assert resolved.tab("overview") is not None
    assert resolved.sources["mac"].enabled is True
    assert resolved.sources["mac"].cadence.active_seconds == 5.0
    assert resolved.sources["docker"].enabled is False
    assert resolved.widgets["portfolio"].bindings["positions"] == "holdings"
    # Two instances of one source type must not share an identity.
    assert (
        resolved.sources["weather_home"].spec.type_id
        == resolved.sources["weather_travel"].spec.type_id
    )
    assert (
        resolved.sources["weather_home"].source_id != resolved.sources["weather_travel"].source_id
    )


def test_lists_replace_and_tables_merge() -> None:
    base = {"tabs": [{"id": "a"}], "sources": {"mac": {"type": "mac.metrics", "enabled": True}}}
    override = {
        "tabs": [{"id": "b"}],
        "sources": {"mac": {"enabled": False}, "new": {"type": "news.feeds"}},
    }
    merged = merge_documents(base, override)
    # Ordered lists replace atomically; ID-indexed tables merge by key.
    assert merged["tabs"] == [{"id": "b"}]
    assert merged["sources"]["mac"] == {"type": "mac.metrics", "enabled": False}
    assert merged["sources"]["new"] == {"type": "news.feeds"}


def test_unknown_source_type_is_a_named_error(registry, paths) -> None:
    document = shipped_defaults()
    document["sources"]["mystery"] = {"type": "nope.nope", "enabled": True}
    with pytest.raises(ConfigError) as info:
        resolve_config(config_from_document(document), registry, paths=paths)
    assert any("unknown source type" in problem for problem in info.value.problems)


def test_wrong_payload_binding_is_rejected(registry, paths) -> None:
    document = shipped_defaults()
    document["widgets"]["weather"]["sources"] = {"primary": "mac"}
    with pytest.raises(ConfigError) as info:
        resolve_config(config_from_document(document), registry, paths=paths)
    problems = " ".join(info.value.problems)
    assert "accepts" in problems and "mac.metrics" in problems


def test_unknown_binding_name_is_rejected(registry, paths) -> None:
    document = shipped_defaults()
    document["widgets"]["weather"]["sources"] = {"nope": "weather_home"}
    with pytest.raises(ConfigError) as info:
        resolve_config(config_from_document(document), registry, paths=paths)
    assert any("no such input" in problem for problem in info.value.problems)


def test_missing_required_binding_is_rejected(registry, paths) -> None:
    document = shipped_defaults()
    document["widgets"]["weather"]["sources"] = {}
    with pytest.raises(ConfigError) as info:
        resolve_config(config_from_document(document), registry, paths=paths)
    assert any("missing required input" in problem for problem in info.value.problems)


def test_tab_referencing_unknown_widget_is_rejected(registry, paths) -> None:
    document = shipped_defaults()
    document["tabs"][1]["widgets"] = ["ghost"]
    with pytest.raises(ConfigError) as info:
        resolve_config(config_from_document(document), registry, paths=paths)
    assert any("not configured" in problem for problem in info.value.problems)


def test_unknown_category_is_rejected(registry, paths) -> None:
    document = shipped_defaults()
    document["widgets"]["news"]["category"] = "nope"
    with pytest.raises(ConfigError) as info:
        resolve_config(config_from_document(document), registry, paths=paths)
    assert any("unknown category" in problem for problem in info.value.problems)


def test_seventh_tab_and_new_category_need_no_python(registry, paths) -> None:
    document = shipped_defaults()
    document["categories"]["operations"] = {"label": "Operations", "order": 5}
    document["tabs"].append(
        {
            "id": "operations",
            "label": "Operations",
            "layout": "stack",
            "widgets": ["mac_summary", "docker_containers"],
        }
    )
    resolved = resolve_config(config_from_document(document), registry, paths=paths)
    assert [tab.id for tab in resolved.tabs][-1] == "operations"
    assert resolved.categories["operations"].label == "Operations"


def test_settings_validation_error_names_the_field(registry, paths) -> None:
    document = shipped_defaults()
    document["sources"]["mac"]["settings"]["volumes"] = "not-a-list"
    with pytest.raises(ConfigError) as info:
        resolve_config(config_from_document(document), registry, paths=paths)
    assert any("sources.mac.settings" in problem for problem in info.value.problems)


def test_docker_remote_endpoint_is_rejected_in_config(registry, paths) -> None:
    document = shipped_defaults()
    document["sources"]["docker"]["settings"]["context"] = "ssh://user@host"
    with pytest.raises(ConfigError) as info:
        resolve_config(config_from_document(document), registry, paths=paths)
    assert any("context" in problem for problem in info.value.problems)


def test_unsupported_config_version_is_named(registry, paths) -> None:
    document = shipped_defaults()
    document["config_version"] = 99
    with pytest.raises(ConfigError) as info:
        config_from_document(document)
    assert any("config_version" in problem for problem in info.value.problems)


def test_credential_like_settings_are_refused(registry, paths) -> None:
    document = shipped_defaults()
    document["sources"]["news"]["settings"]["api_key"] = "a-real-looking-secret"
    with pytest.raises(ConfigError) as info:
        config_from_document(document)
    assert any("credential" in problem for problem in info.value.problems)


def test_render_default_config_includes_registry_fragments(registry) -> None:
    text = render_default_config(registry)
    assert "config_version = 1" in text
    assert "[[tabs]]" in text
    assert "[sources.mac]" in text
    # Round-trips: the rendered text must be valid TOML that resolves.
    import tomllib

    parsed = tomllib.loads(text)
    document = config_from_document(parsed)
    assert len(document.tabs) >= 6


def test_demand_follows_the_visible_view(registry, paths) -> None:
    from sideboard.core.contracts import Demand

    resolved = resolve_config(config_from_document(shipped_defaults()), registry, paths=paths)
    assert resolved.demand_for_source("mac", active_tab="systems") is Demand.ACTIVE
    assert resolved.demand_for_source("mac", active_tab="money") is Demand.AMBIENT
    assert resolved.demand_for_source("mac", active_tab=None) is Demand.AMBIENT
    assert resolved.demand_for_source("markets", active_tab="systems") is Demand.NONE
