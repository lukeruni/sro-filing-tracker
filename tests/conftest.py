"""Shared fixtures.

Every parser test runs against committed HTML/XML captured from the real sites.
No test in this suite touches the network, so the suite is deterministic and
runnable on a machine with no outbound access.

To refresh a fixture after an upstream redesign:
    python scripts/capture_fixtures.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Environment variables the application reads. Cleared for every test so the
# suite is hermetic.
_APP_ENV_PREFIX = "SRO_TRACKER_"
_APP_ENV_EXTRA = ("REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """Run every test as if no application environment variables were set.

    ``config.load`` reads the real environment, so without this the suite
    passes on a clean machine and fails on a configured one - which is every
    machine that actually runs the app. A developer would see three failures
    and reasonably conclude the build was broken.

    Tests that want environment behaviour set it explicitly with monkeypatch;
    because ``monkeypatch`` is function-scoped and shared, those assignments
    still apply after this fixture has cleared the slate.
    """
    for name in list(os.environ):
        if name.startswith(_APP_ENV_PREFIX) or name in _APP_ENV_EXTRA:
            monkeypatch.delenv(name, raising=False)


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
