"""Source framework: every fetcher is isolated and reports its own health.

The rule that keeps this application stable: **no single source may fail the
run.** A source raises whatever it likes; ``run_source`` converts that into a
``SourceResult`` carrying a status and a human-readable reason. The pipeline
then decides what the overall run is worth.

Two tiers:

  ``spine``  authoritative and complete (the SEC). If every spine source fails,
             the run is a failure - there is nothing trustworthy to commit.
  ``edge``   exchange sites, which are fast but fragile. They add freshness.
             An edge failure is a degradation, never an error.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from ..config import Config
from ..http import Client, FetchError, TlsTrustError
from ..models import Filing

log = logging.getLogger(__name__)

TIER_SPINE = "spine"
TIER_EDGE = "edge"

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_FAILED = "failed"


@dataclass(slots=True)
class SourceResult:
    source: str
    tier: str
    status: str
    filings: list[Filing] = field(default_factory=list)
    duration: float = 0.0
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_health(self) -> dict[str, object]:
        return {
            "source": self.source,
            "tier": self.tier,
            "status": self.status,
            "records": len(self.filings),
            "duration": round(self.duration, 2),
            "message": self.message,
        }


class Source(Protocol):
    """What every fetcher must provide."""

    name: str
    tier: str

    empty_is_valid: bool
    """Whether returning zero filings can be a legitimate answer.

    True only for sources that positively verify structure before concluding
    there is nothing there. The SEC spine qualifies: its parser raises if the
    table is missing, so an empty result proves the table existed and was empty
    - which is exactly what a small SRO with no filings this year looks like.

    Sources that cannot tell "nothing to report" from "I no longer understand
    this page" leave this False, and emptiness is treated as breakage.
    """

    def fetch(self, client: Client, cfg: Config) -> Sequence[Filing]:
        """Return filings, or raise. Never returns partial state silently."""
        ...


def run_source(source: Source, client: Client, cfg: Config) -> SourceResult:
    """Execute one source under isolation. This function does not raise."""
    started = time.monotonic()
    try:
        filings = list(source.fetch(client, cfg))
    except TlsTrustError as exc:
        # Distinguished because the remediation is specific and actionable.
        log.error("source %s failed TLS verification", source.name)
        return SourceResult(source.name, source.tier, STATUS_FAILED,
                            duration=time.monotonic() - started, message=str(exc))
    except FetchError as exc:
        log.warning("source %s unreachable: %s", source.name, exc)
        return SourceResult(source.name, source.tier, STATUS_FAILED,
                            duration=time.monotonic() - started, message=str(exc))
    except Exception as exc:  # noqa: BLE001 - isolation is the entire point
        log.exception("source %s raised", source.name)
        return SourceResult(
            source.name, source.tier, STATUS_FAILED,
            duration=time.monotonic() - started,
            message=f"{type(exc).__name__}: {exc}",
        )

    duration = time.monotonic() - started

    if not filings and not getattr(source, "empty_is_valid", False):
        # An empty result from a site that returned HTTP 200 is the classic
        # silent-breakage signature: the page loaded but the parser matched
        # nothing, usually because the markup changed.
        return SourceResult(
            source.name, source.tier, STATUS_DEGRADED, filings, duration,
            "Fetched successfully but parsed 0 filings - the page structure has "
            "probably changed. Check the golden-file test for this source.",
        )

    return SourceResult(source.name, source.tier, STATUS_OK, filings, duration)
