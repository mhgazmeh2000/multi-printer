# -*- coding: utf-8 -*-
"""The interval-history boundary selection must return the same rows as the original OR-join.

The attached review reported expensive SQLite plans for the endpoint OR-join.
The rewrite selects boundary ids in a ``picked`` CTE and filters with ``IN``.
These are correctness tests, not evidence of PostgreSQL performance or of the
production timeout's cause. The single limit placeholder and parameter list
pinned by ``tools/yield_query_lab.py`` remain unchanged.
"""

from __future__ import annotations

import os
import random
import tempfile
import unittest
from unittest.mock import patch

import config.settings as settings
from core import database, yield_engine
from core.db import ENV_DB_BACKEND

_NEW_CTE = (
    "        ), picked AS (\n"
    "            SELECT r.first_id, r.last_id FROM recent r WHERE r.changed = 1 AND r.rn <= ?\n"
    "        )\n"
)
_OLD_CTE = "        )\n"
_NEW_FROM = (
    "        FROM toner_snapshots_v2 s\n"
    "        WHERE s.id IN (SELECT first_id FROM picked UNION SELECT last_id FROM picked)\n"
    "        ORDER BY s.id\n"
)
_OLD_FROM = (
    "        FROM toner_snapshots_v2 s JOIN recent r\n"
    "          ON s.id = r.first_id OR s.id = r.last_id\n"
    "        WHERE r.changed = 1 AND r.rn <= ? ORDER BY s.id\n"
)


class YieldHistoryJoinEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "join_equivalence.db")
        self._patches = [
            patch.object(settings, "DB_PATH", self.db_path),
            patch.dict(os.environ, {ENV_DB_BACKEND: "sqlite", "DB_PATH": self.db_path}, clear=False),
        ]
        for p in self._patches:
            p.start()
        database.init_db()
        yield_engine.ensure_yield_tables()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _seed(self, seed: int):
        rnd = random.Random(seed)
        rows = []
        slots = [("10.50.0.1", "black", 1), ("10.50.0.2", "black", 3), ("10.50.0.2", "cyan", 120), ("10.50.0.3", "Black", 60)]
        for ip, color, n in slots:
            counter, level = 1000, 90
            for idx in range(n):
                counter += rnd.randint(0, 9) if rnd.random() > 0.02 else -rnd.randint(1, 50)
                if rnd.random() < 0.1:
                    level = max(5, level - rnd.randint(1, 3))
                raw = level if rnd.random() < 0.7 else level + rnd.randint(-1, 1)
                rows.append((
                    ip, color, counter, level, raw, rnd.choice(["poll", "estimated"]),
                    1 if rnd.random() > 0.05 else 0, f"2026-10-01T{idx // 3600 % 24:02d}:{idx // 60 % 60:02d}:{idx % 60:02d}Z",
                    rnd.choice(["k-a", "k-b"]), counter, None,
                ))
        with database.db_connection(commit=True) as conn:
            conn.executemany(
                """
                INSERT INTO toner_snapshots_v2
                (printer_ip, color, usage_counter, toner_level, raw_level, source, valid, timestamp, cartridge_key, print_total, full_color)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def test_new_selection_returns_same_rows_as_original_or_join(self):
        self._seed(7)
        with database.db_connection() as conn:
            for limit in (1, 3, 400):
                sql, params = yield_engine._interval_history_query(conn, ip=None, limit=limit)
                self.assertIn(_NEW_CTE, sql, "expected the rewritten boundary CTE")
                self.assertIn(_NEW_FROM, sql, "expected the rewritten FROM/WHERE")
                self.assertEqual(sql.count("?"), len(params), "placeholders must match parameters")
                old_sql = sql.replace(_NEW_CTE, _OLD_CTE).replace(_NEW_FROM, _OLD_FROM)
                self.assertEqual(old_sql.count("?"), len(params))
                new_rows = [tuple(r) for r in conn.execute(sql, params).fetchall()]
                old_rows = [tuple(r) for r in conn.execute(old_sql, params).fetchall()]
                self.assertEqual(new_rows, old_rows, f"limit={limit}")
                self.assertTrue(new_rows, f"limit={limit} produced no rows")

    def test_single_row_run_is_returned_once(self):
        """A one-row run has first_id == last_id; the UNION must not duplicate or drop it."""
        self._seed(11)
        with database.db_connection() as conn:
            sql, params = yield_engine._interval_history_query(conn, ip="10.50.0.1", limit=400)
            rows = conn.execute(sql, params).fetchall()
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM toner_snapshots_v2 WHERE printer_ip='10.50.0.1' ORDER BY id").fetchall()]
            self.assertEqual(len(ids), 1)
            self.assertEqual(len(rows), 1, "single-row slot must yield exactly one boundary row")


if __name__ == "__main__":
    unittest.main()
