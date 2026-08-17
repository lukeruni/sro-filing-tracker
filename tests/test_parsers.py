"""Golden-file parser tests.

These are the tests that matter most. A scraper's characteristic failure is
returning nothing while reporting success, so each of these asserts on the
*shape and content* of what was parsed, not merely that parsing did not raise.
"""

from __future__ import annotations

import datetime as dt

import pytest

from sro_tracker import registry
from sro_tracker.models import (
    STATUS_APPROVED,
    STATUS_IMMEDIATELY_EFFECTIVE,
    STATUS_UNKNOWN,
    derive_status,
    filing_sort_key,
    parse_date,
    parse_filing_no,
)
from sro_tracker.sources.exchange import parse_rss
from sro_tracker.sources.sec_sro import LayoutChanged, advertised_page_count, parse_listing


# ---------------------------------------------------------------------------
# SEC listing
# ---------------------------------------------------------------------------


def test_sec_listing_extracts_filings(sec_listing_html):
    filings = parse_listing(
        sec_listing_html, sro=registry.get("nyse"), source_url="https://example.test/listing"
    )
    assert len(filings) >= 8, "a full SEC page carries ten rows"

    first = filings[0]
    assert first.filing_no.startswith("SR-")
    assert first.sro_family == "NYSE"
    assert first.filing_date is not None
    assert first.summary, "summary must never be blank"
    assert first.filing_url.startswith("https://www.sec.gov/")
    assert first.source == "SEC"


def test_sec_listing_fields_are_normalized(sec_listing_html):
    filings = parse_listing(
        sec_listing_html, sro=registry.get("nyse"), source_url="https://example.test/listing"
    )
    for filing in filings:
        assert filing.filing_no == filing.filing_no.strip()
        assert "  " not in filing.summary, "whitespace should be collapsed"
        # The SEC appends page furniture; none of it belongs in the summary.
        for noise in ("Comments Due:", "Submit a Comment", "See Also"):
            assert noise not in filing.summary, f"{noise!r} leaked into the summary"
        assert filing.filing_year >= 2000
        assert filing.status != ""


def test_sec_listing_year_comes_from_filing_number(sec_listing_html):
    """A 2025 filing released in 2026 is still a 2025 filing."""
    filings = parse_listing(
        sec_listing_html, sro=registry.get("nyse"), source_url="https://example.test/listing"
    )
    for filing in filings:
        parsed = parse_filing_no(filing.filing_no)
        assert parsed is not None
        assert filing.filing_year == parsed[2]


def test_finra_uses_the_same_parser(sec_finra_html):
    """One parser, every SRO - the premise the architecture rests on."""
    filings = parse_listing(
        sec_finra_html, sro=registry.get("finra"), source_url="https://example.test/finra"
    )
    assert len(filings) >= 5
    assert all(f.sro_family == "FINRA" for f in filings)
    assert all(f.filing_no.upper().startswith("SR-FINRA-") for f in filings)


def test_missing_table_raises_rather_than_returning_empty():
    """The single most important behaviour: fail loudly, never silently."""
    with pytest.raises(LayoutChanged):
        parse_listing(
            "<html><body><p>Sorry, this page has moved.</p></body></html>",
            sro=registry.get("nyse"),
            source_url="https://example.test/moved",
        )


def test_renamed_columns_raise():
    html = """
    <table><thead><tr><th>Doc ID</th><th>Published</th><th>Blurb</th></tr></thead>
    <tbody><tr><td>x</td><td>y</td><td>z</td></tr></tbody></table>
    """
    with pytest.raises(LayoutChanged) as excinfo:
        parse_listing(html, sro=registry.get("nyse"), source_url="https://example.test/x")
    assert "missing required column" in str(excinfo.value)


def test_columns_are_found_by_name_not_position():
    """Adding a column upstream must not shift extraction."""
    html = """
    <table>
      <thead><tr>
        <th>Extra</th><th>File Number</th><th>SEC Issue Date</th><th>Details</th>
      </tr></thead>
      <tbody><tr>
        <td>ignore me</td>
        <td>SR-NYSE-2026-99</td>
        <td>Mar 3, 2026</td>
        <td>Order Approving a Proposed Rule Change</td>
      </tr></tbody>
    </table>
    """
    filings = parse_listing(html, sro=registry.get("nyse"), source_url="https://example.test/x")
    assert len(filings) == 1
    assert filings[0].filing_no == "SR-NYSE-2026-99"
    assert filings[0].filing_date == dt.date(2026, 3, 3)
    assert filings[0].status == STATUS_APPROVED


def test_rows_without_filing_numbers_are_skipped():
    html = """
    <table><thead><tr><th>File Number</th><th>Details</th></tr></thead>
    <tbody>
      <tr><td>&nbsp;</td><td>A heading row</td></tr>
      <tr><td>SR-IEX-2026-01</td><td>Notice of Filing</td></tr>
    </tbody></table>
    """
    filings = parse_listing(html, sro=registry.get("iex"), source_url="https://example.test/x")
    assert [f.filing_no for f in filings] == ["SR-IEX-2026-01"]


def test_pager_links_are_html_escaped(sec_listing_html):
    """Regression: anchoring on [?&] missed `&amp;page=`, collapsing every SRO
    to a single page and silently losing ~75% of the data."""
    assert advertised_page_count(sec_listing_html) >= 2


# ---------------------------------------------------------------------------
# RSS feed
# ---------------------------------------------------------------------------


def test_cboe_feed_parses(cboe_feed_xml):
    filings = parse_rss(
        cboe_feed_xml, source_url="https://example.test/feed", source_label="Cboe BZX feed"
    )
    assert len(filings) >= 10
    for filing in filings:
        assert filing.filing_no.startswith("SR-")
        assert filing.sro_family == "Cboe"
        assert filing.filing_date is not None
        assert filing.source == "Cboe BZX feed"


def test_feed_attributes_by_filing_number_not_url(cboe_feed_xml):
    """Cboe's options feed paths ignore the market in the URL. Attribution must
    come from the filing number, so a mis-pointed feed cannot mislabel data."""
    filings = parse_rss(
        cboe_feed_xml, source_url="https://example.test/feed", source_label="wrong label"
    )
    for filing in filings:
        code = parse_filing_no(filing.filing_no)[1]
        expected = registry.by_code(code)
        assert expected is not None
        assert filing.sro == expected.name


def test_malformed_feed_raises():
    with pytest.raises(ValueError):
        parse_rss("not xml at all <<<", source_url="u", source_label="l")


def test_feed_items_without_filing_numbers_are_skipped():
    xml = """<?xml version="1.0"?><rss><channel>
      <item><title>Weekly newsletter</title><description>No filing here</description></item>
      <item><title>SR-CboeBZX-2026-001</title><description>A real one</description></item>
    </channel></rss>"""
    filings = parse_rss(xml, source_url="u", source_label="l")
    assert [f.filing_no for f in filings] == ["SR-CboeBZX-2026-001"]


# ---------------------------------------------------------------------------
# Field-level helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("SR-NYSE-2026-38", ("SR-NYSE-2026-38", "NYSE", 2026, 38)),
        ("SR-NASDAQ-2026-064", ("SR-NASDAQ-2026-064", "NASDAQ", 2026, 64)),
        ("SR-CboeBZX-2026-065", ("SR-CboeBZX-2026-065", "CBOEBZX", 2026, 65)),
        ("SR-PEARL-2026-35", ("SR-PEARL-2026-35", "PEARL", 2026, 35)),
        ("File No. SR-FINRA-2026-009 (amended)", ("SR-FINRA-2026-009", "FINRA", 2026, 9)),
    ],
)
def test_filing_number_parsing(raw, expected):
    assert parse_filing_no(raw) == expected


def test_filing_number_rejects_nonsense():
    assert parse_filing_no("no filing here") is None
    assert parse_filing_no("") is None
    assert parse_filing_no(None) is None


def test_sort_key_orders_numerically_not_lexically():
    numbers = ["SR-NYSE-2026-9", "SR-NYSE-2026-100", "SR-NYSE-2026-10"]
    ordered = sorted(numbers, key=filing_sort_key)
    assert ordered == ["SR-NYSE-2026-9", "SR-NYSE-2026-10", "SR-NYSE-2026-100"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Aug 14, 2026", dt.date(2026, 8, 14)),
        ("August 14, 2026", dt.date(2026, 8, 14)),
        ("2026-08-14", dt.date(2026, 8, 14)),
        ("08/14/2026", dt.date(2026, 8, 14)),
        ("Aug 14, 2026 (updated)", dt.date(2026, 8, 14)),
        ("garbage", None),
        ("", None),
        (None, None),
    ],
)
def test_date_parsing(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Order Granting Accelerated Approval of a Proposed Rule Change", STATUS_APPROVED),
        ("Notice of Filing and Immediate Effectiveness", STATUS_IMMEDIATELY_EFFECTIVE),
        ("Notice of Withdrawal of a Proposed Rule Change", "Withdrawn"),
        ("Order Disapproving a Proposed Rule Change", "Disapproved"),
        ("Notice of Designation of a Longer Period for Commission Action", "Period Extended"),
        # Combined action: proceedings is the operative state.
        ("Suspension of and Order Instituting Proceedings", "Proceedings Instituted"),
        # Noun form standalone - the form every real SEC title actually uses.
        ("Notice of Suspension of a Proposed Rule Change", "Suspended"),
        ("", STATUS_UNKNOWN),
        ("Something entirely unrelated", STATUS_UNKNOWN),
    ],
)
def test_status_derivation(title, expected):
    assert derive_status(title) == expected


def test_approval_beats_immediate_effectiveness():
    """Order matters: this title contains both phrases and must read as Approved."""
    title = ("Notice of Filing of Amendment No. 2 and Order Granting Accelerated "
             "Approval of a Proposed Rule Change")
    assert derive_status(title) == STATUS_APPROVED
