# Registered features, sources and widgets

This index is generated from the live registry, so it matches the running code.
`uv run sideboard features list` prints the same information without fetching data.

`uv run sideboard features list --json` emits it machine-readably.

| Feature | Source type | Payload schema | Capabilities | Cadence (active / ambient / background) | Setup needed | Demo data |
|---|---|---|---|---|---|---|
| weather | `weather.forecast` | `weather.forecast` v1 | http_public | 1200 / 1200 / 3600 s | coordinates + IANA timezone | yes |
| news | `news.feeds` | `news.feeds` v1 | http_public | 1200 / 1200 / 3600 s | at least one validated feed | yes |
| hackernews | `hackernews.stories` | `hackernews.stories` v1 | http_public | 300 / 300 / 900 s | none | yes |
| youtube | `youtube.uploads` | `youtube.uploads` v1 | file, http_public | 1800 / 1800 / 7200 s | channel IDs and/or a Takeout path | yes |
| reddit | `reddit.threads` | `reddit.threads` v1 | http_public | 3600 / 3600 / 7200 s | none for link-only mode | yes |
| portfolio | `portfolio.holdings` | `portfolio.holdings` v1 | repository | import-driven (3600 s fallback) | none; empty state is valid | yes |
| portfolio | `portfolio.prices` | `portfolio.prices` v1 | repository | import-driven (3600 s fallback) | none; empty state is valid | yes |
| fx | `fx.rates` | `fx.rates` v1 | repository, http_public, file | import-driven (43200 s in reference mode) | none for manual mode | yes |
| markets | `markets.quotes` | `markets.quotes` v1 | http_public | 3600 / 3600 / 14400 s | explicit symbol mapping, optional extra | yes |
| mac_system | `mac.metrics` | `mac.metrics` v1 | local_metrics | 5 / 15 / 60 s | none | yes |
| docker_local | `docker.containers` | `docker.containers` v1 | cli | 15 / 15 / 60 s | pinned local context | yes |
| github_actions | `github.actions` | `github.actions` v1 | cli | 60 / 90 / 180 s | an authenticated `gh` CLI and a repo list | yes |
| git_local | `git.repositories` | `git.repositories` v1 | cli | 120 / 300 / 900 s | a list of local checkout paths | yes |
| local_snapshot | `local_snapshot.file` | `local_snapshot.file` v1 | file | 30 / 60 / 120 s | an explicit file path | yes |
| diagnostics | — | host-owned runtime status | — | — | none | n/a |

Notes on cadence: `import-driven` sources never poll. They publish the current local
state once at start-up and again after a manual edit or a successful import. A source no
widget consumes owns no recurring task at all.

## Widgets

| Widget type | Accepted inputs | Category | Compact view | Detail view |
|---|---|---|---|---|
| `weather.summary` | `primary`: weather.forecast | world | temperature with the current-weather symbol, wind, rain next 6 h | current conditions, the next hours, then the 7-day table |
| `news.digest` | `primary`: news.feeds | world | deterministic diversity digest, with the age of the newest story and of each row | per-bucket list with source and age, plus feed health |
| `news.saved` | — (local state) | world | saved reading/video links | same list, longer |
| `hackernews.list` | `primary`: hackernews.stories | software | ranked item list | up to 30 items |
| `youtube.uploads` | `primary`: youtube.uploads | media | recent uploads, one channel per turn | per-channel problem table |
| `reddit.list` | `primary`: reddit.threads (optional) | software | threads, or link-only community links | grouped by community |
| `portfolio.summary` | `positions`, `prices`?, `fx`?, `markets`? | finance | tracked value, priced count, weights | per-holding table |
| `portfolio.holdings` | `positions`, `prices`?, `fx`?, `markets`? | finance | holdings table with basis columns | adds cost basis and reported values |
| `portfolio.events` | `positions` | finance | dividends and investor links | dated events |
| `portfolio.instrument` | `positions`, `prices`?, `fx`?, `markets`? | finance | one holding's concentration and events | quantity, price, value, share |
| `fx.summary` | `primary`: fx.rates | finance | directed rate table | plus missing-direction warning |
| `mac.summary` | `primary`: mac.metrics | infrastructure | CPU, RAM, disk, network, battery, CPU sparkline | overview metrics plus history |
| `mac.details` | `primary`: mac.metrics | infrastructure | full metric set | swap, uptime, per-volume disk, I/O, optional processes |
| `docker.summary` | `primary`: docker.containers | infrastructure | context, counts, engine CPU and memory | engine identity and collected notes |
| `docker.table` | `primary`: docker.containers | infrastructure | containers grouped by Compose project | table with restarts and notes |
| `github.actions` | `primary`: github.actions | dev | running runs, then recent failures | table of recent runs with branch, event and age |
| `git.status` | `primary`: git.repositories | dev | checkouts with uncommitted work, ahead/behind | table with branch, changes and last commit |
| `local_snapshot.metrics` | `primary`: local_snapshot.file (optional) | infrastructure | producer metrics with histories | all metrics with their basis |
| `local_snapshot.items` | `primary`: local_snapshot.file (optional) | infrastructure | producer items | longer item list |
| `diagnostics.sources` | — (host-owned) | infrastructure | counts and short table | full source table and setup problems |
| `diagnostics.host` | — (host-owned) | infrastructure | mode, config revision, paths | same with pending writes |

The weather card prefixes the current temperature with one symbol for the WMO code
(`weather_icon`): text-presentation Unicode, one cell wide, no Nerd Font and no emoji
presentation, and nothing at all for a code it does not know — an icon that guesses is
worse than none. It is the only icon in the interface.

Weather rows link to the location's public forecast page
(`yr.no/en/forecast/daily-table/<lat>,<lon>`, built from the coordinates the source was
configured with): a forecast row has no article behind it, and "no link on this row" on
every row is not useful either. An hour that is not today carries its weekday, so a bare
"09:00" cannot read as belonging to the last date of the table.

The YouTube list takes one upload per channel per turn (`spread_channels`, on by default):
with a favourites-first ranking and no watch history, one favourite channel that uploads
daily otherwise filled the card on its own. Within a channel the ranking still decides the
order, and `group_by_channel` turns the spread off by grouping instead.

A metric's `severity` and a table row's `severity` are **drawn**: warnings and errors take
the status colour, ordinary values the text tone (muted when the card's data is stale).
Before, `severity` was computed by presenters and discarded, so a failure looked like a
success. A widget's label comes from its configured `title`, else a readable fallback —
`reddit.list` is "Reddit", not "List", and `hn_top` is "HN Top".

Footnotes, metric bases and payload notes are recorded by presenters for the record and
are **not rendered**: the dashboard prints values, and a `StatusView` or a warning-severity
metric is how it reports a problem. The one annotation a table may carry is `truncated`.

A widget with an `optional` input renders an honest state when that binding is absent.
`news.saved`, `diagnostics.*` and `local_snapshot.*` need no collector at all.

## Capability vocabulary

| Capability | What the host injects | Boundary |
|---|---|---|
| `http_public` | `HttpReader` in public scope | host allowlist, private/loopback refused, redirects revalidated, 2 MiB cap, 20 s deadline |
| `http_local` | `HttpReader` bound to an explicit host allowlist | for approved local services only |
| `file` | `FileReader` restricted to configured paths | regular files only, 1–2 MiB cap, no directories |
| `local_metrics` | nothing (the feature owns its `psutil` adapter) | read-only counters, no privileged helper |
| `cli` | `CommandRunner` with one slot | fixed argv arrays, timeout, output cap, reaped children |
| `repository` | the shared `Database` handle | feature-owned namespaced SQL only; UI never issues SQL |

## Permissions and privacy notes

* **No credential is required.** `portfolio` and `fx` in `manual_import` mode are fully
  local; `markets` is off by default.
* **Docker:** an existing local socket is powerful. Sideboard restricts itself to
  read-only inventory/stats/narrow inspect and never describes socket access as an
  enforced read-only credential.
* **Mac:** no root, no Full Disk Access, no daemon. Process details are opt-in and
  collect names and IDs only.
* **Privacy mode** masks financial amounts, process names, container names and Compose
  project names.
* **Reddit:** the shipped default is link-only. The public RSS form answered on
  2026-10-13, but permission is the operator's decision — see
  `docs/source-validation.md`.

## Where each feature lives

| Feature | Module | Tests | Fixtures |
|---|---|---|---|
| weather | `src/sideboard/features/weather/` | `tests/features/weather/` | synthetic Open-Meteo body |
| news | `src/sideboard/features/news/` | `tests/features/news/` | synthetic RSS in-test |
| hackernews | `src/sideboard/features/hackernews/` | `tests/features/hackernews/` | synthetic list + item JSON |
| youtube | `src/sideboard/features/youtube/` | `tests/features/youtube/` | synthetic feed, two Takeout CSV variants, history JSON |
| reddit | `src/sideboard/features/reddit/` | `tests/features/reddit/` | synthetic Atom feeds |
| portfolio | `src/sideboard/features/portfolio/` | `tests/features/portfolio/` | synthetic CSVs in-test |
| fx | `src/sideboard/features/fx/` | `tests/features/fx/` | synthetic rate bodies |
| markets | `src/sideboard/features/markets/` | covered by integration tests | synthetic payload |
| mac_system | `src/sideboard/features/mac_system/` | `tests/features/mac_system/` | fake `psutil` adapter |
| docker_local | `src/sideboard/features/docker_local/` | `tests/features/docker_local/` | fake command runner |
| github_actions | `src/sideboard/features/github_actions/` | `tests/features/github_actions/` | fake command runner returning REST run lists |
| git_local | `src/sideboard/features/git_local/` | `tests/features/git_local/` | fake command runner returning porcelain v2 |
| local_snapshot | `src/sideboard/features/local_snapshot/` | `tests/features/local_snapshot/` | valid/invalid/partial snapshots |
| diagnostics | `src/sideboard/features/diagnostics/` | `tests/features/fx/` covers the presenters | — |

No fixture contains personal data. `example.invalid` and `DEMO-*` identifiers are
synthetic.

## Known limits

* Publisher feeds change; a feed that stops parsing is reported per feed, and the rest of
  the digest continues.
* YouTube handles (`@name`) are **not** resolved by scraping — supply a channel ID or a
  Takeout import.
* Google Takeout watch history is imported once; it is a snapshot, not a live
  subscription sync. The ranking is called *recent viewing frequency*, never minutes
  watched.
* Reddit scores and comment counts are absent in RSS mode and stay absent.
* Market quotes come from one optional, unofficial source with an explicit symbol map.
  `VRC.WA` is a candidate mapping until the listing is verified against a statement.
* Missing quotes and missing FX rates are shown as missing, not as zero.
* Docker Desktop storage figures describe the engine's disk image, not the Mac
  filesystem.
* GitHub Actions is read through the authenticated `gh` CLI: one REST call per
  repository per refresh, newest runs only, so a failure older than the fetched page is
  not claimed. The token lives in gh's own keyring and is never read, stored or shown by
  this application. Nothing can cancel, retry or dispatch a workflow.
* GitHub's GraphQL `Repository` type has no Actions field; a `workflowRuns` selection
  fails, so the REST endpoint is the only route.
* Local git reads `status --porcelain=v2` and one `log -1` per checkout, with
  `--no-optional-locks`. Nothing is fetched, pulled, staged, committed, checked out or
  cleaned, and the index is not refreshed. Which files changed is deliberately not shown:
  it grows with the repository and cannot be read in a card row.
* Solar, IBKR Flex, mailbox automation, calendar access, Tailscale and release/CI
  widgets from the plan's optional list are **not implemented**; `local_snapshot.file`
  is the intended route for them and needs no Python.
