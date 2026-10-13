# -*- coding: utf-8 -*-
"""
0005_add_legacy_log_columns — افزودن ۵ ستون legacy به جدول logs (فاز F.5).

legacy columns — منشأ نامشخص، ارزش داده‌ای صفر،
هدف: هم‌راستایی با schema production. حذف در فاز L.

پیش‌زمینه:
---------
در بازبینی فاز F.4 و بررسی فایل‌های واقعی دیتابیس پروداکشن (migration_sample/logs_sample.json)
کشف شد که جدول logs در دیتابیس SQLite پروداکشن دارای ۱۸ ستون است، در حالی که در محیط
توسعه و کلاستر PostgreSQL (تا مایگریشن 0004) جدول logs دارای ۱۳ ستون بوده است.
ستون‌های اضافه شده:
  - sheets: INTEGER (۱۰۰٪ مقادیر در پروداکشن NULL است)
  - raw_counters: TEXT (۱۰۰٪ مقادیر در پروداکشن NULL است)
  - raw_delta: INTEGER (۱۰۰٪ مقادیر در پروداکشن NULL است)
  - event_type: TEXT (۱۰۰٪ مقادیر در پروداکشن مقدار ثابت 'UNKNOWN' است)
  - duplex_mode: TEXT (۱۰۰٪ مقادیر در پروداکشن مقدار ثابت 'unknown' است)

این ستون‌ها در هیچ‌کدام از کوئری‌ها یا لاجیک برنامه مصرف نمی‌شوند (ستون‌های مرده / Dead Columns)،
اما برای برآورده کردن شرط تطابق کامل اسکیمای منبع و مقصد در ابزار مهاجرت CLI (فاز F.1)
وجود آن‌ها در PostgreSQL ضروری است تا از توقف عملیات مهاجرت جلوگیری شود.
این ستون‌ها پس از انجام موفقیت‌آمیز مهاجرت، در قالب بدهی فنی TD-022 در فاز L پاکسازی خواهند شد.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("logs") as batch_op:
        batch_op.add_column(sa.Column("sheets", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("raw_counters", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("raw_delta", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("event_type", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("duplex_mode", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("logs") as batch_op:
        batch_op.drop_column("duplex_mode")
        batch_op.drop_column("event_type")
        batch_op.drop_column("raw_delta")
        batch_op.drop_column("raw_counters")
        batch_op.drop_column("sheets")
