# Agent instructions

## Mission and scope

Build **Sideboard**, a local Mac TUI displayed on an iPad used as an extended monitor. For the initial build read `plan.md`. For later changes read `docs/features.md`, `docs/adding-widget.md` and the relevant feature/plan section. Implement Phases 0–2 as a working application. Phase 3 is optional, not permission to build the entire wishlist.

Core: portfolio/Vercom, weather, news/Hacker News, YouTube, access-dependent Reddit, **Mac statistics, local Docker statistics**, and generic local snapshots. **Exclude price-bot, Spanish-learning and habit widgets.** Manual holdings/prices entry and CSV exports are sufficient; automated brokerage access is not required.

## Architecture

Use Python 3.14 as the supported baseline, Textual, uv, httpx, Pydantic, psutil and SQLite/aiosqlite. Pin compatible stable dependencies in `uv.lock`. One native process; no Docker requirement, web frontend, server, paid API, cloud LLM or mandatory new credential.

Keep ordinary feature changes inside `src/sideboard/features/<feature>/`, its tests, configuration and one trusted registration entry. Each feature exports `FEATURE` with typed source/widget specs. Sources collect; pure presenters derive views; shared renderers display them. Core owns scheduling, persistence, freshness, privacy, rotation and cleanup. Widgets never fetch data or create polling timers. Multiple widgets bound to one source share one collection task.

Tabs, categories, source instances, widget placement, sorting and rotation are configuration-driven. Six tabs are defaults, not a limit. Do not add closed category enums or feature-specific branches to `app.py`, the scheduler or storage engine. Reuse existing sources/renderers before creating a new type. New permissions, domain tables or specialized renderers may justify a small documented core change; an ordinary integration must not require one.

Read the matching feature and contract tests before modifying code. Maintain `docs/features.md`, a compiling feature template and `docs/adding-widget.md` against the real implemented APIs. Test a synthetic new feature plus a seventh tab/category and two widgets sharing one source. Config reload must validate before applying, preserve the previous valid configuration on failure, and close removed source tasks. Python-code changes may require restarting the app.

Do not build an external plugin framework. Registry modules are trusted code, not a sandbox. Never load arbitrary config-specified Python paths, expressions or shell commands. Disabled integrations perform no I/O; optional libraries load lazily.

## UX and data correctness

Keep compact dark tabs, responsive cards, independent rotation, hover pause, keyboard fallback and visible freshness. Collection continues while a displayed snapshot is held; links must not move under the pointer. Initial focus must not freeze rotation. Keep rotating when another Mac window is active. Reuse the shared card shell rather than rebuilding these rules per feature.

Use decimal arithmetic for money. Preserve instrument identity, venue, currency, units, statement dates and provenance. No guessed ETF mappings, quantities or basis. Manual, statement and online values remain distinguishable. Imports preview changes, replace account snapshots atomically, reject silent row loss and are idempotent. Missing quotes/FX are not zero; value change is not investment return.

YouTube favorites, imported viewing frequency, opened links and actual watched events are different. Honor all source terms, authentication requirements and blocks. No browser-cookie extraction or access-evasion workarounds.

## Mac, Docker and privacy

Mac: bounded local metrics, correct counter deltas, warm-up/reset/sleep gaps and capability-aware missing fields. Do not label RAM percentage as macOS memory pressure. No root, privileged hardware helpers or invented temperature/GPU data. Process details are separately opt-in; never collect command lines, environments or document titles.

Docker: existing CLI, explicitly approved/pinned local context, fixed read-only operations, bounded JSON output and subprocess cleanup. Reject remote context/environment redirection. No runtime installation/start, context switching, restart, exec, logs, prune or other mutation. Distinguish engine/container metrics from Mac host resources; no healthcheck is not verified healthy. Do not dump full inspect/config/environment output. Socket access is powerful, not an enforced read-only credential.

Treat all remote/imported content as untrusted data. Escape markup/control sequences. Keep personal data out of Git, fixtures and logs. Demo uses synthetic data and a separate database; offline blocks public network but may collect approved local metrics. Privacy mode also masks private process/container/project names.

## Delivery

Start with one complete source → cache → presenter → widget slice, then extend through the same contracts. Do not stall on missing personal files, credentials or inaccessible sources; implement tested honest fallbacks and report the remaining dependency.

Run offline deterministic tests, lint, formatting and types from `plan.md`. Cover extension contracts, config reload/removal, shared polling, pause races, resizing, failures, import idempotency, units, local-counter resets and privacy. Perform a real Mac/terminal/Docker smoke test when available and report exactly what was tested. Deliver working normal/demo/offline launch flows and concise setup/limits documentation. Do not claim unrun checks or unavailable integrations passed.

---

## Repository map (implementation)

The build is complete for Phases 0–2. Read `docs/architecture.md` first, then the
nearest feature folder.

| Path | Contents |
|---|---|
| `src/sideboard/core/contracts.py` | the entire public extension surface — read it before changing any feature |
| `src/sideboard/core/` | registry, versioned config, scheduler, storage, history, items, capabilities, presentation, clock, errors, logs, paths |
| `src/sideboard/features/registry.py` | the one trusted list of built-in features |
| `src/sideboard/features/<id>/` | `feature.py` (`FEATURE`), `config.py`, `models.py`, `provider.py`, `presenter.py` |
| `src/sideboard/ui/` | shared card shell, rotation state machine, renderers, theme, modal screens |
| `src/sideboard/defaults.toml` | the shipped six-tab configuration; `init` copies it |
| `templates/feature/` | compiling feature template exercised by the extension smoke test |
| `tests/contracts/` | registry, config and the required extension smoke test |
| `docs/adding-widget.md` | the extension cookbook, kept in sync with the real APIs |
| `docs/features.md` | registered types, bindings, permissions, limits |
| `docs/source-validation.md` | what was actually probed, and what remains unconfigured |
| `docs/setup.md` | install, controls, sizes, reload, recovery |

Checks before claiming a change is done:

```sh
uv run pytest -q && uv run pytest -m live -q
uv run ruff check . && uv run ruff format --check . && uv run pyright
```
