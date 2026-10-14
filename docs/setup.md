# Setup, controls, limits and recovery

Sideboard runs as one native Python process in a terminal window. The iPad is a
display; nothing is installed on it and no server listens anywhere.

## 1. Install and first run

```sh
uv sync --locked                 # Python 3.14 baseline, locked dependencies
uv run sideboard init            # writes ~/.config/sideboard/config.toml
uv run sideboard config validate
uv run sideboard                 # normal mode
```

Private locations, each with an explicit override:

| What | Default | Override |
|---|---|---|
| configuration | `~/.config/sideboard/config.toml` | `--config` or `SIDEBOARD_CONFIG` |
| database | `~/Library/Application Support/Sideboard/sideboard.sqlite3` | `--data-dir` or `SIDEBOARD_DATA_DIR` |
| logs | `~/Library/Logs/Sideboard/sideboard.log` | `--log-dir` or `SIDEBOARD_LOG_DIR` |

Files and directories are created owner-only (`0600`/`0700`). Logs contain status,
timings and redacted error categories — never balances, mail bodies, channel history or
credentials. Rotation is capped at 2 MiB × 4 files.

### Launch modes

| Command | Behaviour |
|---|---|
| `uv run sideboard` | normal: enabled sources collect on their configured cadence |
| `uv run sideboard --demo` | synthetic collectors only, separate `sideboard-demo.sqlite3`, permanent `DEMO DATA` label |
| `uv run sideboard --offline` | public HTTP suppressed entirely, permanent `OFFLINE` label; approved local metrics and the selected local Docker socket still collect |
| `uv run sideboard --no-mouse` | ignore hover events (keyboard interaction unchanged) |

## 2. Make it useful

Start with what needs no input at all: Mac statistics and Hacker News work
immediately. Then add only what you want:

```toml
[sources.weather_home.settings]
latitude = 55.7033
longitude = 21.1443
timezone = "Europe/Vilnius"      # display only; never used to guess a location
```

```sh
uv run sideboard doctor --docker  # lists local contexts; no container is touched
uv run sideboard doctor --network # bounded public probes
uv run sideboard doctor --mac     # local counters, no root
uv run sideboard features list    # every registered type, without fetching data
```

Pin a Docker context only after reading the listing:

```toml
[sources.docker.settings]
context = "orbstack"   # a *local* unix socket; ssh:// and tcp:// are refused
enabled = true
```

### Money without a brokerage connection

Manual entry and normalized CSV imports are the supported workflow; there is no
automatic brokerage access and no credential to create.

```sh
uv run sideboard import holdings ~/Downloads/positions.csv
uv run sideboard import prices  ~/Downloads/prices.csv
uv run sideboard import fx      ~/Downloads/rates.csv
uv run sideboard import youtube ~/Downloads/Takeout/YouTube   # preview only
```

Every import prints a preview first and refuses to commit in a non-interactive shell
without `--yes`. Locale, delimiter and date order are explicit flags
(`--decimal point|comma`, `--delimiter ,|;|auto`, `--date-order iso|dmy|mdy`) because a
guessed separator silently corrupts money.

Position CSV: `account,instrument_id,isin,venue,currency,quantity,cost_basis_total,cost_basis_currency,as_of`.
Price CSV: `instrument_id,currency,price,price_unit_multiplier,as_of`.
FX CSV: `base_currency,quote_currency,rate,as_of` — directed, so `PLN,EUR` and
`EUR,PLN` are different records.

A statement import replaces the selected account's positions atomically, archives the
previous state for rollback, rejects rows with invalid values instead of dropping them,
and treats re-importing the same file as a no-op. An older statement date is refused
unless you pass `--allow-older` (historical replacement); an empty snapshot needs
`--allow-empty` and an explicit `--account`.

### Snapshot files for your own tools

`local_snapshot.file` consumes a JSON file that validates against
`src/sideboard/schemas/local-snapshot.schema.json`. The producer should write a
temporary file and rename it atomically; an invalid or partial update is rejected and
the last good snapshot is kept with a warning. Only explicitly configured paths are
read, capped at 1 MiB.

## 3. Controls

| Input | Action |
|---|---|
| `1`–`9` | select the first nine visible tabs, in configured order |
| `Ctrl+P` | palette: any tab, any widget, and the application actions |
| `Ctrl+R` | validate and reload configuration; an invalid update keeps the running one |
| `Tab` / `Shift+Tab` | move focus between cards |
| `↑` / `↓` | move the selection inside the focused card |
| `PgUp` / `PgDn`, `Home` / `End`, wheel | scroll a tab whose cards are taller than the window |
| `Space` | toggle global visual pause (collection is independent) |
| `p` | pin / unpin the focused Overview card |
| `n` | advance the focused rotating card, even while paused |
| `Enter` / click | open the detail view |
| `o` | open the selected validated HTTP(S) link in your browser |
| `s` | save / unsave the selected item |
| `r` | request a rate-limited refresh of the focused card's provider |
| `v` | privacy mode: mask amounts, process names, container and project names |
| `Ctrl+D` | mouse/focus diagnostic and reset holds (any key closes it) |
| `Ctrl+Y` | release every hold |
| `?` | help · `Esc` close/back · `q` quit |

## 4. Interaction rules, and what can go wrong

* **Hover pauses only the hovered card.** Data keeps collecting; the card shows the
  latest valid snapshot when the pointer leaves, then resumes its remaining dwell after
  a two-second grace period.
* **Stationary hover stays paused.** No idle timer overrides it.
* **Keyboard navigation holds the focused card for 15 seconds** (configurable).
  Programmatic initial focus does not start a hold.
* **A detail modal pauses all visible rotation** until it is closed.
* **Losing terminal focus clears stale pointer holds only.** It never pauses rotation
  globally; the app keeps rotating while you work in another window.
* **Holds are tracked per reason** (`hover`, `interaction`, `modal`, `manual`), so
  releasing one cannot clear another.

If hover or mouse selection does not work, the terminal is probably not reporting
motion. Press `Ctrl+D`: the diagnostic screen counts `MouseMove`, `Enter`, `Leave`,
click and blur events and says when motion has never been seen. `Esc`, `q`, or any other
key except `r` closes it again, so a terminal that sends an unexpected byte (Ctrl+D is
also end-of-input) cannot leave you stuck on it. `Ctrl+Y` releases every hold, and
`uv run sideboard --no-mouse` turns hover off entirely. Selection, pausing and
navigation all work from the keyboard, so nothing is lost.

Terminal requirements: any terminal that reports its size. No Nerd Font, image
protocol, gradient, special colour scheme or emoji meaning is required.

### How a tab is laid out

A card is always laid out with a usable height:

* the **Overview** fills the window: every configured slot, two per row, in slot order
  (pin two weather sources to compare them side by side), and narrowing merges the
  rotating queues rather than squeezing them;
* a **detail tab** (Money, Watch, Life) sizes each card from what it actually contains,
  so a table gets rows and a metrics card gets lines;
* a **stack tab** (Systems) does the same and **scrolls** when the cards together need
  more rows than the window has — `PgDn` or the wheel reaches the later cards;
* a **filterable tab** (Read) gives one row to the subfilter bar and the rest to the
  selected view.

If a card looks too small, press `Enter` on it: the detail view uses the whole window.

## 5. Sizes

Measured in terminal columns and rows.

| Size | Overview |
|---|---|
| ≥110 × 32 | two slots per row, as many rows as there are configured slots |
| ≥70 × 20 | pinned slot on top, merged rotating queue below |
| ≥60 × 20 | one card at a time, pins preserved |
| below 60 × 20 | compact one-line summaries and a status list instead of clipped cards |

Resizing preserves selection, read state and pins. The other tabs stack, grid or
filter, and keep working as the window shrinks.

## 6. Configuration reload

`Ctrl+R` (or `uv run sideboard config validate` then reload) parses and validates a
candidate configuration completely before applying it. Only changed source instances
are restarted; new sources start, removed ones are cancelled and closed. Tabs,
categories, queue order, dwell times and theme changes need no restart. **New Python
feature code does need a normal restart** — there is no hot reload.

If an update is rejected, the previous valid configuration keeps running and the
field-level problems are shown in a notification and in the log.

## 7. Recovery

| Symptom | What to do |
|---|---|
| A card says *not configured* | the widget has no source binding; check `widgets.<id>.sources` against `sideboard features list` |
| A card says *no data yet* | the source is bound but has not succeeded; press `r`, then check Systems → Sources for the error category |
| A source shows `blocked` | the provider refused access. Sideboard does not evade blocks; fix access legitimately or disable the source |
| A source shows `unavailable` | a local dependency is missing (Docker CLI, socket, file, sensor). `doctor --docker` / `--mac` explains it |
| Values look old | freshness is shown per card (`as of …` for the source date, `updated …` for the fetch). Old data is labelled, never refreshed silently |
| Nothing renders | `uv run sideboard doctor` reports config validity, and the log file has the failing component |
| Docker source stops after `docker context use` | revalidation refused the new endpoint. Pin the approved context in the config; Sideboard never follows the global context |
| Money total looks incomplete | at least one holding has no quote or no FX rate. That is deliberate: `incomplete: N of M unpriced` |
| The Overview stopped rotating | a manual pause, a held card, or an open modal. `Ctrl+Y` releases holds; the status bar shows `PAUSED` |
| The app misbehaves after editing code | restart the process; Python changes are not hot-reloaded |
| Only the first cards of a tab are visible | that tab is taller than the window; scroll with `PgDn` or the wheel, or press `Enter` for one card full-screen |

## 8. Measured behaviour

Recorded on the development Mac (macOS, Python 3.14, `TERM_PROGRAM=muxy`):

* warm start to first paint: under one second
* idle CPU with the Mac source at 5 s in Systems: dominated by the 5 s collector, not
  by redraws; card rendering is signature-gated, so an unchanged card does not repaint
* the app keeps collecting while another Mac window is focused (no app-level pause)
* real-terminal smoke test (pseudo-terminal via `script`, `--demo`, 25 s): the app
  started, rendered the tab row for all six tabs, the status bar
  (`Sideboard · HH:MM · DEMO DATA · 1 stale`), the footer controls and a full bordered
  modal screen with live counters; it collected demo data (a 346 KB WAL in the demo
  database) and exited cleanly. No stylesheet, Rich-style, identifier or `NoMatches`
  errors appeared in the session or the log file
* the pseudo-terminal keeps only ~24 KB of session output, so the card region of the
  Overview was not captured there; card content, selection, holds, privacy and layout
  are asserted instead by headless Pilot tests in `tests/ui/`

Not verified here: hover and mouse selection in a real terminal. Headless tests passing
does not prove that the terminal reports motion — that is exactly what `Ctrl+D` exists
to check, and the decision to trust it is yours.
