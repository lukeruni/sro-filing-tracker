"""Reconciliation, the quality gate, and the store.

These cover the logic that decides what is true and whether to write it - the
places where a bug corrupts data rather than merely losing a page.
"""

from __future__ import annotations

import datetime as dt

import pytest

from sro_tracker import quality, registry
from sro_tracker.models import (
    STATUS_APPROVED,
    STATUS_IMMEDIATELY_EFFECTIVE,
    STATUS_NOTICE,
    STATUS_UNKNOWN,
    Filing,
)
from sro_tracker.pipeline import reconcile
from sro_tracker.sources import (
    STATUS_FAILED,
    STATUS_OK,
    TIER_EDGE,
    TIER_SPINE,
    SourceResult,
)


def make(filing_no: str, *, status=STATUS_NOTICE, source="SEC", date=None, summary="s",
         url="", release="") -> Filing:
    return Filing.build(
        filing_no=filing_no,
        sro="Test Exchange",
        sro_family="Cboe",
        summary=summary,
        status=status,
        filing_date=date,
        source=source,
        filing_url=url,
        release_number=release,
    )


def spine(*filings) -> SourceResult:
    return SourceResult("SEC:test", TIER_SPINE, STATUS_OK, list(filings))


def edge(*filings) -> SourceResult:
    return SourceResult("feed:test", TIER_EDGE, STATUS_OK, list(filings))


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def test_same_filing_from_both_tiers_merges_to_one():
    merged = reconcile([
        spine(make("SR-CboeBZX-2026-1", source="SEC")),
        edge(make("SR-CboeBZX-2026-1", source="Cboe feed")),
    ])
    assert len(merged) == 1
    assert set(merged[0].seen_by) == {"SEC", "Cboe feed"}


def test_edge_cannot_downgrade_an_authoritative_status():
    """The failure this prevents: a feed that knows nothing overwriting
    'Approved' with 'Unknown' on the next run."""
    merged = reconcile([
        spine(make("SR-CboeBZX-2026-1", status=STATUS_APPROVED)),
        edge(make("SR-CboeBZX-2026-1", status=STATUS_UNKNOWN, source="feed")),
    ])
    assert merged[0].status == STATUS_APPROVED


def test_status_advances_when_the_newer_view_knows_more():
    merged = reconcile([
        spine(make("SR-CboeBZX-2026-1", status=STATUS_NOTICE)),
        spine(make("SR-CboeBZX-2026-1", status=STATUS_APPROVED)),
    ])
    assert merged[0].status == STATUS_APPROVED


def test_edge_only_filings_are_kept():
    """The entire point of the edge tier: filings the SEC has not posted yet."""
    merged = reconcile([
        spine(make("SR-CboeBZX-2026-1")),
        edge(make("SR-CboeBZX-2026-2", source="Cboe feed")),
    ])
    assert {f.filing_no for f in merged} == {"SR-CboeBZX-2026-1", "SR-CboeBZX-2026-2"}


def test_earliest_date_wins():
    """The exchange usually publishes before the SEC issues its release."""
    merged = reconcile([
        spine(make("SR-CboeBZX-2026-1", date=dt.date(2026, 8, 14))),
        edge(make("SR-CboeBZX-2026-1", date=dt.date(2026, 8, 10), source="feed")),
    ])
    assert merged[0].filing_date == dt.date(2026, 8, 10)


def test_empty_fields_are_filled_from_the_other_tier():
    merged = reconcile([
        spine(make("SR-CboeBZX-2026-1", status=STATUS_APPROVED, url="")),
        edge(make("SR-CboeBZX-2026-1", status=STATUS_UNKNOWN, url="https://x/y.pdf",
                  source="feed")),
    ])
    assert merged[0].status == STATUS_APPROVED
    assert merged[0].filing_url == "https://x/y.pdf"


def test_failed_sources_contribute_nothing_but_do_not_break_reconciliation():
    merged = reconcile([
        spine(make("SR-CboeBZX-2026-1")),
        SourceResult("feed:dead", TIER_EDGE, STATUS_FAILED, [], 0.0, "boom"),
    ])
    assert len(merged) == 1


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------


def test_gate_passes_a_healthy_run(cfg):
    verdict = quality.evaluate(
        cfg=cfg, results=[spine(*[make(f"SR-CboeBZX-2026-{i}") for i in range(1, 80)])],
        candidates=79, known_count=75, matched_known=75,
    )
    assert verdict.verdict == quality.VERDICT_PASS
    assert verdict.committable


def test_gate_rejects_when_every_authoritative_source_fails(cfg):
    verdict = quality.evaluate(
        cfg=cfg,
        results=[SourceResult("SEC:a", TIER_SPINE, STATUS_FAILED, [], 0, "network down")],
        candidates=200, known_count=200, matched_known=200,
    )
    assert not verdict.committable
    assert "authoritative" in verdict.reasons[0]


def test_gate_rejects_a_shrinking_dataset(cfg):
    """The classic silent-breakage signature: the run simply sees less."""
    cfg.max_shrink_ratio = 0.10
    verdict = quality.evaluate(
        cfg=cfg, results=[spine()], candidates=500, known_count=1000, matched_known=500,
    )
    assert not verdict.committable
    assert any("partial dataset" in r for r in verdict.reasons)


def test_gate_tolerates_shrinkage_within_the_threshold(cfg):
    cfg.max_shrink_ratio = 0.10
    verdict = quality.evaluate(
        cfg=cfg, results=[spine()], candidates=980, known_count=1000, matched_known=960,
    )
    assert verdict.committable


def test_gate_rejects_an_implausibly_small_harvest(cfg):
    cfg.min_records = 50
    verdict = quality.evaluate(
        cfg=cfg, results=[spine()], candidates=3, known_count=0, matched_known=0,
    )
    assert not verdict.committable


def test_edge_failure_only_warns(cfg):
    """A dead exchange feed must never block a commit."""
    verdict = quality.evaluate(
        cfg=cfg,
        results=[
            spine(*[make(f"SR-CboeBZX-2026-{i}") for i in range(1, 80)]),
            SourceResult("feed:x", TIER_EDGE, STATUS_FAILED, [], 0, "404 Not Found"),
        ],
        candidates=79, known_count=79, matched_known=79,
    )
    assert verdict.committable
    assert verdict.verdict == quality.VERDICT_PASS_WITH_WARNINGS
    assert any("freshness" in w for w in verdict.warnings)


def test_first_run_against_an_empty_store_is_allowed(cfg):
    verdict = quality.evaluate(
        cfg=cfg, results=[spine()], candidates=2000, known_count=0, matched_known=0,
    )
    assert verdict.committable


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_commit_is_idempotent(tmp_store):
    filings = [make(f"SR-CboeBZX-2026-{i}") for i in range(1, 6)]
    run = tmp_store.start_run()
    added, changed = tmp_store.commit_filings(run, filings)
    assert (added, changed) == (5, 0)

    run2 = tmp_store.start_run()
    added2, changed2 = tmp_store.commit_filings(run2, filings)
    assert (added2, changed2) == (0, 0), "re-committing identical data must be a no-op"
    assert tmp_store.count() == 5


def test_changes_are_recorded_with_a_diff(tmp_store):
    run = tmp_store.start_run()
    tmp_store.commit_filings(run, [make("SR-CboeBZX-2026-1", status=STATUS_NOTICE)])

    run2 = tmp_store.start_run()
    added, changed = tmp_store.commit_filings(
        run2, [make("SR-CboeBZX-2026-1", status=STATUS_APPROVED)])
    assert (added, changed) == (0, 1)

    history = list(tmp_store._conn.execute(  # noqa: SLF001
        "SELECT * FROM filing_history WHERE change_type='changed'"))
    assert len(history) == 1
    assert "status" in history[0]["changes"]


def test_history_is_append_only(tmp_store):
    run = tmp_store.start_run()
    tmp_store.commit_filings(run, [make("SR-CboeBZX-2026-1", status=STATUS_NOTICE)])
    for status in (STATUS_IMMEDIATELY_EFFECTIVE, STATUS_APPROVED):
        tmp_store.commit_filings(
            tmp_store.start_run(), [make("SR-CboeBZX-2026-1", status=status)])

    rows = list(tmp_store._conn.execute(  # noqa: SLF001
        "SELECT * FROM filing_history ORDER BY id"))
    assert [r["change_type"] for r in rows] == ["added", "changed", "changed"]


def test_query_filters(tmp_store):
    tmp_store.commit_filings(tmp_store.start_run(), [
        make("SR-CboeBZX-2026-1", status=STATUS_APPROVED, summary="fee schedule"),
        make("SR-CboeBZX-2026-2", status=STATUS_NOTICE, summary="listing standards"),
    ])
    assert len(tmp_store.query(status=STATUS_APPROVED)) == 1
    assert len(tmp_store.query(search="listing")) == 1
    assert len(tmp_store.query(search="nothing here")) == 0
    assert len(tmp_store.query()) == 2


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_sro_key_beats_family_name():
    """Regression: 'nyse' is both an SRO key and a family label. The bare name
    must select the exchange itself, not all six NYSE markets."""
    selected = registry.resolve(("nyse",))
    assert [s.key for s in selected] == ["nyse"]


def test_family_prefix_selects_the_whole_family():
    selected = registry.resolve(("family:NYSE",))
    assert len(selected) > 1
    assert all(s.family == "NYSE" for s in selected)


def test_unambiguous_family_name_still_works():
    selected = registry.resolve(("cboe",))
    # 'cboe' is also a key (Cboe Options), so the key must win.
    assert [s.key for s in selected] == ["cboe"]


def test_unknown_sro_is_an_error():
    with pytest.raises(KeyError):
        registry.resolve(("not-a-real-exchange",))


def test_every_registry_entry_is_addressable():
    for sro in registry.all_sros():
        assert sro.sec_org_id or sro.sec_path, f"{sro.key} has no SEC address"
        assert sro.listing_url(2026).startswith("https://www.sec.gov/")


def test_filing_codes_are_unique_per_sro():
    seen: dict[str, str] = {}
    for sro in registry.all_sros():
        for code in sro.match_codes:
            assert code not in seen or seen[code] == sro.key, (
                f"code {code} maps to both {seen[code]} and {sro.key}")
            seen.setdefault(code, sro.key)
