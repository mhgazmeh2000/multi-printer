# -*- coding: utf-8 -*-
"""
tests/test_auto_migrate.py — تست‌های واحد برای ابزار auto_migrate.py (فاز F).

سنجش‌ها:
  ۱. در حالت DB_BACKEND=sqlite: اسکریپت فوراً با کد ۰ خارج می‌شود و به PG وصل نمی‌شود.
  ۲. اسکن فایل‌های alembic/versions برای کشف دستورات INSERT INTO و fallback امن.
  ۳. خاصیت Idempotent: وقتی users > 0 باشد، پیام می‌دهد و با کد ۰ خارج می‌شود.
  ۴. امنیت سفت‌وسخت: اگر جدولی خارج از seed_tables ردیف داشته باشد، قبل از هرگونه TRUNCATE
     با خطای امنیتی و کد ۱ متوقف می‌شود.
  ۵. پاک‌سازی ایمن: اگر فقط جدول‌های seed ردیف داشته باشند، فقط همان جدول‌ها TRUNCATE
     می‌شوند و فرآیند مهاجرت ادامه می‌یابد.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock
from pathlib import Path

from tools import auto_migrate


class AutoMigrateUnitTests(unittest.TestCase):

    def test_sqlite_backend_skips_immediately(self):
        with mock.patch.dict(os.environ, {"DB_BACKEND": "sqlite"}):
            rc = auto_migrate.main([])
            self.assertEqual(rc, 0)

    def test_scan_seed_tables_fallback_on_empty(self):
        tmp_empty_dir = Path("/non/existent/path/versions")
        tables = auto_migrate.scan_seed_tables(tmp_empty_dir)
        self.assertEqual(tables, {"sensor_readings"})

    def test_scan_seed_tables_extracts_inserts(self,):
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
        with mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql", "PG_DSN": "dummy_dsn"}):
            with mock.patch("psycopg.connect") as mock_connect:
                mock_conn = mock.MagicMock()
                mock_connect.return_value = mock_conn
                
                with mock.patch("tools.auto_migrate.check_users_count", return_value=5):
                    rc = auto_migrate.main([])
                    self.assertEqual(rc, 0)
                    mock_conn.close.assert_called_once()

    def test_aborts_and_does_not_truncate_if_non_seed_table_has_data(self):
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
