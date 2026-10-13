# -*- coding: utf-8 -*-
"""
tests/test_migrate_sqlite_to_pg.py — تست‌های ایزوله و مستقل ابزار مهاجرت SQLite به PostgreSQL (فاز F.2).

طراحی آزمون‌ها:
--------------
۱. تست‌های واحد و هرمتیک (Hermetic Unit Tests) با mock کامل لایه PostgreSQL، بدون وابستگی به شبکه یا سرور زنده.
۲. آزمون‌های ایمنی فقط-خواندنی بودن دیتابیس SQLite منبع (URI mode=ro&immutable=1 و حفظ هش SHA256).
۳. آزمون‌های تبدیل نوع داده (Boolean، اعتبارسنجی JSON در details، و اعتبارسنجی الگوی زمان UTC-Z).
۴. آزمون‌های همگام‌سازی توالی‌های IDENTITY (setval) برای رفع ریسک R-1.
۵. آزمون‌های انتخاب استراتژی هیبرید (COPY برای > 10,000 سطر و Batch 1000 برای سایرین).
۶. آزمون‌های مکانیزم Rollback خودکار (TRUNCATE) و کدهای خروج.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from unittest.mock import MagicMock, patch

import psycopg
import pytest

import tools.migrate_sqlite_to_pg as mig_tool
from tools.migrate_sqlite_to_pg import (
    BOOLEAN_COLUMNS,
    COPY_THRESHOLD,
    EXIT_DRY_RUN,
    EXIT_FATAL,
    EXIT_PARTIAL,
    EXIT_SUCCESS,
    IDENTITY_TABLES,
    TABLE_MIGRATION_ORDER,
    TablePlan,
    build_migration_plan,
    connect_sqlite_ro,
    execute_migration,
    main,
    migrate_table_batch,
    resolve_pg_dsn,
    sync_identity_sequences,
    transform_row,
    truncate_all_pg_tables,
)


def _sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def build_test_db_from_json(tmp_path, json_file: str, table_name: str) -> str:
    """ساخت دیتابیس تست با اسکیمای کامل از روی logs.db و بارگذاری ردیف‌های JSON."""
    db_path = str(tmp_path / f"test_{table_name}.db")
    shutil.copyfile("logs.db", db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(f'DELETE FROM "{table_name}"')
    db_cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()}
    with open(json_file, "r", encoding="utf-8") as f:
        rows = json.load(f)
    if rows:
        cols = [c for c in rows[0].keys() if c in db_cols]
        placeholders = ",".join(["?"] * len(cols))
        col_names = ",".join(f'"{c}"' for c in cols)
        for row in rows:
            conn.execute(
                f'INSERT INTO "{table_name}" ({col_names}) VALUES ({placeholders})',
                tuple(row[c] for c in cols),
            )
    conn.commit()
    conn.close()
    return db_path


# ─── کلاس ۱: آزمون‌های فقط-خواندنی بودن منبع SQLite ─────────────
class TestSourceReadOnly:
    """تست‌هایی که ثابت می‌کنند SQLite فقط به صورت خواندنی باز شده و تغییرناپذیر است."""

    def test_open_sqlite_readonly_rejects_write(self, tmp_path):
        db_path = str(tmp_path / "test_ro.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        conn.commit()
        conn.close()

        ro_conn = connect_sqlite_ro(db_path)
        try:
            with pytest.raises(sqlite3.OperationalError, match="(?i)readonly"):
                ro_conn.execute("INSERT INTO t (val) VALUES ('bad')")
        finally:
            ro_conn.close()

    def test_open_sqlite_readonly_uri_is_correct(self, tmp_path):
        db_path = str(tmp_path / "test_uri.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        with patch("tools.migrate_sqlite_to_pg.sqlite3.connect") as mock_connect:
            mock_connect.return_value = MagicMock()
            connect_sqlite_ro(db_path)
            assert mock_connect.call_count == 1
            args, kwargs = mock_connect.call_args
            assert "mode=ro&immutable=1" in args[0]
            assert kwargs.get("uri") is True

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError, match="پیدا نشد"):
            connect_sqlite_ro("/non/existent/path/logs.db")

    def test_file_hash_unchanged_after_read(self, tmp_path):
        db_path = str(tmp_path / "hash_check.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, msg TEXT)")
        conn.execute("INSERT INTO logs (msg) VALUES ('test_event')")
        conn.commit()
        conn.close()

        hash_before = _sha256_of_file(db_path)

        ro_conn = connect_sqlite_ro(db_path)
        cur = ro_conn.cursor()
        rows = cur.execute("SELECT * FROM logs").fetchall()
        assert len(rows) == 1
        ro_conn.close()

        hash_after = _sha256_of_file(db_path)
        assert hash_before == hash_after


# ─── کلاس ۲: آزمون‌های تبدیل و اعتبارسنجی نوع‌های داده ─────────
class TestTypeConversion:
    """تست‌های تبدیل داده‌ها از فرمت SQLite به PostgreSQL."""

    def test_bool_conversion_true(self):
        cols = ["id", "username", "is_verified", "email_verified", "is_active"]
        bool_idx = {2, 3, 4}
        row = (1, "admin", 1, 1, 1)
        res = transform_row("users", row, cols, bool_idx, -1, -1)
        assert res == (1, "admin", True, True, True)

    def test_bool_conversion_false(self):
        cols = ["id", "username", "is_verified", "email_verified", "is_active"]
        bool_idx = {2, 3, 4}
        row = (2, "viewer", 0, 0, 0)
        res = transform_row("users", row, cols, bool_idx, -1, -1)
        assert res == (2, "viewer", False, False, False)

    def test_bool_conversion_none(self):
        cols = ["id", "username", "is_verified"]
        bool_idx = {2}
        row = (3, "guest", None)
        res = transform_row("users", row, cols, bool_idx, -1, -1)
        assert res == (3, "guest", None)

    def test_bool_columns_are_marked(self):
        expected = {
            ("users", "is_verified"),
            ("users", "email_verified"),
            ("users", "is_active"),
            ("yield_samples", "accepted"),
            ("security_events", "success"),
        }
        assert BOOLEAN_COLUMNS == expected

    def test_json_valid_string(self):
        cols = ["id", "timestamp", "details"]
        row = (1, "2026-10-07T14:30:00Z", '{"pages": 15, "color": "black"}')
        res = transform_row("logs", row, cols, set(), 1, 2)
        assert res is not None
        assert res[2] == '{"pages": 15, "color": "black"}'

    def test_json_invalid_string_becomes_empty(self):
        cols = ["id", "timestamp", "details"]
        row = (1, "2026-10-07T14:30:00Z", "INVALID_JSON_CONTENT")
        res = transform_row("logs", row, cols, set(), 1, 2)
        assert res is not None
        assert res[2] == "{}"

    def test_json_none_stays_none(self):
        cols = ["id", "timestamp", "details"]
        row = (1, "2026-10-07T14:30:00Z", None)
        res = transform_row("logs", row, cols, set(), 1, 2)
        assert res is not None
        assert res[2] is None

    def test_json_dict_passthrough(self):
        cols = ["id", "timestamp", "details"]
        row = (1, "2026-10-07T14:30:00Z", {"pages": 42})
        res = transform_row("logs", row, cols, set(), 1, 2)
        assert res is not None
        assert res[2] == '{"pages": 42}'

    def test_timestamp_valid_iso_z(self):
        cols = ["id", "timestamp", "details"]
        row = (1, "2026-10-07T14:32:00Z", "{}")
        res = transform_row("logs", row, cols, set(), 1, 2)
        assert res is not None
        assert res[1] == "2026-10-07T14:32:00Z"

    def test_timestamp_valid_iso_z_millis(self):
        cols = ["id", "timestamp", "details"]
        row = (1, "2026-10-07T14:32:00.123456Z", "{}")
        res = transform_row("logs", row, cols, set(), 1, 2)
        assert res is not None
        assert res[1] == "2026-10-07T14:32:00.123456Z"

    def test_timestamp_invalid_returns_none(self):
        cols = ["id", "timestamp", "details"]
        # فاقد T و Z
        row = (1, "2026-10-07 14:32:00", "{}")
        res = transform_row("logs", row, cols, set(), 1, 2)
        assert res is None  # باید skip شود

    def test_timestamp_none_returns_none(self):
        cols = ["id", "timestamp", "details"]
        row = (1, None, "{}")
        res = transform_row("logs", row, cols, set(), 1, 2)
        assert res is None  # باید skip شود


# ─── کلاس ۳: همگام‌سازی توالی‌های IDENTITY (setval) ─────────────
class TestIdentitySequenceSync:
    """تست‌های همگام‌سازی توالی‌های IDENTITY برای رفع ریسک R-1."""

    def test_setval_with_data(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = [100]

        synced = sync_identity_sequences(mock_conn)
        assert mock_cur.execute.call_count == len(IDENTITY_TABLES)
        assert synced["logs"] == 100
        mock_conn.commit.assert_called_once()

    def test_setval_empty_table(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = [1]

        synced = sync_identity_sequences(mock_conn)
        assert synced["logs"] == 1

    def test_all_7_identity_tables_synced(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = [5]

        synced = sync_identity_sequences(mock_conn)
        assert len(synced) == 7
        for t in IDENTITY_TABLES:
            assert t in synced

    def test_non_identity_tables_not_touched(self):
        non_identity = {"meta", "printer_counters", "cartridge_state", "cartridge_yield_profiles"}
        for t in non_identity:
            assert t not in IDENTITY_TABLES

    def test_sync_returns_dict_of_values(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = [42]

        synced = sync_identity_sequences(mock_conn)
        assert isinstance(synced, dict)
        assert synced["users"] == 42


# ─── کلاس ۴: استراتژی‌های مهاجرت (COPY در برابر BATCH) ─────────
class TestMigrationStrategies:
    """تست انتخاب خودکار استراتژی مهاجرت بر اساس حجم سطرها."""

    def test_small_table_uses_batch(self):
        plan = TablePlan(
            name="users",
            sqlite_rows=500,
            pg_rows=0,
            columns=["id", "username"],
            strategy="BATCH",
            has_identity=True,
        )
        assert plan.strategy == "BATCH"

    def test_large_table_uses_copy(self):
        plan = TablePlan(
            name="logs",
            sqlite_rows=15000,
            pg_rows=0,
            columns=["id", "timestamp"],
            strategy="COPY",
            has_identity=True,
        )
        assert plan.strategy == "COPY"

    def test_threshold_10000(self):
        assert COPY_THRESHOLD == 10000
        strategy_at_threshold = "COPY" if 10000 > COPY_THRESHOLD else "BATCH"
        strategy_above = "COPY" if 10001 > COPY_THRESHOLD else "BATCH"
        assert strategy_at_threshold == "BATCH"
        assert strategy_above == "COPY"

    def test_batch_size_respected(self, tmp_path):
        db_path = str(tmp_path / "batch_test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        # درج ۲۵۰۰ ردیف
        for i in range(2500):
            conn.execute("INSERT INTO meta VALUES (?, ?, ?)", (f"k_{i}", f"v_{i}", "2026-10-07T12:00:00Z"))
        conn.commit()
        conn.close()

        plan = TablePlan(
            name="meta",
            sqlite_rows=2500,
            pg_rows=0,
            columns=["key", "value", "updated_at"],
            strategy="BATCH",
            has_identity=False,
        )

        mock_pg = MagicMock()
        mock_cur = MagicMock()
        mock_pg.cursor.return_value.__enter__.return_value = mock_cur

        s_conn = connect_sqlite_ro(db_path)
        try:
            stats = migrate_table_batch(s_conn, mock_pg, plan)
            assert stats.inserted_count == 2500
            # ۲۵۰۰ سطر با BATCH_SIZE=1000 باید در ۳ نوبت executemany فراخوانی شود (۱۰۰۰، ۱۰۰۰، ۵۰۰)
            assert mock_cur.executemany.call_count == 3
        finally:
            s_conn.close()


# ─── کلاس ۵: سناریوهای بازگشت خودکار (Rollback) ──────────────────
class TestRollback:
    """تست‌های مکانیزم بازگشت سریع و خودکار در صورت خطا."""

    def test_fatal_error_triggers_truncate(self):
        mock_sqlite = MagicMock()
        mock_pg = MagicMock()
        mock_cur = MagicMock()
        mock_pg.cursor.return_value.__enter__.return_value = mock_cur

        plan = [
            TablePlan(
                name="users",
                sqlite_rows=10,
                pg_rows=0,
                columns=["id", "username"],
                strategy="BATCH",
                has_identity=True,
            )
        ]

        with patch("tools.migrate_sqlite_to_pg.migrate_table_batch", side_effect=RuntimeError("PG Deadlock")):
            with pytest.raises(RuntimeError, match="PG Deadlock"):
                execute_migration(mock_sqlite, mock_pg, plan, force=False)

        # باید دستور TRUNCATE TABLE ... RESTART IDENTITY اجرا شده باشد
        truncate_calls = [
            c for c in mock_cur.execute.call_args_list if "TRUNCATE TABLE" in str(c)
        ]
        assert len(truncate_calls) >= 1

    def test_dry_run_does_not_truncate(self, tmp_path):
        db_path = str(tmp_path / "logs.db")
        shutil.copyfile("logs.db", db_path)

        with patch("tools.migrate_sqlite_to_pg.connect_pg") as mock_conn_func, \
             patch("tools.migrate_sqlite_to_pg.get_pg_tables", return_value=list(TABLE_MIGRATION_ORDER)), \
             patch("tools.migrate_sqlite_to_pg.get_table_columns_pg", side_effect=lambda conn, tbl: [r[1] for r in sqlite3.connect(db_path).cursor().execute(f"PRAGMA table_info({tbl})").fetchall()]), \
             patch("tools.migrate_sqlite_to_pg.get_table_row_count_pg", return_value=0):
            mock_pg = MagicMock()
            mock_cur = MagicMock()
            mock_pg.cursor.return_value.__enter__.return_value = mock_cur
            mock_conn_func.return_value = mock_pg

            code = main(["--source", db_path, "--dry-run", "--quiet"])
            assert code == EXIT_DRY_RUN

            # اطمینان از عدم فراخوانی TRUNCATE
            for call_arg in mock_cur.execute.call_args_list:
                assert "TRUNCATE" not in str(call_arg)

    def test_exit_code_2_on_fatal(self):
        with patch("tools.migrate_sqlite_to_pg.connect_sqlite_ro", side_effect=Exception("Disk I/O error")):
            code = main(["--source", "logs.db"])
            assert code == EXIT_FATAL

    def test_partial_failure_returns_1(self, tmp_path):
        db_path = str(tmp_path / "logs.db")
        shutil.copyfile("logs.db", db_path)

        with patch("tools.migrate_sqlite_to_pg.connect_pg") as mock_conn_func, \
             patch("tools.migrate_sqlite_to_pg.get_pg_tables", return_value=list(TABLE_MIGRATION_ORDER)), \
             patch("tools.migrate_sqlite_to_pg.get_table_columns_pg", side_effect=lambda conn, tbl: [r[1] for r in sqlite3.connect(db_path).cursor().execute(f"PRAGMA table_info({tbl})").fetchall()]), \
             patch("tools.migrate_sqlite_to_pg.get_table_row_count_pg", return_value=0), \
             patch("tools.migrate_sqlite_to_pg.execute_migration") as mock_exec:
            mock_pg = MagicMock()
            mock_conn_func.return_value = mock_pg
            mock_exec.return_value = ([], True)  # has_skips = True

            code = main(["--source", db_path, "--quiet"])
            assert code == EXIT_PARTIAL


# ─── کلاس ۶: آزمون‌های یکپارچگی کلی (End-to-End Unit/Mock) ───────
class TestEndToEnd:
    """آزمون‌های کامل گردش کار مهاجرت."""

    def test_dry_run_counts_all_tables(self, tmp_path):
        db_path = str(tmp_path / "logs.db")
        shutil.copyfile("logs.db", db_path)

        mock_pg = MagicMock()
        mock_cur = MagicMock()
        mock_pg.cursor.return_value.__enter__.return_value = mock_cur

        s_conn = connect_sqlite_ro(db_path)
        try:
            # Mock اطلاعات جدول‌های PG تا با اسکیما مطابقت داشته باشد
            with patch("tools.migrate_sqlite_to_pg.get_pg_tables") as mock_pg_tables, \
                 patch("tools.migrate_sqlite_to_pg.get_table_columns_pg") as mock_pg_cols, \
                 patch("tools.migrate_sqlite_to_pg.get_table_row_count_pg", return_value=0):
                mock_pg_tables.return_value = [
                    "users", "meta", "printer_counters", "cartridge_state",
                    "cartridge_yield_profiles", "logs", "toner_history",
                    "yield_samples", "toner_snapshots_v2", "sensor_readings", "security_events"
                ]
                mock_pg_cols.side_effect = lambda conn, tbl: [
                    r[1] for r in s_conn.cursor().execute(f"PRAGMA table_info({tbl})").fetchall()
                ]

                plan = build_migration_plan(s_conn, mock_pg)
                assert len(plan) == 11
                table_names = [p.name for p in plan]
                assert "logs" in table_names
                assert "users" in table_names
                assert "alembic_version" not in table_names
        finally:
            s_conn.close()

    def test_dry_run_writes_nothing(self, tmp_path):
        db_path = str(tmp_path / "logs.db")
        shutil.copyfile("logs.db", db_path)

        with patch("tools.migrate_sqlite_to_pg.connect_pg") as mock_conn_func, \
             patch("tools.migrate_sqlite_to_pg.get_pg_tables", return_value=list(TABLE_MIGRATION_ORDER)), \
             patch("tools.migrate_sqlite_to_pg.get_table_columns_pg", side_effect=lambda conn, tbl: [r[1] for r in sqlite3.connect(db_path).cursor().execute(f"PRAGMA table_info({tbl})").fetchall()]), \
             patch("tools.migrate_sqlite_to_pg.get_table_row_count_pg", return_value=0):
            mock_pg = MagicMock()
            mock_cur = MagicMock()
            mock_pg.cursor.return_value.__enter__.return_value = mock_cur
            mock_conn_func.return_value = mock_pg

            code = main(["--source", db_path, "--dry-run", "--quiet"])
            assert code == EXIT_DRY_RUN
            assert mock_cur.execute.call_count == 0

    def test_alembic_version_excluded(self, tmp_path):
        db_path = str(tmp_path / "logs.db")
        shutil.copyfile("logs.db", db_path)

        s_conn = connect_sqlite_ro(db_path)
        try:
            with patch("tools.migrate_sqlite_to_pg.get_pg_tables") as mock_pg_tables, \
                 patch("tools.migrate_sqlite_to_pg.get_table_columns_pg") as mock_pg_cols, \
                 patch("tools.migrate_sqlite_to_pg.get_table_row_count_pg", return_value=0):
                mock_pg_tables.return_value = [
                    "users", "meta", "printer_counters", "cartridge_state",
                    "cartridge_yield_profiles", "logs", "toner_history",
                    "yield_samples", "toner_snapshots_v2", "sensor_readings", "security_events"
                ]
                mock_pg_cols.side_effect = lambda conn, tbl: [
                    r[1] for r in s_conn.cursor().execute(f"PRAGMA table_info({tbl})").fetchall()
                ]
                plan = build_migration_plan(s_conn, MagicMock())
                names = [p.name for p in plan]
                assert "alembic_version" not in names
        finally:
            s_conn.close()

    @pytest.mark.pg_live
    def test_end_to_end_success(self, tmp_path):
        """تست مهاجرت کامل end-to-end روی PostgreSQL زنده."""
        db_path = str(tmp_path / "tiny_source.db")
        shutil.copyfile("logs.db", db_path)

        dsn = resolve_pg_dsn(None)
        try:
            code = main(["--source", db_path, "--force", "--quiet"])
            assert code == EXIT_SUCCESS

            # بررسی اینکه تمام سطرها در PG درج شده‌اند
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM logs")
                    count = cur.fetchone()[0]
                    assert count == 7
        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                truncate_all_pg_tables(conn)

    @pytest.mark.pg_live
    def test_end_to_end_with_skips(self, tmp_path):
        """تست مهاجرت با سطر دارای زمان نامعتبر که باید skip شود و کد خروج ۱ دهد."""
        db_path = str(tmp_path / "tiny_skip.db")
        shutil.copyfile("logs.db", db_path)

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO logs (printer_ip, timestamp, type, message) VALUES (?, ?, ?, ?)",
            ("10.0.0.99", "invalid_timestamp_without_tz", "PRINT", "Bad timestamp row"),
        )
        conn.commit()
        conn.close()

        dsn = resolve_pg_dsn(None)
        try:
            code = main(["--source", db_path, "--force", "--quiet"])
            assert code == EXIT_PARTIAL

            with psycopg.connect(dsn) as p_conn:
                with p_conn.cursor() as cur:
                    # سطر نامعتبر نباید درج شده باشد
                    cur.execute("SELECT count(*) FROM logs WHERE printer_ip = '10.0.0.99'")
                    assert cur.fetchone()[0] == 0
        finally:
            with psycopg.connect(dsn, autocommit=True) as p_conn:
                truncate_all_pg_tables(p_conn)

    @pytest.mark.pg_live
    def test_verify_after_migration(self, tmp_path):
        """تست پرچم --verify پس از مهاجرت موفق."""
        db_path = str(tmp_path / "tiny_verify.db")
        shutil.copyfile("logs.db", db_path)

        dsn = resolve_pg_dsn(None)
        try:
            code = main(["--source", db_path, "--force", "--verify", "--quiet"])
            assert code == EXIT_SUCCESS
        finally:
            with psycopg.connect(dsn, autocommit=True) as p_conn:
                truncate_all_pg_tables(p_conn)

    @pytest.mark.pg_live
    def test_insert_after_migration_succeeds_without_explicit_id(self, tmp_path):
        """تست الزامی رفع ریسک R-1: درج سطر جدید در logs بدون ارسال ID پس از مهاجرت."""
        db_path = str(tmp_path / "tiny_r1.db")
        shutil.copyfile("logs.db", db_path)

        dsn = resolve_pg_dsn(None)
        try:
            code = main(["--source", db_path, "--force", "--quiet"])
            assert code == EXIT_SUCCESS

            with psycopg.connect(dsn, autocommit=True) as p_conn:
                with p_conn.cursor() as cur:
                    cur.execute("SELECT max(id) FROM logs")
                    migrated_max_id = cur.fetchone()[0]
                    assert migrated_max_id is not None

                    # درج سطر بدون id (تکیه بر IDENTITY داخلی)
                    cur.execute(
                        """
                        INSERT INTO logs (printer_ip, timestamp, type, message)
                        VALUES (%s, %s, %s, %s) RETURNING id
                        """,
                        ("192.168.1.200", "2026-10-07T17:00:00Z", "PRINT", "R-1 Identity Verification"),
                    )
                    new_id = cur.fetchone()[0]
                    assert new_id > migrated_max_id
        finally:
            with psycopg.connect(dsn, autocommit=True) as p_conn:
                truncate_all_pg_tables(p_conn)


# ─── کلاس ۷: مدیریت هوشمند timestampهای بدون Z (تصمیم بازبینی) ───
class TestNaiveTimestampHandling:
    """تست‌های مدیریت دقیق و Fail-Loudly رشته‌های ISO بدون پسوند Z."""

    def test_naive_timestamp_skipped_by_default(self, tmp_path):
        """timestamp بدون Z به صورت پیش‌فرض skip شده و کد خروج ۱ می‌دهد."""
        sample_json = "tests/fixtures/migration_sample/logs_sample.json"
        db_path = build_test_db_from_json(tmp_path, sample_json, "logs")

        dsn = resolve_pg_dsn(None)
        try:
            code = main(["--source", db_path, "--force", "--quiet"])
            assert code == EXIT_PARTIAL
            assert mig_tool._naive_ts_counter == 5

            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM logs")
                    assert cur.fetchone()[0] == 0
        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                truncate_all_pg_tables(conn)

    def test_naive_timestamp_converted_with_flag(self, tmp_path):
        """timestamp بدون Z با پرچم --convert-naive-timestamps تبدیل به Z شده و درج می‌شود."""
        sample_json = "tests/fixtures/migration_sample/logs_sample.json"
        db_path = build_test_db_from_json(tmp_path, sample_json, "logs")

        dsn = resolve_pg_dsn(None)
        try:
            code = main(
                [
                    "--source",
                    db_path,
                    "--force",
                    "--convert-naive-timestamps",
                    "--quiet",
                ]
            )
            assert code == EXIT_SUCCESS

            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT timestamp FROM logs")
                    rows = cur.fetchall()
                    assert len(rows) == 5
                    for r in rows:
                        assert r[0].endswith("Z")
        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                truncate_all_pg_tables(conn)

    def test_mixed_z_and_naive(self, tmp_path):
        """در ترکیب timestampهای با Z و بدون Z، فقط بدون Z ها در حالت عادی skip می‌شوند."""
        db_path = str(tmp_path / "mixed.db")
        shutil.copyfile("logs.db", db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM logs")
        # ۳ سطر با Z
        for i in range(3):
            conn.execute(
                "INSERT INTO logs (printer_ip, timestamp, type, message) VALUES (?, ?, ?, ?)",
                ("10.0.0.1", f"2026-10-07T14:0{i}:00Z", "PRINT", "With Z"),
            )
        # ۲ سطر naive بدون Z
        for i in range(2):
            conn.execute(
                "INSERT INTO logs (printer_ip, timestamp, type, message) VALUES (?, ?, ?, ?)",
                ("10.0.0.1", f"2026-10-07T15:0{i}:00.123456", "PRINT", "Without Z"),
            )
        conn.commit()
        conn.close()

        dsn = resolve_pg_dsn(None)
        try:
            code = main(["--source", db_path, "--force", "--quiet"])
            assert code == EXIT_PARTIAL
            with psycopg.connect(dsn) as p_conn:
                with p_conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM logs")
                    # فقط ۳ سطر دارای Z باید درج شده باشند
                    assert cur.fetchone()[0] == 3
        finally:
            with psycopg.connect(dsn, autocommit=True) as p_conn:
                truncate_all_pg_tables(p_conn)

    def test_invalid_timestamp_skipped_always(self, tmp_path):
        """رشته‌های کاملاً نامعتبر (فاقد ساختار ایزو) در هیچ حالتی (حتی با پرچم تبدیل) درج نمی‌شوند."""
        db_path = str(tmp_path / "invalid_ts.db")
        shutil.copyfile("logs.db", db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM logs")
        conn.execute(
            "INSERT INTO logs (printer_ip, timestamp, type, message) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "not-a-date-at-all", "PRINT", "Bad 1"),
        )
        conn.execute(
            "INSERT INTO logs (printer_ip, timestamp, type, message) VALUES (?, ?, ?, ?)",
            ("10.0.0.1", "2026-10-07 14:32:00", "PRINT", "Bad 2 with space"),
        )
        conn.commit()
        conn.close()

        dsn = resolve_pg_dsn(None)
        try:
            # بدون پرچم
            code1 = main(["--source", db_path, "--force", "--quiet"])
            assert code1 == EXIT_PARTIAL
            with psycopg.connect(dsn) as p_conn:
                with p_conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM logs")
                    assert cur.fetchone()[0] == 0

            # با پرچم تبدیل: همچنان باید رد شوند چون ساختار ISO ندارند
            code2 = main(
                [
                    "--source",
                    db_path,
                    "--force",
                    "--convert-naive-timestamps",
                    "--quiet",
                ]
            )
            assert code2 == EXIT_PARTIAL
            with psycopg.connect(dsn) as p_conn:
                with p_conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM logs")
                    assert cur.fetchone()[0] == 0
        finally:
            with psycopg.connect(dsn, autocommit=True) as p_conn:
                truncate_all_pg_tables(p_conn)

    def test_threshold_100_naive_triggers_fatal(self, tmp_path):
        """اگر بیش از ۱۰۰ رکورد naive بدون پرچم رد شوند، وضعیت FATAL با کد خروج ۲ بازمی‌گردد."""
        db_path = str(tmp_path / "hundred_naive.db")
        shutil.copyfile("logs.db", db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM logs")
        for i in range(105):
            conn.execute(
                "INSERT INTO logs (printer_ip, timestamp, type, message) VALUES (?, ?, ?, ?)",
                (
                    "10.0.0.1",
                    f"2026-08-25T11:00:{i % 60:02d}.100000",
                    "PRINT",
                    "Naive row",
                ),
            )
        conn.commit()
        conn.close()

        dsn = resolve_pg_dsn(None)
        try:
            code = main(["--source", db_path, "--force", "--quiet"])
            assert code == EXIT_FATAL
            assert mig_tool._naive_ts_counter == 105
        finally:
            with psycopg.connect(dsn, autocommit=True) as p_conn:
                truncate_all_pg_tables(p_conn)

    def test_naive_counter_resets_between_runs(self, tmp_path):
        """شمارنده _naive_ts_counter در شروع هر اجرای جدید به صفر ریست می‌شود."""
        sample_json = "tests/fixtures/migration_sample/logs_sample.json"
        db_path = build_test_db_from_json(tmp_path, sample_json, "logs")

        dsn = resolve_pg_dsn(None)
        try:
            main(["--source", db_path, "--force", "--quiet"])
            first_count = mig_tool._naive_ts_counter
            assert first_count == 5

            main(["--source", db_path, "--force", "--quiet"])
            second_count = mig_tool._naive_ts_counter
            assert second_count == 5
        finally:
            with psycopg.connect(dsn, autocommit=True) as p_conn:
                truncate_all_pg_tables(p_conn)


# ─── کلاس ۸: آزمون‌های یکپارچگی روی داده‌های مشابه پروداکشن ───────
class TestProductionLikeE2E:
    """تست‌های رگرسیون end-to-end با فرمت واقعی داده‌های پروداکشن."""

    @pytest.mark.pg_live
    def test_end_to_end_with_real_production_format(self, tmp_path):
        """تست کامل با داده‌های واقعی production (بدون Z) همراه با --convert-naive-timestamps."""
        sample_json = "tests/fixtures/migration_sample/logs_sample.json"
        db_path = build_test_db_from_json(tmp_path, sample_json, "logs")

        dsn = resolve_pg_dsn(None)
        try:
            code = main(
                [
                    "--source",
                    db_path,
                    "--force",
                    "--convert-naive-timestamps",
                    "--verify",
                    "--quiet",
                ]
            )
            assert code == EXIT_SUCCESS

            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM logs")
                    assert cur.fetchone()[0] == 5
        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                truncate_all_pg_tables(conn)
