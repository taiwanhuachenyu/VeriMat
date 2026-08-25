"""Guard the two claims the runtime makes about its own surface.

The dependency disclosure states that the trusted runtime is standard-library only, and the
cross-platform guarantee only holds while platform primitives stay behind ``src.core.portability``.
Both are structural properties, so they are checked structurally rather than trusted.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_TREES = ("src", "experiments", "examples", "benchmarks")
PLATFORM_MODULES = {"fcntl", "msvcrt", "winreg", "posix", "nt"}
PORTABILITY = ROOT / "src" / "core" / "portability.py"


def _sources() -> list[Path]:
    found: list[Path] = []
    for tree in RUNTIME_TREES:
        found.extend(sorted((ROOT / tree).rglob("*.py")))
    return [path for path in found if "__pycache__" not in path.parts]


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _declared_distributions() -> set[str]:
    """Every distribution pinned by any lock file, normalised to its import name."""
    declared: set[str] = set()
    for lock in sorted(ROOT.glob("requirements-*.lock")):
        for line in lock.read_text(encoding="utf-8").splitlines():
            entry = line.split("#", 1)[0].strip()
            if entry:
                declared.add(entry.split("==")[0].strip().replace("-", "_").lower())
    return declared


def _third_party(path: Path) -> list[str]:
    return sorted(
        root
        for root in _imported_roots(path)
        if root not in sys.stdlib_module_names and root not in {"src", "tools"}
    )


def test_at_least_one_source_file_was_discovered():
    assert len(_sources()) > 20


@pytest.mark.parametrize(
    "path",
    [p for p in _sources() if p.is_relative_to(ROOT / "src")],
    ids=lambda p: str(p.relative_to(ROOT)),
)
def test_trusted_runtime_imports_only_the_standard_library(path: Path):
    assert not _third_party(path), (
        f"{path.relative_to(ROOT)} imports non-stdlib {_third_party(path)}; "
        "the trusted runtime is standard-library only"
    )


@pytest.mark.parametrize(
    "path",
    [p for p in _sources() if not p.is_relative_to(ROOT / "src")],
    ids=lambda p: str(p.relative_to(ROOT)),
)
def test_tooling_imports_are_pinned_by_a_lock_file(path: Path):
    declared = _declared_distributions()
    undeclared = [name for name in _third_party(path) if name.lower() not in declared]
    assert not undeclared, (
        f"{path.relative_to(ROOT)} imports {undeclared}, which no requirements-*.lock pins"
    )


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.relative_to(ROOT)))
def test_platform_primitives_stay_behind_the_portability_layer(path: Path):
    if path == PORTABILITY:
        return
    leaked = sorted(_imported_roots(path) & PLATFORM_MODULES)
    assert not leaked, (
        f"{path.relative_to(ROOT)} imports {leaked} directly; "
        "route platform primitives through src.core.portability"
    )


def test_portability_is_the_only_holder_of_platform_imports():
    holders = [
        path.relative_to(ROOT)
        for path in _sources()
        if _imported_roots(path) & PLATFORM_MODULES
    ]
    assert holders == [PORTABILITY.relative_to(ROOT)]
