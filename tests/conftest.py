"""Shared fixtures.

Every parser test runs against committed HTML/XML captured from the real sites.
No test in this suite touches the network, so the suite is deterministic and
runnable on a machine with no outbound access.

To refresh a fixture after an upstream redesign:
    python scripts/capture_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"fixture {name} not captured; run scripts/capture_fixtures.py")
    return path.read_text(encoding="utf-8")


@pytest.fixture
def sec_listing_html() -> str:
    return load_fixture("sec_nyse_listing.html")


@pytest.fixture
def sec_finra_html() -> str:
    return load_fixture("sec_finra_listing.html")


@pytest.fixture
def cboe_feed_xml() -> str:
    return load_fixture("cboe_bzx_feed.xml")


@pytest.fixture
def tmp_store(tmp_path):
    from sro_tracker.store import Store

    with Store(tmp_path / "test.db") as store:
        yield store


@pytest.fixture
def cfg(tmp_path):
    from sro_tracker import config

    return config.Config(contact="tests@example.invalid", root=tmp_path)
