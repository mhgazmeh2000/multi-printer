"""Anti-double-count / anti-omission guard for CI (.github/workflows/tests.yml).

Every test must run through exactly one CI route:
  * pytest-only tests -> marked ``pytest_only`` (CI: ``pytest -m pytest_only``)
  * unittest tests    -> CI: ``python -m unittest discover -s tests -p "test_*.py"``

The check does not run any test. For every module it compares two views:
  * unittest's loader (what ``unittest discover`` would run), and
  * pytest's collection of ``unittest.TestCase`` items with the ``pytest_only``
    marker deselected (what the pytest route would run as well).

Both must match per module. A mismatch means a test is in both routes (counted
twice) or in neither (omitted). Plain pytest functions are not compared: unittest
cannot see them, and that is expected.

Exit codes: 0 = every unittest-style test is in exactly one route, 1 = mismatch,
2 = a test module failed to import or collect.
"""

from __future__ import annotations

import collections
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

import pytest

pytest_hookimpl_trylast = pytest.hookimpl(trylast=True)


def _unittest_counts(tests_dir: Path) -> tuple[collections.Counter, list[str]]:
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(tests_dir), pattern="test_*.py", top_level_dir=str(tests_dir))
    counts: collections.Counter = collections.Counter()
    errors: list[str] = []

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                yield from walk(t)
            else:
                yield t

    for t in walk(suite):
        name = type(t).__name__
        if name == "_FailedTest":
            errors.append(f"unittest could not import: {t.id()}")
            continue
        counts[type(t).__module__] += 1
    return counts, errors


class _PytestCounter:
    """Collects the TestCase items that pytest would run without the pytest_only marker."""

    def __init__(self) -> None:
        self.counts: collections.Counter = collections.Counter()

    # trylast: runs after pytest's own -m deselection, so only items that would really run are seen.
    @pytest_hookimpl_trylast
    def pytest_collection_modifyitems(self, session, config, items):  # pytest hook
        self.counts.clear()
        for item in items:
            cls = getattr(item, "cls", None)
            if cls is not None and isinstance(cls, type) and issubclass(cls, unittest.TestCase):
                self.counts[cls.__module__] += 1


def _pytest_counts(root: Path) -> tuple[collections.Counter, int]:
    counter = _PytestCounter()
    args = [str(root / "tests"), "-m", "not pytest_only", "--collect-only", "-q", "--color=no", "-p", "no:cacheprovider"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = pytest.main(args, plugins=[counter])
    if rc not in (0, 5):
        print(buf.getvalue())  # collection output is only useful when collection failed
    return counter.counts, rc


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    tests_dir = root / "tests"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))  # test modules import core/, web/, ...
    os.chdir(root)

    ut_counts, ut_errors = _unittest_counts(tests_dir)
    py_counts, rc = _pytest_counts(root)

    if rc not in (0, 5):  # 5 = nothing collected
        print(f"pytest collection failed (exit {rc}); see the output above")
        return 2
    if ut_errors:
        print("\n".join(ut_errors))
        return 2

    mismatches = []
    for mod in sorted(set(ut_counts) | set(py_counts)):
        if ut_counts[mod] != py_counts[mod]:
            mismatches.append(f"  {mod}: unittest={ut_counts[mod]} pytest={py_counts[mod]}")

    print(f"unittest-style tests: {sum(ut_counts.values())} (unittest route)")
    print(f"pytest TestCase items outside pytest_only: {sum(py_counts.values())} (pytest route)")
    if mismatches:
        print("MISMATCH: these modules are in both routes or in neither (double count / omission):")
        print("\n".join(mismatches))
        return 1
    print("OK: every unittest-style test is in exactly one route")
    return 0


if __name__ == "__main__":
    sys.exit(main())
