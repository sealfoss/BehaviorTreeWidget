"""The version numbers the project states agree with each other (pyproject.toml, the CI
constraints of the oldest versions supported, the package and the changelog)."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest

import behavior_tree_widget

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not (ROOT / "pyproject.toml").is_file(), reason="needs the source tree (pyproject.toml)"
)


def project() -> dict:
    with open(ROOT / "pyproject.toml", "rb") as stream:
        return tomllib.load(stream)["project"]


def minimum_versions() -> dict[str, str]:
    """``{distribution name: version}`` of ``.github/minimum-versions.txt``."""
    pins = {}
    for line in (ROOT / ".github" / "minimum-versions.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, version = line.split("==")
            pins[name.strip().lower().replace("-", "_")] = version.strip()
    return pins


def test_the_ci_tests_the_minimum_of_every_dependency():
    pins = minimum_versions()
    for requirement in project()["dependencies"]:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)\s*>=\s*([0-9.]+)", requirement.split(";")[0].strip())
        assert match, f"{requirement!r} names no minimum version"
        name, minimum = match.group(1).lower().replace("-", "_"), match.group(2)
        assert pins.get(name) == minimum, f"{name}: pyproject.toml says >={minimum}, the CI pins {pins.get(name)}"
    pyside = pins["pyside6"]
    assert pins["pyside6_essentials"] == pins["pyside6_addons"] == pins["shiboken6"] == pyside


def test_the_minimum_python_is_the_one_the_tools_check_for():
    minimum = re.fullmatch(r">=\s*3\.(\d+)", project()["requires-python"])
    assert minimum, project()["requires-python"]
    minor = int(minimum.group(1))
    assert sys.version_info >= (3, minor)
    with open(ROOT / "pyproject.toml", "rb") as stream:
        tools = tomllib.load(stream)["tool"]
    assert tools["ruff"]["target-version"] == f"py3{minor}"
    assert tools["mypy"]["python_version"] == f"3.{minor}"
    assert f"Programming Language :: Python :: 3.{minor}" in project()["classifiers"]


def test_the_changelog_starts_with_the_version_of_the_package():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    first = re.search(r"^## (\S+)", changelog, re.MULTILINE)
    assert first and first.group(1) == behavior_tree_widget.__version__
