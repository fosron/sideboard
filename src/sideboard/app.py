"""Sideboard application: composition root and UI orchestration.

``app.py`` contains no feature-specific branches. It loads configuration through the
registry, owns collection tasks, presentation, rotation, holds, local item state,
open-URL validation and configuration reload. Tabs, widgets, categories and rotation
queues are configuration entries.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import subprocess
import sys
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Static, TabbedContent, TabPane

from .core.capabilities import CommandRunner, HttpLimits, validate_public_url
from .core.clock import Clock, SystemClock, display_timezone
from .core.config import (
    AppSettings,
    ResolvedConfig,
    config_from_document,
    document_from_text,
    load_toml,
    merge_documents,
    resolve_config,
    set_config_value,
    shipped_defaults,
    write_config_text,
)
from .core.contracts import (
    Item,
    ItemState,
    RuntimeStatus,
    SourceEnvelope,
    ViewContext,
    ViewMode,
    ViewModel,
)
from .core.errors import BlockedError, ConfigError
from .core.history import BoundedHistory
from .core.items import ItemRecord, ItemStore
from .core.paths import Paths
from .core.present import classify_view, combine_envelopes, present_widget
from .core.registry import Registry
from .core.scheduler import Scheduler
from .core.storage import CORE_MIGRATIONS, Database, ensure_private_dir
from .features import build_feature_registry
from .ui.card import CardShell, freshness_line
from .ui.navigation import Box, neighbour
from .ui.overview import LayoutMode, OverviewPane, SlotSpec, layout_mode, slot_specs
from .ui.palettes import PALETTES, canonical_name, palette_names, resolve_palette
from .ui.rotation import Candidate, HoldReason, RotationController
from .ui.screens import (
    CommandPaletteScreen,
    DetailScreen,
    HelpScreen,
    MouseDiagnosticScreen,
    PaletteCommand,
    ThemePickerScreen,
)
from .ui.shell import DetailPane, FilterBar
from .ui.theme import build_rich_theme, build_theme

log = logging.getLogger("sideboard.app")

TICK_SECONDS = 0.25
STATUS_SECONDS = 1.0
MAINTENANCE_SECONDS = 30.0
# A repaint of one card is a few kilobytes; a pointer sweeping across the grid would
# otherwise repaint a card per event. The hold itself is applied immediately — only the
# repaint that shows it is coalesced, so the indicator can lag the pointer by at most
# this much while a fast sweep costs a bounded number of repaints.
HOVER_COALESCE_SECONDS = 0.2

TAB_KEYS = ("1", "2", "3", "4", "5", "6", "7", "8", "9")


@dataclass
class Runtime:
    """Everything the UI needs, assembled once at start-up."""

    paths: Paths
    registry: Registry
    config: ResolvedConfig
    database: Database
    items: ItemStore
    history: BoundedHistory
    scheduler: Scheduler
    commands: CommandRunner
    clock: Clock
    http_limits: HttpLimits
    revision: int = 1


async def build_runtime(
    *,
    paths: Paths,
    registry: Registry | None = None,
    offline: bool = False,
    demo: bool = False,
    config_document: Mapping[str, Any] | None = None,
) -> Runtime:
    """Open storage, run migrations, resolve configuration and start collection."""
    registry = registry or build_feature_registry()
    document = config_document
    if document is None:
        if paths.config_file.exists():
            document = merge_documents(shipped_defaults(), load_toml(paths.config_file))
        else:
            document = shipped_defaults()
    parsed = config_from_document(document)
    config = resolve_config(parsed, registry, paths=paths, offline=offline, demo=demo, revision=1)

    ensure_private_dir(paths.data_dir)
    ensure_private_dir(paths.log_dir)
    database = await Database.open(paths.database_file)
    await database.initialize(tuple(CORE_MIGRATIONS) + registry.migrations())

    clock = SystemClock()
    history = BoundedHistory(database=database)
    items = ItemStore(database)
    commands = CommandRunner(concurrency=1)
    limits = HttpLimits()

    cached = await database.load_envelopes(
        {source_id: instance.spec.payload_model for source_id, instance in config.sources.items()},
        now=clock.now(),
        retention={sid: instance.retention_seconds for sid, instance in config.sources.items()},
    )

    scheduler = Scheduler(
        database=database,
        clock=clock,
        history=history,
        config=config,
        commands=commands,
        file_roots=_file_roots(config),
        limits=limits,
        offline=offline,
        demo=demo,
        client_factory=_http_client,
    )
    await scheduler.start(cached)
    return Runtime(
        paths=paths,
        registry=registry,
        config=config,
        database=database,
        items=items,
        history=history,
        scheduler=scheduler,
        commands=commands,
        clock=clock,
        http_limits=limits,
    )


def _file_roots(config: ResolvedConfig) -> dict[str, tuple[str, ...]]:
    """Only explicitly configured paths may be read by a file-backed source."""
    roots: dict[str, tuple[str, ...]] = {}
    for source_id, instance in config.sources.items():
        paths: list[str] = []
        for value in instance.settings.model_dump().values():
            if isinstance(value, str) and value.startswith(("/", "~")):
                paths.append(value)
            elif isinstance(value, list):
                paths.extend(str(item) for item in value if str(item).startswith(("/", "~")))
        if paths:
            roots[source_id] = tuple(paths)
    return roots


def _http_client() -> Any:
    import httpx

    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
        follow_redirects=False,
        headers={"User-Agent": "Sideboard/0.1 (+local dashboard)"},
    )


class SideboardApp(App[None]):
    """The dashboard."""

    CSS_PATH = "ui/theme.tcss"
    TITLE = "Sideboard"
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, runtime: Runtime, *, mouse: bool = True) -> None:
        super().__init__()
        # No animation anywhere. App.__init__ assigns animation_level from the
        # environment, so a class attribute would be shadowed: this has to come after
        # super().__init__(). Textual animates the tab underline for 0.3 s at 60 fps,
        # and each of those frames repainted the whole screen — nineteen full-screen
        # paints in the first third of a second of every run.
        self.animation_level = "none"
        # Hover interaction can be disabled for a terminal that misreports motion; the
        # keyboard controls stay fully usable.
        self.mouse_enabled = mouse
        # Register and select the theme before the stylesheet is parsed: the semantic
        # tokens ($sb-dim and friends) must exist when the CSS variables are resolved.
        # The palette is configuration: the Textual half now, the Rich half in on_load.
        self._apply_palette(runtime.config.app, push_rich=False)
        self.runtime = runtime
        self.registry = runtime.registry
        self.privacy = runtime.config.app.privacy_mode
        self.timezone = runtime.config.app.timezone
        self.offline = runtime.config.offline
        self.demo = runtime.config.demo
        self.paused = False
        self._user_navigation = False
        self._runtime_pins: set[str] = set()
        self._item_state: dict[str, ItemState] = {}
        self._saved_ids: frozenset[str] = frozenset()
        self._saved_items: tuple[Item, ...] = ()
        self._read_ids: frozenset[str] = frozenset()
        self._runtime_status: RuntimeStatus | None = None
        self._rotation: RotationController | None = None
        self._overview: OverviewPane | None = None
        self._detail_panes: dict[str, DetailPane] = {}
        self._filter_bars: dict[str, FilterBar] = {}
        self._last_second: int = -1
        self._active_tab: str | None = None
        self._hold_timers: dict[str, Any] = {}
        self._last_footer_status = ""
        self._last_footer = ""
        self._reload_note = ""
        self._mouse_screen: MouseDiagnosticScreen | None = None
        self._unsubscribe: Any = None
        self._tab_signature: tuple[str, ...] = ()
        self._pending_slot_repaints: set[str] = set()
        self._rich_theme_pushed = False
        #: The palette in force right now. A session switch sets this; a reload resets it
        #: to the configured value, which stays the single source of truth on disk.
        self._session_theme: str | None = None
        self._slot_repaint_timer: Any = None

    # ------------------------------------------------------------------ bindings

    BINDINGS: list[BindingType] = [  # type: ignore[assignment]
        Binding("ctrl+p", "palette", "palette"),
        Binding("ctrl+r", "reload_config", "reload config"),
        Binding("space", "toggle_pause", "pause"),
        Binding("p", "pin", "pin"),
        Binding("n", "advance", "next card"),
        Binding("o", "open_url", "open link"),
        Binding("s", "save_item", "save"),
        Binding("r", "refresh_source", "refresh"),
        Binding("v", "toggle_privacy", "privacy"),
        Binding("t", "next_theme", "next palette"),
        Binding("ctrl+t", "theme_picker", "palette"),
        Binding("question_mark", "help", "help"),
        Binding("ctrl+y", "reset_holds", "reset holds"),
        Binding("q", "quit", "quit"),
    ] + [
        Binding(key, f"select_tab({index})", f"tab {index}", show=False)
        for index, key in enumerate(TAB_KEYS, start=1)
    ]

    # ------------------------------------------------------------------ compose

    def compose(self) -> ComposeResult:
        # The tabs are the first row: the application name and the clock live in the
        # footer, so the window gives one more row to the cards.
        yield TabbedContent(id="sb-body")
        yield Horizontal(
            Static(id="sb-footer-status"),
            Static("? help", id="sb-footer-help"),
            id="sb-footer",
        )

    # ------------------------------------------------------------------ lifecycle

    async def on_mount(self) -> None:
        self._runtime_status = self.runtime.scheduler.runtime_status()
        await self._load_privacy_mode()
        await self._load_item_state()
        await self._build_tabs()
        self._present_everything()
        self.set_interval(TICK_SECONDS, self._on_tick)
        self.set_interval(STATUS_SECONDS, self._on_status_tick)
        self.set_interval(MAINTENANCE_SECONDS, self._on_maintenance)
        self._unsubscribe = self.runtime.scheduler.subscribe(self._on_source_updated)
        self._update_chrome(force=True)

    async def on_unmount(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        with contextlib.suppress(Exception):
            await self.runtime.scheduler.aclose()
        with contextlib.suppress(Exception):
            await self.runtime.database.aclose()

    # ------------------------------------------------------------------ structure

    def _visible_tabs(self) -> tuple[Any, ...]:
        return tuple(tab for tab in self.runtime.config.tabs if tab.visible)

    async def _build_tabs(self, *, preserve: str | None = None) -> None:
        """(Re)build every tab pane from configuration. Used at mount and on reload."""
        content = self.query_one("#sb-body", TabbedContent)
        self._overview = None
        self._detail_panes.clear()
        self._filter_bars.clear()
        await content.clear_panes()
        for tab in self._visible_tabs():
            pane = TabPane(tab.label, id=f"tab-{tab.id}")
            await content.add_pane(pane)
            if self._is_overview(tab.id):
                overview = OverviewPane(id=f"sb-pane-{tab.id}", classes="sb-pane")
                await pane.mount(overview)
                self._overview = overview
            else:
                if tab.layout == "filterable":
                    bar = FilterBar(filters=self._filters_for(tab.id), id=f"sb-filter-{tab.id}")
                    self._filter_bars[tab.id] = bar
                    await pane.mount(bar)
                detail = DetailPane(id=f"sb-detail-{tab.id}", classes="sb-pane")
                await pane.mount(detail)
                await detail.rebuild(self._tab_widgets(tab.id), tab.layout, labels=self._labels())
                self._detail_panes[tab.id] = detail
        self._tab_signature = tuple(tab.id for tab in self._visible_tabs())
        target = preserve if preserve in self._tab_signature else self._first_tab_id()
        self._active_tab = target
        if self._overview is not None:
            await self._rebuild_overview()
        if target is not None:
            self._set_tab(target, activate=True)

    def _first_tab_id(self) -> str | None:
        for tab in self._visible_tabs():
            return tab.id
        return None

    def _is_overview(self, tab_id: str) -> bool:
        tab = self.runtime.config.tab(tab_id)
        return bool(tab and (tab.id == "overview" or tab.layout == "rotating_grid"))

    def _tab_widgets(self, tab_id: str) -> tuple[str, ...]:
        tab = self.runtime.config.tab(tab_id)
        if tab is None:
            return ()
        if tab.layout != "filterable":
            return tab.widgets
        bar = self._filter_bars.get(tab_id)
        active = bar.active if bar is not None else None
        if active in tab.widgets:
            return (active,)
        return tab.widgets[:1]

    def _filters_for(self, tab_id: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for widget_id in self.runtime.config.widgets_for_tab(tab_id):
            instance = self.runtime.config.widget(widget_id)
            if instance is None:
                continue
            label = instance.title or instance.spec.type_id.split(".", 1)[-1].replace("_", " ")
            out.append((widget_id, label.title()))
        return out

    def _labels(self) -> dict[str, str]:
        return {
            widget_id: (instance.title or widget_id.replace("_", " ").title())
            for widget_id, instance in self.runtime.config.widgets.items()
        }

    # ------------------------------------------------------------------ rotation

    def _build_rotation(self) -> None:
        slots, pinned, dwell = self._slot_source()
        self._rotation = RotationController(
            slots=slots,
            pinned=pinned,
            dwell=dwell,
            labels={slot_id: slot_id.upper() for slot_id in slots},
            clock_now=self.runtime.clock.monotonic,
            classify=self._classify,
            grace_seconds=self.runtime.config.overview.resume_grace_seconds,
            keyboard_hold_seconds=self.runtime.config.overview.keyboard_hold_seconds,
        )

    def _slot_source(self) -> tuple[dict[str, list[str]], dict[str, bool], dict[str, float]]:
        config = self.runtime.config
        slots: dict[str, list[str]] = {}
        pinned: dict[str, bool] = {}
        dwell: dict[str, float] = {}
        for slot_id, slot in config.slots.items():
            slots[slot_id] = [w for w in slot.widgets if w in config.widgets]
            pinned[slot_id] = slot.pinned
            dwell[slot_id] = slot.dwell_seconds
        for widget_id in sorted(self._runtime_pins):
            if widget_id not in config.widgets:
                continue
            target = next((sid for sid, is_pinned in pinned.items() if is_pinned), None)
            if target is None:
                target = next(iter(slots), "a")
                slots.setdefault(target, [])
                pinned[target] = True
                dwell.setdefault(
                    target, config.slots[target].dwell_seconds if target in config.slots else 40.0
                )
            slots[target] = [widget_id] + [w for w in slots.get(target, []) if w != widget_id]
        if not slots and config.widgets:
            slots["a"] = [next(iter(config.widgets))]
            pinned["a"] = True
            dwell["a"] = 40.0
        return slots, pinned, dwell

    async def _rebuild_overview(self, mode: str | None = None) -> None:
        pane = self._overview
        if pane is None:
            return
        batch = self.batch_update()
        batch.__enter__()
        self._build_rotation()
        resolved = mode or self._layout_mode()
        slots, pinned, dwell = self._slot_source()
        specs = slot_specs(slots, dwell, pinned, mode=resolved)
        await pane.rebuild(resolved, specs)
        rotation = self._rotation
        if rotation is not None:
            rotation.reconfigure(
                slots={spec.slot_id: spec.queue for spec in specs},
                pinned={spec.slot_id: spec.pinned for spec in specs},
                dwell={spec.slot_id: spec.dwell for spec in specs},
                labels={spec.slot_id: spec.label for spec in specs},
            )
            rotation.initialize()
        batch.__exit__(None, None, None)
        self._present_overview()

    def _layout_mode(self) -> LayoutMode:
        pane = self._overview
        width = pane.size.width if pane is not None and pane.size.width else self.size.width
        height = pane.size.height if pane is not None and pane.size.height else self.size.height
        return layout_mode(width or 120, height or 40)

    # ------------------------------------------------------------------ present

    def _view_context(
        self,
        *,
        mode: ViewMode = ViewMode.COMPACT,
        selected_id: str | None = None,
        held: bool = False,
        width: int = 80,
        height: int = 24,
    ) -> ViewContext:
        return ViewContext(
            mode=mode,
            width=width,
            height=height,
            privacy=self.privacy,
            now=self.runtime.clock.now(),
            timezone=self.timezone,
            selected_id=selected_id,
            held=held,
            offline=self.offline,
            demo=self.demo,
            saved_ids=self._saved_ids,
            saved_items=self._saved_items,
            read_ids=self._read_ids,
            item_states=self._item_state,
            runtime=self._runtime_status,
        )

    def present(
        self,
        widget_id: str,
        *,
        mode: ViewMode = ViewMode.COMPACT,
        selected_id: str | None = None,
        held: bool = False,
        width: int = 80,
        height: int = 24,
    ) -> tuple[ViewModel | None, Any]:
        instance = self.runtime.config.widget(widget_id)
        if instance is None:
            return None, None
        # Presenters receive snapshots keyed by their declared binding names, not by
        # source-instance ids: a widget must not need to know how its source is named.
        envelopes: dict[str, SourceEnvelope | None] = {
            binding: self.runtime.scheduler.snapshot(source_id)
            for binding, source_id in instance.bindings.items()
        }
        context = self._view_context(
            mode=mode, selected_id=selected_id, held=held, width=width, height=height
        )
        view = self._decorate(present_widget(instance, envelopes, context))
        return view, combine_envelopes(envelopes)

    def _decorate(self, view: ViewModel) -> ViewModel:
        """Apply local read/saved state to items. Purely presentational."""
        if not view.items:
            return view
        items = []
        changed = False
        for item in view.items:
            state = self._item_state.get(item.id)
            saved = bool(state and state.saved) or item.saved
            read = bool(state and state.read) or item.read
            if saved != item.saved or read != item.read:
                changed = True
                items.append(replace(item, saved=saved, read=read))
            else:
                items.append(item)
        return replace(view, items=tuple(items)) if changed else view

    def _classify(self, widget_id: str) -> Candidate:
        view, _ = self.present(widget_id)
        if view is None:
            return Candidate.EMPTY
        try:
            return Candidate(classify_view(view))
        except ValueError:  # pragma: no cover - classification keeps its own vocabulary
            return Candidate.READY

    def _present_card(
        self,
        card: CardShell,
        widget_id: str | None,
        *,
        mode: ViewMode = ViewMode.COMPACT,
        held: bool = False,
        hold_reasons: tuple[str, ...] = (),
        pinned: bool = False,
    ) -> None:
        if widget_id is None:
            card.set_card(
                widget_id=None,
                title="",
                view=None,
                envelope=None,
                freshness="no card for this slot",
                held=held,
                hold_reasons=hold_reasons,
                pinned=pinned,
            )
            return
        instance = self.runtime.config.widget(widget_id)
        if instance is None:
            card.set_card(
                widget_id=widget_id,
                title=widget_id,
                view=None,
                envelope=None,
                freshness="widget removed from configuration",
                held=held,
                hold_reasons=hold_reasons,
            )
            return
        view, envelope = self.present(
            widget_id,
            mode=mode,
            selected_id=card.selected_id(),
            held=held,
            width=card.size.width or 40,
            height=card.size.height or 12,
        )
        # The card title must say what the card is. The type id gives every widget of a
        # family the same name ("Summary" for the portfolio, the weather and the Mac), and
        # two weather cards then look identical while showing two different cities. A
        # configured title wins; otherwise the presenter's own title does, because it
        # already carries the distinction ("Portfolio", "Torrevieja", "Mac").
        title = (
            instance.title
            or (view.title if view is not None else "")
            or instance.spec.type_id.split(".", 1)[-1].replace("_", " ").title()
        )
        freshness = freshness_line(
            envelope,
            now=self.runtime.clock.now(),
            timezone=self.timezone,
            held_reasons=hold_reasons,
            demo=self.demo,
            offline=self.offline,
            pinned=pinned,
        )
        card.set_card(
            widget_id=widget_id,
            title=title,
            view=view,
            envelope=envelope,
            freshness=freshness,
            held=held,
            hold_reasons=hold_reasons,
            pinned=pinned,
            privacy=self.privacy,
            mode=mode,
            timezone=self.timezone,
            now=self.runtime.clock.now(),
            offline=self.offline,
            demo=self.demo,
            min_width=instance.spec.min_width,
            min_height=instance.spec.min_height,
        )

    def _schedule_slot_repaint(self, slot_id: str) -> None:
        """Ask for a slot repaint, coalescing bursts from a moving pointer."""
        self._pending_slot_repaints.add(slot_id)
        if self._slot_repaint_timer is None:
            self._slot_repaint_timer = self.set_timer(
                HOVER_COALESCE_SECONDS, self._flush_slot_repaints
            )

    def _flush_slot_repaints(self) -> None:
        self._slot_repaint_timer = None
        pending = sorted(self._pending_slot_repaints)
        self._pending_slot_repaints.clear()
        with self.batch_update():
            for slot_id in pending:
                self._present_slot(slot_id)

    def _present_slot(self, slot_id: str) -> None:
        """Re-present a single overview slot, for a change that only affects it."""
        pane = self._overview
        if pane is None:
            return
        for spec in pane.specs:
            if spec.slot_id == slot_id:
                self._present_overview_slot(spec)
                return

    def _present_overview(self) -> None:
        pane = self._overview
        if pane is None or self._rotation is None:
            return
        # One repaint for the whole grid: without this, presenting four cards produced
        # four separate paints, and a tab switch produced about twenty.
        with self.batch_update():
            for spec in pane.specs:
                self._present_overview_slot(spec)

    def _present_overview_slot(self, spec: SlotSpec) -> None:
        """Present one slot's card. Used directly by hover, which affects one slot."""
        pane = self._overview
        rotation = self._rotation
        if pane is None or rotation is None:
            return
        card = pane.card(spec.slot_id)
        if card is None:
            return
        self._present_card(
            card,
            rotation.current(spec.slot_id),
            mode=ViewMode.COMPACT,
            held=rotation.held(spec.slot_id),
            hold_reasons=rotation.hold_reasons(spec.slot_id),
            pinned=spec.pinned,
        )

    def _present_detail(self, tab_id: str) -> None:
        pane = self._detail_panes.get(tab_id)
        if pane is None:
            return
        if tab_id != self._active_tab:
            # A hidden pane still has cards, and presenting them assigns heights, which
            # marks the screen for layout. Doing that for five tabs at start-up produced
            # twenty compositor updates in the first second. The tab is presented the
            # moment it is shown, which is the first time anyone can see it.
            return
        if tuple(self._tab_widgets(tab_id)) != pane.widget_ids:
            self.run_worker(self._rebuild_filter_pane(tab_id), exclusive=False)
            return
        with self.batch_update():
            for widget_id in pane.widget_ids:
                card = pane.card(widget_id)
                if card is not None:
                    # A tab card is the full card: it owns a whole pane and scrolls. The
                    # Overview's half-width slots are the ones that get the compact view,
                    # which is why the two are different modes rather than one card size.
                    self._present_card(card, widget_id, mode=ViewMode.DETAIL)

    async def _rebuild_filter_pane(self, tab_id: str) -> None:
        pane = self._detail_panes.get(tab_id)
        tab = self.runtime.config.tab(tab_id)
        if pane is None or tab is None:
            return
        await pane.rebuild(self._tab_widgets(tab_id), tab.layout, labels=self._labels())
        self._present_detail(tab_id)

    def _present_everything(self) -> None:
        """Present what can be seen: the rotating Overview and the active tab."""
        with self.batch_update():
            self._present_overview()
            if self._active_tab is not None:
                self._present_detail(self._active_tab)

    # ------------------------------------------------------------------ ticks

    def _on_tick(self) -> None:
        rotation = self._rotation
        if rotation is None:
            return
        if not self.paused and rotation.tick():
            self._present_overview()

    def _on_status_tick(self) -> None:
        self._runtime_status = self.runtime.scheduler.runtime_status()
        # Neither of these may be unconditional every second:
        #   * set_active_tab rebuilds the demand index and can start or stop tasks, and
        #     the tab only changes on a user action (`_set_tab` already does it);
        #   * Static.update() defaults to layout=True, so forcing the chrome would run a
        #     full screen layout twice a second for a clock that changes once a minute.
        self._update_chrome()
        now = int(self.runtime.clock.monotonic())
        if now != self._last_second:
            self._last_second = now
            self._present_overview()
            if self._active_tab is not None:
                self._present_detail(self._active_tab)

    async def _on_maintenance(self) -> None:
        with contextlib.suppress(Exception):
            await self.runtime.history.flush()
        with contextlib.suppress(Exception):
            await self.runtime.history.prune(self.runtime.clock.monotonic())
        with contextlib.suppress(Exception):
            await self.runtime.items.purge_expired(self.runtime.clock.now())

    def _update_chrome(self, *, force: bool = False) -> None:
        status = self._runtime_status
        labels: list[str] = []
        local = self.runtime.clock.now().astimezone(display_timezone(self.timezone))
        labels.append(local.strftime("%H:%M"))
        if self.demo:
            labels.append("DEMO DATA")
        if self.offline:
            labels.append("OFFLINE")
        if self.privacy:
            labels.append("PRIVACY")
        if self.paused:
            labels.append("PAUSED")
        if status is not None:
            if status.stale_count:
                labels.append(f"{status.stale_count} stale")
            if status.unavailable_count:
                labels.append(f"{status.unavailable_count} unavailable")
        if self._reload_note:
            labels.append(self._reload_note)
        line = "Sideboard · " + " · ".join(labels)
        if force or line != self._last_footer_status:
            self._last_footer_status = line
            self._update_static("#sb-footer-status", line)
        # One hint, not a keyboard reference: the dashboard is read from across a room, and
        # `?` lists every key for the moment someone actually needs one.
        footer = "? help"
        if force or footer != self._last_footer:
            self._last_footer = footer
            self._update_static("#sb-footer", footer)

    def _update_static(self, selector: str, text: str) -> None:
        """Update a chrome widget if it is currently mounted.

        The status line and footer are cosmetic: a timer tick that lands during mount or
        teardown must never raise, because that would surface as a crash in the middle of
        an interaction.
        """
        for widget in self.query(selector):
            if isinstance(widget, Static):
                # layout=False: the status line and footer are one row tall whatever they
                # say. Textual's default asks for a layout, which turned a changed clock
                # digit into a full-screen reflow and repaint.
                widget.update(text, layout=False)
            return

    # ------------------------------------------------------------------ events

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab_id = self._tab_id_for(str(event.tab.id or ""))
        if tab_id and tab_id != self._active_tab:
            self._set_tab(tab_id, activate=False)

    def _tab_id_for(self, raw: str) -> str | None:
        """Recover a configured tab id from Textual's sanitised pane id.

        ``TabbedContent`` renames pane ids (``--content-tab-<id>``), so the configured
        id is matched by suffix instead of by string surgery.
        """
        for tab in self._visible_tabs():
            if raw.endswith(f"-{tab.id}") or raw == tab.id:
                return str(tab.id)
        return None

    def _pane_id(self, tab_id: str) -> str:
        contents = [
            widget for widget in self.query("#sb-body") if isinstance(widget, TabbedContent)
        ]
        if not contents:
            return f"tab-{tab_id}"
        content = contents[0]
        for pane in content.query(TabPane):
            pane_id = str(pane.id or "")
            if pane_id.endswith(f"-{tab_id}") or pane_id == f"tab-{tab_id}":
                return pane_id
        return f"tab-{tab_id}"

    def _set_tab(self, tab_id: str, *, activate: bool = True) -> None:
        """Switch to a tab, presenting its cards *before* it becomes visible.

        Presenting first means the pane's card heights are set while it is still hidden,
        so making it visible needs one layout and one paint. Doing it the other way round
        painted the newly visible pane, then re-laid it out when the heights arrived, then
        painted again — several full screen writes for one key press.
        """
        # One batch for the whole switch: activating the pane, presenting its cards,
        # laying them out and moving focus each used to trigger its own full-screen paint.
        with self.batch_update():
            self._active_tab = tab_id
            self.runtime.scheduler.set_active_tab(tab_id)
            self._present_detail(tab_id)
            pane = self._detail_panes.get(tab_id)
            if pane is not None:
                # Laid out once, now that the cards hold their data: a card's height
                # follows its content at layout time, and later refreshes only repaint it.
                pane.apply_heights()
            if activate:
                with contextlib.suppress(Exception):
                    self.query_one("#sb-body", TabbedContent).active = self._pane_id(tab_id)
            cards = self._cards_for_tab(tab_id)
            if cards:
                self._user_navigation = False
                cards[0].focus()

    def _cards_for_tab(self, tab_id: str) -> list[CardShell]:
        if self._is_overview(tab_id) and self._overview is not None:
            return [
                card
                for spec in self._overview.specs
                if (card := self._overview.card(spec.slot_id)) is not None
            ]
        pane = self._detail_panes.get(tab_id)
        if pane is None:
            return []
        return [card for card in (pane.card(w) for w in pane.widget_ids) if card is not None]

    def on_card_shell_hold_changed(self, event: CardShell.HoldChanged) -> None:
        rotation = self._rotation
        slot_id = self._slot_of_card(event.card_key)
        if rotation is None or slot_id is None:
            return
        if not self.runtime.config.overview.hover_pause:
            return
        if str(event.reason) != str(HoldReason.HOVER) or not self.mouse_enabled:
            return
        if event.active:
            self._cancel_hold_timer(slot_id)
            if rotation.set_hold(slot_id, HoldReason.HOVER, True):
                # One slot, not all four, and coalesced: a pointer sweep across the
                # grid used to re-present every card on every transition, each with a
                # layout pass.
                self._schedule_slot_repaint(slot_id)
        else:
            self._arm_hover_release(slot_id)

    def on_card_shell_selection_changed(self, event: CardShell.SelectionChanged) -> None:
        self._apply_interaction_hold(event.card_key)

    def _slot_of_card(self, card_key: str) -> str | None:
        rotation = self._rotation
        if rotation is None:
            return None
        return card_key if card_key in rotation.slots else None

    def _arm_hover_release(self, slot_id: str) -> None:
        """Pointer left the card: release the hold now and let the controller keep grace.

        The snapshot is reapplied immediately; the rotation controller suspends the
        remaining dwell for ``resume_grace_seconds`` on its own.
        """
        rotation = self._rotation
        if rotation is None:
            return
        self._cancel_hold_timer(slot_id)
        if rotation.set_hold(slot_id, HoldReason.HOVER, False):
            self._schedule_slot_repaint(slot_id)

    def _cancel_hold_timer(self, slot_id: str) -> None:
        timer = self._hold_timers.pop(slot_id, None)
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.stop()

    def _apply_interaction_hold(self, card_key: str) -> None:
        rotation = self._rotation
        slot_id = self._slot_of_card(card_key)
        if rotation is None or slot_id is None:
            return
        rotation.set_hold(slot_id, HoldReason.INTERACTION, True)
        self._cancel_hold_timer(slot_id)

        def release() -> None:
            self._hold_timers.pop(slot_id, None)
            rotation.set_hold(slot_id, HoldReason.INTERACTION, False)
            self._present_overview()

        self._hold_timers[slot_id] = self.set_timer(rotation.keyboard_hold_seconds, release)
        self._schedule_slot_repaint(slot_id)

    def on_app_blur(self) -> None:
        """Terminal blur: drop stale pointer holds. Never pause globally."""
        rotation = self._rotation
        if rotation is not None:
            rotation.clear_pointer_holds()
        for card in self._all_cards():
            card.clear_pointer()
        if self._mouse_screen is not None:
            self._mouse_screen.note_blur()

    def on_resize(self, event: Any) -> None:
        pane = self._overview
        if pane is None:
            return
        mode = layout_mode(event.size.width, event.size.height)
        if mode != pane.mode:
            # The pane's own size is stale during the resize event, so the mode computed
            # from the event is what the rebuild must use.
            self.run_worker(self._rebuild_overview(mode), exclusive=True, group="overview-layout")

    def on_card_shell_activated(self, event: CardShell.Activated) -> None:
        widget_id = event.widget_id
        card = self._card_by_key(event.card_key)
        if widget_id is None or card is None:
            return
        view, _ = self.present(
            widget_id,
            mode=ViewMode.DETAIL,
            width=max(40, self.size.width - 8),
            height=max(10, self.size.height - 8),
        )
        if view is None:
            return
        instance = self.runtime.config.widget(widget_id)
        title = instance.title if instance is not None and instance.title else widget_id
        self._apply_global_hold(HoldReason.MODAL, True)
        provenance = freshness_line(
            card.envelope,
            now=self.runtime.clock.now(),
            timezone=self.timezone,
            held_reasons=card.hold_reasons,
            demo=self.demo,
            offline=self.offline,
            pinned=card.is_pinned,
            verbose=True,
        )
        self.push_screen(
            DetailScreen(
                title=title,
                view=view,
                privacy=self.privacy,
                url_provider=card.selected_url,
                item_provider=card.selected_id,
                selected_index=card.cursor_index,
                provenance=provenance,
            ),
            callback=self._after_modal,
        )

    def _after_modal(self, _result: object = None) -> None:
        self._apply_global_hold(HoldReason.MODAL, False)

    def _apply_global_hold(self, reason: HoldReason, active: bool) -> None:
        rotation = self._rotation
        if rotation is not None:
            rotation.set_global_hold(reason, active)

    def on_filter_bar_filter_changed(self, event: FilterBar.FilterChanged) -> None:
        if self._active_tab:
            self.run_worker(self._rebuild_filter_pane(self._active_tab), exclusive=False)

    def on_mouse_diagnostic_screen_reset_holds(
        self, event: MouseDiagnosticScreen.ResetHolds
    ) -> None:
        _ = event
        self._reset_holds()

    def on_detail_screen_save_requested(self, event: DetailScreen.SaveRequested) -> None:
        self.run_worker(self._toggle_saved(event.item_id))

    def on_palette_command(self, event: PaletteCommand) -> None:
        self._run_command(event.command_id)

    class SourceUpdated(Message):
        def __init__(self, source_id: str) -> None:
            self.source_id = source_id
            super().__init__()

    def _on_source_updated(self, source_id: str) -> None:
        self.post_message(self.SourceUpdated(source_id))

    def on_source_updated(self, event: SourceUpdated) -> None:
        consumers = self.runtime.config.widgets_by_source().get(event.source_id, ())
        visible = self._visible_widgets()
        for widget_id in consumers:
            if widget_id not in visible:
                continue
            card = self._card_for_widget(widget_id)
            if card is not None:
                self._present_card(card, widget_id)
        if self._active_tab and self._is_overview(self._active_tab):
            self._present_overview()

    # ------------------------------------------------------------------ actions

    def action_select_tab(self, index: int = 1) -> None:
        tabs = self._visible_tabs()
        if 0 < index <= len(tabs):
            self._set_tab(tabs[index - 1].id, activate=True)

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        self._apply_global_hold(HoldReason.MANUAL, self.paused)
        self._update_chrome(force=True)
        self.notify("Visual rotation paused" if self.paused else "Visual rotation resumed")

    def action_pin(self) -> None:
        card = self.focused_card()
        if card is None or card.widget_id is None:
            self.notify("Focus a card to pin it")
            return
        widget_id = card.widget_id
        if widget_id in self._runtime_pins:
            self._runtime_pins.discard(widget_id)
            note = "unpinned"
        else:
            self._runtime_pins.add(widget_id)
            note = "pinned into the first pinned slot"
        self.notify(f"{widget_id} {note}")
        self.run_worker(self._rebuild_overview(), exclusive=True, group="overview-layout")

    def action_advance(self) -> None:
        rotation = self._rotation
        card = self.focused_card()
        if rotation is None or card is None:
            return
        slot_id = self._slot_of_card(card.card_key)
        if slot_id is None:
            self.notify("Nothing rotating here")
            return
        rotation.advance(slot_id, manual=True)
        self._present_overview()

    def action_open_url(self) -> None:
        card = self.focused_card()
        if card is None:
            self.notify("Focus a card first: the arrow keys walk the cards")
            return
        url = card.selected_url()
        if not url:
            self.notify("No link on the selected row")
            return
        self.open_validated_url(url)

    def open_validated_url(self, url: str) -> None:
        """Open only an HTTP(S) URL, after explicit selection."""
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            self.notify(f"Refused to open a non-HTTP(S) link: {parts.scheme or '(none)'}")
            return
        try:
            validate_public_url(url)
        except BlockedError as error:
            # A feed is untrusted input: "open this link" must not become a request to
            # 127.0.0.1, the router or anything else on the local network. Suppressing the
            # error here (which is what this did) opened exactly those links.
            log.warning("refused to open %s: %s", url, error.message)
            self.notify(
                f"Refused to open {parts.hostname}: {error.message}",
                severity="warning",
                timeout=12,
            )
            return
        if self._hand_to_browser(url):
            self.notify(f"Opening {parts.hostname} in your browser")
        else:
            # Say what happened and put the link on screen: a silent failure looks like a
            # key that does nothing, which is what "o does not work" turned out to be.
            log.warning("could not hand %s to a browser", url)
            self.notify(
                f"Could not open a browser for {url}",
                severity="warning",
                timeout=12,
            )

    def _hand_to_browser(self, url: str) -> bool:
        """Give the URL to the desktop and report whether it was accepted.

        ``webbrowser`` is the portable answer, but on macOS it drives ``osascript``, which
        can be refused by Automation permissions and then returns quietly. ``open`` is the
        documented way to open a URL in the default browser on macOS and needs no such
        permission; it is spawned detached so a slow browser launch cannot stall the UI.
        """
        if sys.platform == "darwin":
            try:
                subprocess.Popen(
                    ["/usr/bin/open", url],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError:
                return False
            return True
        with contextlib.suppress(Exception):
            return bool(webbrowser.open(url, new=2, autoraise=False))
        return False

    def action_save_item(self) -> None:
        card = self.focused_card()
        item_id = card.selected_id() if card is not None else None
        if not item_id:
            self.notify("No item selected")
            return
        self.run_worker(self._toggle_saved(item_id))

    async def _toggle_saved(self, item_id: str) -> None:
        state = self._item_state.get(item_id)
        saved = not bool(state and state.saved)
        record = self._record_for(item_id)
        if saved and record is not None:
            await self.runtime.items.upsert(
                record.source_id, record.kind, [record], now=self.runtime.clock.now()
            )
        await self.runtime.items.set_state(item_id, saved=saved, now=self.runtime.clock.now())
        await self._load_item_state()
        self.notify("Saved" if saved else "Unsaved")
        self._present_everything()

    def _record_for(self, item_id: str) -> ItemRecord | None:
        for card in self._all_cards():
            view = card.view
            if view is None:
                continue
            for item in view.items:
                if item.id == item_id:
                    return ItemRecord(
                        item_id=item.id,
                        source_id=item.source_id or card.widget_id or "saved",
                        kind="item",
                        title=item.title,
                        subtitle=item.subtitle,
                        url=item.url,
                        category=item.category,
                        published_at=item.published_at,
                        meta=item.meta,
                        privacy=item.privacy,
                    )
        return None

    def action_refresh_source(self) -> None:
        card = self.focused_card()
        if card is None or card.widget_id is None:
            self.notify("Focus a card to refresh")
            return
        instance = self.runtime.config.widget(card.widget_id)
        if instance is None:
            return
        source_ids = instance.source_ids()
        if not source_ids:
            self.notify("This card has no collector")
            return
        accepted = [
            source_id
            for source_id in source_ids
            if self.runtime.scheduler.request_refresh(source_id)
        ]
        if accepted:
            self.notify(f"Refresh requested: {', '.join(accepted)}")
        else:
            self.notify("Refresh throttled (rate limited)")

    def action_toggle_privacy(self) -> None:
        self.privacy = not self.privacy
        self.run_worker(self._persist_privacy(), exclusive=False)
        self._present_everything()
        self._update_chrome(force=True)
        self.notify("Privacy mode on" if self.privacy else "Privacy mode off")

    async def _load_privacy_mode(self) -> None:
        """Restore the last explicit privacy choice, so a restart does not reveal amounts.

        The configuration sets the starting point; a toggle during a session is remembered.
        Without this, `v` was persisted and never read back, so the setting appeared to
        survive in the database and not in the application.
        """
        with contextlib.suppress(Exception):
            row = await self.runtime.database.fetchone(
                "SELECT value FROM app_state WHERE key = 'privacy_mode'"
            )
            if row is not None:
                self.privacy = str(row["value"]) == "1"

    async def _persist_privacy(self) -> None:
        with contextlib.suppress(Exception):
            await self.runtime.database.execute(
                "INSERT INTO app_state(key, value, updated_at) VALUES ('privacy_mode', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                ("1" if self.privacy else "0", self.runtime.clock.now().isoformat()),
            )

    def action_help(self) -> None:
        self._apply_global_hold(HoldReason.MODAL, True)
        self.push_screen(HelpScreen(), callback=self._after_modal)

    def action_mouse_diagnostic(self) -> None:
        screen = MouseDiagnosticScreen(terminal=self._terminal_name())
        self._mouse_screen = screen
        self._apply_global_hold(HoldReason.MODAL, True)

        def closed(_result: object = None) -> None:
            self._mouse_screen = None
            self._after_modal()

        self.push_screen(screen, callback=closed)

    def _apply_palette(self, app_settings: AppSettings, *, push_rich: bool = True) -> None:
        """Register and select the configured palette for Textual *and* for Rich.

        Textual resolves ``$sb-*`` CSS variables; the card renderers pass the same names to
        Rich, which cannot read those variables. Both are built from one palette here, so a
        change in configuration recolours the card text and not just the borders.

        ``push_rich`` is False during construction: Textual replaces its console when the
        driver starts, so a Rich theme pushed that early is gone by the first render.
        """
        key = canonical_name(app_settings.theme)
        self._session_theme = key
        palette = resolve_palette(key, app_settings.theme_colors)
        theme = build_theme(palette, key=key)
        self.register_theme(theme)
        self.theme = theme.name
        if not push_rich:
            return
        if self._rich_theme_pushed:
            with contextlib.suppress(Exception):
                self.console.pop_theme()
        self.console.push_theme(build_rich_theme(palette))
        self._rich_theme_pushed = True

    def on_load(self) -> None:
        """Push the Rich half of the palette once the driver's console exists."""
        self._apply_palette(self.runtime.config.app, push_rich=True)

    def _set_theme(self, key: str) -> None:
        """Switch palette, and keep it: the choice is written to the configuration.

        Both halves are rebuilt (Textual CSS variables and the Rich theme on the console),
        then the screen is repainted: the renderers hold style *names*, so a repaint is
        enough to recolour every card. The configuration is edited in place, one line, so
        the comments around it survive.
        """
        try:
            palette = resolve_palette(key)
        except ValueError as error:
            self.notify(str(error), severity="error", timeout=8)
            return
        self._session_theme = canonical_name(key)
        theme = build_theme(palette, key=self._session_theme)
        self.register_theme(theme)
        self.theme = theme.name
        if self._rich_theme_pushed:
            with contextlib.suppress(Exception):
                self.console.pop_theme()
        self.console.push_theme(build_rich_theme(palette))
        self._rich_theme_pushed = True
        self.refresh_css(animate=False)
        self._present_everything()
        problem = self._save_theme_to_config(self._session_theme)
        if problem is None:
            self.notify(f"{palette.label} — saved to the configuration")
        else:
            # The switch still applies; only keeping it failed, and that is worth saying.
            self.notify(
                f"{palette.label} — could not save it: {problem}",
                severity="warning",
                timeout=12,
            )

    def _save_theme_to_config(self, key: str) -> str | None:
        """Write ``app.theme`` into the configuration file. Returns an error, or None.

        The edited document is parsed and resolved before it replaces the working file: a
        dashboard must never leave behind a configuration it cannot read. The running
        configuration is updated in memory too, so the file and the application agree and
        a later reload does not revert the choice.
        """
        path = self.runtime.paths.config_file
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Creating one here would write a file containing only this line, and the next
            # start would come up with no tabs at all. Refuse, and say what to do.
            return "there is no configuration file to edit; run `uv run sideboard init`"
        except OSError as error:
            return f"{type(error).__name__}: {error}"
        updated = set_config_value(text, table="app", key="theme", value=key)
        if updated == text:
            return None
        try:
            document = document_from_text(updated)
            resolve_config(
                document,
                self.runtime.registry,
                paths=self.runtime.paths,
                offline=self.offline,
                demo=self.demo,
                revision=self.runtime.config.revision,
            )
        except Exception as error:
            return f"the edited configuration was rejected ({error})"
        try:
            write_config_text(path, updated)
        except OSError as error:
            return f"{type(error).__name__}: {error}"
        self.runtime.config = dataclasses.replace(
            self.runtime.config,
            app=self.runtime.config.app.model_copy(update={"theme": key}),
        )
        log.info("palette %s written to %s", key, path)
        return None

    def action_next_theme(self) -> None:
        """t: the next palette in the list, wrapping."""
        names = palette_names()
        current = self._session_theme or canonical_name(self.runtime.config.app.theme)
        index = names.index(current) if current in names else -1
        self._set_theme(names[(index + 1) % len(names)])

    def action_theme_picker(self) -> None:
        """Ctrl+T: pick from the list, with the current one marked."""
        current = self._session_theme or canonical_name(self.runtime.config.app.theme)
        self._apply_global_hold(HoldReason.MODAL, True)
        self.push_screen(
            ThemePickerScreen([(key, PALETTES[key].label) for key in palette_names()], current),
            callback=self._after_modal,
        )

    def _terminal_name(self) -> str:
        import os

        return os.environ.get("TERM_PROGRAM") or os.environ.get("TERM", "unknown")

    def action_reset_holds(self) -> None:
        self._reset_holds()

    def _reset_holds(self) -> None:
        for slot_id in list(self._hold_timers):
            self._cancel_hold_timer(slot_id)
        self.paused = False
        rotation = self._rotation
        if rotation is not None:
            rotation.reset_holds()
            rotation.set_global_hold(HoldReason.MANUAL, False)
        self._present_everything()
        self._update_chrome(force=True)
        self.notify("Holds released")

    def action_palette(self) -> None:
        self._apply_global_hold(HoldReason.MODAL, True)
        self.push_screen(CommandPaletteScreen(self._palette_commands()), callback=self._after_modal)

    def _palette_commands(self) -> list[tuple[str, str]]:
        commands: list[tuple[str, str]] = []
        for index, tab in enumerate(self._visible_tabs(), start=1):
            commands.append((f"tab:{tab.id}", f"Go to tab: {tab.label} ({index})"))
        for widget_id in sorted(self.runtime.config.widgets):
            instance = self.runtime.config.widget(widget_id)
            label = instance.title if instance is not None and instance.title else widget_id
            commands.append((f"widget:{widget_id}", f"Show details: {label}"))
        commands.extend(
            [
                ("action:theme_picker", "Colour palette: choose"),
                ("action:next_theme", "Colour palette: next"),
                ("action:pause", "Toggle global visual pause"),
                ("action:reload", "Reload configuration"),
                ("action:reset_holds", "Release all holds"),
                ("action:refresh", "Refresh the focused card's provider"),
                ("action:privacy", "Toggle privacy mode"),
                ("action:mouse", "Mouse / focus diagnostic"),
                ("action:help", "Show help"),
                ("action:quit", "Quit Sideboard"),
            ]
        )
        return commands

    def _run_command(self, command_id: str) -> None:
        kind, _, value = command_id.partition(":")
        if kind == "tab":
            self._set_tab(value, activate=True)
            return
        if kind == "widget":
            self._show_widget(value)
            return
        if kind == "theme":
            self._set_theme(value)
            return
        actions = {
            "pause": self.action_toggle_pause,
            "reload": self.action_reload_config,
            "reset_holds": self._reset_holds,
            "refresh": self.action_refresh_source,
            "privacy": self.action_toggle_privacy,
            "mouse": self.action_mouse_diagnostic,
            "theme_picker": self.action_theme_picker,
            "next_theme": self.action_next_theme,
            "help": self.action_help,
            "quit": self.action_quit,
        }
        action = actions.get(value)
        if action is not None:
            action()

    def _show_widget(self, widget_id: str) -> None:
        card = self._card_for_widget(widget_id)
        if card is not None:
            self._user_navigation = True
            try:
                card.focus()
            finally:
                self._user_navigation = False
            self._apply_interaction_hold(card.card_key)
            return
        for tab in self._visible_tabs():
            if widget_id in tab.widgets:
                self._set_tab(tab.id, activate=True)
                card = self._card_for_widget(widget_id)
                if card is not None:
                    card.focus()
                return
        self.notify(f"{widget_id} is not placed on any visible tab")

    # ------------------------------------------------------------------ reload

    def action_reload_config(self) -> None:
        self.run_worker(self._reload_config(), exclusive=True, group="reload")

    async def _reload_config(self) -> None:
        """Validate a candidate configuration, then apply it or keep the running one."""
        try:
            document = merge_documents(
                shipped_defaults(), load_toml(self.runtime.paths.config_file)
            )
            parsed = config_from_document(document)
            candidate = resolve_config(
                parsed,
                self.registry,
                paths=self.runtime.paths,
                offline=self.offline,
                demo=self.demo,
                revision=self.runtime.config.revision + 1,
            )
        except ConfigError as error:
            self._reload_note = "reload failed"
            self._update_chrome(force=True)
            detail = "; ".join(error.problems[:4]) if error.problems else error.message
            self.notify(f"Configuration rejected: {detail}", severity="error", timeout=12)
            return
        except Exception as error:  # pragma: no cover - unexpected parse failure
            self._reload_note = "reload failed"
            self._update_chrome(force=True)
            self.notify(f"Configuration rejected: {type(error).__name__}", severity="error")
            return
        await self._apply_config(candidate)
        self._reload_note = ""
        self.notify("Configuration reloaded")
        self._update_chrome(force=True)

    async def _apply_config(self, candidate: ResolvedConfig) -> None:
        selection = self._selection_snapshot()
        previous_tab = self._active_tab
        await self.runtime.scheduler.reconfigure(candidate)
        self.runtime.config = candidate
        # The configuration is authoritative on reload, in both directions: "or" here
        # meant a session that had ever enabled privacy could not be turned off again by
        # editing the file.
        self.privacy = candidate.app.privacy_mode
        await self._persist_privacy()
        # A palette may have changed under the same theme name, which the reactive would
        # not notice; re-parse the stylesheet either way.
        self._apply_palette(candidate.app)
        self.refresh_css(animate=False)
        self.timezone = candidate.app.timezone
        await self._load_item_state()
        await self._build_tabs(preserve=previous_tab)
        self._restore_selection(selection)
        self._runtime_status = self.runtime.scheduler.runtime_status()
        self._present_everything()
        log.info("configuration applied (revision %s)", candidate.revision)

    def _selection_snapshot(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for card in self._all_cards():
            if card.widget_id:
                out[card.widget_id] = card.selected_id()
        return out

    def _restore_selection(self, snapshot: Mapping[str, str | None]) -> None:
        for card in self._all_cards():
            item_id = snapshot.get(card.widget_id or "")
            if not item_id:
                continue
            index = card.find_item(item_id)
            if index is not None:
                card.select_index(index)

    # ------------------------------------------------------------------ helpers

    def _all_cards(self) -> list[CardShell]:
        cards: list[CardShell] = []
        if self._overview is not None:
            cards.extend(self._overview.cards.values())
        for pane in self._detail_panes.values():
            cards.extend(pane.cards.values())
        return cards

    def _visible_widgets(self) -> frozenset[str]:
        widgets: set[str] = set()
        if self._active_tab is None:
            return frozenset()
        if self._is_overview(self._active_tab) and self._rotation is not None:
            widgets |= set(self._rotation.visible_widgets())
        else:
            widgets |= set(self._tab_widgets(self._active_tab))
        return frozenset(widgets)

    def _card_for_widget(self, widget_id: str) -> CardShell | None:
        for card in self._all_cards():
            if card.widget_id == widget_id:
                return card
        return None

    def _card_by_key(self, card_key: str) -> CardShell | None:
        for card in self._all_cards():
            if card.card_key == card_key:
                return card
        return None

    def focused_card(self) -> CardShell | None:
        focused = self.focused
        return focused if isinstance(focused, CardShell) else None

    def on_card_shell_focus_request(self, event: CardShell.FocusRequest) -> None:
        """Arrow keys at a card's edge: hand the movement to the neighbouring card."""
        self._focus_neighbour(event.direction)

    def _active_cards(self) -> list[CardShell]:
        """Cards the user can reach right now: the visible tab's, in layout order."""
        if self._active_tab is None:
            return []
        return self._cards_for_tab(self._active_tab)

    def _focus_neighbour(self, direction: str) -> None:
        cards = [card for card in self._active_cards() if card.display and card.size.height > 0]
        if not cards:
            return
        current = self.focused_card()
        if current is None or current not in cards:
            target = cards[0]
        else:
            key = neighbour(
                [Box(card.card_key, *card.region) for card in cards],
                current.card_key,
                direction,
            )
            target = next((card for card in cards if card.card_key == key), None)
        if target is None or target is current:
            return
        self._focus_card(target)

    def _focus_card(self, target: CardShell) -> None:
        previous = self.focused_card()
        self._user_navigation = True
        try:
            target.focus()
            with contextlib.suppress(Exception):
                # A card in a scrolling stack must be brought into view, or focus moves
                # somewhere the user cannot see.
                target.scroll_visible()
        finally:
            self._user_navigation = False
        if previous is not None and previous is not target:
            # The hold moves with the keyboard: holding every card that was ever visited
            # left the whole grid looking highlighted.
            self._release_interaction_hold(previous.card_key)
        self._apply_interaction_hold(target.card_key)

    def _release_interaction_hold(self, card_key: str) -> None:
        rotation = self._rotation
        slot_id = self._slot_of_card(card_key)
        if rotation is None or slot_id is None:
            return
        self._cancel_hold_timer(slot_id)
        if rotation.set_hold(slot_id, HoldReason.INTERACTION, False):
            self._schedule_slot_repaint(slot_id)

    def _cycle_card(self, step: int) -> bool:
        """Walk the cards of the visible tab and wrap. False when not on a card.

        Textual's own focus chain also visits filter bars and the tab strip, which makes
        "next card" unpredictable on a tab with several cards. The cards are what the user
        is walking through — but a filter bar that already has focus keeps Tab, or it could
        not be used at all.
        """
        if not isinstance(self.focused, CardShell):
            return False
        cards = [card for card in self._active_cards() if card.display and card.size.height > 0]
        if not cards:
            return False
        current = self.focused_card()
        index = cards.index(current) if current in cards else -step
        self._focus_card(cards[(index + step) % len(cards)])
        return True

    def action_focus_next(self) -> None:
        """Tab. The Screen binds this key to ``app.focus_next``; a binding of our own on
        "tab" never ran, because the screen's binding is checked first."""
        if not self._cycle_card(1):
            super().action_focus_next()

    def action_focus_previous(self) -> None:
        """Shift+Tab, the same walk backwards."""
        if not self._cycle_card(-1):
            super().action_focus_previous()

    async def _load_item_state(self) -> None:
        store = self.runtime.items
        self._saved_ids = await store.ids_with_state("saved")
        self._read_ids = await store.ids_with_state("read")
        tracked = await self._tracked_item_ids()
        self._item_state = dict(await store.states(tracked))
        # The Saved widget is built from this, not from the cards on screen: a saved link
        # must survive its source rotating away, and the provider content expiring.
        self._saved_items = tuple(
            record.to_item(ItemState(saved=True)) for record in await store.saved_items()
        )

    async def _tracked_item_ids(self) -> tuple[str, ...]:
        ids: list[str] = []
        for widget_id in self.runtime.config.widgets:
            view, _ = self.present(widget_id)
            if view is not None:
                ids.extend(item.id for item in view.items)
        return tuple(dict.fromkeys(ids))
