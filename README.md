# SRO Filing Tracker

Tracks SEC self-regulatory organization (SRO) rule filings — the `SR-*-YYYY-NN`
19b-4 proposed rule changes — across every US national securities exchange plus
FINRA. It scrapes, normalizes, stores, displays, exports, and reports on them.

Runs entirely on one machine. Loopback-only web UI, SQLite storage, no service
to deploy, no account to create.

```
sro-tracker refresh     # fetch and commit
sro-tracker serve --open  # local dashboard
```

---

## Why it is built this way

Trackers like this usually rot for one reason: they make N fragile scrapers
load-bearing. Twenty exchange websites means twenty redesigns waiting to happen,
and each one fails silently — the page still returns HTTP 200, the parser just
matches nothing, and a thin result quietly overwrites good data.

This design removes that failure mode.

### Two tiers, reconciled on the filing number

**Tier 1 — the SEC spine.** sec.gov publishes every SRO's rule filings in one
table with one layout, addressed per SRO by a stable numeric id. That means
**one parser, exercised by all 29 SROs**. A break is loud and immediate rather
than a slow rot across twenty bespoke scrapers. This tier is authoritative and
complete; it alone can never leave you empty-handed.

**Tier 2 — exchange edge feeds.** Exchanges publish their own filings days
before the SEC release appears. During development the Cboe BZX feed carried
`SR-CboeBZX-2026-066` while the SEC listing still topped out at `-065`. These
sources add freshness and **cannot break a run** — a dead feed marks that source
degraded and the pipeline continues on the spine.

The two tiers join on `SR-<code>-<year>-<seq>`, which both publish. Every record
carries `first_seen`, `last_seen` and `seen_by`, so you can always answer *who
told us, and when*.

### Trust the filing number, not the URL

Attribution always comes from the code inside the filing number, resolved
through the registry — never from which endpoint the record arrived on. This is
not theoretical: Cboe's options feed paths (`/cone/`, `/ctwo/`, `/c2/`) ignore
the market segment in the URL and serve whatever they please. Anyone trusting
the endpoint gets confidently mislabelled data. Resolving from the filing number
makes a mis-pointed feed harmless.

### A run must earn its commit

The quality gate refuses to write when:

- every authoritative source failed
- the harvest is implausibly small (`min_records`)
- the run accounts for less of the store than `max_shrink_ratio` allows — the
  signature of a partial scrape
- more than half the spine failed at once (a site-wide change, not flakiness)

A refusal is not a crash. Previous data stays live, the run is recorded with its
reason, and the dashboard shows it. **A stale-but-correct dataset always beats a
fresh-but-wrong one.**

### Nothing is destroyed

`filings` holds current state and is written only inside a single transaction,
after the gate passes. `filing_history` is append-only: every field-level change
is recorded with the run that caused it. "When did this become Approved?" is an
answerable question, and any run can be explained after the fact.

---

## Install

Requires **Python 3.11+**. All four dependencies are pure Python — no compiler,
no binary wheels, no admin rights.

```bash
git clone <your-repo-url> sro-filing-tracker
cd sro-filing-tracker
python -m venv .venv
.venv\Scripts\python -m pip install -e .
```

### One required setting

The SEC's automated-access policy requires a User-Agent carrying a real contact
address; requests without one get HTTP 403. There is deliberately no default — a
placeholder would get you rate-limited under someone else's name.

```powershell
$env:SRO_TRACKER_CONTACT = "you@example.com"
```

Or copy `config.example.toml` to `config.toml` and set `contact` there.

```bash
sro-tracker doctor      # verify config + connectivity before anything else
```

### First run

A fresh clone has no data — `data/` is gitignored. Populate it before running
validation:

```bash
sro-tracker refresh       # ~4 minutes for 29 SROs across 2 years
sro-tracker validate
sro-tracker serve --open
```

The launcher polls `/healthz` and opens a browser only once the server answers,
so you never get a spurious "connection refused" page.

---

## Commands

| Command | Purpose |
|---|---|
| `sro-tracker doctor` | Check configuration, scope, and connectivity |
| `sro-tracker refresh` | Fetch, reconcile, gate, commit |
| `sro-tracker serve` | Local dashboard on `127.0.0.1:5057` |
| `sro-tracker export` | Write CSV or Excel |
| `sro-tracker report` | Weekly change report |
| `sro-tracker sources` | List SROs and coverage |
| `sro-tracker validate` | Post-refresh sanity checks |

Useful flags:

```bash
sro-tracker refresh --years 2026 --sro nyse nasdaq cboe-bzx
sro-tracker refresh --sro family:NYSE     # all six NYSE markets
sro-tracker refresh --no-edge             # authoritative sources only
sro-tracker export --family Cboe --format xlsx
sro-tracker report --days 7 --attach
```

Note `nyse` is both an SRO key and a family label. A bare name always means the
specific SRO; use `family:NYSE` for the whole group.

### Exit codes

`0` success · `1` run rejected or command failed · `2` succeeded with warnings ·
`3` configuration invalid, nothing attempted

Meaningful because a scheduler can only see the number.

---

## Coverage

29 SROs in the default scope, all covered authoritatively by the SEC source:

- **NYSE** — NYSE, Arca, American, National, Chicago, Texas
- **Nasdaq** — Nasdaq, BX, PHLX, ISE, GEMX, MRX
- **Cboe** — Cboe Options, C2, BZX, BYX, EDGA, EDGX
- **MIAX** — Options, Pearl, Emerald, Sapphire
- **Independent** — MEMX, LTSE, IEX, TXSE, GIX, BOX
- **FINRA**

Four also have verified same-day exchange feeds (Cboe BZX, BYX, EDGA, EDGX).
Cboe Options and C2 have no trustworthy per-market feed, so they are served by
the spine alone — complete, just not same-day. That is a deliberate trade.

Adding an SRO is a one-line entry in `registry.py`. No parser changes.

---

## Configuration

Precedence: **environment > `config.toml` > defaults.** Every setting has an
env var of the form `SRO_TRACKER_<NAME>`.

Settings worth knowing:

| Setting | Default | Notes |
|---|---|---|
| `contact` | *(required)* | Address in the User-Agent |
| `years` | current + previous | Filing years to track |
| `sros` | core scope | SRO keys or `family:NAME` |
| `ca_bundle` | system | Corporate root CA bundle (see below) |
| `port` | `5057` | Chosen to avoid colliding with anything on 5000/5001 |
| `min_records` | `50` | Quality-gate floor |
| `max_shrink_ratio` | `0.10` | Maximum tolerated record loss |
| `mail_transport` | `file` | `file` · `smtp` · `outlook` |

### TLS behind a corporate proxy

Certificate verification is **never** disabled. A TLS-inspecting proxy that
re-signs traffic will fail the handshake, and the app raises an error explaining
exactly how to fix it — by trusting the root properly:

```powershell
$env:REQUESTS_CA_BUNDLE = "C:\path\to\corporate-root.pem"
```

Falling back to `verify=False` turns a fixable trust-store problem into a
permanent silent downgrade, so this application does not offer that option.

### Email

Sending is opt-in at three levels: the transport must be set, recipients must be
configured, and `--send` must be passed. A fresh clone is never one command away
from mailing real people. `sro-tracker report` writes a preview by default.

The `outlook` transport opens a pre-filled draft via COM and calls `Display`,
not `Send` — a human still presses send.

---

## Development

```bash
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest        # 62 tests, no network required
```

Parser tests run against committed HTML/XML captured from the real sites, so the
suite is deterministic and works offline. When an upstream site changes:

```bash
python scripts/capture_fixtures.py
```

Then **review the diff before committing**. A fixture update should be a
deliberate acknowledgement that the site changed, never a reflex to make tests
green.

### Layout

```
src/sro_tracker/
  models.py       the record contract: normalization, status vocabulary
  registry.py     which SROs exist and how to address them
  config.py       file + env configuration, validation
  http.py         retries, rate limiting, honest TLS
  store.py        SQLite: current state + append-only history
  quality.py      the commit gate
  pipeline.py     fetch -> reconcile -> gate -> commit
  sources/
    sec_sro.py    tier 1: the SEC spine (one parser, all SROs)
    exchange/     tier 2: verified exchange feeds
  exports/        CSV and Excel
  report.py       weekly change report
  mail.py         file / SMTP / Outlook transports
  web/            Flask dashboard, server-rendered
  cli.py          one entry point for every context
```

No build step, no bundler, no client framework — the dashboard is Jinja plus a
little vanilla JS, so it will still start in three years on a machine where
nobody has run `npm`.

---

## Scope

**In:** SRO rule filings (19b-4 proposed rule changes), their lifecycle status,
and the documents behind them.

**Out:** market data, listings, disciplinary actions, Federal Register text,
chatbots, and semantic search. Widening scope is a config change where possible
and a registry entry where not — but it should be a decision, not a drift.

## Data sources

- SEC — Self-Regulatory Organization Rulemaking listings (public)
- Cboe — public rule-filing RSS feeds

Both are public. The scraper is polite by construction: a shared rate limiter
(5 req/s, half the SEC's published ceiling), honest identification, retries with
backoff, and no retry on 403.

## License

MIT.
