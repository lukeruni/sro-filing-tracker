"""The record contract.

Every source, no matter how it fetches, must emit a ``Filing``. This module is
the single place that defines what a filing *is*, how its fields are normalized,
and what makes one valid. Nothing downstream (store, exports, dashboard, report)
may invent its own shape.

Design rules:
  * ``filing_no`` is the natural key. It is the one identifier both the SEC and
    the exchanges publish, which is what lets the two tiers reconcile.
  * Normalization happens exactly once, at construction, via ``Filing.build``.
    Sources hand over raw strings; they never pre-clean.
  * The record is frozen. Reconciliation produces new records rather than
    mutating existing ones, so a merge can never half-apply.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import re
from typing import Iterable

# --------------------------------------------------------------------------
# Filing status vocabulary
# --------------------------------------------------------------------------
# Derived from the SEC release title. Order matters: the first pattern that
# matches wins, so the more specific lifecycle events are listed before the
# generic "notice of filing".

STATUS_APPROVED = "Approved"
STATUS_NOTICE = "Notice"
STATUS_IMMEDIATELY_EFFECTIVE = "Immediately Effective"
STATUS_WITHDRAWN = "Withdrawn"
STATUS_SUSPENDED = "Suspended"
STATUS_DISAPPROVED = "Disapproved"
STATUS_PROCEEDINGS = "Proceedings Instituted"
STATUS_EXTENDED = "Period Extended"
STATUS_AMENDED = "Amended"
STATUS_UNKNOWN = "Unknown"

_STATUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\border\s+.*\bdisapprov", re.I), STATUS_DISAPPROVED),
    (re.compile(r"\bdisapprov", re.I), STATUS_DISAPPROVED),
    (re.compile(r"\bwithdraw", re.I), STATUS_WITHDRAWN),
    # "Suspension of and Order Instituting Proceedings ..." is a single combined
    # SEC action. Proceedings is the operative state - the filing is now under
    # formal review - so it is matched before the standalone suspension case.
    (re.compile(r"\binstitut\w*\s+proceedings", re.I), STATUS_PROCEEDINGS),
    (re.compile(r"\bproceedings\s+to\s+determine", re.I), STATUS_PROCEEDINGS),
    # Matches both "suspend" and "suspension"; `\bsuspend` alone silently missed
    # every real SEC title, which all use the noun form.
    (re.compile(r"\bsuspen(?:d|sion)", re.I), STATUS_SUSPENDED),
    (re.compile(r"\bdesignation\s+of\s+a?\s*longer\s+period", re.I), STATUS_EXTENDED),
    (re.compile(r"\bextend\w*\s+.*\bperiod", re.I), STATUS_EXTENDED),
    (re.compile(r"\bgranting\s+.*\bapproval", re.I), STATUS_APPROVED),
    (re.compile(r"\border\s+approving", re.I), STATUS_APPROVED),
    (re.compile(r"\bapprov", re.I), STATUS_APPROVED),
    (re.compile(r"\bimmediate\s+effectiveness", re.I), STATUS_IMMEDIATELY_EFFECTIVE),
    (re.compile(r"\bamendment\s+no", re.I), STATUS_AMENDED),
    (re.compile(r"\bnotice\s+of\s+filing", re.I), STATUS_NOTICE),
)

# Lifecycle precedence. When the same filing_no is seen with several statuses
# across runs or sources, the highest-ranked status is the current one. This is
# what stops a stale "Notice" row from overwriting a later "Approved".
STATUS_RANK: dict[str, int] = {
    STATUS_UNKNOWN: 0,
    STATUS_NOTICE: 1,
    STATUS_IMMEDIATELY_EFFECTIVE: 2,
    STATUS_AMENDED: 3,
    STATUS_EXTENDED: 4,
    STATUS_PROCEEDINGS: 5,
    STATUS_SUSPENDED: 6,
    STATUS_WITHDRAWN: 7,
    STATUS_DISAPPROVED: 8,
    STATUS_APPROVED: 9,
}


def derive_status(title: str | None) -> str:
    """Map an SEC release title onto the status vocabulary."""
    if not title:
        return STATUS_UNKNOWN
    for pattern, status in _STATUS_PATTERNS:
        if pattern.search(title):
            return status
    return STATUS_UNKNOWN


# --------------------------------------------------------------------------
# Filing-number parsing
# --------------------------------------------------------------------------
# Real-world examples this must handle:
#   SR-NYSE-2026-38        SR-NASDAQ-2026-064     SR-CboeBZX-2026-065
#   SR-PEARL-2026-35       SR-MEMX-2026-22        SR-IEX-2026-26
#   SR-FINRA-2026-011      SR-NYSEARCA-2025-9
_FILING_NO_RE = re.compile(
    r"\bSR[-\s]?([A-Za-z0-9]+)[-\s]?(\d{4})[-\s]?(\d{1,4})\b", re.I
)


def parse_filing_no(text: str | None) -> tuple[str, str, int, int] | None:
    """Extract ``(canonical, sro_code, year, sequence)`` from free text.

    Canonical form is ``SR-<CODE>-<YEAR>-<SEQ>`` with the sequence's original
    zero padding preserved, because that is how the SEC and the exchanges both
    print it and how a human will search for it.
    """
    if not text:
        return None
    m = _FILING_NO_RE.search(text)
    if not m:
        return None
    code, year, seq = m.group(1), m.group(2), m.group(3)
    canonical = f"SR-{code}-{year}-{seq}"
    return canonical, code.upper(), int(year), int(seq)


def filing_sort_key(filing_no: str) -> tuple[str, int, int]:
    """Sort key that orders by SRO, then year, then numeric sequence.

    Naive string sort puts ``-100`` before ``-9``; this does not.
    """
    parsed = parse_filing_no(filing_no)
    if not parsed:
        return (filing_no.upper(), 0, 0)
    _, code, year, seq = parsed
    return (code, year, seq)


# --------------------------------------------------------------------------
# Date parsing
# --------------------------------------------------------------------------

_DATE_FORMATS = (
    "%b %d, %Y",      # Aug 14, 2026   <- SEC listing format
    "%B %d, %Y",      # August 14, 2026
    "%Y-%m-%d",       # 2026-08-14     <- ISO / our own storage
    "%m/%d/%Y",       # 08/14/2026     <- common exchange format
    "%m/%d/%y",
    "%d %B %Y",
    "%Y/%m/%d",
)


def parse_date(text: str | None) -> dt.date | None:
    """Parse the date formats seen across the SEC and exchange sites."""
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", str(text)).strip()
    if not cleaned:
        return None
    # Trim trailing noise such as "Aug 14, 2026 (updated)".
    cleaned = re.sub(r"\s*\(.*?\)\s*$", "", cleaned)
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    # Last resort: a bare ISO date embedded in a longer string.
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", cleaned)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Text normalization
# --------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
# The SEC details cell carries the release title followed by page furniture:
# comment deadlines, "See Also" exhibit links, and a "Submit a Comment" call to
# action. The deadline is useful context so it is kept in `notes`; none of it
# belongs in the summary, where it would pollute every export and search.
_COMMENT_TAIL_RE = re.compile(
    r"\s*(?:Comments?\s+Due:|See\s+Also\s*[-–]|Submit\s+a\s+Comment\b).*$",
    re.I | re.S,
)


def clean_text(value: str | None) -> str:
    """Collapse whitespace and strip HTML entities left by crude extraction."""
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\xa0", " ").replace("&nbsp;", " ")
    text = text.replace("&amp;", "&").replace("&#039;", "'").replace("&quot;", '"')
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    return _WS_RE.sub(" ", text).strip()


def split_summary(raw: str | None) -> tuple[str, str]:
    """Return ``(summary, trailing_notes)`` from an SEC details cell."""
    text = clean_text(raw)
    if not text:
        return "", ""
    m = _COMMENT_TAIL_RE.search(text)
    if not m:
        return text, ""
    return text[: m.start()].strip(" .;"), text[m.start():].strip()


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Filing:
    """One SRO rule filing, normalized.

    ``filing_no`` is the natural key. ``sro`` and ``sro_family`` come from the
    registry rather than from scraped text, so display names stay stable even
    when a site rewords its own headers.
    """

    filing_no: str
    sro: str
    sro_family: str
    filing_year: int
    summary: str
    status: str
    filing_date: dt.date | None
    release_number: str = ""
    filing_url: str = ""
    source: str = ""
    source_url: str = ""
    notes: str = ""

    # Provenance. Set by the pipeline, not by sources.
    first_seen: dt.datetime | None = None
    last_seen: dt.datetime | None = None
    seen_by: tuple[str, ...] = ()

    # ---- construction ---------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        filing_no: str,
        sro: str,
        sro_family: str,
        summary: str = "",
        status: str | None = None,
        filing_date: object = None,
        release_number: str = "",
        filing_url: str = "",
        source: str = "",
        source_url: str = "",
        notes: str = "",
        filing_year: int | None = None,
    ) -> "Filing":
        """Normalize raw source output into a valid record.

        Raises ``ValueError`` if the filing number is unusable, because a record
        without a natural key cannot be reconciled and must not enter the store.
        """
        parsed = parse_filing_no(filing_no)
        if not parsed:
            raise ValueError(f"unparseable filing number: {filing_no!r}")
        canonical, _code, year, _seq = parsed

        summary_text, tail = split_summary(summary)
        if tail and not notes:
            notes = tail

        date_value: dt.date | None
        if isinstance(filing_date, dt.datetime):
            date_value = filing_date.date()
        elif isinstance(filing_date, dt.date):
            date_value = filing_date
        else:
            date_value = parse_date(filing_date if filing_date is None else str(filing_date))

        return cls(
            filing_no=canonical,
            sro=clean_text(sro),
            sro_family=clean_text(sro_family),
            # The year comes from the filing number, not the date. The SEC's
            # own year filter keys on release date, so SR-NYSE-2025-43 can be
            # released in 2026; it is still a 2025 filing and staff will look
            # for it under 2025.
            filing_year=filing_year or year,
            summary=summary_text,
            status=status or derive_status(summary_text),
            filing_date=date_value,
            release_number=clean_text(release_number),
            filing_url=clean_text(filing_url),
            source=clean_text(source),
            source_url=clean_text(source_url),
            notes=clean_text(notes),
        )

    # ---- derived --------------------------------------------------------

    @property
    def content_hash(self) -> str:
        """Hash of the substantive fields, ignoring provenance.

        Used to decide whether a re-scrape actually changed anything, which is
        what keeps the change log signal and not noise.
        """
        payload = "|".join(
            (
                self.filing_no,
                self.sro,
                self.summary,
                self.status,
                self.filing_date.isoformat() if self.filing_date else "",
                self.release_number,
                self.filing_url,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def with_provenance(
        self,
        *,
        first_seen: dt.datetime | None = None,
        last_seen: dt.datetime | None = None,
        seen_by: Iterable[str] | None = None,
    ) -> "Filing":
        return dataclasses.replace(
            self,
            first_seen=first_seen if first_seen is not None else self.first_seen,
            last_seen=last_seen if last_seen is not None else self.last_seen,
            seen_by=tuple(seen_by) if seen_by is not None else self.seen_by,
        )

    def to_row(self) -> dict[str, object]:
        """Flat dict for CSV/Excel/JSON. Column order is the contract."""
        return {
            "filing_no": self.filing_no,
            "sro": self.sro,
            "sro_family": self.sro_family,
            "filing_year": self.filing_year,
            "filing_date": self.filing_date.isoformat() if self.filing_date else "",
            "status": self.status,
            "summary": self.summary,
            "release_number": self.release_number,
            "filing_url": self.filing_url,
            "source": self.source,
            "source_url": self.source_url,
            "notes": self.notes,
            "first_seen": self.first_seen.isoformat(sep=" ", timespec="seconds") if self.first_seen else "",
            "last_seen": self.last_seen.isoformat(sep=" ", timespec="seconds") if self.last_seen else "",
            "seen_by": ", ".join(self.seen_by),
        }


# The canonical column order, exported once so CSV, Excel, the API and the
# dashboard cannot drift apart.
COLUMNS: tuple[str, ...] = (
    "filing_no",
    "sro",
    "sro_family",
    "filing_year",
    "filing_date",
    "status",
    "summary",
    "release_number",
    "filing_url",
    "source",
    "source_url",
    "notes",
    "first_seen",
    "last_seen",
    "seen_by",
)
