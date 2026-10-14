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

The bottom bar carries the application name and the clock with the mode markers
(`Sideboard · 14:52 · DEMO DATA · 1 stale`) on the left, and one hint, `? help`, on the
right. Every key is in the help screen (`?`) and in this table; the bar deliberately does
not list them, because a dashboard read from across a room does not need a keyboard
reference on screen. The tabs are the first row, so the cards get the rest of the window.

| Input | Action |
|---|---|
| `1`–`9` | select the first nine visible tabs, in configured order |
| `t` / `Ctrl+T` | next colour palette / pick one (written to `app.theme`) |
| `Ctrl+P` | palette: any tab, any widget, any palette, and the application actions |
| `Ctrl+R` | validate and reload configuration; an invalid update keeps the running one |
| `Tab` / `Shift+Tab` | next / previous card of the visible tab, wrapping |
| `←` / `→` (`h` / `l`) | move to the card on the left / right, wrapping at the ends |
| `↑` / `↓` (`k` / `j`) | move the selection inside the focused card; at the first or last row it moves to the card above or below |
| `PgUp` / `PgDn`, `Home` / `End`, wheel | scroll a tab whose cards are taller than the window |
| `Space` | toggle global visual pause (collection is independent) |
| `p` | pin / unpin the focused Overview card |
| `n` | advance the focused rotating card, even while paused |
| `Enter` / click | open the detail view |
| `Enter` / click inside the detail view | open the link on the selected row; a click on the dimmed margin closes the view |
| `o` | open the selected validated HTTP(S) link in your browser |
| `s` | save / unsave the selected item |
| `r` | request a rate-limited refresh of the focused card's provider |
| `v` | privacy mode: mask amounts, process names, container and project names |
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
* **Chrome says one thing.** A card's border shows one timestamp — `updated 17:05`, or
  `stale`/`expired` when that is the honest answer — and nothing else: interaction state
  ("held", "pinned") is visible from the border itself, and coverage ("partial") is stated
  next to the numbers it qualifies. The full view repeats the same line in the same place,
  so a card and its detail can never disagree about when the data arrived.
* **Nothing explains itself on screen.** Attribution, methodology notes, coverage caveats,
  collection warm-up lines ("rate baseline reset"), ranking bases and interaction state are
  recorded in the payload and in the widget contract but **not drawn anywhere** — not on a
  card, not in a full view, not under a table. A card shows values; a full view shows more
  values. The only extra words allowed are real states: a source that has no data yet, a
  service expected up that is not, a feed that failed, a table cut short ("truncated"), a
  degraded connection, and the one `stale`/`expired` word on the timestamp line. A state is
  one short line — `StatusView.detail` and `hints` are recorded for `doctor` and the docs
  and are not drawn, and a card with nothing wrong shows no state line at all.
* **A missing value is an em dash.** Why it is missing is not printed beside it.
* **A modal closes when you click outside it.** The detail, help, palette and theme
  pickers are centred with a margin all round; clicking that margin dismisses them, and a
  click inside does not (clicks bubble from children, so the test is against the content
  region rather than the widget that received the event).
* **Links open from the detail view.** `Enter` or a click opens the link on the row under
  the cursor; a row with no link says so instead of opening another row's.
* **Losing terminal focus clears stale pointer holds only.** It never pauses rotation
  globally; the app keeps rotating while you work in another window.
* **Holds are tracked per reason** (`hover`, `interaction`, `modal`, `manual`), so
  releasing one cannot clear another.

If hover or mouse selection does not work, the terminal is probably not reporting
motion. The pointer/focus diagnostic is a development tool rather than a dashboard
control: open it from the palette (`Ctrl+P`, then "mouse") or with
`uv run sideboard doctor --mouse`. It counts `MouseMove`, `Enter`, `Leave`, click and
blur events and says when motion has never been seen; `Esc`, `q`, or any other key
except `r` closes it again. `Ctrl+Y` releases every hold, and
`uv run sideboard --no-mouse` turns hover off entirely. Selection, pausing and
navigation all work from the keyboard, so nothing is lost.

Terminal requirements: any terminal that reports its size. No Nerd Font, image
protocol, gradient, special colour scheme or emoji meaning is required.

### How a tab is laid out

A card is always laid out with a usable height:

* the **Overview** fills the window: every configured slot, two per row, in slot order
  (pin two weather sources to compare them side by side), and narrowing merges the
  rotating queues rather than squeezing them;
* a **detail tab** (Money) sizes each card from what it actually contains, so a table gets
  rows and a metrics card gets lines;
* a **grid tab** (Life) puts two cards per row and gives each row half the window: the two
  weather cards share the top row and the uploads list takes the second one at full
  height, so all three are on screen without scrolling;
* a **stack tab** (Systems) sizes each card from its content and **scrolls** when they
  together need more rows than the window has — `PgDn` or the wheel reaches the later
  cards;
* a **filterable tab** (Read) gives one row to the subfilter bar and the rest to the
  selected view.

If a card looks too small, press `Enter` on it: the detail view uses the whole window.

## 5. Colour palettes

`app.theme` names the palette; the default is `sideboard`, the restrained dark theme the
dashboard has always used. Eleven ship with it, taken from the palettes their authors
publish:

| `app.theme` | Palette |
|---|---|
| `sideboard` | the default: low chroma, thin borders |
| `catppuccin-mocha` | [Catppuccin](https://catppuccin.com/palette) Mocha |
| `tokyo-night` | [Tokyo Night](https://github.com/folke/tokyonight.nvim) |
| `nord` | [Nord](https://www.nordtheme.com) |
| `gruvbox` | [Gruvbox](https://github.com/morhetz/gruvbox) dark |
| `dracula` | [Dracula](https://draculatheme.com) |
| `one-dark` | [One Dark](https://github.com/atom/one-dark-syntax) |
| `solarized-dark` | [Solarized](https://ethanschoonover.com/solarized) dark |
| `rose-pine` | [Rosé Pine](https://rosepinetheme.com) |
| `kanagawa` | [Kanagawa](https://github.com/rebelot/kanagawa.nvim) |
| `everforest` | [Everforest](https://github.com/sainnhe/everforest) |

```toml
[app]
theme = "catppuccin-mocha"
```

`uv run sideboard themes list` prints the same list, and `uv run sideboard themes show
<name>` prints every colour of one palette.

**Switching in the app keeps the choice.** `t` moves to the next palette, `Ctrl+T` opens a
picker, and `Ctrl+P` lists them as commands. A switch applies immediately *and* writes
`app.theme` into the configuration — that one line, edited in place, so your comments,
ordering and every other setting survive untouched. The edited file is parsed and
resolved before it replaces the working one, so a dashboard never leaves behind a
configuration it cannot read; if the file cannot be written (or does not exist yet), the
palette still applies for the session and a notification says what failed.

Any single colour can be adjusted without leaving the palette — the token names are the
palette's own field names (`background`, `surface`, `panel`, `boost`, `foreground`,
`value`, `accent`, `primary`, `secondary`, `ok`, `info`, `warn`, `error`, `dim`, `item`,
`read`, `border`, `border_focus`, `focus`, `focus_bg`, `held`, `border_held`):

```toml
[app]
theme = "nord"

[app.theme_colors]
focus_bg = "#123456"
dim = "#8a95a1"
```

Rules the implementation holds to:

* one palette drives **both** the Textual theme (borders, chrome, backgrounds) and the Rich
  theme the card renderers resolve their token names against, so a palette change
  recolours the text as well as the frame;
* a missing or misspelled colour is a configuration error, never a silent fallback: an
  unknown palette name or token is rejected by `config validate` with the valid names;
* a palette must define every token — a test asserts it, because a missing one would
  surface as a `MissingStyle` error in the middle of a repaint;
* no feature chooses a colour: features render through the shared token names, so every
  palette applies to every card, including features added later.

## 6. Sizes

Measured in terminal columns and rows.

| Size | Overview |
|---|---|
| ≥110 × 32 | two slots per row, as many rows as there are configured slots |
| ≥70 × 20 | pinned slot on top, merged rotating queue below |
| ≥60 × 20 | one card at a time, pins preserved |
| below 60 × 20 | compact one-line summaries and a status list instead of clipped cards |

Resizing preserves selection, read state and pins. The other tabs stack, grid or
filter, and keep working as the window shrinks.

## 7. Configuration reload

`Ctrl+R` (or `uv run sideboard config validate` then reload) parses and validates a
candidate configuration completely before applying it. Only changed source instances
are restarted; new sources start, removed ones are cancelled and closed. Tabs,
categories, queue order, dwell times and theme changes need no restart. **New Python
feature code does need a normal restart** — there is no hot reload.

If an update is rejected, the previous valid configuration keeps running and the
field-level problems are shown in a notification and in the log.

## 8. Recovery

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

## 9. Measured behaviour

Recorded on the development Mac (macOS, Python 3.14, `TERM_PROGRAM=muxy`):

* warm start to first paint: under one second
* idle CPU with the Mac source at 5 s in Systems: dominated by the 5 s collector, not
  by redraws; card rendering is signature-gated, so an unchanged card does not repaint
* the app keeps collecting while another Mac window is focused (no app-level pause)
* real-terminal smoke test (pseudo-terminal via `script`, `--demo`, 25 s): the app
  started, rendered the tab row for all six tabs, the bottom bar
  (`Sideboard · HH:MM · DEMO DATA · 1 stale` and `? help`) and a full bordered
  modal screen with live counters; it collected demo data (a 346 KB WAL in the demo
  database) and exited cleanly. No stylesheet, Rich-style, identifier or `NoMatches`
  errors appeared in the session or the log file
* the pseudo-terminal keeps only ~24 KB of session output, so the card region of the
  Overview was not captured there; card content, selection, holds, privacy and layout
  are asserted instead by headless Pilot tests in `tests/ui/`

Not verified here: hover and mouse selection in a real terminal. Headless tests passing
does not prove that the terminal reports motion — that is exactly what the diagnostic
screen exists to check, and the decision to trust it is yours.
