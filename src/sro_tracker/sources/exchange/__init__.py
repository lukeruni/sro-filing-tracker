"""Tier 2 - exchange edge sources.

These exist for one reason: freshness. An exchange publishes its own filing the
day it goes to the SEC, days before the SEC release appears. Measured during
design, the Cboe BZX feed carried ``SR-CboeBZX-2026-066`` while the SEC listing
still topped out at ``-065``.

Three rules govern everything here:

1. **Trust the filing number, not the URL.** The SRO is resolved from the code
   inside ``SR-<code>-<year>-<seq>`` via the registry, never from which feed the
   record arrived on. Cboe's options feed paths, for instance, ignore the market
   segment in the URL and serve whatever they please - a source of quietly
   mislabelled data for anyone who trusts the endpoint. Resolving from the
   filing number makes a mis-pointed feed harmless.

2. **Prefer machine-readable endpoints.** An RSS feed is a contract; a rendered
   page is an implementation detail. Feeds are used wherever they exist.

3. **Only verified endpoints are configured.** Every URL below was confirmed to
   return the market it claims. Plausible-looking guesses are worse than
   absence, because absence is visible and wrong data is not.

Coverage note: Cboe's four equities feeds are verified and configured. Cboe
Options and C2 have no trustworthy per-market feed, so they are served by the
SEC spine alone - complete, just not same-day. That is a deliberate trade, not
an oversight.
"""

from __future__ import annotations

import logging
from email.utils import parsedate_to_datetime
from typing import Sequence
from xml.etree import ElementTree

from ...config import Config
from ...http import Client
from ...models import Filing, clean_text, parse_filing_no
from ...registry import Sro, by_code
from .. import TIER_EDGE

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------


def _item_text(item: ElementTree.Element, tag: str) -> str:
    node = item.find(tag)
    return clean_text(node.text) if node is not None and node.text else ""


def parse_rss(xml_text: str, *, source_url: str, source_label: str) -> list[Filing]:
    """Extract filings from an RSS 2.0 feed.

    Pure function, so the golden-file test needs no network. Items whose title
    carries no recognisable filing number are skipped rather than guessed at.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"{source_url} is not well-formed XML: {exc}") from exc

    filings: list[Filing] = []
    for item in root.iter("item"):
        title = _item_text(item, "title")
        parsed = parse_filing_no(title)
        if not parsed:
            # Some feeds put the filing number in the description instead.
            parsed = parse_filing_no(_item_text(item, "description"))
        if not parsed:
            log.debug("feed item without a filing number: %r", title[:80])
            continue
        _canonical, code, _year, _seq = parsed

        owner = by_code(code)
        if owner is None:
            log.warning(
                "%s carried unknown SRO code %r (%s); skipping. Add it to the "
                "registry if this market is in scope.", source_label, code, title)
            continue

        date_value = None
        pub = _item_text(item, "pubDate")
        if pub:
            try:
                date_value = parsedate_to_datetime(pub).date()
            except (TypeError, ValueError):
                log.debug("unparseable pubDate %r in %s", pub, source_label)

        try:
            filings.append(
                Filing.build(
                    filing_no=title if parse_filing_no(title) else _item_text(item, "description"),
                    sro=owner.name,
                    sro_family=owner.family,
                    summary=_item_text(item, "description"),
                    # Status is deliberately left to derivation, and will
                    # usually be Unknown. The SEC spine owns lifecycle state and
                    # outranks the edge during reconciliation, so an edge record
                    # can add a filing but never downgrade its status.
                    filing_date=date_value,
                    filing_url=_item_text(item, "link"),
                    source=source_label,
                    source_url=source_url,
                )
            )
        except ValueError as exc:
            log.debug("skipping feed item: %s", exc)

    return filings


# ---------------------------------------------------------------------------
# Feed configuration - verified endpoints only
# ---------------------------------------------------------------------------

CBOE_EQUITIES_FEED = "https://www.cboe.com/us/equities/regulation/rss/rule_filings/{market}/approved"

# registry key -> feed URL. Each was confirmed to return its own market's
# filings. Cboe's "pending" path serves identical content to "approved", so only
# one is fetched.
FEEDS: dict[str, str] = {
    "cboe-bzx":  CBOE_EQUITIES_FEED.format(market="bzx"),
    "cboe-byx":  CBOE_EQUITIES_FEED.format(market="byx"),
    "cboe-edga": CBOE_EQUITIES_FEED.format(market="edga"),
    "cboe-edgx": CBOE_EQUITIES_FEED.format(market="edgx"),
}


class RssFeedSource:
    """Fetches one exchange RSS feed."""

    tier = TIER_EDGE

    def __init__(self, sro: Sro, url: str) -> None:
        self.sro = sro
        self.url = url
        self.name = f"feed:{sro.key}"

    def fetch(self, client: Client, cfg: Config) -> Sequence[Filing]:
        response = client.get(self.url)
        filings = parse_rss(
            response.text, source_url=self.url, source_label=f"{self.sro.name} feed"
        )

        expected = set(self.sro.match_codes)
        mismatched = [
            f for f in filings
            if (p := parse_filing_no(f.filing_no)) and p[1] not in expected
        ]
        if mismatched:
            # Not fatal: the records are still correctly attributed, because the
            # SRO came from the filing number. Worth saying out loud though,
            # since it means the endpoint no longer means what its URL implies.
            log.warning(
                "%s returned %d filing(s) belonging to other markets; they were "
                "attributed by filing number, not by feed URL.",
                self.name, len(mismatched),
            )

        years = set(cfg.target_years())
        return [f for f in filings if f.filing_year in years]


def build_sources(sros: Sequence[Sro]) -> list[RssFeedSource]:
    """Edge adapters for the selected SROs that have a verified endpoint."""
    sources: list[RssFeedSource] = []
    for sro in sros:
        url = FEEDS.get(sro.key)
        if url:
            sources.append(RssFeedSource(sro, url))
    return sources


def coverage() -> dict[str, bool]:
    """Which registry keys have an edge source. Surfaced by ``doctor``."""
    from ...registry import SROS

    return {s.key: s.key in FEEDS for s in SROS}
