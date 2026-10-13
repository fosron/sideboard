# Implemented architecture

A modular monolith: one process, explicit feature registrations, ordinary Python
protocols and dataclasses, shared infrastructure, configuration-driven composition. No
service container, message broker, subprocess per widget, entry-point scanning or
runtime plugin download.

## Data flow

```text
features/registry.py  (one trusted list)
        │
core/registry.py  ── validates specs, builds lookups
        │
core/config.py    ── one versioned TOML schema, merged by ID, lists replaced
        │
core/scheduler.py ── one task per source instance, demand-driven cadence
        │              ├── capabilities.py  (bounded HTTP / file / CLI)
        │              ├── features/<f>/provider.py   (one bounded fetch)
        │              ├── storage.py + history.py    (cache, aggregates)
        │              └── items.py                   (read/saved/purge)
        │
core/present.py   ── calls a pure presenter, contains failures
        │
ui/card.py        ── shared shell: holds, freshness, privacy, resize, pin, detail
        │
ui/overview.py + ui/rotation.py ── responsive slots and the rotation state machine
```

## Module map, and why each exists

| Module | Responsibility |
|---|---|
| `core/contracts.py` | the whole public extension surface: `FeatureSpec`, `SourceSpec`, `WidgetSpec`, `Provider`, `SourcePayload`, envelopes, `ViewModel`, `CategoryConfig`/`TabConfig`, `ViewContext`, `setup_required` |
| `core/registry.py` | validates and indexes specs. Contains no feature-specific branches; duplicate/incompatible ids become named errors |
| `core/config.py` | versioned document validation, ID-indexed merge, ordered-list replacement, cadence resolution, cross-reference checks, `init` rendering |
| `core/scheduler.py` | source-instance tasks, demand from visible views, one in-flight acquisition, retry/backoff/circuit state, generation-based discard of late results, import-driven one-shots, diagnostics summaries |
| `core/capabilities.py` | `HttpReader`, `FileReader`, `CommandRunner`, `HttpLimits`. Also home to subprocess execution: a separate `core/subprocesses.py` would have been a second name for the same boundary |
| `core/storage.py` | one SQLite handle, WAL, owner-only permissions, **one serialized write path**, migrations, envelope persistence/loading |
| `core/history.py` | bounded in-memory rings plus one aggregate per minute, gap-preserving downsampling, pruning |
| `core/items.py` | minimal item metadata, local read/saved/dismissed state, retention purge (offline-safe), saved-link preservation |
| `core/present.py` | presenter invocation, binding checks, contained errors, envelope combination, rotation classification |
| `core/clock.py` | real and fake clocks. `FakeClock.sleep` advances virtual time *and* yields, so a scheduler test cannot spin the event loop |
| `core/errors.py` | typed failures and redacted `SourceErrorInfo` |
| `core/logs.py` | rotating, capped, redacted logs |
| `core/paths.py` | private locations and their overrides |
| `ui/card.py` | the shared card shell. Feature code never reimplements holds, freshness, privacy, sizing or detail navigation |
| `ui/rotation.py` | hold reasons, preemptible dwell, grace period, pinning, dedupe, setup-card suppression |
| `ui/overview.py` | responsive slot layouts; narrowing merges queues and never drops a widget |
| `ui/renderers/` | metric, item, table, status and sparkline renderers built from `rich.text.Text` — no markup parsing, so untrusted content cannot inject style |
| `ui/styles.py` | the only place a Rich-rendered colour is chosen. Textual CSS variables are *not* Rich style names |
| `ui/screens.py` | detail, help, palette and mouse-diagnostic modals |
| `app.py` | composition root and orchestration: tabs, presentation, holds, item state, URL validation, configuration reload. No feature-specific branch |
| `cli.py` | `init`, `doctor`, `config validate`, `features list`, `import …`, and the launch modes |

## The six concepts, kept separate

| Concept | Where | Example |
|---|---|---|
| Feature | `features/<id>/` | `mac_system`, `docker_local`, `portfolio` |
| Source type | `SourceSpec` | `mac.metrics`, `docker.containers`, `weather.forecast` |
| Source instance | `sources.<id>` in config | `mac`, `docker`, `weather_home`, `weather_travel` |
| Widget type | `WidgetSpec` | `docker.summary` |
| Widget instance | `widgets.<id>` in config | `docker_summary` |
| Category | `categories.<id>` | `infrastructure`, `finance`, `software` |
| Tab | `[[tabs]]` | `systems`, or a seventh `operations` tab |

One source instance serves every widget bound to it. Two widgets never double the
requests, and the reverse index lives in `ResolvedConfig.widgets_by_source()`.

## Host envelope

Stored and displayed separately:

* `connectivity`: `ok` · `offline` · `error` · `blocked` · `unconfigured` · `unavailable`
* `freshness`: `fresh` · `stale` · `expired` · `none`, recomputed on every read from
  `last_success_at`, `valid_until` and `stale_after_seconds`
* UI hold: `live` · `held`, tracked per reason

A failed acquisition keeps the last good payload and records a redacted error. A `304`
returns the same payload object, which validates the representation without advancing
the source's own date.

## Scheduling rules implemented

* one in-flight acquisition per source instance; global HTTP concurrency 6, per-host 2
* one transient retry per refresh, then exponential backoff, honouring `Retry-After`
* a disabled instance and an unreferenced instance own no recurring task
* import-driven instances never poll; `suspend()`/`resume()` exist for non-interactive
  commands and deterministic tests
* sleep/wake or a long gap staggers catch-up and marks a history gap instead of
  replaying ticks or drawing zero load
* reconfigure cancels and closes only affected instances and increments their
  generation so late results are discarded
* shutdown drains pending writes, reaps owned subprocesses and closes clients

## Persistence

| Table group | Owner | Notes |
|---|---|---|
| `schema_migrations` | core | versioned, per-owner, conflict-checked |
| `source_state` | core | envelope + payload JSON, validators, coverage |
| `items`, `item_state`, `saved_links` | core | minimal metadata, local state, purge |
| `metric_samples` | core | one aggregate per minute per metric |
| `events`, `app_state` | core | dated events, small key/value state |
| `instruments`, `positions`, `position_accounts`, `prices`, `price_history`, `reported_values`, `portfolio_imports`, `import_rows`, `import_rollback`, `manual_edits`, `dividends`, `investor_links` | portfolio (migration 10) | money as decimal **text**; snapshot replacement archived for rollback |
| `fx_rates` | portfolio (migration 21), re-exported by `fx` | directed base/quote records; EUR/PLN and PLN/EUR are different rows |

Features own namespaced migrations and repository functions; UI code never issues SQL.
Database migrations and payload schema versions are independent, and a cached payload
whose schema id/version does not match the registered spec is dropped rather than
interpreted with the wrong model.

## Privacy boundaries

* Public feeds never reach private networks or local files; a private/local service
  provider needs an explicit host allowlist.
* Only explicitly configured file paths are readable.
* Docker uses fixed read-only operations, a pinned local context, a minimal child
  environment (no `DOCKER_HOST`), and re-validates the endpoint before daemon calls.
* No shell strings, `eval`, configuration-supplied Python paths or arbitrary commands.
* Logs exclude payloads, balances and private identifiers; privacy mode masks them on
  screen too.

## Deliberate deviations from the plan's file tree

| Planned | Implemented | Why |
|---|---|---|
| `core/subprocesses.py` | `CommandRunner` in `core/capabilities.py` | one bounded I/O boundary module; a second module would only re-export it |
| `features/*/view.py` for custom renderers | not needed | every current widget is expressible with the shared renderers; the contract for a feature-local renderer exists via `WidgetSpec.present` |
| `schemas/local-snapshot.schema.json` | present, plus `config.schema.json` | the config schema is generated-in-spirit documentation; `config validate` remains the authority |
| `core/registry.py` namespacing rule | type ids only need a `namespace.name` shape | the plan's own example uses `mac.metrics` from the `mac_system` feature |
| `src/sideboard/migrations/` as SQL files | migrations are Python `Migration` objects: `CORE_MIGRATIONS` in `core/storage.py` plus one tuple per feature, merged by version | keeps a feature's schema next to the models and repository that use it, and makes version conflicts a named error at start-up |
| `ui/palette.py` | the palette lives in `ui/screens.py` with the other modals; the command list is built from the registry in `app._palette_commands()` | the palette needs no feature knowledge beyond the config, so a separate module only re-exported it |
| `features/*/view.py` (already listed) | the same | the same |
| FX payload and `fx_rates` table | defined in the money domain (`portfolio/models.py`, portfolio migration 21, `portfolio/repo.py`) and re-exported by `fx/models.py` | keeps a one-way dependency (`fx` → `portfolio`) instead of a module cycle, while the collector and widget still live in the `fx` feature |
