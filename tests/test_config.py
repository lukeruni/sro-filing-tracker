"""Configuration loading.

This is the surface a person (or an assistant) touches on a work machine, so it
has to fail loudly and legibly. Silent misconfiguration is the failure mode
these tests exist to prevent.
"""

from __future__ import annotations

import pytest

from sro_tracker import config


def write(tmp_path, text: str, *, bom: bool = False, name: str = "config.toml"):
    path = tmp_path / name
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    return path


BASIC = """
[identity]
contact = "person@example.com"

[scope]
years = [2025, 2026]
sros = ["nyse", "finra"]

[web]
port = 6001
"""


def test_the_suite_runs_in_an_isolated_environment():
    """Regression: the suite used to pass on a clean machine and fail on a
    configured one. Every machine that actually runs this app has
    SRO_TRACKER_CONTACT set, so three config tests failed there and looked like
    a broken build. `conftest.isolated_environment` clears them."""
    import os

    leaked = [n for n in os.environ if n.startswith("SRO_TRACKER_")]
    assert not leaked, f"application environment leaked into tests: {leaked}"


def test_reads_a_plain_config(tmp_path):
    cfg = config.load(write(tmp_path, BASIC))
    assert cfg.contact == "person@example.com"
    assert cfg.target_years() == (2025, 2026)
    assert cfg.sros == ("nyse", "finra")
    assert cfg.port == 6001
    assert not cfg.problems()


def test_utf8_bom_is_tolerated(tmp_path):
    """PowerShell's `Set-Content -Encoding utf8` and several Windows editors
    emit a BOM. tomllib reads binary and does not skip it, failing with
    "Invalid statement at line 1" - which says nothing about the real cause."""
    cfg = config.load(write(tmp_path, BASIC, bom=True))
    assert cfg.contact == "person@example.com"
    assert cfg.port == 6001


def test_malformed_toml_raises_a_readable_error(tmp_path):
    path = write(tmp_path, "this is not toml at all {{{")
    with pytest.raises(config.ConfigError) as excinfo:
        config.load(path)
    message = str(excinfo.value)
    assert "not valid TOML" in message
    assert str(path) in message, "the message must name the offending file"


def test_section_scoped_aliases_are_honoured(tmp_path):
    """`[mail] transport = ...` is what a person naturally writes. Ignoring it
    would leave delivery silently unconfigured."""
    cfg = config.load(write(tmp_path, """
[identity]
contact = "a@b.com"
[mail]
transport = "smtp"
to = ["team@example.com"]
from = "tracker@example.com"
"""))
    assert cfg.mail_transport == "smtp"
    assert cfg.mail_to == ("team@example.com",)
    assert cfg.mail_from == "tracker@example.com"


def test_the_real_field_names_also_work(tmp_path):
    cfg = config.load(write(tmp_path, """
[identity]
contact = "a@b.com"
[mail]
mail_transport = "smtp"
mail_to = ["team@example.com"]
smtp_host = "smtp.example.com"
"""))
    assert cfg.mail_transport == "smtp"
    assert cfg.smtp_host == "smtp.example.com"


def test_unknown_keys_are_reported_not_ignored(tmp_path):
    cfg = config.load(write(tmp_path, """
[identity]
contact = "a@b.com"
[mail]
recipeints = ["typo@example.com"]
"""))
    warnings = " ".join(cfg.warnings())
    assert "recipeints" in warnings
    assert "[mail]" in warnings


def test_the_shipped_example_config_parses_and_has_no_unknown_keys(tmp_path):
    """Guards the example against drifting away from the real field names."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    text = example.read_text(encoding="utf-8").replace(
        'contact = "you@example.com"', 'contact = "a@b.com"')
    cfg = config.load(write(tmp_path, text))
    assert cfg.unknown_keys == (), f"example config has dead keys: {cfg.unknown_keys}"
    assert cfg.mail_transport == "file"
    assert not cfg.problems()


def test_environment_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SRO_TRACKER_PORT", "7777")
    monkeypatch.setenv(config.CONTACT_ENV, "env@example.com")
    cfg = config.load(write(tmp_path, BASIC))
    assert cfg.port == 7777
    assert cfg.contact == "env@example.com"


def test_missing_contact_is_a_blocking_problem(tmp_path):
    cfg = config.load(write(tmp_path, "[web]\nport = 5057\n"))
    assert any("contact" in p.lower() for p in cfg.problems())


def test_transport_without_recipients_is_flagged(tmp_path):
    cfg = config.load(write(tmp_path, """
[identity]
contact = "a@b.com"
[mail]
transport = "outlook"
"""))
    assert any("mail_to is empty" in w for w in cfg.warnings())


def test_smtp_without_a_host_is_blocking(tmp_path):
    cfg = config.load(write(tmp_path, """
[identity]
contact = "a@b.com"
[mail]
transport = "smtp"
to = ["x@y.com"]
"""))
    assert any("smtp_host" in p for p in cfg.problems())


def test_bad_transport_is_rejected(tmp_path):
    cfg = config.load(write(tmp_path, """
[identity]
contact = "a@b.com"
[mail]
transport = "carrier-pigeon"
"""))
    assert any("mail_transport" in p for p in cfg.problems())


def test_missing_file_is_not_an_error(tmp_path):
    """A fresh clone has no config.toml; env alone must be enough."""
    cfg = config.load(tmp_path / "nope.toml", contact="a@b.com")
    assert cfg.contact == "a@b.com"
    assert not cfg.problems()


def test_non_loopback_host_warns(tmp_path):
    cfg = config.load(write(tmp_path, """
[identity]
contact = "a@b.com"
[web]
host = "0.0.0.0"
"""))
    assert any("loopback" in w for w in cfg.warnings())
