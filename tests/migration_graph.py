# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/migration_graph.py
# @brief:        Test helper that reads the Alembic migration graph from files.
# @details:      Responsibilities: Parse migration revisions and return the graph and its head.
#                Main API: none (no public functions or classes)
#                Side effects: none at import time
# @dependencies: stdlib only
# @notes:        None.
# =============================================================================

"""
Common helper for tests: reading the graph of Alembic migrations from files.

Why was it necessary: ​​some tests were comparing "migration heads" with a fixed number (`0003`);
With every new migration, those tests would break for no reason (and someone to green the number
was updating — that is, the test was not guarding anything). This head helper **from the files**
Computes so that locks remain meaningful: "executed schema = actual head of chain".
"""
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _field(lines, name):
    """Helper: field."""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{name} ="):
            return stripped.split("=", 1)[1].strip().strip("\"'")
    return None


def migration_graph():
    """``{revision: down_revision}'' — only from file module level variables."""
    graph = {}
    for path in sorted(VERSIONS_DIR.glob("0*.py")):
        revision = _field(path.read_text(encoding="utf-8").splitlines(), "revision")
        parent = _field(path.read_text(encoding="utf-8").splitlines(), "down_revision")
        if not revision:
            raise AssertionError(f"{path.name}: متغیر revision ندارد")
        graph[revision] = None if parent in (None, "None", "") else parent
    return graph


def migration_head() -> str:
    """the head of the chain; If there is more than one end (branching), the test breaks."""
    graph = migration_graph()
    heads = set(graph) - {p for p in graph.values() if p}
    if len(heads) != 1:
        raise AssertionError(
            f"انتظار یک سر داشتیم، شد: {sorted(heads)} (گراف: {graph})")
    return heads.pop()
