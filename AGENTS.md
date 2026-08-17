# AGENTS.md — working on this codebase

Orientation for an AI assistant or a new developer. Read this before changing
anything; several decisions here look arbitrary and are not.

## What this is

A local Windows-first Python app that tracks SEC self-regulatory organization
(SRO) rule filings — the `SR-<code>-<year>-<seq>` 19b-4 proposed rule changes —
across 29 US national securities exchanges plus FINRA. It scrapes, normalizes,
stores, displays, exports to Excel, and emails a weekly comparison report.

Runs on one machine. Loopback-only web UI, SQLite storage, no service to deploy.

```
sro-tracker doctor      # verify config + connectivity FIRST
sro-tracker refresh     # fetch and commit (~4 min for full scope)
sro-tracker serve --open
sro-tracker weekly      # refresh + report + workbook + deliver
```

## Invariants — do not break these

These are the load-bearing decisions. Each exists because the obvious
alternative fails in production.

1. **One parser for all SROs.** `sources/sec_sro.py` parses one table layout
   that sec.gov serves for every SRO. Do **not** add per-exchange SEC parsers.
   The whole reliability argument is that breakage is loud and immediate rather
   than rotting silently across twenty scrapers.

2. **Columns are located by header text, never by position.** If the SEC adds a
   column, extraction must keep working. See `_HEADER_MAP`.

3. **A missing table raises `LayoutChanged`; it never returns empty.** Silent
   emptiness is the failure mode this project is engineered against.

4. **Attribution comes from the filing number, not the URL.** The SRO is
   resolved via `registry.by_code()` from the code inside `SR-<code>-...`.
   Cboe's options feed paths ignore the market segment in their own URLs, so
   trusting an endpoint yields confidently mislabelled data.

5. **Edge sources can never fail a run.** Tier `edge` failures degrade; only the
   tier `spine` (SEC) can reject a run. See `sources/__init__.py:run_source`.

6. **A run must earn its commit.** `quality.py` rejects partial or implausible
   harvests. A stale-but-correct dataset beats a fresh-but-wrong one. Do not
   loosen `max_shrink_ratio` to make a failing run pass — investigate instead.

7. **`filing_history` is append-only.** Never rewrite it. It is how "when did
   this become Approved?" stays answerable.

8. **TLS verification is never disabled.** No `verify=False`, ever. A cert
   failure raises `TlsTrustError` with remediation. Corporate proxies are
   handled by trusting their root via `ca_bundle`.

9. **Report periods are measured by filing date, not scrape time.** The change
   log records when *we* learned something, so keying comparison off it makes
   week-over-week meaningless after a backfill — everything lands in "this
   week". Status transitions are the deliberate exception.

10. **The weekly email must render in Outlook**, which uses the Word engine.
    Nested tables, inline styles, `bgcolor` attributes. No flexbox, grid,
    float, `border-radius`, or `<style>` blocks. A test enforces this.

11. **Dependencies stay pure Python** (`requests`, `beautifulsoup4`, `Flask`,
    `openpyxl`). Target machines are locked down with no compiler. A test fails
    if the dependency set changes.

12. **No identity in source.** Contact address, recipients, proxies, CA paths
    all come from config. Never hardcode.

## Where to change what

| Task | File | Effort |
|---|---|---|
| Email recipients, transport, SMTP | `config.toml` | config only |
| Which SROs / years are tracked | `config.toml` (`sros`, `years`) | config only |
| Quality thresholds | `config.toml` | config only |
| Corporate CA bundle / proxy | `config.toml` or env | config only |
| Add or edit an SRO | `registry.py` → `SROS` | one line |
| Add an exchange freshness feed | `sources/exchange/__init__.py` → `FEEDS` | one line + fixture |
| Change the record shape | `models.py` → `Filing`, `COLUMNS` | touches everything |
| Excel layout | `exports/__init__.py` | isolated |
| Email layout | `report.py` | isolated |
| Dashboard | `web/templates/`, `web/static/styles.css` | isolated |

Configuration precedence: **environment > `config.toml` > defaults.** Every
setting has an env var `SRO_TRACKER_<NAME>`.

## Common tasks

### Change who gets the weekly email

Edit `config.toml` only:

```toml
[mail]
transport = "outlook"          # file | smtp | outlook
mail_to = ["a@example.com", "b@example.com"]
mail_from = "tracker@example.com"
```

Then `sro-tracker report --days 7` (preview, writes a file) before
`sro-tracker weekly` (delivers). Sending is opt-in at three levels: transport
set, recipients set, and `--send`/not `--no-send`. Preserve that.

### Add an SRO

One entry in `registry.SROS`. Find its `sec_org_id` on the SEC index page
(`SEC_EXCHANGES_PATH`) — the numeric `sro_organization` query parameter. Set
`code` to the token that appears inside its filing numbers, and `website` to a
URL you have **actually verified resolves**. No parser changes.

### Add an exchange freshness feed

Add `"<registry-key>": "<feed url>"` to `sources/exchange/FEEDS`. Before you do:
confirm the feed returns **that market's** filings, not another's. Then capture
a fixture (`scripts/capture_fixtures.py`) and add a parser test. An unverified
feed is worse than no feed.

### Change the schedule

```powershell
.\scripts\install_schedule.ps1 -Time 07:30 -Day Monday -Contact "you@example.com"
.\scripts\install_schedule.ps1 -Remove
```

A scheduled task does not inherit your shell environment, which is why the
contact is captured at registration.

## Tests

```bash
.venv\Scripts\python -m pytest          # 102 tests, fully offline
.venv\Scripts\python -m pytest -m network  # opt-in: SRO websites resolve
```

Parser tests run against committed fixtures in `tests/fixtures/`. When an
upstream site changes, `python scripts/capture_fixtures.py` re-captures them —
then **review the diff**. Updating a fixture should be a deliberate
acknowledgement that the site changed, never a reflex to make tests green.

`pytest` puts `src/` on the path, so it tests the working tree rather than what
was committed. `tests/test_packaging.py` closes that gap; it exists because an
unanchored `.gitignore` pattern once excluded a whole package while all tests
still passed.

## Traps already hit — do not reintroduce

- **`&amp;page=N`** — SEC pager hrefs are HTML-escaped. Anchoring a regex on
  `[?&]page=` matches nothing and silently collapses every SRO to one page,
  losing ~75% of the data. Pagination is now self-terminating and does not
  depend on pager markup at all.
- **`filing_year` from the date** — the SEC's year filter keys on *release*
  date, so `SR-NYSE-2025-43` can be released in 2026. The year comes from the
  filing number.
- **`nyse` is both an SRO key and a family label.** A bare name means the
  specific SRO; `family:NYSE` means all six markets.
- **Empty is not always broken.** A small SRO genuinely files nothing some
  years. Only sources that verify structure set `empty_is_valid = True`.
- **`\bsuspend` never matches "Suspension"** — every real SEC title uses the
  noun form.
- **`position: sticky` inside `overflow-x: auto`** — the wrapper becomes a
  scrollport, so a sticky `<th>` pins to it and paints over the first row.
- **Unanchored `.gitignore` patterns** match at any depth. Runtime paths are
  `/data/`, `/exports/`, `/logs/` with a leading slash.
- **SEC returns 403 to a spoofed browser User-Agent** but 200 with a declared
  contact UA. If a probe 403s, check the UA before concluding a link is dead.

## Style

Match the surrounding code. Comments explain *why*, not *what*, and are used
where a decision would otherwise look arbitrary. Dataclasses over dicts for
anything with a shape. No ORM — the schema is small and explicit. Meaningful
CLI exit codes: `0` ok, `1` failed, `2` warnings, `3` config invalid.
