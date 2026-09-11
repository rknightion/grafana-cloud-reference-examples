"""Every vendored source file must parse on the oldest runtime the repo offers.

The shared library is copied verbatim into an example's deployment package, so
its syntax floor is whichever Python runtime an example is allowed to select -
not the local interpreter, and not the newest runtime.

This exists because the formatter got this wrong once and nothing noticed. At
``target-version = "py314"`` ruff rewrites ``except (A, B):`` into PEP 758
``except A, B:``, which is valid on 3.14 and a SyntaxError on 3.12 and 3.13. The
whole gate stayed green because every check ran on 3.14.

The floor is read from ``common/cloudformation/conformance.yaml`` rather than
hardcoded, so retiring a runtime automatically raises it and there is no second
copy of the fact to drift.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFORMANCE = REPO_ROOT / "common" / "cloudformation" / "conformance.yaml"

# Directories whose contents are vendored into a deployment package, plus the
# tools, which run on a developer's or CI's interpreter and are held to the same
# floor so a contributor on an older Python can still build a release.
VENDORED_ROOTS = (
    REPO_ROOT / "common" / "python" / "src",
    REPO_ROOT / "examples",
    REPO_ROOT / "tools",
)

_EXCLUDED_PARTS = {".venv", ".tools", "dist", "build", "__pycache__", ".terraform"}


def oldest_supported_python() -> tuple[int, int]:
    """The lowest GA python runtime an example may select."""
    conformance = yaml.safe_load(CONFORMANCE.read_text(encoding="utf-8"))
    versions: list[tuple[int, int]] = []
    for identifier in conformance["runtimes"]["ga"]:
        match = re.fullmatch(r"python(\d+)\.(\d+)", identifier)
        if match:
            versions.append((int(match.group(1)), int(match.group(2))))
    if not versions:
        pytest.fail("conformance.yaml lists no GA python runtime")
    return min(versions)


def python_sources() -> list[Path]:
    found: list[Path] = []
    for root in VENDORED_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if _EXCLUDED_PARTS.isdisjoint(path.parts):
                found.append(path)
    return found


SOURCES = python_sources()
FLOOR = oldest_supported_python()


def test_the_floor_is_discoverable() -> None:
    """Guards the guard: a conformance.yaml rename would otherwise make every
    parametrised case below vanish and the suite still pass."""
    assert FLOOR >= (3, 9)
    assert len(SOURCES) > 10, f"only found {len(SOURCES)} python files to check"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_parses_on_the_oldest_supported_runtime(path: Path) -> None:
    major, minor = FLOOR
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=FLOOR)
    except SyntaxError as exc:
        pytest.fail(
            f"{path.relative_to(REPO_ROOT)}:{exc.lineno} does not parse on "
            f"python{major}.{minor}, the oldest runtime "
            f"common/cloudformation/conformance.yaml offers: {exc.msg}. "
            f"Either use syntax that version supports, or retire that runtime "
            f"from the allowlist and raise tool.ruff.target-version to match."
        )


def test_the_ruff_target_version_matches_the_floor() -> None:
    """A mismatch here is how the formatter silently introduced newer syntax."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^target-version\s*=\s*"py(\d)(\d+)"', pyproject, re.MULTILINE)
    assert match is not None, "tool.ruff.target-version is not set"
    configured = (int(match.group(1)), int(match.group(2)))
    assert configured == FLOOR, (
        f"tool.ruff.target-version is py{configured[0]}{configured[1]} but the oldest GA "
        f"python runtime in conformance.yaml is python{FLOOR[0]}.{FLOOR[1]}. The formatter "
        f"rewrites code to its target version, so a target above the floor emits syntax "
        f"that fails on a runtime an example is allowed to pick."
    )
