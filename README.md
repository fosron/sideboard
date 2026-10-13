# Sideboard

A local Mac terminal dashboard (Textual) that lives in a terminal window on the iPad
used as an extended display. It answers *"what is worth noticing now?"*: a calm
rotating Overview plus deliberate Money, Read, Watch, Life and Systems tabs.

**No account, API key, subscription, cloud service or Docker requirement.** Manual
entry, local files, approved local sockets and public feeds are the whole input surface.

```sh
uv sync --locked
uv run sideboard init          # writes ~/.config/sideboard/config.toml
uv run sideboard               # run it
uv run sideboard --demo        # synthetic data, separate database
uv run sideboard --offline     # no public network; local metrics still collect
uv run sideboard doctor        # local checks only
```

Read `docs/setup.md` for the full install, controls, limits and recovery, and
`docs/features.md` for every registered source and widget.

## What it does

| Area | What is on screen | Input |
|---|---|---|
| Overview | four slots: two pinned, two rotating with independent dwell | configuration |
| Money | tracked value in EUR, per-holding weight and value basis, dividend events, directed FX | manual entry and normalized CSV imports |
| Read | news digest with enforced category diversity, HN Top/New, saved links | public RSS/Atom, HN API |
| Watch | new uploads from channels you chose, ranked by favorites or imported viewing history | per-channel Atom feed, Google Takeout |
| Life | current conditions, rain outlook, seven-day forecast | Open-Meteo |
| Systems | Mac CPU/RAM/swap/disk/network/battery, local Docker containers by Compose project, source diagnostics | psutil, existing Docker CLI |

## Controls

`1`–`9` select tabs · `Ctrl+P` palette · `Ctrl+R` reload configuration ·
`Tab`/`Shift+Tab` move between cards · `↑`/`↓` move the selection ·
`Space` pause visual rotation (collection continues) · `p` pin · `n` next card ·
`Enter` detail · `o` open the selected link · `s` save · `r` refresh ·
`v` privacy mode · `Ctrl+D` mouse/focus diagnostic · `Ctrl+Y` release holds ·
`?` help · `q` quit.

Hover pauses only the hovered card, then resumes the remaining dwell after a two-second
grace. See `docs/setup.md` for what to do when a terminal misreports mouse motion.

## What it deliberately does not do

No trading, no banking writes, no scraping of logins or cookies, no Reddit access
evasion, no paid or live market feed, no cloud summaries or embeddings, no plugin
marketplace, no privilege escalation (`sudo`, privileged helpers, Full Disk Access),
no Docker lifecycle or cleanup commands, and no automatic updates to other
applications. Price-bot, Spanish-learning and habit widgets are excluded by design.

## Verified state

* `uv run pytest` — 389 offline tests, including the extension smoke test
* `uv run pytest -m live` — 4 explicit public-network probes (deselected by default)
* `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright` — clean
* Real Docker smoke test against a pinned local `orbstack` socket: engine identity,
  27 containers with state/CPU/memory/restarts/exit codes, health status (including
  *running / health unknown*), `system df` and derived I/O rates
* Real-terminal smoke test under a pseudo-terminal: chrome, all six tabs, a full modal
  screen and demo collection, with a clean exit and no stylesheet or Rich errors.
  Interactive hover still needs a human at the keyboard; see `docs/setup.md`.
* Four defects were found only by these real smoke tests (nested Docker endpoint shape,
  a backslash-escaped CLI template, an absent `State.Health` field, and a missing
  migration); all four now have regression tests — see `docs/source-validation.md`.

## Repository map

| Path | Contents |
|---|---|
| `src/sideboard/core/` | contracts, registry, configuration, storage, scheduler, capabilities, presentation |
| `src/sideboard/features/` | one folder per feature plus the single trusted registry list |
| `src/sideboard/ui/` | card shell, rotation, renderers, theme, screens |
| `src/sideboard/defaults.toml` | the shipped six-tab configuration |
| `templates/feature/` | compiling feature template |
| `docs/` | setup, features, architecture, extension cookbook, source validation |
