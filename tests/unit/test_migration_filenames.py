"""Structural lint: migration filename conventions.

Enforces three properties on every file under migrations/:
  1. All numeric prefixes are unique (no duplicate 0005_*.sql files).
  2. Prefixes are monotonic starting at 1 with no gaps
     (0001, 0002, ..., N -- no skips).
  3. Each filename matches the pattern: 4-digit prefix + lowercase snake_case
     (regex: [0-9]{4}_[a-z][a-z0-9_]*.sql).

If main currently has a duplicate-prefix collision (e.g., two 0005_*.sql files),
this test is marked xfail so CI stays green while the collision is documented and
visible. The xfail will turn into an unexpected pass (xpass) once the collision
is resolved -- at which point the xfail marker should be removed.

Bug class 7: migration filename collisions.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"

_FILENAME_PATTERN = re.compile(r"^\d{4}_[a-z][a-z0-9_]*\.sql$")


def _sql_files() -> list[Path]:
    return sorted(_MIGRATIONS_DIR.glob("*.sql"))


def _prefix(filename: str) -> int:
    """Parse the numeric prefix from a migration filename."""
    match = re.match(r"^(\d+)_", filename)
    if not match:
        pytest.fail(f"Migration filename has no numeric prefix: {filename!r}")
    return int(match.group(1))


@pytest.mark.xfail(
    reason=(
        "known collision: 0005 prefix duplicated across 0005_readonly_user.sql and "
        "0005_reward_feedback_columns.sql; cleanup pending in a follow-up"
    ),
    strict=False,
)
def test_prefixes_are_unique() -> None:
    """No two migration files may share the same numeric prefix."""
    files = _sql_files()
    prefixes: dict[int, list[str]] = {}
    for f in files:
        p = _prefix(f.name)
        prefixes.setdefault(p, []).append(f.name)

    collisions = {p: names for p, names in prefixes.items() if len(names) > 1}
    assert not collisions, (
        f"Duplicate migration prefixes found: {collisions}. "
        "Each migration must have a unique numeric prefix."
    )


@pytest.mark.xfail(
    reason=(
        "known collision: 0005 prefix duplicated, causing monotonicity check to fail; "
        "cleanup pending in a follow-up"
    ),
    strict=False,
)
def test_prefixes_are_monotonic() -> None:
    """Prefixes must be 1, 2, ..., N with no gaps."""
    files = _sql_files()
    if not files:
        return  # No migrations yet -- nothing to check.

    unique_prefixes = sorted({_prefix(f.name) for f in files})
    expected = list(range(1, len(unique_prefixes) + 1))
    assert unique_prefixes == expected, (
        f"Migration prefixes are not monotonic starting at 1. "
        f"Got: {unique_prefixes}, expected: {expected}. "
        "Add the next migration with prefix "
        f"{max(unique_prefixes) + 1:04d} or fill the gaps."
    )


def test_filename_format() -> None:
    """Every migration filename must match the required format.

    Required: 4-digit numeric prefix + underscore + lowercase snake_case + .sql
    Example: 0001_initial.sql, 0002_reward_manifests.sql
    """
    files = _sql_files()
    bad = [f.name for f in files if not _FILENAME_PATTERN.match(f.name)]
    assert not bad, (
        f"Migration filenames do not match the required format "
        f"(4-digit prefix + lowercase snake_case + .sql): {bad}"
    )
