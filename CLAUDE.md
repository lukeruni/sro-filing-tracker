# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read AGENTS.md first

`AGENTS.md` is the authority on *policy*: the twelve invariants, the "where to
change what" table, and the traps already hit. Read it before changing
behaviour. Three of those invariants shape nearly every task here — there is one
SEC parser for all SROs, a run must earn its commit, and an edge source can
never fail a run.

This file covers what AGENTS.md does not: the commands that work in this
checkout, and how a refresh flows across modules.

## Commands

Windows-first; `.venv\Scripts\python` is the interpreter in a normal checkout.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Dev
# or, into an existing venv:
.venv\Scripts\python -m pip install -e ".[dev]"
```

One setting is required before anything reaches the network. The SEC returns
403 to a User-Agent with no contact address, and there is deliberately no
default:

```powershell
$env:SRO_TRACKER_CONTACT = "you@example.com"
```

`data/` is gitignored, so a fresh clone has an empty database. Bootstrap it
before anything that reads the store:

```powershell
sro-tracker doctor      # config + connectivity; run this first
sro-tracker refresh     # ~4 minutes for 29 SROs across 2 years
sro-tracker validate
sro-tracker serve --open
```

### Tests

```powershell
.venv\Scripts\python -m pytest                       # whole suite, fully offline
.venv\Scripts\python -m pytest tests/test_parsers.py  # one file
.venv\Scripts\python -m pytest tests/test_parsers.py::test_columns_are_found_by_name_not_position
.venv\Scripts\python -m pytest -m network            # opt-in: SRO websites resolve
```

`addopts = "-m 'not network'"` excludes the live checks by default, so the
default suite is deterministic and runs with no outbound access.
`filterwarnings = ["error::DeprecationWarning"]` means a deprecation fails the
suite rather than scrolling past.

`pythonpath = ["src"]` makes pytest exercise the working tree, not an installed
package — `tests/test_packaging.py` is what closes that gap, and it is not
optional decoration (an unanchored `.gitignore` pattern once excluded a whole
package while every other test passed).

Parser tests run against committed fixtures. After an upstream redesign:

```powershell
python scripts/capture_fixtures.py   # then review the diff before committing
```

Exit codes are load-bearing — the scheduled task can only see the number:
`0` ok · `1` failed or run rejected · `2` warnings · `3` config invalid.

## Architecture

One refresh, end to end. This is the only path that writes filing data:

```
cli.cmd_refresh
  -> pipeline.build_sources(cfg)         registry.resolve -> sec_sro + exchange feeds
  -> ThreadPoolExecutor(6) x run_source  isolated; converts any raise into a result
  -> pipeline.reconcile(results)         spine folded first, then edge
  -> quality.evaluate(...)               Verdict: pass / pass-with-warnings / reject
  -> store.commit_filings(run_id, ...)   one transaction, only if committable
```

**Two source tiers** (`sources/__init__.py`). `TIER_SPINE` is the SEC —
authoritative and complete, and the only tier that can fail a run.
`TIER_EDGE` is exchange feeds, which add freshness and degrade rather than fail.
`run_source` is total: it never raises, so one bad source cannot abort a run. A
source sets `empty_is_valid` only if it can distinguish "nothing to report" from
"I no longer understand this page"; the SEC parser qualifies because it raises
on a missing table.

**Reconciliation** (`pipeline._prefer`). Sources join on `filing_no`. Status only
ever advances, ranked by `models.STATUS_RANK`, so an edge record cannot undo an
SEC "Approved". The higher-status record supplies the narrative fields, empty
fields are filled from whichever side has them, and the earliest `filing_date`
wins because the exchange usually publishes first.

**The gate** (`quality.evaluate`). Rejects on total spine failure, majority spine
failure, a harvest below `min_records`, or a store shrinking past
`max_shrink_ratio`. A rejection is recorded on the run and surfaced in the
dashboard — it is not an exception, and previous data stays live.

**Storage** (`store.py`). SQLite, WAL, `SCHEMA_VERSION = 1`, no ORM. `filings`
holds current state; `filing_history` is append-only field-level diffs; `runs`
and `source_health` record why each run did what it did.

**Config** (`config.py`). Precedence is env > `config.toml` > dataclass
defaults. Every field has a `SRO_TRACKER_<NAME>` env var, and `load()` reports
unknown keys rather than ignoring them.

**Attribution** (`registry.by_code`). The SRO comes from the code inside
`SR-<code>-<year>-<seq>`, never from the endpoint a record arrived on.

**Everything downstream reads, nothing downstream writes.** `exports/` (the
four-sheet workbook), `report.py` (Outlook-safe HTML) and `web/` (server-rendered
Flask, no build step, no bundler) each query the store. Only `pipeline.refresh`
commits.

## Conventions

- `from __future__ import annotations` at the top of every module; dataclasses
  with `slots=True` for anything with a shape.
- The version is declared once in `pyproject.toml` and asserted against
  `config.VERSION`; the Python floor is guarded the same way. Bumping one
  without the other fails `tests/test_packaging.py`.
- Runtime `.gitignore` patterns stay anchored — `/data/`, `/exports/`, `/logs/`.
  The unanchored forms match at any depth and will silently exclude
  `src/sro_tracker/exports/`.
