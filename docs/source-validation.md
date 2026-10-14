# Source validation record

Probes executed on the development Mac on **2026-10-13** with
`sideboard doctor --network` and `doctor --mac` / `doctor --docker`, plus direct bounded
HTTP checks. Nothing here was executed from a research container.

A result is a fact about this network at this moment. It is **not** proof that a
provider is down, and it is **not** evidence that the provider's terms permit automated
access.

## Public sources

| Source | Endpoint | Result | Enabled in shipped defaults |
|---|---|---|---|
| Open-Meteo | `api.open-meteo.com/v1/forecast` | `200`, valid JSON (one transient `503` observed, then three consecutive `200`) | yes |
| Hacker News | `hacker-news.firebaseio.com/v0/topstories.json` | `200`, 4501 bytes | yes |
| Frankfurter v2 | `api.frankfurter.dev/v2/providers/ecb/rate/PLN/EUR` | `200`, `{"date":"2026-10-12","base":"PLN","quote":"EUR","rate":0.22805}` — the documented v2 shape | manual mode by default |
| Frankfurter v1 | `api.frankfurter.dev/v1/latest?base=PLN&symbols=EUR` | `200`, `rates.EUR` present; kept as a fallback route | fallback only |
| YouTube channel Atom | `youtube.com/feeds/videos.xml?channel_id=UCsBjURrPoezykLs9EqgamOA` | `200`, `application/atom+xml`, well-formed `<feed>` | source enabled only after channels are configured |
| Reddit public RSS | `reddit.com/r/selfhosted/top/.rss?t=day` | `200`, `application/atom+xml`, 63731 bytes | **no** — shipped default is link-only mode; see below |

Publisher feeds:

| Feed | Result | Category | Default |
|---|---|---|---|
| `https://www.atviraklaipeda.lt/feed/` | `200`, `application/rss+xml` | local | enabled |
| `https://www.lrt.lt/?rss` | `200`, `application/rss+xml` (the site root itself is HTML, so the `?rss` form is the feed) | lithuania | enabled |
| `https://rss.dw.com/rdf/rss-en-all` | `200`, `text/xml`, RDF | europe | enabled |
| `https://feeds.bbci.co.uk/news/world/rss.xml` | `200`, `text/xml` | world | enabled |
| `https://feeds.bbci.co.uk/news/business/rss.xml` | `200` | business | enabled |
| `https://laravel-news.com/feed` | `200`, `text/xml` | software | enabled |
| `https://changelog.com/news/feed` | `200`, `application/xml` | software | enabled |
| `https://newsletter.pragmaticengineer.com/feed` | `200`, `application/xml`, 649 KB | software | enabled (public excerpts only) |
| Lėtas Pelnas | not probed — no publisher-supported feed URL was established | personal_finance | **not shipped**; the plan's candidate entry is disabled and points at a same-category placeholder |
| Klaipėda municipality/tourism | not probed | local, optional | not shipped |

Consequences recorded honestly:

* The **personal finance** bucket has no validated publisher feed. The digest will show
  that bucket as unavailable rather than padding it with stale or unrelated news.
* The plan's candidate feed list is a starting point, not a guarantee: URLs change, and
  each publisher's terms still apply to your use.
* `sideboard doctor --network` re-runs these probes; treat a `503` as transient first.

### Reddit

The public RSS form answered with `200` and `application/atom+xml` from this Mac.
That response is **not** a grant of permission. Reddit documents sign-up,
authentication and conditional free/paid access, and its terms are the operator's
responsibility. Therefore:

* the shipped default is `mode = "links"`, which performs **no** network I/O and shows
  community links plus an explanation;
* `mode = "rss"` exists and is one configuration edit away, but switching it on is an
  explicit decision;
* the adapter contains no proxy rotation, alternate host, `.json` substitution, cookie
  copying or browser impersonation, and does not retry a `403`.

## Local sources

| Source | Probe | Result |
|---|---|---|
| Mac counters | `doctor --mac` | CPU warming-up sample then a real `26.2 %` host-wide reading; RAM `77.1 %` (16.9 GiB available); swap `6.5 GiB` used; `/` 22.6 GiB free; `en0` rates over a 0.5 s real interval; battery present, `100 %`, plugged in, time remaining unknown |
| Mac fields that are absent | `doctor --mac` | temperature, fans, GPU and power draw are not collected; no privileged helper or `sudo powermetrics` is used |
| Docker CLI | `doctor --docker` | `/usr/local/bin/docker` present |
| Docker contexts | `doctor --docker` | `default` → `unix:///var/run/docker.sock`; `orbstack` (current) → `unix:///Users/<user>/.orbstack/run/docker.sock`. Both local; the daemon reported server version `29.8.2`. No container was started, stopped or inspected at validation time |
| Docker pins | config validation | a `ssh://`/`tcp://`/`npipe://` context value is rejected in the settings model and again at runtime |
| Docker collector end-to-end | read-only fetch against the pinned `orbstack` context | engine `29.8.2`, 18 CPUs, endpoint `unix:///Users/<user>/.orbstack/run/docker.sock`; 27 containers read with state, CPU, memory, restart count and exit code; the one container with a healthcheck reported `healthy` and the rest reported *running / health unknown*; `system df` read Images/Containers/Volumes/Build Cache; a second sample 4 s later derived network rates from the counters and did not report a false gap |

The shipped Docker default leaves `context = ""`, so nothing is collected until a
context is pinned explicitly. That is deliberate: there is no guessed default.

## Bugs found only by the real smoke test

The fixture-based Docker tests passed while the adapter was still broken against a real
engine. Three defects were found by pointing it at the local socket, and all three are
now covered by regression tests:

1. `docker context inspect` nests the endpoint as an object
   (`{"docker": {"Host": "unix://…"}}`), so the endpoint looked non-local and every
   collection was refused as "remote".
2. The inventory format template used backslash-escaped quotes. Because arguments are
   passed directly (no shell), the backslash was literal and Go's template parser
   aborted the call.
3. `{{.State.Health}}` does not exist for a container without a healthcheck, and Docker
   runs templates with `missingkey=error`, which aborted the whole inspect call. The
   template now uses `index .State "Health"`, which yields the zero value instead.

A fifth and sixth defect were found by an actual terminal run, and one of them is why
"nothing worked" on first use:

5. The tab body chain (`TabbedContent` → `ContentSwitcher` → `TabPane` → pane) had no
   height anywhere. Textual defaults all three to `height: auto`, and an `auto`
   container cannot give a `1fr` child space, so **every card was laid out at height 0**:
   the user saw tab titles, card border titles and the footer, and no widget content.
6. On the Read tab the unconstrained filter bar claimed the entire tab, leaving the
   card at height 0.

`tests/ui/test_rendering.py` now reads back the *composited screen text* (through
Textual's SVG export, because `Screen.render_line` only renders the screen widget
itself) and asserts real values on screen, positive card heights at every target size,
and that a tall tab scrolls to reach its later cards. Asserting `card.view` was never
enough, and that is exactly what the earlier test suites did.

A fourth defect was found by running the CLI on a fresh database: no migration created
the `fx_rates` table, so `import fx` only worked where the table had been created by
hand. `tests/contracts/test_migrations.py` now asserts that every table a repository
reads is created by a registered migration.

## Weather forecast links (2026-10-14)

`https://www.yr.no/en/forecast/daily-table/<lat>,<lon>` and
`https://www.wunderground.com/weather/<lat>,<lon>` both resolve for a coordinate pair
(HTTP 200); `timeanddate.com/weather/@<lat>,<lon>` does not. The weather rows use the
yr.no form, built from the coordinates the source was configured with. No API, key or
scraping is involved — it is a page opened in the user's browser on request.

## Dev sources (2026-10-14)

Probed on the machine this was built on, read-only:

* `gh` 2.100.0, authenticated as a real account with `repo`, `workflow`, `read:org` and
  `gist` scopes. `gh api "/repos/<owner>/<name>/actions/runs?per_page=12"` returned live
  runs, including three in progress and two recent failures, in about 0.8 s per
  repository; six repositories cost 8.3 s of one child process at a time.
* `gh api graphql` was tried first and **fails**: `Field 'workflowRuns' doesn't exist on
  type 'Repository'`. The REST endpoint is the supported route.
* `gh search prs --review-requested=@me` and `gh api /notifications` also work; neither is
  implemented yet.
* `git status --porcelain=v2 --branch` and `git log -1` returned branch, ahead/behind,
  changed and untracked counts and the last commit for six real checkouts.
* No Linear, GitLab, Jira or Sentry CLI is installed, and no API key is present in the
  environment, so those integrations are unimplemented rather than unverified.

## Unconfigured, blocked or unverified

| Item | State |
|---|---|
| Portfolio holdings | **unconfigured**: valid empty state until a manual entry or import |
| Prices | **unconfigured**: no quotes stored; a missing quote is not zero |
| FX | manual mode with an empty store; `reference` mode validated against Frankfurter only |
| Market quotes | **disabled**; requires the optional `markets` extra and an explicit symbol map |
| YouTube | **unconfigured**: no channel IDs and no Takeout files were supplied; handle resolution is refused by design |
| Local snapshots | **unconfigured**: no producer file was supplied |
| Solar (APVA), IBKR Flex, mailbox, calendar, Tailscale, CI/release widgets | **not implemented**; described in the plan as optional later work |

No personal data was read during validation: no broker statement, YouTube history,
mailbox export or application database was inspected.
