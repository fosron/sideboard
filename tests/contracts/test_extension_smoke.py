"""Offline tests for a generated copy of the feature template.

The extension smoke test renders the template into a temporary directory, imports it,
registers it, mounts two widgets bound to one source plus a seventh tab and a new
category, asserts **one acquisition per source interval**, and then removes it again to
prove no polling task leaks. No core module is edited to make this work.
"""

from __future__ import annotations

import contextlib
import importlib
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sideboard.core.clock import FakeClock
from sideboard.core.config import config_from_document, resolve_config
from sideboard.core.contracts import (
    Connectivity,
    Demand,
    FeatureSpec,
    PresentInput,
    Provider,
    ProviderBuild,
    SourceEnvelope,
    SourcePayload,
    ViewContext,
    ViewMode,
    ViewModel,
)
from sideboard.core.history import BoundedHistory
from sideboard.core.paths import default_paths
from sideboard.core.registry import build_registry
from sideboard.core.scheduler import Scheduler
from sideboard.core.storage import CORE_MIGRATIONS, Database

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "feature"


def render_template(target: Path, *, feature_id: str, namespace: str, title: str) -> str:
    """Render the template into ``target`` and return the importable module name."""
    package_root = target / "sideboard_generated"
    package_root.mkdir(parents=True, exist_ok=True)
    (package_root / "__init__.py").write_text('"""Generated template package."""\n')
    feature_dir = package_root / feature_id
    feature_dir.mkdir(parents=True, exist_ok=True)
    (feature_dir / "__init__.py").write_text(f'"""Generated {feature_id} feature."""\n')
    body = (TEMPLATE_DIR / "feature.py.tmpl").read_text(encoding="utf-8")
    rendered = (
        body.replace("{{FEATURE_ID}}", feature_id)
        .replace("{{NAMESPACE}}", namespace)
        .replace("{{TITLE}}", title)
        .replace("{{CLASS}}", title)
    )
    (feature_dir / "feature.py").write_text(rendered, encoding="utf-8")
    if str(package_root.parent) not in sys.path:
        sys.path.insert(0, str(package_root.parent))
    return f"sideboard_generated.{feature_id}.feature"


@pytest.fixture
def generated_feature(tmp_path: Path) -> Iterator[FeatureSpec]:
    """A rendered, imported, unregistered feature spec."""
    module_name = render_template(
        tmp_path, feature_id="seventh", namespace="seventh", title="Seventh"
    )
    module = importlib.import_module(module_name)
    yield module.FEATURE
    for name in [name for name in sys.modules if name.startswith("sideboard_generated")]:
        del sys.modules[name]


def test_generated_template_compiles_and_registers(generated_feature: FeatureSpec) -> None:
    registry = build_registry((generated_feature,))
    assert registry.source_types() == ("seventh.file",)
    assert registry.widget_types() == ("seventh.list",)
    spec = registry.source("seventh.file")
    assert spec is not None
    assert spec.payload_schema_version == 1


def test_generated_template_widget_renders_all_states(generated_feature: FeatureSpec) -> None:
    widget = generated_feature.widget("seventh.list")
    assert widget is not None
    settings = widget.validate_settings({})
    for envelope in (
        None,
        SourceEnvelope(
            source_id="seventh",
            payload_schema="seventh.file",
            payload_schema_version=1,
            connectivity=Connectivity.UNAVAILABLE,
        ),
    ):
        view = widget.present(
            PresentInput(
                settings=settings,
                inputs={"primary": envelope},
                context=ViewContext(mode=ViewMode.COMPACT, width=40, height=12),
            )
        )
        assert isinstance(view, ViewModel)


class _Fixture:
    """A complete miniature application wired from the generated feature plus a config."""

    def __init__(self) -> None:
        self.clock = FakeClock(datetime(2026, 10, 13, 12, 0, tzinfo=UTC))

    def document(self) -> dict:
        return {
            "config_version": 1,
            "app": {"timezone": "UTC", "base_currency": "EUR"},
            "categories": {"operations": {"label": "Operations", "order": 5}},
            "sources": {
                "seventh": {
                    "type": "seventh.file",
                    "enabled": True,
                    "active_seconds": 10,
                    "settings": {"message": "tick"},
                }
            },
            "widgets": {
                "seventh_a": {
                    "type": "seventh.list",
                    "sources": {"primary": "seventh"},
                    "category": "operations",
                },
                "seventh_b": {
                    "type": "seventh.list",
                    "sources": {"primary": "seventh"},
                    "category": "operations",
                    "settings": {"limit": 2},
                },
            },
            "tabs": [
                {"id": "overview", "label": "Overview", "layout": "rotating_grid"},
                {"id": "money", "label": "Money", "layout": "detail"},
                {"id": "read", "label": "Read", "layout": "detail"},
                {"id": "watch", "label": "Watch", "layout": "detail"},
                {"id": "life", "label": "Life", "layout": "detail"},
                {"id": "systems", "label": "Systems", "layout": "detail"},
                {
                    "id": "operations",
                    "label": "Operations",
                    "layout": "detail",
                    "widgets": ["seventh_a", "seventh_b"],
                },
            ],
            "overview": {
                "slots": {"a": {"widgets": ["seventh_a"], "pinned": True}},
            },
        }


class CountingProvider:
    """Wraps a provider to count acquisitions, so "one per interval" is testable."""

    def __init__(self, inner: Provider | None) -> None:
        assert inner is not None, "the scheduler creates the provider on first use"
        self._inner = inner
        self.calls = 0

    async def fetch(self, context: ProviderBuild) -> SourcePayload:
        self.calls += 1
        return await self._inner.fetch(context)

    async def aclose(self) -> None:
        await self._inner.aclose()


async def test_two_widgets_share_one_acquisition_and_a_seventh_tab(
    generated_feature: FeatureSpec, tmp_path: Path
) -> None:
    registry = build_registry((generated_feature,))
    fixture = _Fixture()
    parsed = config_from_document(fixture.document())
    paths = default_paths(home=tmp_path, config_file=tmp_path / "config.toml")
    config = resolve_config(parsed, registry, paths=paths)
    assert len(config.tabs) == 7
    assert config.tab("operations") is not None
    # Two widgets consume one source instance: the reverse index proves it.
    assert set(config.widgets_by_source()["seventh"]) == {"seventh_a", "seventh_b"}

    database = await Database.open(tmp_path / "sideboard.sqlite3")
    await database.initialize(CORE_MIGRATIONS + registry.migrations())
    scheduler = Scheduler(
        database=database,
        clock=fixture.clock,
        history=BoundedHistory(database=database),
        config=config,
        client_factory=None,
    )
    try:
        await scheduler.start({})
        scheduler.set_active_tab("operations")
        runtime = scheduler.runtimes["seventh"]
        assert runtime.demand is Demand.ACTIVE
        assert runtime.task is not None
        # One collection task for two widgets, and the cadence comes from the source
        # instance, not from how many widgets are mounted.
        assert runtime.interval == 10.0

        await scheduler.refresh_now("seventh", force=True)
        envelope = scheduler.snapshot("seventh")
        assert envelope is not None and envelope.payload is not None
        assert envelope.connectivity is Connectivity.OK

        counted = CountingProvider(runtime.provider)
        runtime.provider = counted
        await scheduler.refresh_now("seventh", force=True)
        # Exactly one acquisition per refresh, never one per widget.
        assert counted.calls == 1
        # The next automatic acquisition is a full interval away.
        assert 9.0 <= runtime.next_due - fixture.clock.monotonic() <= 11.0

        presenter = generated_feature.widget("seventh.list")
        assert presenter is not None
        settings_model = presenter.settings_model
        view_a = presenter.present(
            PresentInput(
                settings=settings_model(),
                inputs={"primary": envelope},
                context=ViewContext(),
            )
        )
        view_b = presenter.present(
            PresentInput(
                settings=settings_model(limit=2),
                inputs={"primary": envelope},
                context=ViewContext(),
            )
        )
        assert len(view_b.items) == 2
        assert view_a.items[0].id == view_b.items[0].id
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_removal_through_config_reload_leaves_no_task(
    generated_feature: FeatureSpec, tmp_path: Path
) -> None:
    registry = build_registry((generated_feature,))
    fixture = _Fixture()
    paths = default_paths(home=tmp_path, config_file=tmp_path / "config.toml")
    config = resolve_config(config_from_document(fixture.document()), registry, paths=paths)
    database = await Database.open(tmp_path / "sideboard.sqlite3")
    await database.initialize(CORE_MIGRATIONS + registry.migrations())
    scheduler = Scheduler(
        database=database,
        clock=fixture.clock,
        history=BoundedHistory(database=database),
        config=config,
    )
    try:
        await scheduler.start({})
        scheduler.set_active_tab("operations")
        assert scheduler.runtimes["seventh"].task is not None
        document = fixture.document()
        del document["sources"]["seventh"]
        del document["widgets"]["seventh_a"]
        del document["widgets"]["seventh_b"]
        document["tabs"][6]["widgets"] = []
        document["overview"]["slots"] = {}
        reduced = resolve_config(config_from_document(document), registry, paths=paths, revision=2)
        actions = await scheduler.reconfigure(reduced)
        assert actions["seventh"] == "removed"
        assert "seventh" not in scheduler.runtimes
        assert scheduler.snapshot("seventh") is None
    finally:
        await scheduler.aclose()
        await database.aclose()


async def test_invalid_binding_and_missing_dependency_are_named_errors(
    generated_feature: FeatureSpec, tmp_path: Path
) -> None:
    registry = build_registry((generated_feature,))
    fixture = _Fixture()
    document = fixture.document()
    document["widgets"]["seventh_a"]["sources"] = {"primary": "does_not_exist"}
    paths = default_paths(home=tmp_path, config_file=tmp_path / "config.toml")
    with pytest.raises(Exception) as info:
        resolve_config(config_from_document(document), registry, paths=paths)
    assert "does_not_exist" in str(info.value) or "does_not_exist" in str(
        getattr(info.value, "problems", "")
    )

    document = fixture.document()
    document["widgets"]["seventh_a"]["type"] = "not.registered"
    with pytest.raises(Exception) as info2:
        resolve_config(config_from_document(document), registry, paths=paths)
    problems = getattr(info2.value, "problems", [])
    assert any("not.registered" in problem for problem in problems)


async def test_generated_feature_renders_in_a_headless_app_run(
    generated_feature: FeatureSpec, tmp_path: Path
) -> None:
    """The full requirement: register the template, mount it, run the app headlessly.

    Two widgets bound to one source, a seventh tab and a new category, one acquisition
    per source interval, and correct rendering in a real (headless) application run.
    """
    from sideboard.app import SideboardApp, build_runtime
    from sideboard.ui.overview import OverviewPane

    registry = build_registry((generated_feature,))
    fixture = _Fixture()
    paths = default_paths(
        home=tmp_path,
        config_file=tmp_path / "config" / "config.toml",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    runtime = await build_runtime(
        paths=paths,
        registry=registry,
        demo=False,
        config_document=fixture.document(),
    )
    app = SideboardApp(runtime)
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await runtime.scheduler.refresh_now("seventh", force=True)
            app._present_everything()
            await pilot.pause()
            # The seventh tab exists and the overview shows this feature's card.
            assert app.runtime.config.tab("operations") is not None
            assert app._overview is not None
            assert isinstance(app._overview, OverviewPane)
            card = app._overview.card("a")
            assert card is not None
            assert card.widget_id == "seventh_a"
            assert card.view is not None
            assert len(card.view.items) == 5  # the template widget's default limit
            assert card.view.items[0].id.startswith("seventh:")

            # Both widgets share the one collection task, and the cadence follows the
            # visible view: ambient while it only participates in the Overview rotation…
            assert len(runtime.scheduler.runtimes) == 1
            runtime_ = runtime.scheduler.runtimes["seventh"]
            assert runtime_.interval == 300.0
            # …and the configured 10 s once its own tab is visible.
            app._set_tab("operations", activate=True)
            await pilot.pause()
            assert runtime_.demand is Demand.ACTIVE
            assert runtime_.interval == 10.0
            # Scaling the feature down leaves no task behind.
            document = fixture.document()
            del document["sources"]["seventh"]
            del document["widgets"]["seventh_a"]
            del document["widgets"]["seventh_b"]
            document["tabs"][6]["widgets"] = []
            document["overview"]["slots"] = {}
            reduced = resolve_config(
                config_from_document(document), registry, paths=paths, revision=2
            )
            actions = await runtime.scheduler.reconfigure(reduced)
            assert actions["seventh"] == "removed"
            assert "seventh" not in runtime.scheduler.runtimes
    finally:
        with contextlib.suppress(Exception):
            await runtime.database.aclose()
