# -*- coding: utf-8 -*-
"""
tests/test_cutover_rehearsal.py — آزمون تمرین Cutover با داده‌ی مصنوعی ۳۰MB (فاز F.4).

اهداف آزمون:
-----------
۱. اندازه‌گیری دقیق و واقعی زمان مراحل مختلف migration (Dry-run, COPY, Batch, setval, Verify, Rollback).
۲. اعتبارسنجی فرآیند بازگشت اضطراری (Rollback با TRUNCATE) در کمتر از ۳۰ ثانیه.
۳. تضمین سلامت توالی‌های IDENTITY و امکان درج ردیف جدید بدون خطا (R-1).
"""

from __future__ import annotations

import os
import pathlib
import time

import psycopg
import pytest

from tools.generate_cutover_fixture import DEFAULT_OUTPUT_PATH, generate_synthetic_db
from tools.migrate_sqlite_to_pg import (
    EXIT_DRY_RUN,
    EXIT_SUCCESS,
    main as migration_main,
    resolve_pg_dsn,
    truncate_all_pg_tables,
)

FIXTURE_PATH = pathlib.Path(DEFAULT_OUTPUT_PATH)


@pytest.mark.pg_live
@pytest.mark.slow
class TestCutoverRehearsal:
    """تمرین عملیات Cutover با داده‌های مصنوعی ۳۰ مگابایت بر روی کلاستر زنده PostgreSQL."""

    def setup_method(self):
        # بررسی وجود DSN زنده
        dsn = os.environ.get("PG_DSN", "") or ""
        if not dsn.strip():
            pytest.skip("PG_DSN ست نشده است ⇒ تمرین Cutover روی PG زنده اجرا نمی‌شود")

    def test_cutover_rehearsal_with_synthetic_30mb(self, tmp_path):
        """تمرین کامل cutover با ۳۰MB داده‌ی مصنوعی و ثبت زمان‌های هر گام."""
        # ۱. تولید fixture در صورت عدم وجود
        if not FIXTURE_PATH.exists() or FIXTURE_PATH.stat().st_size < 25 * 1024 * 1024:
            generate_synthetic_db(
                output_path=str(FIXTURE_PATH),
                size_mb=30.0,
                quiet=True,
            )

        fixture_size_bytes = FIXTURE_PATH.stat().st_size
        fixture_size_mb = fixture_size_bytes / (1024 * 1024)
        assert 25.0 <= fixture_size_mb <= 35.0, f"حجم fixture خارج از محدوده مجاز: {fixture_size_mb:.2f} MB"

        dsn = resolve_pg_dsn(None)
        metrics = {
            "fixture_size_mb": round(fixture_size_mb, 2),
            "fixture_size_bytes": fixture_size_bytes,
        }

        # ابتدا مطمئن شویم جداول PG خالی هستند
        with psycopg.connect(dsn, autocommit=True) as conn:
            truncate_all_pg_tables(conn)

        try:
            # ۲. سنجش زمان Dry-Run
            t0 = time.perf_counter()
            exit_code_dry = migration_main([
                "--source", str(FIXTURE_PATH),
                "--dry-run",
                "--quiet",
            ])
            dry_run_seconds = time.perf_counter() - t0
            assert exit_code_dry == EXIT_DRY_RUN
            metrics["dry_run_seconds"] = round(dry_run_seconds, 3)

            # ۳. سنجش زمان اجرای واقعی Migration (شامل COPY و Batch و setval)
            t1 = time.perf_counter()
            exit_code_mig = migration_main([
                "--source", str(FIXTURE_PATH),
                "--convert-naive-timestamps",
                "--verify",
                "--quiet",
            ])
            total_migration_seconds = time.perf_counter() - t1
            assert exit_code_mig == EXIT_SUCCESS
            metrics["total_seconds"] = round(total_migration_seconds, 3)

            # ۴. بررسی سلامت R-1 (درج بدون ID در جدول logs)
            with psycopg.connect(dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT max(id) FROM logs")
                    migrated_max_id = cur.fetchone()[0]
                    assert migrated_max_id is not None

                    # درج سطر جدید متکی بر توالی
                    cur.execute(
                        """
                        INSERT INTO logs (printer_ip, timestamp, type, message)
                        VALUES (%s, %s, %s, %s) RETURNING id
                        """,
                        ("192.168.10.99", "2026-10-07T12:00:00Z", "PRINT", "Rehearsal Live Insert R-1"),
                    )
                    new_id = cur.fetchone()[0]
                    assert new_id > migrated_max_id
                    metrics["insert_after_migration_ok"] = True

            # ۵. سنجش زمان بازگشت (Rollback با TRUNCATE)
            t2 = time.perf_counter()
            with psycopg.connect(dsn, autocommit=True) as conn:
                truncate_all_pg_tables(conn)
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM logs")
                    assert cur.fetchone()[0] == 0
            rollback_seconds = time.perf_counter() - t2
            metrics["rollback_seconds"] = round(rollback_seconds, 3)

            # ۶. بررسی آستانه‌های پذیرش (Thresholds)
            assert metrics["dry_run_seconds"] < 10.0, "Dry-run بیش از حد طول کشید"
            assert metrics["total_seconds"] < 30.0, "کل زمان مهاجرت ۳۰MB بیش از ۳۰ ثانیه شد"
            assert metrics["rollback_seconds"] < 5.0, "عملیات Rollback بیش از ۵ ثانیه طول کشید"
            assert metrics["insert_after_migration_ok"] is True

            print("\n[Cutover Rehearsal Metrics]", metrics)

        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                truncate_all_pg_tables(conn)
