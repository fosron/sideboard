# Sideboard — local Mac TUI dashboard

**Implementation handoff · revision 2 · updated 13 October 2026**  
**Working name:** Sideboard. The name is provisional, not a claim of uniqueness.  
**Companion instructions:** `AGENTS.md` and `docs/adding-widget.md`.

**Scope update:** Mac and local Docker statistics are first-release features. Manual ETF/holding entry and broker-export imports are first-class workflows. Price-bot, Spanish-learning and habit widgets are excluded. Extensibility for future agent-led additions is a required acceptance criterion, not an optional refactor.

## 1. Decision and product boundary

Build a single-user **Python + Textual terminal application**, running on the Mac in a terminal window moved onto the iPad's extended desktop. The iPad is a display, not a server or a separate client. Use SQLite for local state, public feeds/APIs for public information, and explicit read-only imports or bridges for personal information.

The product should answer **“What is worth noticing now?”**, not reproduce six websites in miniature. Its main screen is a calm rotating dashboard; dedicated tabs allow deliberate reading. It must be useful without creating an account, registering an API key, running Docker, or granting access to private applications. Monitoring an existing local Docker engine is supported, but the dashboard itself runs natively and must still work when Docker is absent or stopped.

### Non-negotiable constraints

- **Zero mandatory API keys; no paid API, free trial requiring payment, paid LLM, or per-request billing.** Optional existing credentials are never silently discovered or copied.
- A free library does not imply unrestricted access to its upstream data. Respect provider terms, authentication requirements, rate limits, and blocks.
- Local-first storage, no telemetry, no cloud synchronization, no public HTTP listener, no automatic updates to other applications.
- No trading, banking writes, sending email, marking messages read, voting on Reddit, infrastructure mutations, or automatic media playback.
- Missing data must look missing. Old data must look old. Demonstration data must never appear as personal/live data.

**Research boundary:** prior conversations and selected recent email were reviewed. Official documentation was reviewed online. The Mac, its applications, broker statements, and YouTube history were not inspected. Direct endpoint probes from the research container could not resolve hosts; this does **not** establish provider downtime. Publisher feed URLs and instrument mappings marked “candidate” require a small Mac-side validation before enablement. This document is a plan, not a report of a running implementation.

## 2. Personal context that should influence the build

These are design inputs, not permission to scan the user's machine. Do not commit personal holdings, mailbox exports, or these notes as application fixtures.

| Evidence from chats or email | Consequence for the dashboard |
|---|---|
| Latest user-reported Vercom holding is **7,000 shares**; older conversations contain a superseded number. | Offer 7,000 as an explicitly unverified onboarding suggestion. Do not silently install it as broker-confirmed holdings. |
| Recent ETF conversations use **VGLA/VALL**, IBKR, and Revolut. The precise ETF identity and executed quantity were not established. | Resolve ISIN, trading venue and currency from a statement. Do not substitute VWCE or equate VGLA with VALL. |
| The user has discussed monthly investing and moving part of Vercom into ETFs in early 2028. | Optional contribution progress and a planning note; no exact unlock date, assumed legal availability, or investment recommendation. |
| Recent mail includes IBKR activity-statement notifications and closure of a Revolut **Robo-Advisor** account; the user explicitly accepts manually supplied/exported ETF values. | Prioritize manual entry and normalized CSV imports; broker-specific importers are conveniences. Robo-Advisor closure does not establish closure of all Revolut investing accounts. No automated brokerage connection is required. |
| CapTrack was discussed as a simpler AI-usage application; Sofalynx as a local YouTube client. | Reuse sanitized local data where an actual implementation/interface exists. Do not assume the planned projects expose an API. |
| The user requests Mac resource statistics and local Docker monitoring. | Include independent Mac and Docker source modules, Overview summaries and a useful Systems tab. Do not assume which Docker-compatible runtime or context is installed. |
| The user wants future widgets, categories and layout changes to be easy for coding agents. | Use feature-local code, explicit typed extension contracts, declarative placement and a tested extension recipe. No widget-specific branches in core orchestration. |
| Recurring mail includes TLDR, Changelog Nightly, The Pragmatic Engineer, Lėtas Pelnas and Laisvės TV. | Suggest a curated reading queue. Receiving a newsletter does not prove it is read, preferred, or a watched YouTube channel. |
| GitHub, Hetzner, Cloudflare and Tailscale appear in recent email. | Suggest actionable CI/review, service-health, backup and renewal summaries, rather than another full notification inbox. |
| A Cloudflare renewal failure was followed by a successful renewal. | Attention widgets must reconcile later success instead of preserving a permanent failure badge. |
| On 13 October, the solar provider said generation-monitoring access was still to follow; ESO data is reported monthly, at the beginning of the next month. | Solar is a pending integration. Separate monitoring output from monthly ESO accounting; do not label either as live without evidence. |
| Home context is Klaipėda; recent travel is in Spain. | Support explicit Home and Travel weather profiles. Never select weather location from an email login IP or timezone alone. |
| The user previously asked for news not to become overwhelmingly AI-focused. | Maintain category diversity, including local, Lithuania/Europe, world, business and personal finance. |

The solar project is a **share of a remote solar park**, not necessarily an inverter attached to the user's home network. Provider identity, ownership allocation and monitoring protocol still need discovery. No current YouTube favorite-channel list or Reddit memberships were established.

## 3. What belongs in the product

### Required first release

| Area | What should be glanceable | Acquisition and setup | Priority |
|---|---|---|---|
| Portfolio | Tracked value in EUR, ETF/Vercom split, price movements, freshness, statement date | Manual/CSV positions; optional no-key quotes; daily reference FX | Core |
| Vercom | Holding value in PLN/EUR, declared/received dividends, official report/calendar links | Same holdings engine; official investor-relations pages | Core |
| Weather | Current conditions, rain next six hours, wind, today/tomorrow and seven-day forecast | Open-Meteo; selected city/coordinates, no key | Core |
| News | A few useful local, Lithuanian/European, world, business and technology headlines | Curated public RSS/Atom, no news-service account | Core |
| Hacker News | Top and New; optional Show HN and Ask HN | Official public API, no key | Core |
| YouTube | New uploads from selected channels, ranked using favorites or imported viewing history | Channel feeds; manual list/Takeout; optional Sofalynx bridge | Core |
| Reddit | Selected communities, daily top/new threads, explainable relevance | Conditional public feed support; otherwise clearly labelled link-only mode | Core surface; access-dependent data |
| Mac statistics | CPU, memory, swap, disk free, network rates, battery and uptime | Local `psutil` collector; no key, root access or helper daemon | Core |
| Local Docker | Container/Compose status, CPU/RAM, I/O and restarts | Existing Docker CLI, explicitly selected local context, read-only operations | Core; local-runtime-dependent |
| Local snapshots | One reusable card/list interface for the user's own apps | User-selected JSON file, no new service or credentials | Core adapter |

“Core” means an implemented, tested integration and usable setup/fallback, not fabricated live data when credentials or user files are absent. Report an externally blocked integration separately from a successfully populated one.

### Recommended additions, in order

| Widget | Why it earns screen space | First safe integration route |
|---|---|---|
| **AI usage / reset window** | Can prevent a coding session from running into a known limit | CapTrack snapshot, if implemented. Distinguish measured tokens from provider-reported quota/reset. |
| **Needs attention in development** | Failed CI, reviews requested, stalled jobs and failed backups are actionable | Existing authorized `gh` CLI; selected local service snapshots. Only allowlisted repos/services. |
| **Reading queue / release radar** | Fits the newsletters and technical stack already followed | Public publisher/release feeds, saved links; no full inbox reader needed. |
| **Home-lab health and backup age** | Complements the requested Mac/Docker statistics with actionable service failures | Existing health endpoints or explicit local snapshots; optional Tailscale status. |
| **Renewals and recurring services** | Consolidates domain/service renewals and genuine payment problems | Manual records first; later an approved local mail bridge. A receipt is not an unpaid bill. |
| **Solar generation and APVA status** | Personally relevant new asset with pending monitoring setup | Manual monthly readings initially; later the actual provider's permitted interface. |
| **Next appointment / focus window** | Useful while coding, without another calendar application | User-selected ICS file; optional permission-gated native calendar helper later. |

Keep these additions optional. Mac and Docker statistics above are core, not part of this optional list. Do not turn the first release into a Gmail client, observability platform, complete portfolio-accounting package, or personal assistant. The explicitly excluded widgets must not reappear in sample layouts, fixtures or the implementation backlog.

## 4. Source strategy and credential budget

Use the best documented source first, then a clearly labelled fallback. A source can be publicly documented yet still fail on the user's connection. Record actual validation results in `docs/source-validation.md` during implementation.

| Provider | Access and reliability | Starting refresh policy | Fallback |
|---|---|---|---|
| Open-Meteo | Documented free personal/non-commercial service; attribution required [S04] | 20 minutes per configured active location | Last good forecast, with validity/staleness |
| HN official API | Documented public endpoints [S05] | Top/New 5 minutes; optional Ask/Show 15 minutes | Cached stories |
| Publisher RSS/Atom | Individual publisher terms; starter URLs need validation | 20 minutes; conditional HTTP requests | Other enabled feeds, cached items, source link |
| YouTube channel Atom | Feed URL documented by Google [S06]; not a full account-history API | Top 12 channels every 30 minutes; others every 2 hours | Cached uploads; pinned-channel links |
| Yahoo via yfinance | Unofficial, best-effort, personal-use restrictions; not guaranteed market coverage [S09] | Hourly by default; optional 15-minute mode for a small allowlist | Dated manual/statement quotes, not invented prices |
| Frankfurter v2, ECB provider | No key; daily reference FX, not execution/live trading rates [S10] | Every 12 hours; retain source date | Last reference rate with date, or no EUR conversion |
| IBKR Flex, optional | Preconfigured statement queries with an expiring reporting token [S11] | Once daily while the app runs | Manual statement import |
| Reddit | Access and authorization are conditional [S12] | 30–60 minutes only after permitted access is validated | Blocked/link-only state, no evasion |
| Vercom investor relations | Official reports/calendar/dividend information [S13] | At most every 6 hours after a suitable source is validated | Official links and manual dated events |
| Local files | Explicit paths; proposed snapshot format, not an existing app contract | Check modification time every 30 seconds | Stale snapshot or unconfigured state |
| Mac metrics | Local `psutil` APIs; capability-check on macOS [S20] | 5 s in Systems; 15 s in Overview; 60 s otherwise | Field-level unavailable/warming/stale state |
| Docker local | Existing CLI and a pinned, approved local context [S21–S24] | Stats 15 s; metadata 30 s; hidden sources 60 s | Runtime unavailable; never auto-start it |
| GitHub, optional | Public feeds or existing `gh` authorization; honor API budgets [S14] | 10 minutes for selected repos | Release links / unavailable state |

**Mandatory credentials: zero.** Optional IBKR Flex introduces one reporting credential. Existing `gh` authorization is optional and only used after enabling the integration. Private ICS URLs are credentials even though they are not called API keys. macOS Automation/Calendar access is permission-gated, not equivalent to credential-free unrestricted access.

The ChatGPT email connector used for this research is **not** a runtime dependency available to the new application. Do not design around ChatGPT's connectors, a ChatGPT subscription as an API entitlement, or exported browser session tokens.

## 5. Tabs, display and interaction

### Information architecture

Ship six compact default tabs: **Overview · Money · Read · Watch · Life · Systems**. They are configuration entries, not an architectural limit. Tab labels, order, visibility and widget placement are editable without Python changes; categories are open string IDs, not a closed enum.

- **Overview:** rotating cards with optional pinned cards.
- **Money:** portfolio table, Vercom details, quotes/FX dates, dividend/event list.
- **Read:** News / HN / Reddit / Saved subfilters, not four extra top-level tabs.
- **Watch:** selected channels, new uploads, favorites, import/freshness information.
- **Life:** weather detail; optional appointments and solar snapshots.
- **Systems:** Mac resource summary/history, Docker containers grouped by Compose project, optional local integrations, and Sources/Diagnostics. Never confuse host metrics with Docker-engine metrics.

Use a dark, restrained theme, thin borders and generous whitespace. One short tab row; no large logo/header. A footer can show `Sideboard · updated 14:20 · 1 source stale · Space pause`. Only status changes or explicit interaction should attract attention. Do not require Nerd Fonts, image protocols, gradients, a particular terminal theme, or emoji for meaning.

### Responsive layout

Measure **terminal columns and rows**, not physical iPad pixels. At 110+ columns and 32+ rows, use a 2×2 Overview grid. At narrower widths use two vertically stacked slots with the same content queue; preserve pins without rendering four unreadable cards. Below 70 columns use one card at a time. At less than 60×20, show a useful compact list/status instead of clipping.

Test at 120×40, 100×32, 70×45 and 60×24. Resize must preserve selection, read state and pinned content. A Mac terminal window on the extended display is sufficient; no iPad-specific application is needed for this design.

### Overview behavior: implement as a small state machine

Default wide layout:

| Slot | Default queue | Dwell |
|---|---|---|
| A | Portfolio summary; falls back to HN if money is unconfigured | Pinned when configured |
| B | Weather; falls back to source/setup status if unconfigured | Pinned when configured |
| C | Balanced news → HN Top → YouTube → HN New | 40 seconds per card |
| D | Mac summary → Docker summary → Reddit → saved reading → Vercom events → configured local snapshot | 55 seconds per card, offset from C |

Queues are configurable. Skip empty/unconfigured optional cards; show one setup card in the whole Overview rather than repeating it. Do not show the same item in two slots simultaneously. Never rotate the user's selected tab.

**Hover pauses only the hovered card**, including card replacement, internal item ordering and displayed snapshot replacement. Fetching and caching continue. When the pointer leaves, apply the latest valid snapshot and resume the remaining dwell after a two-second grace period. A pending refresh must not move a link under the pointer. Stationary hover remains paused; no idle timer may override it.

Keyboard interaction temporarily holds the focused card for 15 seconds after explicit navigation. Initial programmatic focus must not pause a card indefinitely. A detail modal pauses all visual rotation until closed. Pinned cards do not rotate, but their data updates unless held. Keep pause reasons separate (`hover`, `interaction`, `modal`, `manual`) so releasing one cannot accidentally clear another.

The app must keep rotating while the user works in another Mac window. On a delivered terminal blur event, clear a stale pointer hold and require fresh pointer events to reacquire it; do not globally pause merely because the terminal is unfocused. Motion/focus reporting varies by terminal, so provide a mouse diagnostic and a visible reset-holds action. Never claim hover works solely because headless tests pass. Textual exposes mouse events and concurrency facilities suitable for this design [S01–S03].

| Input | Action |
|---|---|
| `1`–`9` | Select the first nine visible tabs in configured order; the default configuration has six |
| `Ctrl+P` | Command palette: switch to any tab, including tabs beyond nine; find widgets and actions |
| `Ctrl+R` | Validate and reload configuration; reject an invalid update without losing the last valid layout |
| `Tab` / `Shift+Tab`, arrows | Move focus/selection |
| `Space` | Toggle global visual pause; network refresh remains independent |
| `p` | Pin/unpin the focused Overview card |
| `n` | Explicitly advance a rotating card, including while manually paused |
| `Enter` / click | Open a stable detail view |
| `o` | Open the selected validated URL in the default browser |
| `s` | Save/unsave a reading or video item locally |
| `r` | Request a rate-limited refresh of the selected provider |
| `v` | Privacy mode: hide financial amounts and private content |
| `?`, `Esc`, `q` | Help, close/back, quit |

Text entry widgets own their keys; global bindings must not steal ordinary input. No hover action should open a link. Plain text extraction must escape terminal controls and Rich markup.

## 6. Integration specifications

### 6.1 Portfolio and Vercom: correctness before apparent liveness

Keep **positions, instrument identities, quotes, exchange rates and cash flows separate**.

A position identifies an account alias and an instrument with an ISIN where known, listing/venue, quote currency and decimal quantity. Quote providers map to that identity; a ticker string alone is not a unique asset. `VRC.WA` is only a **candidate** Yahoo mapping for Vercom until metadata and exchange/currency are checked. VGLA and VALL remain unresolved until a user-selected broker statement identifies them.

**Manual-first is the chosen product workflow.** Implement a small local add/edit form and normalized CSV imports before optional quote providers. The user may periodically enter quantities/prices or import a broker export. This is sufficient for first-release acceptance; account linking, Flex tokens, OAuth and automatic broker synchronization are not required. Preserve the last supplied values with their actual dates.

Provide an import preview, saved column/locale mappings and row-level validation. Broker-specific IBKR/Revolut adapters can be added against redacted real examples when available, without changing the holdings engine. Do not claim support for every export variant based on one invented fixture.

```csv
account,instrument_id,isin,venue,currency,quantity,cost_basis_total,cost_basis_currency,as_of
example,DEMO-EQUITY,,DEMO,EUR,10,,,2026-10-12
```

This row is a format example, not an actual instrument. Empty basis means unknown. Import identifiers/currency must be confirmed before online quotation; CSV-imported symbols must not automatically choose a different listing.

Provide a separate dated price import as well as the same fields in the manual editor:

```csv
instrument_id,currency,price,price_unit_multiplier,as_of
DEMO-EQUITY,EUR,12.34,1,2026-10-12
```

An export containing both positions and prices may populate both stores in one previewed transaction. User-supplied statement market values may be kept as a separate dated `reported_market_value` field; do not derive an apparently precise unit price when quantity or price-unit information is missing. A quote/FX-free view may show these reported values in native currency. Converted totals still require an explicit dated FX rate, supplied manually or fetched from the optional reference-rate source.

Every displayed value identifies its basis: **manual**, **broker statement**, or **market quote**. No automatic data source silently overwrites a manual override. Reconcile conflicting account-level prices explicitly. Show “updated from statement on …”; missing current prices or previous closes means no invented daily-change figure. A compact update/import action should be accessible from Money without editing database files. The manual FX importer accepts `base_currency,quote_currency,rate,as_of`; an EUR/PLN rate and a PLN/EUR rate are different directed records, validated explicitly.

**Snapshot imports replace the selected account's positions atomically**, rather than adding the same quantity again. Persist file hash, import timestamp, statement date, mapping decisions and row-level warnings. Reject older replacement snapshots unless explicitly selected as historical. Handle decimal commas/thousands separators through an explicit locale choice, not guessing. Reject a replacement with invalid rows rather than silently dropping holdings; explicitly preview empty-account replacements. Keep an import rollback path and an audit trail for local manual edits.

Use decimal arithmetic for quantities and money. Prices carry `currency`, `price_unit_multiplier`, `as_of`, `fetched_at`, source and quote type. Distinguish GBp from GBP where applicable. Use unadjusted prices for current valuation; adjusted history is a separate series and must not silently be multiplied by current quantities across corporate actions.

```text
native_value = quantity × price × price_unit_multiplier
value_eur = native_value × FX(native_currency → EUR)
```

For EUR instruments use an exact rate of one. For PLN/EUR obtain the explicit direction from Frankfurter's ECB route. Store the FX publication date independently of the quote timestamp. No live exchange-rate claim.

Display tracked portfolio value, each holding's share of the **tracked** total, native-price daily change, and optionally a clearly labelled indicative EUR valuation. Missing prices or FX produce an **incomplete valuation with a priced subtotal**, not a deceptively complete total. Show how many holdings are priced. Display last-good values with their dates; an HTTP fetch today does not turn an old quote into a today's quote.

Do not call portfolio-value change “investment return”: deposits, withdrawals and changed quantities affect it. MVP may plot dated portfolio-value snapshots and native instrument-price sparklines. Portfolio performance, money-weighted returns, cost-basis tax treatment and corporate-action accounting are out of scope until sufficient transaction history exists. Unknown basis means unknown gain; gifted shares do not automatically have zero tax basis.

Vercom's detail view should prioritize concentration, official report links and **declared/received** dividend events. Store ex-date, record date and payment date separately if available. Historical dividends supplied by the user can be imported as dated actual payments; do not invent gross amounts from a remembered tax percentage. Forecast dividends require a separate estimated label and are not an MVP feature. The “early 2028” note is a planning note, not a precise legal unlock countdown.

The optional Yahoo adapter must be isolated behind the provider interface and current upstream terms [S09]. Keep it off until the listing mapping and a small sample pass validation. Never switch providers/listings silently when blocked. IBKR Flex is an optional later automation of statements, not a reason to install TWS or enable trading [S11].

### 6.2 YouTube: uploads are easy; “most viewed by me” needs local evidence

Use Google's documented per-channel Atom URL [S06]:

```text
https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID
```

Start with explicit channel IDs/favorites. Accept a channel URL containing a channel ID; handle resolution is a setup convenience only when the public page supplies a reliable canonical ID. Unresolved handles should request a channel ID or Takeout import, not launch a scraping pipeline.

Then support user-selected **Google Takeout subscriptions CSV and watch-history JSON**, parsing local files only [S08]. Prefer an already implemented Sofalynx read-only interface later, after inspecting its actual schema with permission. Never scrape browser cookies or try to access watch-history playlists through YouTube's API; those histories are not accessible through that API [S07].

Import only the minimum relevant fields. Resolve channel IDs from records that contain them or from the imported channel mapping. Report unresolved/deleted entries and coverage. A title alone is not reliable channel identity. No network call per historical video to fill every gap. Subscription imports are snapshots, not continuous account synchronization; show their import date.

Suggested local ordering: favorites first, then decayed viewing-event count over the last 90 days, then upload recency. Store the formula in configuration; e.g. each history event contributes `2 ** (-age_days / 30)`. Call this **recent viewing frequency**, not minutes watched. Match imported history to subscription IDs by default; extra watched-but-unsubscribed channels require user selection. Label a favorites-only ranking when history is unavailable.

Deduplicate by video ID. Display title, channel, publication age and read/saved state. A feed may be incomplete and omit duration, Shorts classification or live-state details; missing fields remain unknown. Do not promise complete backfill, accurate Shorts filtering or global watched-state sync. Opening a URL in Sideboard is not proof the video was watched. Keep `opened`, `saved`, and imported `watched` events distinct.

### 6.3 Weather

Use selected latitude/longitude and an explicit IANA timezone. Offer Home/Travel labels with user-confirmed places, not automatic location tracking. Cache geocoding selections rather than looking up a city every refresh.

Fetch current temperature/apparent temperature, precipitation/weather code and wind, hourly precipitation probability/temperature for the next 24 hours, and daily min/max, precipitation and sunrise/sunset for seven days in a small request. Render only the useful subset on the Overview. Label model-derived current conditions appropriately. Display units, location and forecast validity. A forecast issued yesterday for today is different from a current observation.

Include Open-Meteo attribution in weather detail and Sources. The free service is for personal/non-commercial use and has published limits; this private dashboard should stay far below them [S04]. No weather API account is required.

### 6.4 News and the reading queue

Use publisher RSS/Atom rather than a general paid news API. Start with a small configurable source pack. Candidate feed URLs are **not live-validated** here:

| Bucket | Suggested publisher/source | Candidate or discovery route |
|---|---|---|
| Lithuania | LRT; optionally 15min as an alternative | Discover the publisher's current news RSS; do not invent a media/podcast feed as a news feed |
| Klaipėda local | Atvira Klaipėda | `https://www.atviraklaipeda.lt/feed/` — candidate |
| World | BBC World | `https://feeds.bbci.co.uk/news/world/rss.xml` — candidate |
| Europe/world alternative | DW | `https://rss.dw.com/rdf/rss-en-all` — candidate |
| Software | Laravel News | `https://laravel-news.com/feed` — candidate |
| Engineering | The Pragmatic Engineer | `https://newsletter.pragmaticengineer.com/feed` — candidate, public excerpts only |
| Developer discovery | Changelog News / Nightly | Discover the correct feed or use selected local mail; do not substitute a podcast feed unknowingly |
| Personal finance / Lithuanian reading | Lėtas Pelnas | Publisher-supported public feed where available; no paywall bypass |
| Travel-local, optional | Municipality/tourism source for the selected travel area | Find a public feed; otherwise link-only, not whole-site crawling |

Publisher selection is a recommendation, not a claim all feeds are currently offered. Verification should establish status, content type, parse success, article dates, publisher identity, language, source category and terms. Store the resolved canonical URL and a sanitized fixture. If one source has no suitable feed, document that and use another **same-category** source; do not replace Lithuanian news with more AI news.

Use source/category weights with deterministic diversity caps. For a six-item Overview digest, aim for one local, one Lithuania/Europe, one world, one business/personal-finance, one software/AI and one gadgets/infrastructure item when those buckets contain fresh content. Never exceed two AI-tagged items in a six-item digest. Leave a category visibly unavailable rather than inventing or retaining ancient news to fill a slot.

Use a source excerpt, not a fabricated summary. Keep original LT/EN/ES text, publication time and source. Full articles open in the browser. Deduplicate exact canonical article URLs across feeds; optionally cluster similar headlines conservatively, without asserting independently reported stories are identical. Read/saved/dismissed state is local. No paid model, embeddings or automatic translation is required.

### 6.5 Hacker News

Use official endpoints [S05]:

```text
https://hacker-news.firebaseio.com/v0/topstories.json
https://hacker-news.firebaseio.com/v0/newstories.json
https://hacker-news.firebaseio.com/v0/showstories.json
https://hacker-news.firebaseio.com/v0/askstories.json
https://hacker-news.firebaseio.com/v0/item/{id}.json
```

Fetch up to 30 IDs per selected list, deduplicate the union, cache items and cap concurrency. Preserve the API's Top/New ordering in dedicated views. Skip dead/deleted items and optionally jobs. Display score, comment count and age where supplied. Open the article or HN discussion explicitly; do not recursively fetch comment trees. Overview relevance filtering must be visibly different from the unmodified HN rankings.

### 6.6 Reddit

Treat Reddit as **access-dependent**, not an anonymous JSON endpoint guaranteed to work forever. Reddit documents sign-up/authentication requirements and conditional free/paid access [S12]. Inspect current applicable rules before enabling a feed or API adapter.

A candidate public RSS form is:

```text
https://www.reddit.com/r/selfhosted/top/.rss?t=day
```

Enable only if permitted and successfully validated from the Mac. Otherwise show “Reddit feed unavailable / authorization required” with selected community links. An explicitly approved free API integration may be considered later, but do not require it, promise approval, or introduce paid access. Do not evade a block with proxy rotation, alternate hosts, copied cookies, `.json` substitution or browser impersonation.

Suggested starting communities, **not inferred subscriptions**: `r/selfhosted`, `r/homelab`, `r/LocalLLaMA`, `r/PHP`, `r/eupersonalfinance`, `r/lithuania`. Let the user select a few; avoid fetching every possible interest. Rank using freshness, configured topic/source weights and metadata genuinely provided by the permitted source. RSS-only missing scores/comments must remain absent.

Store minimal metadata, not full discussions. Use a short retention window, default 48 hours; honor stricter current provider requirements and removal signals. Remove expired provider content even when offline. Locally saved bookmarks retain the user's link, not an indefinite cached copy of deleted content. No training or cloud summarization of Reddit data.

### 6.7 Local applications, systems, mail and calendar

Implement a **generic snapshot consumer before application-specific integrations**. The schema below is a proposed contract for Sideboard; it is not evidence that CapTrack or Sofalynx already supports it.

```json
{
  "schema_version": 1,
  "source_id": "example-backup",
  "generated_at": "2026-10-13T10:00:00Z",
  "expires_at": "2026-10-13T12:00:00Z",
  "privacy": "private",
  "title": "Example backup status",
  "metrics": [
    {
      "id": "last_success",
      "label": "Last successful backup",
      "value": "2026-10-13T09:45:00Z",
      "unit": "timestamp",
      "basis": "measured"
    }
  ],
  "items": []
}
```

Define and test a JSON Schema. Support text/number/timestamp metrics, optional bounded numeric histories for sparklines, and short lists with stable IDs and optional HTTP(S) links. Metric values do not contain executable markup or command strings. Producers should write a temporary file then atomically rename it. Consumers reject partial/invalid updates and retain the last good snapshot with a warning. Limit each file to 1 MiB and accept only explicitly configured regular-file paths.

For existing apps, first inspect a user-selected interface/schema. Prefer a sanitized snapshot or stable read-only endpoint. A direct SQLite integration is acceptable only with explicit permission, a known schema and correct read-only handling of a live WAL database; never assume copying just its main database file gives a consistent snapshot. Do not modify another project to create a bridge as part of the core dashboard work.

Specific candidates:

- **CapTrack:** current quota/reset when actually reported, plus separate measured local usage. Never infer subscription percentage or provider billing from tokens alone; never copy its OAuth secrets.
- **Sofalynx:** channel preferences and viewing events only. No second player, copied authorization token or unverified database-path assumption.
- **Mac and local Docker:** required feature specifications are in sections 6.8 and 6.9, not deferred snapshot-only integrations.
- **Tailscale, optional:** opt-in using the existing CLI. `tailscale status --json` is documented [S15]; a connected peer is not proof its application is healthy. Do not bring a VPN up/down or expose peer addresses in privacy mode.
- **Home lab / document-processing jobs:** consume existing allowlisted health endpoints or snapshots with service status, failed-job count and backup age. No music contracts, client names or extracted document contents on the ambient screen.
- **Solar:** manual monthly production readings until monitoring access/protocol is known. Record whether data refers to the whole park or the owned allocation. Never multiply output by an assumed ownership fraction. APVA is a dated note/change watch, not automatic legal eligibility or a deadline prediction.
- **Calendar:** start with a user-selected ICS file; secret feed URLs are an optional credential. Use tested timezone/recurrence libraries for recurring events. A native EventKit helper is a later, explicitly permission-gated option, not required for v1.
- **Mail/renewals:** no live mailbox integration in v1. Later, inspect an already configured mail application's supported automation or accept selected local exports. Scope to allowlisted folders/senders and minimal metadata. No private Mail database scraping or blanket Full Disk Access request. Match later confirmations to earlier failures; ambiguous payments/due dates remain “needs review,” never asserted overdue.

### 6.8 Mac statistics — required, lightweight and capability-aware

Implement `features/mac_system/` as an ordinary feature module, not a special branch in `app.py`. Use local `psutil` APIs with a small typed adapter so tests can supply fake counters [S20]. No account, key, root access, Full Disk Access or installed monitoring daemon is required for the intended basic collector; any unavailable field stays unavailable.

| Surface | Default contents |
|---|---|
| Overview card | CPU utilization, memory utilization/available RAM, free disk, current receive/send rate, battery if present |
| Systems detail | The same metrics with short histories; swap usage, uptime and optional per-core breakdown |
| Optional process panel | Five highest CPU/RSS processes; explicitly enable separately; names and IDs only |

The implementation should follow these measurement rules:

- **CPU:** use counter deltas with a retained baseline, or a correctly primed nonblocking sampler on a consistent worker. The first sample is “warming up,” not a measured zero. Mac total CPU has a clearly labelled host-wide 0–100% scale. Load average, if shown, is a separate metric, not CPU utilization.
- **Memory:** expose total, available and a consistently defined utilization percentage. Label the displayed basis; do not combine `used` from one formula with a percentage from another. A RAM percentage is not the macOS Activity Monitor memory-pressure classification. Swap is a separate number, not proof of current heavy swapping.
- **Disk:** read configured local volume capacity/free space; optionally read host disk-I/O counters. Do not recursively scan files or sum the same storage capacity across multiple mount points. No cleanup action.
- **Network:** derive bytes/second from successive counters and monotonic elapsed time. Select physical interfaces explicitly or use a documented local-interface heuristic with user override. Exclude loopback and avoid counting the same traffic again through virtual interfaces. Do not perform speed tests or inspect connection destinations.
- **Battery/uptime:** show charging state and battery percentage when present. Unknown time remaining and a device without a battery are different states. Do not collect hardware serial numbers or stable hardware identifiers.
- **Processes:** opt-in, preferably detail-only. Catch per-process permission errors and process exits. Do not read command lines, environment variables, file paths, open files or document/window titles. Key samples by process ID and creation time to handle PID reuse. No kill/priority controls.

GPU/ANE utilization, temperature, fans and power draw are **not promised by the basic collector**. In particular, psutil's documented temperature/fan support is not a general macOS solution [S20]. A later explicitly requested hardware-specific adapter can add supported metrics; do not install a privileged helper or invoke `sudo powermetrics` to fill missing fields.

Sampling defaults are product choices: 5 seconds while Systems is visible, 15 seconds while a Mac card participates in the Overview rotation, 60 seconds when referenced only by an inactive tab. Read slow-changing disk/battery fields at most once per minute and identity/uptime metadata less often as appropriate. One source instance serves all its widgets. Hover holds the displayed snapshot, not collection.

Keep at most 15 minutes of high-resolution samples in memory. Persist no more than one aggregate per minute per metric, retaining 24 hours by default. Metric history is bounded and pruned. Mark gaps on sleep, counter reset, interface change or long sampling intervals; do not draw them as zero load or derive a huge speed spike. Persisting histories is optional per source, not a requirement to record every poll.

### 6.9 Local Docker statistics — required adapter, read-only runtime access

Use the **existing Docker CLI** with fixed argument arrays, JSON formatting and bounded subprocesses [S21–S24]. Support the selected local Docker-compatible context rather than assuming Docker Desktop, Colima or another runtime is installed. Do not install or start a runtime. Missing CLI, missing socket, stopped daemon, access denied and no containers are distinct states.

**Scope the connection before collecting:** show available context names/endpoints during explicit setup, pin the user-selected context and use `docker --context <name> …` on every daemon call. Inspect only its endpoint metadata. The core adapter accepts an approved local Unix-socket endpoint; reject SSH/TCP remote contexts, including an inherited remote `DOCKER_HOST`. Control environment overrides so they cannot redirect collection, and revalidate an edited endpoint. Never call `docker context use` or silently follow a subsequently changed global context [S22]. A local socket is not a security sandbox; the user must approve its intended engine.

| Widget | Contents |
|---|---|
| Docker summary | Context label, running/stopped/unhealthy counts, tracked-container CPU and memory summary, sample age |
| Containers table | Name, Compose project/service, state, health when defined, CPU, memory used/limit, receive/send rates, restart count |
| Project grouping | Group the same rows by Compose project; fall back to an ungrouped section when labels are absent |
| Storage, optional | Engine-reported images/volumes/build-cache usage, fetched infrequently or on demand; no reclaim button |

Use `container ls --all` for inventory and the documented formatter/filters to select only required fields. Never persist the entire `{{json .}}` output for inventory: it can contain command strings or unwanted labels. Read only allowlisted Compose project/service labels. Obtain restarts/exit code/health status via a fixed narrow inspect template; do not retrieve full configuration, environment, mounts or health-check logs. Batch selected IDs, cap the inventory (e.g. 100 rows) and disclose truncation [S23].

Use a single bounded `container stats --no-stream --format '{{json .}}'` call for selected running IDs per refresh, not a new process for each card or container. Parse its JSON-lines output and human-unit values with tested unit conversion [S21]. Keep adapter output typed. Validate fixed templates against the installed CLI; do not parse the alignment of a human-readable table.

Metric interpretation is part of the feature, not the shared renderer:

- Preserve the CLI CPU scale without clipping multi-core readings to 100%. Label it as container/engine CPU, not Mac host CPU. Only offer an engine-capacity-normalized percentage when the actual engine CPU count is known.
- Docker memory figures and limits are engine/container measures. Do not sum individual limits as total available RAM, or add container usage to Mac usage as though they were disjoint. Preserve the CLI memory basis; a later Engine API adapter must explicitly reconcile its different cache accounting [S21].
- Net I/O and block I/O are cumulative counters; derive rates only across valid samples for the same container ID. Replacement/restart/reset or a long gap resets the baseline. Aggregate container traffic is not a measurement of external internet bandwidth.
- A running container without a healthcheck is **running / health unknown**, not verified healthy. An intentionally stopped container is not an incident. Highlight unexpected exits only for configured expected-running services, or report observed state without asserting intent.
- Restart count is lifetime count for that container. An observed increase can be labelled “since last sample”; do not invent a historical restart rate. Removed containers disappear from current totals while remaining in bounded history.

For Docker Desktop, distinguish engine storage from the Mac filesystem's actual disk consumption; its container/image storage resides in a disk-image file [S25]. Optional `docker system df` data is a separate, slow collection task (30 minutes or explicit refresh), not something to run every 15 seconds [S24]. Never automatically inspect the entire VM disk or run prune/cleanup.

Defaults: resource stats every 15 seconds in Systems/Overview, inventory and narrow metadata every 30 seconds, reduced to 60 seconds when only referenced by an inactive tab. Apply backoff when the daemon is unavailable. Limit Docker subprocess concurrency to one; give each call a timeout/output cap, terminate and reap it on cancellation, and do not leave a permanent stats stream running. Measure child-process overhead as well as Sideboard's own CPU.

Approved operations are read-only inventory/statistics/selected metadata. **No start, stop, restart, exec, logs, build, pull, prune, delete, context mutation, Docker-socket network exposure or credential copying.** Existing socket access is powerful despite these application restrictions; do not describe it as an OS-enforced read-only credential. An unhealthy container must never trigger an automatic remediation action.

## 7. Implementation architecture — extension-friendly by construction

### Stack

| Layer | Choice |
|---|---|
| Runtime | Python **3.14 as a supported baseline**, not a claim it is the newest Python; select a compatible stable patch release |
| Dependency management | `uv`, committed `uv.lock`, `pyproject.toml`, isolated environment [S18] |
| Interface | Textual; shared card/list/table/status/sparkline renderers and theme tokens |
| Network | Shared `httpx.AsyncClient`, explicit timeouts and bounded response sizes |
| Parsing | `feedparser`; Pydantic settings/payload validation; standard-library CSV/TOML/JSON where suitable |
| Local telemetry | `psutil` for supported Mac counters; existing Docker CLI through a narrow read-only adapter |
| Persistence | SQLite through `aiosqlite`; migrations, WAL mode and one serialized write path |
| Optional market data | `yfinance` in an optional dependency extra, isolated from import/manual workflows |
| Tests and quality | `pytest`, Textual Pilot, fake clock, `respx` or equivalent HTTP fixtures, Ruff and Pyright |

Resolve compatible stable package versions during bootstrap and commit the lockfile. Do not introduce prereleases just to claim “latest.” Textual is selected for its UI/event/test facilities [S01–S03, S26]. WTF is dashboard inspiration [S16]; Bubble Tea is a valid Go alternative [S17], not a reason to implement the app twice.

**Use a modular monolith, not a plugin platform:** one process, explicit feature registrations, ordinary Python protocols/dataclasses, shared infrastructure and configuration-driven composition. No service container, message broker, subprocess per widget, entry-point scanning or runtime plugin downloads. Build only contracts exercised by the initial integrations. The goal is small, predictable changes by coding agents, not maximum abstraction.

### Extension boundaries and expected change size

| Requested future change | Expected implementation |
|---|---|
| Add a feed, second city, snapshot file, category, tab or queue placement | Configuration only, using existing registered source/widget types |
| Show existing data in another standard card/table/list | Another widget instance in config; add a feature-local pure presenter only when the view needs new logic |
| Add a genuinely new data integration | One feature folder, one entry in the trusted feature registry, config and feature-local tests/fixtures |
| Add a specialized interactive visualization | Feature-local view/renderer registration and styles; preserve the shared card shell and lifecycle |
| Add a new privileged capability or persistent domain model | An explicit reviewed capability/storage change, migration if needed, and tests; do not disguise it as a routine widget |

An ordinary feature addition must not require editing the scheduler, shared storage engine, navigation keybindings, rotation state machine or a central `if widget_type == ...` chain. Reordering or creating a seventh tab must not require Python changes. These are testable requirements.

Keep the following concepts separate:

| Concept | Responsibility | Example |
|---|---|---|
| Feature | Owns a domain's configuration schemas, source/widget types, presenters and tests | `mac_system`, `docker_local`, `portfolio` |
| Source type | Defines how a collector validates configuration and produces typed data | `mac.metrics`, `docker.containers`, `weather.forecast` |
| Source instance | One configured collection/cache identity shared by consumers | `mac`, `docker`, `weather_home`, `weather_travel` |
| Widget type/instance | Maps source data into a compact/detail presentation; owns no polling timer | `docker.summary` instantiated as `docker_summary` |
| Category | Open metadata for filtering and digest/queue selection | `infrastructure`, `world`, a later `music` |
| Tab | Configured layout and widget placement | `systems`, a later `music_desk` |

Do not equate a category with a tab or a source with a widget. One source may power several widgets; a composite portfolio widget may bind holdings, quotes and FX from separate sources. Reuse data by explicit source ID. Configuration must not use arbitrary code expressions for formulas or sorting; use named, validated operations or a feature-local presenter.

### Repository structure

```text
pyproject.toml
uv.lock
README.md
AGENTS.md
plan.md
config.example.toml
src/sideboard/
  __main__.py
  cli.py
  app.py                      # composition root only
  core/
    contracts.py              # FeatureSpec, SourceSpec, WidgetSpec, typed envelopes
    registry.py               # validates/looks up specs; no feature-specific branches
    config.py                 # versioned config validation, merge, reload
    scheduler.py              # source-instance tasks and visibility-aware cadence
    storage.py                # shared cache/repository primitives; serialized writes
    history.py                # bounded rings, downsampling and retention
    capabilities.py           # approved HTTP/file/local-metric/CLI access boundaries
    subprocesses.py           # execution, timeout, output caps, cancellation/reaping
    clock.py                  # real/fake clock boundary
    errors.py
  features/
    registry.py               # explicit trusted built-in module list
    weather/
      feature.py              # exports FEATURE: FeatureSpec
      config.py
      models.py
      provider.py
      presenter.py
    mac_system/               # same pattern; optional local sampling helpers
    docker_local/             # same pattern; fixed Docker operation builders
    news/
    hackernews/
    youtube/                  # includes importers when needed
    reddit/
    portfolio/                # local editor/importers, domain repository/calculations
    fx/
    markets/                  # optional dependencies loaded only when enabled
    local_snapshot/
    diagnostics/
  ui/
    shell.py                  # constructs configured tabs/widgets
    overview.py
    rotation.py
    card.py                   # shared hold/freshness/privacy/error/min-size behavior
    palette.py                # discovers registered actions/tabs/widgets
    renderers/
      metrics.py
      items.py
      table.py
      status.py
      sparkline.py
    theme.tcss                # semantic tokens, no feature colors in core logic
  migrations/                 # shared cache/state migrations only
  defaults.toml               # shipped config with six default tabs
  schemas/
    local-snapshot.schema.json
    config.schema.json        # generated from registered schemas, not a second truth
  templates/feature/          # compiling, tested minimal extension template
    README.md
    ...
tests/
  contracts/                  # reusable tests exercised against registered features
  features/<feature_id>/      # parser/provider/presenter tests and synthetic fixtures
  ui/
  integration/
docs/
  architecture.md             # concise map of actual implemented boundaries
  adding-widget.md            # extension cookbook, updated to match actual APIs
  features.md                 # registered types, bindings, permissions and examples
  source-validation.md
```

The tree is a target, not a command to create empty files for every feature. A simple feature may keep its schemas/presenter next to `feature.py`; split only when useful. Domain code remains local. Do not force a new integration to edit a global `models.py`, one huge provider file and one huge UI file.

### Small, explicit extension contracts

Define the following API in `core/contracts.py`. Use typed protocols and validated immutable payloads; no sprawling inheritance hierarchy. The named methods/fields below are the v1 contract to implement and document, not an assertion that code already exists.

| Contract | Required responsibilities |
|---|---|
| `FeatureSpec` | Stable `id`, `api_version=1`, source type registrations, widget type registrations; optional renderer registrations, config defaults and domain migrations |
| `SourceSpec` | Namespaced `type_id`, settings model, payload model/schema version, required capabilities, default cadence/retention and a lazy factory |
| `Provider` | `async fetch(context) -> SourcePayload`; `async aclose()` for owned resources; fetch is one bounded acquisition, never its own polling loop |
| `SourcePayload` | Validated typed data, source `as_of`/precision, provenance and optional validity/coverage; the host wraps attempt/success/error metadata |
| `WidgetSpec` | Namespaced `type_id`, settings model, named input bindings and accepted payload schema IDs, pure `present(inputs, settings, view_context)` function; optional detail/action spec |
| `ViewModel` | One validated standard presentation shape: metrics, item list, table, status or time series; stable item/metric IDs, units, source timestamps and privacy labels |
| `CategoryConfig` / `TabConfig` | Open stable string IDs, labels, ordering/filtering and registered layout/widget references; no enum listing today's six tabs |

`view_context` supplies compact/detail mode, available size and local UI state; presentation must not fetch data, query arbitrary databases or start processes. It may derive values from its bound snapshots. A missing binding produces a standard incomplete/unconfigured view, not a guessed value. Keep money arithmetic/units in feature-domain functions, not a generic string formatter.

`fetch(context)` receives only its configured source identity plus injected services it needs: a clock, scoped HTTP/file reader, read-only domain repository and/or an approved local adapter. Narrow typed services are preferable to an unrestricted global application object. Shared source retries, cancellation, status and cache writes belong to the host; imports/manual editing use explicit transactional domain commands rather than pretending an external API is being polled.

A feature exports `FEATURE`. Add its module to **one repository-owned list** in `features/registry.py`; the loader builds all other registries and help/config descriptions from those specs. Configuration chooses registered IDs, never import paths. Feature manifests must be cheap and have no import-time I/O; optional heavy libraries are loaded only inside their enabled factory. A missing optional dependency disables only that feature with setup guidance. Duplicate IDs, incompatible API/schema versions or invalid bindings produce named validation errors.

The feature registry is trusted application code, **not a sandbox** for untrusted modules. Do not load packages from feeds, uploaded snapshots, arbitrary user paths or a marketplace. New capabilities require a deliberate code review. Ordinary configuration can only narrow approved scopes, never turn a read-only Docker integration into arbitrary command execution.

Standard renderers use the shared `Card` shell. That shell implements loading/stale/blocked/error states, hover/keyboard holds, privacy masking, resize, pinning and detail navigation. A feature must not reimplement those behaviors. A rare custom renderer is registered with its own typed view model and feature-local styles; the shell still controls lifecycle. Actions are named application intents such as refresh, save, show detail, import or open a validated link, not shell commands embedded in remote data.

### Data flow, isolation and persistence

```text
feature registry + validated config
    → source instances → bounded collectors → validated snapshots/shared cache
                                            → pure feature presenters
                                            → shared renderers/card shell
                                            → configured tabs/rotation queues
```

Own collection tasks at application scope. One source instance serves all cards/tabs using it; mounting a second widget never doubles requests. Source instance IDs, not feature names alone, key caches/tasks so two weather locations cannot overwrite each other. Treat derived-widget bindings as a data dependency map, not polling of another provider from inside a presenter.

The host envelope includes `source_id`, payload schema/version, `generation`, `last_attempt_at`, `last_success_at`, `fetched_at`, source `as_of`/precision, `valid_until`, content hash, provenance and bounded error information. Connectivity/access (`ok`, `offline`, `error`, `blocked`, `unconfigured`, `unavailable`) is independent of freshness (`fresh`, `stale`, `expired`, `none`). UI “held” is a third dimension. One field can be unsupported while the source succeeds with partial coverage.

Providers raise typed failures; the host preserves the last good payload. An exception in one source/presenter/renderer shows a contained error card and diagnostic, never crashes the entire dashboard. Do not suppress developer tracebacks from sanitized debug logs, but never store raw private payloads with them. Every feature gets the same demo, offline, disabled and freshness rules.

Use migration-managed tables for source state/snapshots, reading items/state, imports, instruments/positions, quotes/FX and dated events. Generic snapshot and metric-history tables support new ordinary integrations without schema changes. A feature needing durable domain tables owns namespaced migrations/repository functions, registered centrally; UI code never issues SQL. Database migrations and source-payload schema versions are separate. Unknown cached payload versions must not be interpreted with the wrong model.

Store money losslessly and UTC timestamps internally; retain source date-only precision. Bound history per source; do not persist high-frequency metrics on every repaint. Private data locations: `~/.config/sideboard/config.toml`, `~/Library/Application Support/Sideboard/sideboard.sqlite3`, `~/Library/Logs/Sideboard/`, with explicit overrides, owner-only permissions and Git exclusions. Source terms control cache retention. A provider-specific purge removes its cached data without deleting unrelated saved items.

### Scheduling and lifecycle

- Load cache before fetching. Keep network, parsers and blocking local calls off the UI loop [S02]. Inject a clock and I/O boundaries so providers and UI can be tested independently.
- One in-flight acquisition per source instance; global HTTP concurrency 6, with per-host limits. HN's bounded item fetches share that global budget. Docker subprocess concurrency is one.
- Network defaults: connect 5 seconds, read 10 seconds, total deadline 20 seconds, with output/decompression/parser caps. Local operations have narrower per-operation deadlines.
- Add jitter; honor Retry-After, ETag and Last-Modified. One transient retry per refresh, then backoff/circuit state. A 401/403 is not a reason for access evasion or an endless retry.
- A 304 may validate a representation, never advance the original quote/event date. Failed parsing cannot replace valid data with an empty success.
- Source specs supply active/ambient/background cadence or an import-driven update policy for local holdings/prices/rates. The host derives demand from configured consumers and active views; widgets never start timers. Hover holds rendering only. A disabled/unreferenced source owns no recurring task.
- Sleep/wake or a long sampling gap triggers a staggered catch-up and reset of rate baselines, not replay of missed ticks. Rotation uses monotonic time. Keep UTC data timestamps and presentation timezones separate.
- Config removal/reconfiguration cancels and closes only affected source instances. Increment their generation and discard late results from old tasks. Shutdown drains writes, closes clients, terminates/reaps owned subprocesses and restores terminal state.
- Blocking libraries require bounded workers and actual underlying I/O timeouts. Cancelling an await does not terminate a blocked thread; do not accumulate abandoned requests or create a new sampler thread every poll.

### Configuration is the composition layer

Use **one canonical versioned TOML schema**, not separate ad-hoc configuration systems in each feature. Merge ID-indexed tables (sources, widgets and categories) by stable ID; replace ordered lists (tabs, widget placement and queues) atomically rather than concatenating them. `init` emits a complete editable configuration, so adding a tab means appending to that existing tabs array. Validate references, duplicate IDs, capabilities, source settings and payload bindings before starting tasks. Disabled known integrations can omit credentials/paths, but no disabled source may perform I/O.

The following is a schema/example to implement. It uses disabled candidate/private sources where setup is still required; it is not populated personal data. Existing news, portfolio, YouTube and Reddit options from section 6 belong in their registered source/widget settings.

```toml
config_version = 1

[app]
timezone = "Europe/Madrid" # editable display preference, not geolocation
base_currency = "EUR"
privacy_mode = false

[categories.infrastructure]
label = "Infrastructure"
order = 60

[categories.world]
label = "World"
order = 30

[categories.finance]
label = "Finance"
order = 40

[sources.mac]
type = "mac.metrics"
enabled = true
[sources.mac.settings]
active_seconds = 5
ambient_seconds = 15
background_seconds = 60
volumes = ["/"]
network_interfaces = [] # documented auto-selection; never sum every interface
include_processes = false

[sources.docker]
type = "docker.containers"
enabled = false # init may enable after selecting/approving a local context
[sources.docker.settings]
context = "" # no guessed default and no remote context
local_only = true
stats_seconds = 15
metadata_seconds = 30
background_seconds = 60
include_projects = [] # all in approved local engine; configurable restriction
expected_running = []
include_storage = false

[sources.hn]
type = "hackernews.stories"
enabled = true
[sources.hn.settings]
lists = ["top", "new"]
items_per_list = 30
refresh_seconds = 300

[sources.news]
type = "news.feeds"
enabled = false # enable with at least one validated feed
[sources.news.settings]
refresh_seconds = 1200
feeds = [] # entries have id, url, category, language and enabled

[sources.weather_home]
type = "weather.forecast"
enabled = false
[sources.weather_home.settings]
refresh_seconds = 1200
# Set selected coordinates and location timezone during setup.

[sources.holdings]
type = "portfolio.holdings"
enabled = true # valid empty state until manual entry/import
[sources.holdings.settings]
mode = "manual_import"

[sources.prices]
type = "portfolio.prices"
enabled = true
[sources.prices.settings]
mode = "manual_import"

[sources.fx]
type = "fx.rates"
enabled = true # local manual/imported rates require no network
[sources.fx.settings]
mode = "manual_import" # optional "reference" mode fetches ECB rates
provider = "frankfurter-ecb"
refresh_seconds = 43200

[sources.youtube]
type = "youtube.uploads"
enabled = false
[sources.youtube.settings]
channel_ids = []
favorite_channel_ids = []
active_channel_limit = 12
refresh_seconds = 1800

[sources.reddit]
type = "reddit.threads"
enabled = false
[sources.reddit.settings]
mode = "links"
communities = ["selfhosted", "homelab", "PHP"]
refresh_seconds = 3600

[widgets.mac_summary]
type = "mac.summary"
sources = { primary = "mac" }
category = "infrastructure"

[widgets.mac_details]
type = "mac.details"
sources = { primary = "mac" }
category = "infrastructure"

[widgets.docker_summary]
type = "docker.summary"
sources = { primary = "docker" }
category = "infrastructure"

[widgets.docker_containers]
type = "docker.table"
sources = { primary = "docker" }
category = "infrastructure"
[widgets.docker_containers.settings]
group_by = "compose_project"
sort_by = "memory_bytes"

[widgets.portfolio]
type = "portfolio.summary"
sources = { positions = "holdings", prices = "prices", fx = "fx" }
category = "finance"

[widgets.weather]
type = "weather.summary"
sources = { primary = "weather_home" }

[widgets.news]
type = "news.digest"
sources = { primary = "news" }
category = "world"
[widgets.news.settings]
max_ai_items_per_digest = 2
# Ship the full category diversity rules from section 6.4 in defaults.toml.

[widgets.hn_top]
type = "hackernews.list"
sources = { primary = "hn" }
[widgets.hn_top.settings]
list = "top"

[widgets.hn_new]
type = "hackernews.list"
sources = { primary = "hn" }
[widgets.hn_new.settings]
list = "new"

[widgets.youtube]
type = "youtube.uploads"
sources = { primary = "youtube" }

[widgets.reddit]
type = "reddit.list"
sources = { primary = "reddit" }

[widgets.saved]
type = "news.saved"
sources = {} # local UI-owned saved-item state, no network collector

[widgets.sources]
type = "diagnostics.sources"
sources = {} # host-owned runtime status, no collector with ambient privileges

[[tabs]]
id = "overview"
label = "Overview"
layout = "rotating_grid"

[[tabs]]
id = "money"
label = "Money"
layout = "detail"
widgets = ["portfolio"]

[[tabs]]
id = "read"
label = "Read"
layout = "filterable"
widgets = ["news", "hn_top", "hn_new", "reddit", "saved"]
category_filter = "all_registered"

[[tabs]]
id = "watch"
label = "Watch"
layout = "detail"
widgets = ["youtube"]

[[tabs]]
id = "life"
label = "Life"
layout = "grid"
widgets = ["weather"]

[[tabs]]
id = "systems"
label = "Systems"
layout = "stack"
widgets = ["mac_details", "docker_containers", "sources"]

[overview]
rotation_enabled = true
hover_pause = true
keyboard_hold_seconds = 15
resume_grace_seconds = 2

[overview.slots.a]
widgets = ["portfolio", "hn_top"] # first available; fallback when money empty
pinned = true

[overview.slots.b]
widgets = ["weather", "sources"]
pinned = true

[overview.slots.c]
widgets = ["news", "hn_top", "youtube", "hn_new"]
dwell_seconds = 40

[overview.slots.d]
widgets = ["mac_summary", "docker_summary", "reddit", "saved"]
dwell_seconds = 55
```

The full shipped defaults also declare the remaining news categories, data policies and optional Vercom/local-snapshot placements described earlier. Register `local_snapshot.file` as the generic source type and `local_snapshot.metrics` / `local_snapshot.items` as its widget types. This route can display a new producer using only its path and a standard metrics/list widget. A different weather location is another `weather.forecast` source instance, not another Python class.

Defaults must include local editors/import actions for manual holdings/prices/rates. `fx.rates` exposes manual/imported rates in `manual_import` mode, with an optional network updater in `reference` mode. Import-driven source instances update on successful local transactions rather than repeatedly polling a broker. Optional network failures do not hide existing manual rates, and visiting a tab must never advance their source dates. Portfolio presentation has partial-binding rules: EUR-native/manual valuations remain useful when no external FX or quote source exists. Disabled external sources do not block startup.

Provide `config validate` and an in-app **Reload configuration** action. Parse and validate a candidate before applying changes; invalid updates preserve the previous running configuration and show a field-level error. Reconcile only changed instances. Preserve selection/holds by stable IDs where those items still exist; deliberately removed widgets close or show an explicit removed state. Theme, tab/category changes, queue order and dwell updates need no restart. New Python feature code requires a normal app restart; Python hot reload is not required.

### Agent-facing extension workflow and proof

Keep root `AGENTS.md` short; point agents to the relevant feature and `docs/adding-widget.md`. `docs/features.md` maps registered IDs to modules, input schemas, capabilities and fixtures. Every implemented feature has a small working configuration example, synthetic valid/error fixtures and a presenter test at compact size. Shared contracts, not copied boilerplate, provide freshness/privacy/rotation behavior.

Implement a **compiling feature template** in `templates/feature/` and test a generated temporary copy in CI. It contains a typed settings model, immutable payload model, one provider, one pure presenter, `FEATURE` registration example and offline tests. No incomplete production feature is enabled by default. Add a scaffold command only if it meaningfully simplifies copying; a second code-generation framework is unnecessary.

Required extension smoke test: register the template feature in a test registry, configure two widgets sharing its source and a seventh tab/new category, run the app headlessly and verify correct rendering with **one acquisition per source interval**. Remove it through config reload and verify no polling/task leak; other tabs continue working. Exercise invalid schema/bindings and missing optional dependencies. Ordinary registry/config additions must not require core source edits to make this test work.

A future agent's change is complete when its local tests and shared contract suite pass, compact/detail/privacy/offline states work, source setup/limits are documented, and the diff stays within the intended feature/registry/config/test files. A justified new core capability or renderer is allowed, but must be documented and independently tested rather than spreading integration-specific conditionals.

## 8. Privacy and safe handling

Treat every feed, article, email export, history file and snapshot as **untrusted data**, not instructions. Never execute embedded commands, follow tool-use requests inside content, render terminal escape sequences, or interpolate values into a shell.

Use fixed subprocess argument arrays, timeouts and output limits. Do not run arbitrary configured shell commands as a generic widget system. Repository feature modules are trusted code reviewed by the user/agent, not sandboxed plugins. Configuration may choose registered types and validated settings, never Python import paths, `eval` expressions or shell templates. Docker uses its narrow approved operations from section 6.9. Open only validated HTTP(S) URLs through the system browser after explicit selection. Public feeds must not redirect into private networks or local files; private/local service providers use a separate explicit host allowlist. Do not automatically fetch enclosure files, article HTML, remote images or attachment URLs.

Default JSON/XML/feed payload cap: 2 MiB after decompression. Use safe XML handling with no external entity resolution. Import selected Takeout files through a streaming parser for large histories; do not blindly extract archives or traverse all of Downloads. Bound line/string sizes and report skipped invalid records. Keep raw personal imports outside fixtures and do not retain a second copy unnecessarily.

Secrets, if later enabled, belong in macOS Keychain through an appropriate library, not config, subprocess command lines, diagnostic exports or logs. In particular redact IBKR Flex tokens from URL query strings. Never dump another application's auth files or container environment.

Logs contain provider status, timings and redacted error categories, not mail bodies, full URLs containing secrets, financial balances, private channel-history records or contract data. Rotate and cap logs. Privacy mode suppresses sensitive titles and amounts in every tab/detail, not just Overview. Fixture/demo screenshots must use synthetic data. Process names, container/project names and private service identifiers are sensitive too; mask them in privacy mode and default diagnostic exports.

## 9. Delivery sequence and acceptance gates

### Phase 0 — prove the environment and source assumptions

Bootstrap the project/lockfile, inspect the chosen terminal's capabilities and implement a minimal mouse/resize diagnostic. Test hover, stationary hover, pointer leaving the terminal, and the terminal becoming unfocused while another Mac window is active. Keep a keyboard-only fallback and reset-holds control.

Make a small explicit live probe for weather, HN, one world feed, one Lithuanian/local feed and a selected YouTube channel. Record results, access/terms notes and fixtures without leaking personal content. Verify quote metadata only for instruments actually identified from setup. Inspect local application paths only when selected by the user. Do not scan the home directory looking for credentials.

**Gate:** a working terminal shell, successful deterministic tests, and a source-validation record distinguishing documented, live-tested, unconfigured and blocked sources. A blocked optional source must not stall the build. Establish the versioned feature/source/widget contracts and a tiny synthetic registered feature before adding product integrations; do not postpone extensibility until all widgets are hard-coded.

### Phase 1 — useful ambient dashboard

Implement the configuration-driven shell with six default tabs, shared scheduler/cache, four-slot rotation and compact layouts. Add weather, generic news feeds, HN, saved/read state, source health, generic local snapshots and Mac statistics. Mac, news and weather must use the same feature registration path; do not reserve it for future additions. Provide separate `--demo` and `--offline` modes with persistent visible labels. Demo uses a separate database and synthetic collectors only; offline suppresses public network access but can collect enabled Mac metrics and the approved local Docker socket.

**Gate:** populated real-source cards where configured, correctly labelled offline fallback, genuine hover/keyboard pause and stable selection during refresh. Mac metric deltas, gaps and field-level unavailable states work without root access. At least a world and Lithuanian/local news source must be live-validated when accessible; missing category coverage must be documented rather than concealed. A synthetic extra category/tab and a second widget using an existing source must require config only, not core changes.

### Phase 2 — complete the requested first-release integrations

Add manual holdings/prices entry, normalized position/price imports, the Money tab, explicit instrument mapping, optional Yahoo quotes, manual/ECB FX, Vercom links/events, YouTube favorites/channel feeds and Takeout imports. Complete the local Docker collector, Compose grouping, summary/table views and failure states. Implement the Reddit adapter only where access is permitted; otherwise deliver the explicit blocked/link-only experience and record the limitation.

**Gate:** synthetic import tests pass, the same import twice does not double holdings, unresolved ETF identities never acquire guessed quotes, missing quotes never become zero, and YouTube ordering is honestly labelled favorites-only or history-derived. Validate personal data only when supplied. The app remains usable with zero keys. Docker context isolation, no-mutation command policy, missing-runtime behavior and metric-unit tests pass. A functioning manual portfolio view is sufficient without any brokerage integration. Deliver `docs/adding-widget.md`, the feature template, feature index and extension smoke test described in section 7.

### Phase 3 — select extras, do not implement the whole wishlist automatically

Recommended first pair: an existing CapTrack snapshot and one backup/service-health snapshot. Then consider existing `gh` integration and release radar. Solar monitoring, IBKR Flex, mailbox automation and native calendar access are separate opt-in work, with their own source/schema/permission checks.

**Gate:** each extra adds a useful read-only signal without new mandatory credentials or changes to another project. Unavailable local interfaces produce clear setup instructions, not fictitious integrations.

### Required tests

| Area | Essential cases |
|---|---|
| Rotation | Fake-clock dwell; independent slots; overlapping pause reasons; stationary hover; resume grace; initial focus; manual advance; pinned refresh; modal close; blur handling |
| UI | All target sizes, resize while reading, long LT/ES titles, stable selected IDs, keyboard-only operation, privacy mode, theme/character fallback |
| Network | Timeout, 429/Retry-After, 401/403, invalid XML/JSON, oversized decompressed response, 304, stale payload, recovery, offline boot, duplicate in-flight prevention |
| Money | Fractional quantities, PLN/EUR direction, GBp scale, date-only quotes, unknown basis, missing FX/quote, incomplete totals, duplicate/older imports, wrong-listing rejection |
| YouTube | Different export variants, missing channel IDs, repeated imports, unresolved records, deleted videos, favorites-only ranking, opened versus watched separation |
| Content | Cross-feed URL dedup, stable read state, deleted/expired Reddit data, unavailable news bucket, AI cap, absent score/duration fields |
| Local data | Missing/unreadable path, partial atomic update, schema mismatch, expired snapshot, secret/markup redaction, producer stopping |
| Mac | Warm-up sample, valid CPU/I/O deltas, counter reset, sleep gap, interface change, unavailable sensor, no battery, no process permission, PID reuse, bounded history |
| Docker | CLI/socket absent, daemon stopped, remote context/env override rejected, timeout/cancellation, empty inventory, no healthcheck, deliberate stop, recreated IDs, CPU >100, byte units, counter reset, no secret fields or mutating commands |
| Extensibility | Synthetic feature registration, additional tab/category, two widgets sharing one collection task, multiple configured instances of one source type, optional dependency absent, duplicate IDs, invalid binding/schema, config rollback, removal cleanup |

Default tests use fixtures, not the public internet or the developer's private files. Live smoke tests are explicit, small, non-mutating and separate. Use Textual Pilot for headless interaction tests [S03], followed by an actual terminal smoke test on the Mac. Report which parts were truly tested.

### Runtime targets and commands to provide

These are acceptance targets to measure, not asserted performance: warm first paint under one second on the target Mac; responsive keyboard input during slow providers; settled idle CPU around or below 1% of one core; memory preferably below 200 MiB without optional heavy providers. Measure these targets with the configured Mac/Docker cadence and include subprocess overhead; do not achieve them by silently omitting the requested collectors. Record a short idle/refresh measurement and address avoidable redraw loops. Do not keep the Mac awake with `caffeinate` by default.

The delivered application should provide these commands and document what each touches:

```sh
uv sync --locked
uv run sideboard init
uv run sideboard
uv run sideboard --demo
uv run sideboard --offline
uv run sideboard doctor
uv run sideboard doctor --mouse
uv run sideboard doctor --network
uv run sideboard doctor --mac
uv run sideboard doctor --docker
uv run sideboard config validate
uv run sideboard features list
uv run sideboard import holdings /path/to/positions.csv
uv run sideboard import prices /path/to/prices.csv
uv run sideboard import fx /path/to/rates.csv
uv run sideboard import youtube /path/to/selected-takeout-directory
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

These are implementation requirements, **not commands that already work because this plan exists**. Default `doctor` performs only local/config checks; `--network` performs explicit bounded public probes; `--mac` tests approved local counters; `--docker` probes only an already approved local context. `features list` lists registered source/widget types, supported settings and availability without fetching data. Configuration reload is an in-app action, not an implied IPC service. Import commands must preview mappings/account replacement and allow cancellation before committing. Document optional market dependencies separately, such as `uv sync --locked --extra markets`, only once the extra is implemented.

**Definition of done:** working install/launch flow, all core surfaces implemented, real sources validated where available, credentials and personal mappings never guessed, blocked dependencies documented, tests and Mac smoke results recorded, and a README with setup, controls, limits and recovery. A demo screenshot alone is not completion. Extension contract tests and a successfully added synthetic feature are required, not just documentation promising modularity. Remaining optional work belongs in a clearly separated backlog.

## 10. Deferred by design

No automatic trading or tax engine, brokerage login scraping, paid/live exchange feed promise, universal YouTube-history synchronization, Reddit access evasion, cloud AI summaries, automatic newsletter subscription management, whole-home monitoring, mobile companion, web server, public deployment, arbitrary plugin marketplace, daemon/autostart installer or changes to other repositories. Price-bot and Spanish/habit widgets are explicitly excluded by the user. No privileged hardware probing or Docker lifecycle controls. Repository-local feature modules and configuration-based new tabs/categories are required; an external plugin marketplace is not.

Autostart, richer calendar/mail access and a single-binary package can be evaluated after the ordinary local app is useful. The user should not need to create five credentials just to find out whether they like the dashboard.

## 11. Sources and evidence register

Public documentation was reviewed for the plan; exact source response schemas, installed versions and candidate feed availability still need implementation-time validation. Source references below describe documented capabilities, not a guarantee of uptime or approval. Personal context in section 2 comes from the user's conversations and the scoped email review on 13 October 2026; no raw messages or private identifiers are included here.

- **S01 — Textual mouse events:** `https://textual.textualize.io/events/mouse_move/`
- **S02 — Textual workers/concurrency:** `https://textual.textualize.io/guide/workers/`
- **S03 — Textual testing/Pilot:** `https://textual.textualize.io/guide/testing/`
- **S04 — Open-Meteo service/terms:** `https://open-meteo.com/` and `https://open-meteo.com/en/terms`
- **S05 — Official Hacker News API:** `https://github.com/HackerNews/API`
- **S06 — Google-documented YouTube channel feed URL:** `https://developers.google.com/youtube/v3/guides/push_notifications`
- **S07 — YouTube API history, including inaccessible watch history:** `https://developers.google.com/youtube/v3/revision_history`
- **S08 — Google data export / Takeout:** `https://support.google.com/accounts/answer/3024190`
- **S09 — yfinance capabilities and upstream-data disclaimer:** `https://ranaroussi.github.io/yfinance/`
- **S10 — Frankfurter v2 and provider-scoped FX:** `https://frankfurter.dev/`; proposed PLN/EUR route: `https://api.frankfurter.dev/v2/providers/ecb/rate/pln/eur`
- **S11 — IBKR Flex Web Service:** `https://www.ibkrguides.com/clientportal/performanceandstatements/flex-web-service.htm`
- **S12 — Reddit access/authentication/free-versus-paid rules:** `https://support.reddithelp.com/hc/en-us/articles/14945211791892-Developer-Platform-Accessing-Reddit-Data`
- **S13 — Vercom official investor relations:** `https://vercom.pl/en/investor-relations/`
- **S14 — GitHub REST rate limits:** `https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api`
- **S15 — Tailscale CLI reference:** `https://tailscale.com/docs/reference/tailscale-cli`
- **S16 — WTF existing terminal dashboard:** `https://wtfutil.com/`
- **S17 — Bubble Tea alternative:** `https://github.com/charmbracelet/bubbletea`
- **S18 — uv project workflow:** `https://docs.astral.sh/uv/guides/projects/`
- **S19 — Publisher discovery starting points, not verified RSS contracts:** `https://www.lrt.lt/`, `https://www.atviraklaipeda.lt/`, `https://laravel-news.com/`, `https://changelog.com/news`

- **S20 — psutil local metrics and platform limitations:** `https://psutil.io/api/` (CPU, memory, disk/network counters, battery, processes and sensor availability). Verify the documentation against the stable version selected in `uv.lock`; development documentation may describe unreleased behavior.
- **S21 — Docker resource statistics and CLI/API memory semantics:** `https://docs.docker.com/reference/cli/docker/container/stats/`
- **S22 — Docker contexts and environment/CLI overrides:** `https://docs.docker.com/engine/manage-resources/contexts/`
- **S23 — Docker container inventory, filters and format fields:** `https://docs.docker.com/reference/cli/docker/container/ls/`
- **S24 — Docker engine disk usage:** `https://docs.docker.com/reference/cli/docker/system/df/`
- **S25 — Docker Desktop for Mac storage boundary:** `https://docs.docker.com/desktop/troubleshoot-and-support/faqs/macfaqs/`
- **S26 — Textual configurable/dynamic tab panes:** `https://textual.textualize.io/widgets/tabbed_content/`
