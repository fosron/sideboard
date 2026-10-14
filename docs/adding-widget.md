# Adding a widget, source, category or tab

Extension cookbook for Sideboard v1. Everything here matches the implemented APIs in
`src/sideboard/core/contracts.py`; if it drifts, the code is right and this file is
wrong.

## Choose the smallest change

| Need | Change | Files touched |
|---|---|---|
| Another RSS feed, weather city, snapshot file, category, tab or queue placement | configuration only | `config.toml` |
| Another presentation of data you already collect | a widget instance in config; a feature-local presenter only if the view needs new logic | `config.toml`, maybe one `presenter.py` |
| A genuinely new data source | one feature folder, **one** line in the trusted registry, config, tests | see §3 |
| A new capability, domain table or specialized renderer | a reviewed core change plus tests, documented in `docs/architecture.md` | see §5 |

Do not start by editing `app.py`, the scheduler, the rotation state machine or
`core/storage.py`. An ordinary integration must not require it, and
`tests/contracts/test_extension_smoke.py` fails if it does.

## 1. Configuration-only additions

Configuration selects registered types by stable instance ID. `sources` names
collectors, `widgets` binds their outputs, `categories` labels them, `tabs` and
`overview.slots` place them.

Another feed:

```toml
[[sources.news.settings.feeds]]
id = "my-publisher"
url = "https://example.invalid/feed"
category = "software"     # must be a declared category, or use an existing one
language = "en"
enabled = true
```

A second city is another **instance of the same source type**, never another Python
class:

```toml
[sources.weather_travel]
type = "weather.forecast"
enabled = true
[sources.weather_travel.settings]
label = "Travel"
latitude = 41.3874
longitude = 2.1686
timezone = "Europe/Madrid"
```

A new category and a seventh tab. Tabs are an ordered list: it **replaces** rather than
appends, so add to the existing array instead of replacing it. `sideboard init` writes
the full list first.

```toml
[categories.operations]
label = "Operations"
order = 5

[[tabs]]
id = "operations"
label = "Operations"
layout = "stack"          # stack · detail · grid · filterable · rotating_grid
widgets = ["mac_details", "docker_containers"]
```

Then:

```sh
uv run sideboard config validate
# and Ctrl+R in the running app: an invalid update keeps the previous configuration
```

Tab switching beyond the ninth tab remains available through `Ctrl+P`.

## 2. New view of existing data

Bind an existing source instance and pick a registered metrics/list/table/status/
sparkline presentation. Different columns, a supported sort mode, metric selection,
title or limit are typed widget settings, not a new collector.

When the *logic* is new, put `present(inputs, settings, view_context) -> ViewModel` in
that feature. It is pure:

* it receives `PresentInput(settings, inputs, context)` where `inputs` is keyed by the
  widget's **declared binding names**, not by source-instance ids. The host maps
  `bindings[binding_name] -> source_id` and hands over that source's current envelope
  or `None`;
* it must not fetch, query a database, start a process or read the clock except through
  `view_context.now`;
* a missing input produces a `ViewModel` with an honest `StatusView`. `ViewMode.DETAIL`
  is requested through `view_context.mode`: the **Overview** presents `COMPACT` (half a
  window, so a few short columns and no footnotes) while a **tab card** and the `Enter`
  detail view present `DETAIL` (a whole pane that scrolls, so the full table, the
  excerpts and the notes belong there). `view_context.width` is the width the card will
  actually be drawn at, so a presenter can choose a short label that fits instead of
  letting the renderer cut it off;
* use stable IDs for metrics, items and rows; state money arithmetic in the feature, not
  in a generic string formatter.

Register the widget type in that feature's `FEATURE`; the host then exposes it to
configuration validation, `features list` and the palette automatically. Reuse the
shared card shell for holds, staleness, sizing, pinning and privacy.

## 3. New source/feature module

Copy `templates/feature/`. The template compiles, is exercised by
`tests/contracts/test_extension_smoke.py`, and uses **absolute** imports
(`from sideboard.core.contracts import ...`) so a rendered copy works from any
directory. Existing feature modules use relative imports (`from ...core.contracts`);
either is fine inside the repository.

```text
src/sideboard/features/<name>/
  __init__.py       # re-export FEATURE
  feature.py        # FEATURE = FeatureSpec(...)
  config.py         # typed settings models
  models.py         # versioned, immutable payload models
  provider.py       # one bounded fetch + aclose()
  presenter.py      # pure compact/detail mapping
tests/features/<name>/
  test_*.py
  fixtures/         # synthetic or properly redacted; never personal data
```

Then add exactly one line — the import and the tuple entry — to
`src/sideboard/features/registry.py`. That list is trusted application code, not a
sandbox: no downloaded modules, no configuration-specified Python paths, no `eval`, no
shell recipes.

### The contract in practice

```python
FEATURE = FeatureSpec(
    id="mydata",
    sources=(
        SourceSpec(
            type_id="mydata.items",  # <namespace>.<name>
            payload_model=MyPayload,  # pydantic, frozen, extra="forbid"
            settings_model=MySettings,
            payload_schema="mydata.items",
            payload_schema_version=1,
            factory=lambda build: MyProvider(settings=_settings(build)),
            capabilities=frozenset({Capability.HTTP_PUBLIC}),
            cadence=Cadence(active_seconds=600, ambient_seconds=900, background_seconds=3600),
            retention_seconds=7 * 24 * 3600,
            description="One line for `features list`.",
            setup_hint="What the user must configure.",
            setup_check=lambda settings: bool(settings.endpoint),  # instance-level state
            metrics_extractor=metrics_of,  # optional bounded history
            demo_builder=demo_payload,  # for --demo, no I/O
        ),
    ),
    widgets=(
        WidgetSpec(
            type_id="mydata.list",
            settings_model=MyWidgetSettings,
            inputs=(InputBinding(name="primary", accepts=frozenset({"mydata.items"})),),
            present=present,
            default_category="infrastructure",
            min_width=24,
            min_height=3,
        ),
    ),
)
```

Rules the host relies on:

* `fetch(context)` performs exactly one bounded acquisition and never starts a timer,
  thread or loop. `aclose()` must be safe to call twice.
* Return a typed error from `core.errors` for failures. The host keeps the last good
  payload, records a redacted error and backs off.
* If you make a conditional request and get `304`, return the *same payload object* so
  the host refreshes freshness without advancing the source's own date.
* Only capabilities you declare are injected: `context.http` (allowlisted, capped),
  `context.files` (configured paths), `context.commands` (fixed argv), `context.database`
  (your namespaced repository), `context.previous`, `context.clock`, `context.offline`,
  `context.demo`.
* `import_driven=True` means "never poll". Publish from local state on demand; the host
  triggers it at start-up and after an import or manual edit.
* Payload timestamps: set `as_of` (the source's own time), `precision`,
  `valid_until`, `provenance` and `coverage`. A partial result must report what is
  missing.
* Mark private content with `Privacy.PRIVATE` so privacy mode masks it.

### Setup, blocked and fallback states

Decide before writing code: what does this look like when it is unconfigured, when access
is blocked, when the local dependency is missing, and offline? A plain link-only or
"authorization required" state is a legitimate shipped mode — `reddit.threads` does
exactly that and performs no I/O. A `304`-style validation path, a rate-limit backoff and
a stale label come from the host for free.

### Optional dependencies

Import heavy libraries inside the factory that needs them, never at module import.
Declare an extra in `pyproject.toml` and fail with a `BlockedError` naming the extra,
like `markets.quotes` does. Registration and `features list` must keep working without
the extra installed.

## 4. Tests and completion

Run the feature tests plus the shared suites:

```sh
uv run pytest tests/features/<name> -q
uv run pytest tests/contracts -q          # registry, config and extension contracts
uv run pytest tests/ui -q                 # card, rotation and layout behaviour
uv run sideboard config validate
uv run sideboard features list
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

Handle at least:

* valid, partial, empty, stale and unavailable input; malformed data; bounded failures
* compact and detail layouts, long Lithuanian/Spanish titles, stable selection, privacy
  mode, and a held snapshot that does not start a second polling timer
* two widgets sharing one source instance, two instances of one source type, an absent
  optional dependency, and removal plus reconfiguration with no leaked task
* for local counters: warm-up, counter reset, sleep gap, interface change, missing sensor
* for money: decimal precision, currency direction, minor units (GBp), missing quote or
  FX, conflicting sources, and idempotent repeat imports

Prefer `FakeClock`, fake commands, injected adapters and synthetic HTTP fixtures
(`respx`) over live services or private files. `uv run pytest -m live` runs the
explicit, small, non-mutating live probes.

## 5. Justified core changes

A new *capability* (a new bounded I/O boundary), a new *shared renderer*, a new domain
table or a new persisted concept is a reviewed core change: add it with tests, document
it in `docs/architecture.md`, and explain why the existing contracts could not express
the requirement. Integration-specific conditionals scattered across core modules are not
an acceptable substitute.

## 6. Checklist

- [ ] one feature folder; one line in `features/registry.py`
- [ ] payload schema id and version declared; settings model rejects unknown keys
- [ ] declared capabilities only; no capability a config can widen
- [ ] no import-time I/O; demo builder added (or the source honestly reports
      "unavailable in demo mode")
- [ ] presenter pure; compact and detail both useful; missing data stays missing
- [ ] privacy annotations on private content
- [ ] setup hint, blocked mode and fallback documented in `docs/features.md`
- [ ] feature tests, contract suite, `config validate`, ruff and pyright green
