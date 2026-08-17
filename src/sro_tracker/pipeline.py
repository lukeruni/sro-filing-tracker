"""The refresh pipeline: fetch, reconcile, judge, commit.

One path, used identically by the CLI, the scheduled task and the dashboard's
refresh button. There is no second way to write data.

    sources -> isolated fetch -> reconcile on filing_no -> quality gate -> commit

Concurrency is modest and deliberate. Sources are fetched in a small thread pool
because they are IO-bound, but the shared rate limiter in ``http.Client`` still
paces every outbound request, so parallelism never turns into impoliteness.
"""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import quality, registry
from .config import Config
from .http import Client
from .models import STATUS_RANK, Filing
from .quality import Verdict
from .sources import TIER_EDGE, TIER_SPINE, Source, SourceResult, run_source
from .sources import exchange as edge_sources
from .sources import sec_sro
from .store import Store

log = logging.getLogger(__name__)

MAX_WORKERS = 6


@dataclass(slots=True)
class RefreshReport:
    run_id: int
    verdict: Verdict
    results: list[SourceResult] = field(default_factory=list)
    fetched: int = 0
    committed: int = 0
    added: int = 0
    changed: int = 0

    @property
    def ok(self) -> bool:
        return self.verdict.committable

    def render(self) -> str:
        lines = [f"Run #{self.run_id}: {self.verdict.summary()}", ""]
        by_tier: dict[str, list[SourceResult]] = {}
        for result in self.results:
            by_tier.setdefault(result.tier, []).append(result)
        for tier in (TIER_SPINE, TIER_EDGE):
            entries = by_tier.get(tier)
            if not entries:
                continue
            label = "authoritative (SEC)" if tier == TIER_SPINE else "freshness (exchanges)"
            ok = sum(1 for e in entries if e.status == "ok")
            lines.append(f"  {label}: {ok}/{len(entries)} healthy")
            for entry in entries:
                if entry.status != "ok":
                    first = entry.message.splitlines()[0] if entry.message else ""
                    lines.append(f"    ! {entry.source}: {entry.status} - {first}")
        lines += [
            "",
            f"  fetched   {self.fetched}",
            f"  committed {self.committed}",
            f"  added     {self.added}",
            f"  changed   {self.changed}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _prefer(current: Filing, incoming: Filing) -> Filing:
    """Merge two views of the same filing into the better one.

    Field-level rules, in order:
      * lifecycle status only ever advances (STATUS_RANK), so an edge record
        that knows nothing cannot undo an SEC "Approved";
      * the record carrying the higher status supplies the narrative fields;
      * empty fields are filled from whichever side has them;
      * the earliest known date wins, since the exchange usually publishes first.
    """
    if STATUS_RANK.get(incoming.status, 0) > STATUS_RANK.get(current.status, 0):
        winner, loser = incoming, current
    else:
        winner, loser = current, incoming

    filing_date = winner.filing_date or loser.filing_date
    if winner.filing_date and loser.filing_date:
        filing_date = min(winner.filing_date, loser.filing_date)

    sources = []
    for value in (current.source, incoming.source):
        if value and value not in sources:
            sources.append(value)

    return Filing(
        filing_no=winner.filing_no,
        sro=winner.sro or loser.sro,
        sro_family=winner.sro_family or loser.sro_family,
        filing_year=winner.filing_year or loser.filing_year,
        summary=winner.summary or loser.summary,
        status=winner.status,
        filing_date=filing_date,
        release_number=winner.release_number or loser.release_number,
        filing_url=winner.filing_url or loser.filing_url,
        source=", ".join(sources),
        source_url=winner.source_url or loser.source_url,
        notes=winner.notes or loser.notes,
        seen_by=tuple(sources),
    )


def reconcile(results: list[SourceResult]) -> list[Filing]:
    """Fold every source's output into one record per filing number.

    The spine is folded first so that authoritative values are established
    before edge records get a chance to fill gaps.
    """
    merged: dict[str, Filing] = {}
    ordered = [r for r in results if r.tier == TIER_SPINE] + \
              [r for r in results if r.tier != TIER_SPINE]

    for result in ordered:
        for filing in result.filings:
            existing = merged.get(filing.filing_no)
            if existing is None:
                merged[filing.filing_no] = filing.with_provenance(
                    seen_by=(filing.source,) if filing.source else ()
                )
                continue
            combined = _prefer(existing, filing)
            seen = tuple(dict.fromkeys(existing.seen_by + combined.seen_by))
            merged[filing.filing_no] = combined.with_provenance(seen_by=seen)

    return sorted(
        merged.values(),
        key=lambda f: (f.filing_date or dt.date.min, f.filing_no),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Source assembly
# ---------------------------------------------------------------------------


def build_sources(cfg: Config) -> list[Source]:
    sros = registry.resolve(cfg.sros)
    sources: list[Source] = list(sec_sro.build_sources(sros))
    if cfg.enable_exchange_sources:
        sources.extend(edge_sources.build_sources(sros))
    return sources


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def refresh(cfg: Config, store: Store, *, progress=None) -> RefreshReport:
    """Run one full refresh. Never raises for source-level failure."""
    cfg.ensure_dirs()
    sources = build_sources(cfg)
    run_id = store.start_run()
    log.info("run #%d starting with %d source(s)", run_id, len(sources))

    results: list[SourceResult] = []
    with Client(cfg) as client:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(run_source, s, client, cfg) for s in sources]
            for future in as_completed(futures):
                # run_source is total: it converts every failure into a result,
                # so this cannot raise and one bad source cannot abort the run.
                result = future.result()
                results.append(result)
                if progress:
                    progress(result)

    results.sort(key=lambda r: (r.tier != TIER_SPINE, r.source))
    candidates = reconcile(results)
    fetched = sum(len(r.filings) for r in results)

    known = store.existing_hashes()
    matched = sum(1 for f in candidates if f.filing_no in known)

    verdict = quality.evaluate(
        cfg=cfg,
        results=results,
        candidates=len(candidates),
        known_count=len(known),
        matched_known=matched,
    )

    store.record_source_health(run_id, [r.as_health() for r in results])

    report = RefreshReport(run_id=run_id, verdict=verdict, results=results, fetched=fetched)

    if not verdict.committable:
        log.error("run #%d rejected: %s", run_id, "; ".join(verdict.reasons))
        store.finish_run(
            run_id,
            outcome="rejected",
            records_fetched=fetched,
            message=verdict.summary(),
            detail={"reasons": verdict.reasons, "warnings": verdict.warnings,
                    "stats": verdict.stats},
        )
        return report

    added, changed = store.commit_filings(run_id, candidates)
    report.committed = len(candidates)
    report.added = added
    report.changed = changed

    store.finish_run(
        run_id,
        outcome="ok" if verdict.verdict == quality.VERDICT_PASS else "ok-warnings",
        records_fetched=fetched,
        records_committed=len(candidates),
        added=added,
        changed=changed,
        message=verdict.summary(),
        detail={"warnings": verdict.warnings, "stats": verdict.stats},
    )
    log.info("run #%d committed %d records (+%d new, ~%d changed)",
             run_id, len(candidates), added, changed)
    return report
