"""One version number, checked everywhere it is used.

The release workflow tags a commit; the app reports its version in /api/health,
the window title, the self-check and the macOS bundle. When those drift, a bug
report names a version that never shipped — which is what happened at v1.3.0
while the package still said 1.0.0.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:  # tomllib is stdlib from 3.11; the CI matrix still runs 3.10.
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent


def declared_version() -> str:
    return (ROOT / "aegis" / "__init__.py").read_text(encoding="utf-8").split(
        "__version__ = "
    )[1].splitlines()[0].strip().strip('"').strip("'")


def test_version_is_dynamic_in_pyproject():
    """pyproject must not restate the version; it reads the package."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "version" not in project, "the version is declared twice"
    assert "version" in project["dynamic"]
    dynamic = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert dynamic["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "aegis.__version__"


def test_version_is_a_plausible_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", declared_version())


def test_health_reports_the_package_version(client):
    http, auth = client
    assert http.get("/api/health", headers=auth).json()["version"] == declared_version()


def test_no_hardcoded_version_in_the_spec():
    spec = (ROOT / "packaging" / "aegis.spec").read_text(encoding="utf-8")
    assert "_aegis_version()" in spec, "the bundle hardcodes a version again"


def test_readme_test_count_matches_the_suite():
    """The README advertises a test count; stale numbers undermine it."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"# (\d+) tests", readme)
    assert match, "README no longer states a test count"
    collected = 0

    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    collected = sum(1 for line in out.splitlines() if "::" in line)
    assert collected == int(match.group(1)), (
        f"README says {match.group(1)} tests, there are {collected}"
    )
