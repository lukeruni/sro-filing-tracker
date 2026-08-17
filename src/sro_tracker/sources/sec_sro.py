"""Tier 1 - the SEC spine.

sec.gov publishes every SRO rule filing in one table with one layout, addressed
per SRO. That is the whole reason this application can be reliable: there is a
single parser, exercised by every SRO, so a break is loud and immediate rather
than a slow rot across twenty bespoke scrapers.

Parsing rules that matter:

  * Columns are located **by header text**, never by position. If the SEC adds
    or reorders a column, existing extraction keeps working.
  * A missing required header raises ``LayoutChanged`` rather than returning
    empty. Silent emptiness is the failure mode we are engineering against.
  * Pagination is discovered from the rendered pager, then bounded, so a markup
    change cannot turn into an unbounded crawl.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator, Sequence
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import Config
from ..http import Client
from ..models import Filing, derive_status, parse_filing_no
from ..registry import Sro, by_code
from . import TIER_SPINE

log = logging.getLogger(__name__)

SEC_ROOT = "https://www.sec.gov"

# Header text -> canonical field. Matching is case-insensitive on the header's
# visible text with sort-affordance wording removed.
_HEADER_MAP = {
    "release number": "release_number",
    "sec issue date": "filing_date",
    "issue date": "filing_date",
    "date": "filing_date",
    "file number": "filing_no",
    "sro organization": "sro_org",
    "details": "details",
}

_REQUIRED = {"filing_no", "details"}

# Bound on pages fetched per SRO-year. A single SRO files well under 300 times
# in a year, so anything beyond this means the pager was misread.
MAX_PAGES = 40

_SORT_NOISE = re.compile(r"\bsort (?:ascending|descending)\b", re.I)


class LayoutChanged(RuntimeError):
    """The SEC table no longer matches what this parser understands."""


def _header_key(cell) -> str:
    text = cell.get_text(" ", strip=True)
    text = _SORT_NOISE.sub("", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _first_pdf(cell) -> str:
    """The release PDF, preferred over comment-page and exhibit links."""
    hrefs = [a.get("href", "") for a in cell.find_all("a", href=True)]
    for href in hrefs:
        if href.lower().endswith(".pdf") and "-ex" not in href.lower():
            return urljoin(SEC_ROOT, href)
    for href in hrefs:
        if href.lower().endswith(".pdf"):
            return urljoin(SEC_ROOT, href)
    return urljoin(SEC_ROOT, hrefs[0]) if hrefs else ""


def parse_listing(html: str, *, sro: Sro, source_url: str) -> list[Filing]:
    """Extract filings from one SEC listing page.

    Pure function over HTML so it can be tested against committed fixtures with
    no network access.
    """
    soup = BeautifulSoup(html, "html.parser")

    table = None
    for candidate in soup.find_all("table"):
        if candidate.find("thead") and candidate.find("tbody"):
            table = candidate
            break
    if table is None:
        raise LayoutChanged(
            f"No filings table found at {source_url}. The page rendered but "
            f"contains no <table> with a header and body."
        )

    head = table.find("thead")
    columns: dict[int, str] = {}
    for index, cell in enumerate(head.find_all("th")):
        field = _HEADER_MAP.get(_header_key(cell))
        if field:
            columns[index] = field

    missing = _REQUIRED - set(columns.values())
    if missing:
        found = [_header_key(c) for c in head.find_all("th")]
        raise LayoutChanged(
            f"SEC table at {source_url} is missing required column(s) "
            f"{sorted(missing)}. Headers present: {found}. "
            f"Update _HEADER_MAP in sources/sec_sro.py."
        )

    filings: list[Filing] = []
    body = table.find("tbody")
    for row in body.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue

        values: dict[str, str] = {}
        links: dict[str, str] = {}
        for index, cell in enumerate(cells):
            field = columns.get(index)
            if not field:
                continue
            values[field] = cell.get_text(" ", strip=True)
            if field in {"release_number", "details"}:
                links[field] = _first_pdf(cell)

        raw_no = values.get("filing_no", "")
        parsed = parse_filing_no(raw_no)
        if not parsed:
            # Rows without a file number are pager artefacts or notices that do
            # not correspond to a filing. Skipping them is correct, not a loss.
            log.debug("skipping row without a filing number: %r", raw_no[:80])
            continue
        _canonical, code, _year, _seq = parsed

        # Prefer the registry entry implied by the filing number itself; fall
        # back to the SRO whose page we requested.
        owner = by_code(code) or sro

        details = values.get("details", "")
        try:
            filings.append(
                Filing.build(
                    filing_no=raw_no,
                    sro=owner.name,
                    sro_family=owner.family,
                    summary=details,
                    status=derive_status(details),
                    filing_date=values.get("filing_date"),
                    release_number=values.get("release_number", ""),
                    filing_url=links.get("release_number") or links.get("details", ""),
                    source="SEC",
                    source_url=source_url,
                )
            )
        except ValueError as exc:
            log.debug("skipping unusable row: %s", exc)

    return filings


def advertised_page_count(html: str) -> int:
    """Highest page index the pager advertises, as a 1-based count.

    Advisory only - see ``SecSroSource._fetch_year`` for why pagination does not
    depend on this. Note the deliberately loose pattern: pager hrefs arrive
    HTML-escaped (``&amp;page=3``), so anchoring on ``[?&]`` matches nothing and
    silently collapses every SRO to a single page.
    """
    pages = [int(m) for m in re.findall(r"page=(\d+)", html)]
    if not pages:
        return 1
    return min(max(pages) + 1, MAX_PAGES)


class SecSroSource:
    """Fetches one SRO across the configured years."""

    tier = TIER_SPINE

    # ``parse_listing`` raises LayoutChanged when the table is absent or its
    # headers are unrecognisable, so reaching a zero-row result proves the table
    # was found and genuinely had no rows. Smaller SROs routinely file nothing
    # for a year; that is data, not damage.
    empty_is_valid = True

    def __init__(self, sro: Sro) -> None:
        self.sro = sro
        self.name = f"SEC:{sro.key}"

    def fetch(self, client: Client, cfg: Config) -> Sequence[Filing]:
        collected: dict[str, Filing] = {}
        for year in cfg.target_years():
            for filing in self._fetch_year(client, year):
                # Later pages win: the SEC lists newest first, and a filing can
                # legitimately appear twice when it has multiple releases.
                collected.setdefault(filing.filing_no, filing)
        return list(collected.values())

    def _fetch_year(self, client: Client, year: int) -> Iterator[Filing]:
        """Walk every page for one year.

        Pagination is self-terminating: we advance until a page yields no
        filings we have not already seen. That makes the crawl independent of
        the pager's markup, which is the part of the page most likely to be
        redesigned. ``MAX_PAGES`` bounds the loop so a server that ignores the
        ``page`` parameter cannot spin forever.
        """
        base = self.sro.listing_url(year)
        seen: set[str] = set()

        for page in range(MAX_PAGES):
            url = base if page == 0 else f"{base}&page={page}"
            response = client.get(url)
            batch = parse_listing(response.text, sro=self.sro, source_url=url)
            if not batch:
                break

            fresh = [f for f in batch if f.filing_no not in seen]
            if not fresh:
                # Every row repeated - either the last page, or the server is
                # ignoring `page` and re-serving page one.
                break
            seen.update(f.filing_no for f in fresh)
            yield from fresh
        else:
            log.warning(
                "%s hit the %d-page ceiling for %s; results may be truncated.",
                self.name, MAX_PAGES, year,
            )


def build_sources(sros: Sequence[Sro]) -> list[SecSroSource]:
    return [SecSroSource(sro) for sro in sros]
