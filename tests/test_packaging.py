"""Packaging guards.

These exist because of a real failure: `.gitignore` contained an unanchored
`exports/`, which matched `src/sro_tracker/exports/` as well as the runtime
output directory. The package was therefore never committed, and a fresh clone
installed cleanly and then died on first import.

The rest of the suite could not catch it. pytest puts `src/` on the path
directly, so it tests the working tree rather than what was actually committed.
These tests close that gap.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE_ROOT = SRC / "sro_tracker"
REPO = Path(__file__).resolve().parents[1]


def _package_dirs() -> list[Path]:
    return [p.parent for p in PACKAGE_ROOT.rglob("__init__.py")]


def test_every_subpackage_imports():
    """Catches a package that exists on disk but is missing or broken."""
    for directory in _package_dirs():
        dotted = ".".join(directory.relative_to(SRC).parts)
        importlib.import_module(dotted)


def test_cli_imports_everything_it_declares():
    """The CLI imports the whole application surface at module load, so this
    single import is a load-bearing smoke test for packaging."""
    cli = importlib.import_module("sro_tracker.cli")
    for attribute in ("cmd_refresh", "cmd_serve", "cmd_export", "cmd_report",
                      "cmd_doctor", "cmd_validate", "cmd_sources", "main"):
        assert hasattr(cli, attribute), f"cli.{attribute} is missing"


def test_no_package_directory_is_gitignored():
    """The exact regression: a package silently excluded from the repository."""
    try:
        subprocess.run(["git", "rev-parse", "--git-dir"], cwd=REPO,
                       capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")

    ignored: list[str] = []
    for directory in _package_dirs():
        init = directory / "__init__.py"
        result = subprocess.run(
            ["git", "check-ignore", str(init.relative_to(REPO)).replace("\\", "/")],
            cwd=REPO, capture_output=True, text=True,
        )
        # exit 0 means the path IS ignored, which for a package is a bug.
        if result.returncode == 0:
            ignored.append(str(init.relative_to(REPO)))

    assert not ignored, (
        "these package files are excluded by .gitignore and would be missing "
        f"from a clone: {ignored}. Anchor the offending pattern with a leading "
        "slash so it only matches at the repository root."
    )


def test_package_data_is_present():
    """Templates and static assets ship with the package, not just in dev."""
    web = PACKAGE_ROOT / "web"
    assert (web / "templates" / "index.html").exists()
    assert (web / "templates" / "base.html").exists()
    assert (web / "static" / "styles.css").exists()


def test_declared_dependencies_are_all_pure_python():
    """A compiler-free install is a deliberate constraint, not an accident:
    the target machines are locked down and have no build tools."""
    import tomllib

    with (REPO / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    allowed = {"requests", "beautifulsoup4", "flask", "openpyxl"}
    declared = {
        dep.split(">")[0].split("=")[0].split("[")[0].strip().lower()
        for dep in project["dependencies"]
    }
    assert declared == allowed, (
        f"dependency set changed to {declared}. Anything added here must be "
        f"pure Python, or installs will need a compiler."
    )
