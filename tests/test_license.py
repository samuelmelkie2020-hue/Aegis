"""Licensing and release metadata.

`pyproject.toml` claimed MIT for a long time with no LICENSE file in the repo,
which legally means "all rights reserved" — the opposite of what it said. These
checks keep the claim and the file together.
"""

from __future__ import annotations

from pathlib import Path

try:  # tomllib is stdlib from 3.11; the CI matrix still runs 3.10.
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent


def test_license_file_exists_and_is_mit():
    license_file = ROOT / "LICENSE"
    assert license_file.is_file(), "LICENSE is missing"
    text = license_file.read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "WITHOUT WARRANTY OF ANY KIND" in text
    assert "Copyright" in text


def test_packaging_metadata_points_at_the_file():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"]["file"] == "LICENSE"


def test_readme_mentions_the_license():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "mit" in readme or "license" in readme
