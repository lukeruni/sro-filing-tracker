"""Capture live pages as test fixtures.

Run this when an upstream site changes and a golden-file test starts failing.
Inspect the diff before committing: a fixture update should be a deliberate
acknowledgement that the site changed, never a reflex to make tests green.

    python scripts/capture_fixtures.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sro_tracker import config, registry  # noqa: E402
from sro_tracker.http import Client  # noqa: E402
from sro_tracker.sources.exchange import FEEDS  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

TARGETS = {
    "sec_nyse_listing.html": registry.get("nyse").listing_url(2026),
    "sec_finra_listing.html": registry.get("finra").listing_url(2026),
    "cboe_bzx_feed.xml": FEEDS["cboe-bzx"],
}


def main() -> int:
    cfg = config.load()
    if cfg.problems():
        for problem in cfg.problems():
            print(f"  x {problem}", file=sys.stderr)
        return 3

    FIXTURES.mkdir(parents=True, exist_ok=True)
    with Client(cfg) as client:
        for name, url in TARGETS.items():
            print(f"fetching {name} <- {url}")
            response = client.get(url)
            (FIXTURES / name).write_text(response.text, encoding="utf-8")
            print(f"  wrote {len(response.text):,} bytes")
    print(f"\nFixtures written to {FIXTURES}")
    print("Review the diff before committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
