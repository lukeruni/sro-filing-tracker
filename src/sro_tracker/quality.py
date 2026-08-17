"""The quality gate: the last thing standing between a bad scrape and the store.

Scrapers do not usually fail loudly. They fail by returning *less* - a changed
selector, a partial outage, a silent redirect to a login page - and the damage
is done at the moment that thin result overwrites a good dataset.

So a run must earn its commit. The gate is deliberately conservative: when it is
unsure, it refuses, because a stale-but-correct dataset is always better than a
fresh-but-wrong one. A refusal is not a crash; the previous data stays live, the
run is recorded with its reason, and the dashboard shows the failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .sources import STATUS_FAILED, TIER_EDGE, TIER_SPINE, SourceResult

VERDICT_PASS = "pass"
VERDICT_PASS_WITH_WARNINGS = "pass-with-warnings"
VERDICT_REJECT = "reject"


@dataclass(slots=True)
class Verdict:
    verdict: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, object] = field(default_factory=dict)

    @property
    def committable(self) -> bool:
        return self.verdict != VERDICT_REJECT

    def summary(self) -> str:
        if self.verdict == VERDICT_REJECT:
            return "REJECTED: " + "; ".join(self.reasons)
        if self.warnings:
            return "PASSED with warnings: " + "; ".join(self.warnings)
        return "PASSED"


def evaluate(
    *,
    cfg: Config,
    results: list[SourceResult],
    candidates: int,
    known_count: int,
    matched_known: int,
) -> Verdict:
    """Decide whether this run may be committed.

    ``candidates``    records produced by this run after reconciliation
    ``known_count``   records already in the store
    ``matched_known`` how many stored records this run saw again
    """
    reasons: list[str] = []
    warnings: list[str] = []

    spine = [r for r in results if r.tier == TIER_SPINE]
    edge = [r for r in results if r.tier == TIER_EDGE]
    spine_failed = [r for r in spine if r.status == STATUS_FAILED]
    edge_failed = [r for r in edge if r.status == STATUS_FAILED]

    # --- blocking conditions -------------------------------------------

    # 1. Nothing authoritative came back. Committing now would mean writing the
    #    edge tier's partial view over a complete dataset.
    if spine and len(spine_failed) == len(spine):
        reasons.append(
            f"every authoritative source failed ({len(spine_failed)}/{len(spine)}). "
            f"First error: {spine_failed[0].message.splitlines()[0]}"
        )

    # 2. Implausibly small harvest.
    if candidates < cfg.min_records:
        reasons.append(
            f"only {candidates} records were produced, below the floor of "
            f"{cfg.min_records}. Set min_records lower if this scope is genuinely small."
        )

    # 3. Record loss. The signature of a partial scrape: the run simply stops
    #    seeing things it saw before.
    if known_count:
        lost = known_count - matched_known
        ratio = lost / known_count
        if ratio > cfg.max_shrink_ratio:
            reasons.append(
                f"this run accounts for only {matched_known} of {known_count} known "
                f"records - {lost} missing ({ratio:.1%}), over the "
                f"{cfg.max_shrink_ratio:.0%} tolerance. Refusing to commit a "
                f"partial dataset over a complete one."
            )

    # 4. A majority of the spine degrading at once is a site-wide change, not
    #    coincidence.
    if spine and len(spine_failed) > len(spine) / 2:
        reasons.append(
            f"{len(spine_failed)} of {len(spine)} authoritative sources failed, "
            f"which suggests a site-wide change rather than isolated flakiness."
        )

    # --- non-blocking advisories ---------------------------------------

    for result in results:
        if result.status == STATUS_FAILED and result.tier == TIER_EDGE:
            continue  # counted below
        if result.status != "ok":
            warnings.append(f"{result.source}: {result.message.splitlines()[0]}")

    if edge_failed:
        warnings.append(
            f"{len(edge_failed)} of {len(edge)} freshness sources failed "
            f"({', '.join(r.source for r in edge_failed)}). Coverage is complete "
            f"but may lag the exchanges by a day or two."
        )

    if spine_failed and len(spine_failed) <= len(spine) / 2:
        warnings.append(
            f"{len(spine_failed)} authoritative source(s) failed: "
            f"{', '.join(r.source for r in spine_failed)}. Those SROs were not refreshed."
        )

    stats = {
        "candidates": candidates,
        "known": known_count,
        "matched_known": matched_known,
        "sources_ok": sum(1 for r in results if r.status == "ok"),
        "sources_total": len(results),
    }

    if reasons:
        return Verdict(VERDICT_REJECT, reasons, warnings, stats)
    if warnings:
        return Verdict(VERDICT_PASS_WITH_WARNINGS, [], warnings, stats)
    return Verdict(VERDICT_PASS, [], [], stats)
