# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_auto_migrate.py
# @brief:        Unit tests for the auto_migrate tool.
# @details:      Responsibilities: Check the auto-migration logic in isolation.
#                Main API: AutoMigrateUnitTests
#                Side effects: file I/O
# @dependencies: internal: tools
# @notes:        Can be run directly as a script.
# =============================================================================

"""
tests/test_auto_migrate.py — Unit tests for the auto_migrate.py tool (Phase F).

Measurements:
  1. In the case of DB_BACKEND=sqlite: the script immediately exits with code 0 and does not connect to PG.
  2. Scan alembic/versions files to detect safe INSERT INTO and fallback commands.
  3. Idempotent property: when users > 0, it sends a message and exits with code 0.
  4. Strict security: if a table has rows outside of seed_tables, before any TRUNCATE
     It stops with security error and code 1.
  5. Safe cleanup: If only seed tables have rows, TRUNCATE only those tables
     and the immigration process continues.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from tools import auto_migrate


class AutoMigrateUnitTests(unittest.TestCase):

    """Tests for auto migrate unit."""
    def test_sqlite_backend_skips_immediately(self):
        """Verifies sqlite backend skips immediately."""
        with mock.patch.dict(os.environ, {"DB_BACKEND": "sqlite"}):
            rc = auto_migrate.main([])
            self.assertEqual(rc, 0)

    def test_scan_seed_tables_fallback_on_empty(self):
        """Verifies scan seed tables fallback on empty."""
        tmp_empty_dir = Path("/non/existent/path/versions")
        tables = auto_migrate.scan_seed_tables(tmp_empty_dir)
        self.assertEqual(tables, {"sensor_readings"})

    def test_scan_seed_tables_extracts_inserts(self,):
        """Verifies scan seed tables extracts inserts."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            v_dir = Path(tmpdir)
            f1 = v_dir / "0001_test.py"
            f1.write_text("op.execute(\"INSERT INTO test_seed (id) VALUES (1)\")", encoding="utf-8")
            f2 = v_dir / "0002_test.py"
            f2.write_text("INSERT INTO \"custom_lookup\" (k, v) VALUES ('a', 'b')", encoding="utf-8")

            tables = auto_migrate.scan_seed_tables(v_dir)
            self.assertIn("test_seed", tables)
            self.assertIn("custom_lookup", tables)

    def test_idempotent_when_users_already_exist(self):
        """Verifies idempotent when users already exist."""
        with mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql", "PG_DSN": "dummy_dsn"}):
            with mock.patch("psycopg.connect") as mock_connect:
                mock_conn = mock.MagicMock()
                mock_connect.return_value = mock_conn

                with mock.patch("tools.auto_migrate.check_users_count", return_value=5):
                    rc = auto_migrate.main([])
                    self.assertEqual(rc, 0)
                    mock_conn.close.assert_called_once()

    def test_aborts_and_does_not_truncate_if_non_seed_table_has_data(self):
        """Verifies aborts and does not truncate if non seed table has data."""
        with mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql", "PG_DSN": "dummy_dsn"}):
            with mock.patch("psycopg.connect") as mock_connect:
                mock_conn = mock.MagicMock()
                mock_cur = mock.MagicMock()
                mock_conn.cursor.return_value.__enter__.return_value = mock_cur
                mock_connect.return_value = mock_conn

                # users count is 0
                with mock.patch("tools.auto_migrate.check_users_count", return_value=0):
                    # user tables in PG
                    with mock.patch("tools.auto_migrate.get_user_tables", return_value=["logs", "sensor_readings"]):
                        with mock.patch("tools.auto_migrate.scan_seed_tables", return_value={"sensor_readings"}):
                            # logs has 10 rows, sensor_readings has 0
                            def mock_execute(sql):
                                """Helper: mock execute."""
                                if '"logs"' in sql:
                                    mock_cur.fetchone.return_value = [10]
                                elif '"sensor_readings"' in sql:
                                    mock_cur.fetchone.return_value = [0]
                            mock_cur.execute.side_effect = mock_execute

                            rc = auto_migrate.main([])
                            # Should abort with code 1
                            self.assertEqual(rc, 1)
                            # TRUNCATE must NEVER have been called!
                            for call in mock_cur.execute.call_args_list:
                                self.assertNotIn("TRUNCATE", call[0][0])


if __name__ == "__main__":
    unittest.main()
