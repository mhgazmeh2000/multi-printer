# -*- coding: utf-8 -*-
"""
0001_initial_schema — migration پایه (baseline) فاز E.1.

چه می‌سازد؟
------------
۱۱ جدول = ۱۰ جدول برنامه + جدول `meta` (تنها افزوده‌ی فاز E.0، کار E.0.13)
به‌همراه ۱۳ ایندکس. این migration **وضعیت نهاییِ امروزِ SQLite** را بازتولید می‌کند؛
ستون‌هایی که تا امروز با `ALTER TABLE` به جدول‌ها اضافه شده‌اند
(`users.sensor_*`، `printer_counters.toner_overrides`، `cartridge_state.cycle_*`) در
همین DDL یک‌جا آمده‌اند، چون baseline به‌جای بازپخش تاریخچه، شکل نهایی را می‌سازد.

تصمیم‌های آگاهانه
-----------------
• نوع‌ها **همان SQLite امروز** می‌مانند: timestampها `TEXT` و booleanها `INTEGER`.
  تبدیل `TEXT → TIMESTAMPTZ` کار فاز E.4 است (تبصره‌ی صریح فاز E.1).
• دو نگاشتِ دوحالته (variant) برای اینکه یک migration هم SQLite را عیناً بازتولید کند و
  هم روی PG معنای درست داشته باشد:
    - شناسه‌ها: `BIGINT` روی PG، `INTEGER` روی SQLite. (`INTEGER` عیناً لازم است، چون
      `AUTOINCREMENT` در SQLite فقط روی `INTEGER PRIMARY KEY` مجاز است؛ با `BIGINT`
      هم schema یکسان نمی‌شد و هم autoincrement کار نمی‌کرد.)
    - اعشاری‌ها: `DOUBLE PRECISION` روی PG (۸ بایت، هم‌معنای `REAL` در SQLite)،
      `REAL` روی SQLite (تا schema بایت‌به‌بایت یکسان بماند؛ `FLOAT` می‌شد `FLOAT`).
• هیچ کلید خارجی‌ای وجود ندارد (مثل امروز) و هیچ داده‌ای درج نمی‌شود.
• `sqlite_sequence` ساخته نمی‌شود؛ خود SQLite در حضور `AUTOINCREMENT` می‌سازد.
  برای همین ۷ جدول `sqlite_autoincrement=True` صریح آمده (SQLAlchemy به‌طور پیش‌فرض
  این کلمه را رندر نمی‌کند و بدون آن، DDL با امروزِ برنامه یکی نمی‌شد و درجِ بدون id
  رفتار «بازاستفاده‌ی id» می‌گرفت).

تفاوت‌های عمدی و مستندشده با schema امروز SQLite (هر دو مورد «سخت‌گیرانه‌تر و هم‌سو با PG»اند)
--------------------------------------------------------------------------------------------
۱. ۹ ستون در migration صریحاً `NOT NULL` هستند: هفت `id` (کلیدهای AUTOINCREMENT) و دو
   کلید متنی `printer_counters.ip` و `cartridge_yield_profiles.cartridge_key`.
   در SQLite امروز این‌ها `notnull=0` ثبت می‌شوند (ویژگی خودِ SQLite برای ستون کلید)،
   ولی در عمل هرگز NULL نمی‌گیرند و در PG هم کلید اصلی ذاتاً NOT NULL است.
   SQLAlchemy برای ستون‌های `primary_key=True` همیشه NOT NULL می‌نویسد؛ حذف آن ممکن
   نیست مگر با کنارگذاشتن قید کلید اصلی — که تغییر معنای schema بود، نه بازتولید آن.
۲. روی PostgreSQL، شناسه‌ها با `BIGSERIAL` (seq + `nextval`) ساخته می‌شوند، نه
   `GENERATED … AS IDENTITY`. هر دو رفتار autoincrement یکسان می‌دهند؛ تغییر به
   IDENTITY در فاز E.4 (همراه بازبینی نوع‌ها) انجام می‌شود تا این baseline «شکل امروز»
   را بازتولید کند.

نکته‌ی مهم برای دیتابیس موجود
-----------------------------
روی SQLite موجودِ سرور (`logs.db` که همین جدول‌ها را دارد) نباید `upgrade head` زد؛
باید `alembic stamp head` خورد تا نسخه‌ی schema ثبت شود. `upgrade head` فقط برای
دیتابیس خالی (PostgreSQL تازه یا فایل SQLite خالی) است. جزئیات در `docs/phase-e-plan.md`.
"""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION

from alembic import op
import sqlalchemy as sa

# شناسه‌ی یکتای این migration
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cartridge_state",
        sa.Column("printer_ip", sa.Text, primary_key=True),
        sa.Column("color", sa.Text, primary_key=True),
        sa.Column("printer_model", sa.Text, nullable=True),
        sa.Column("cartridge_name", sa.Text, nullable=True),
        sa.Column("cartridge_key", sa.Text, nullable=True),
        sa.Column("current_level", sa.Integer, nullable=True),
        sa.Column("last_valid_level", sa.Integer, nullable=True),
        sa.Column("last_counter", sa.Integer, nullable=True),
        sa.Column("anchor_counter", sa.Integer, nullable=True),
        sa.Column("anchor_level", sa.Integer, nullable=True),
        sa.Column("anchor_timestamp", sa.Text, nullable=True),
        sa.Column("yield_per_page", sa.Integer, nullable=True, server_default=sa.text('2000')),
        sa.Column("yield_source", sa.Text, nullable=True, server_default=sa.text("'default'")),
        sa.Column("confidence", sa.Text, nullable=True, server_default=sa.text("'low'")),
        sa.Column("sample_count", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("total_weight", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('0')),
        sa.Column("learned_at", sa.Text, nullable=True),
        sa.Column("last_refill_at", sa.Text, nullable=True),
        sa.Column("last_refill_counter", sa.Integer, nullable=True),
        sa.Column("pending_refill_level", sa.Integer, nullable=True),
        sa.Column("pending_refill_counter", sa.Integer, nullable=True),
        sa.Column("pending_refill_timestamp", sa.Text, nullable=True),
        sa.Column("cycle_start_counter", sa.Integer, nullable=True),
        sa.Column("cycle_start_level", sa.Integer, nullable=True),
        sa.Column("cycle_start_timestamp", sa.Text, nullable=True),
        sa.Column("zero_reached_counter", sa.Integer, nullable=True),
        sa.Column("zero_reached_timestamp", sa.Text, nullable=True),
        sa.Column("pages_after_zero", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("cycle_status", sa.Text, nullable=True, server_default=sa.text("'active'")),
        sa.Column("force_estimate", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("updated_at", sa.Text, nullable=True),
    )
    op.create_table(
        "cartridge_yield_profiles",
        sa.Column("cartridge_key", sa.Text, primary_key=True),
        sa.Column("printer_model", sa.Text, nullable=True),
        sa.Column("cartridge_name", sa.Text, nullable=True),
        sa.Column("color", sa.Text, nullable=True),
        sa.Column("yield_per_page", sa.Integer, nullable=False),
        sa.Column("confidence", sa.Text, nullable=False, server_default=sa.text("'high'")),
        sa.Column("sample_count", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("total_weight", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('0')),
        sa.Column("source_printer_ip", sa.Text, nullable=True),
        sa.Column("updated_at", sa.Text, nullable=True),
    )
    op.create_table(
        "logs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("printer_ip", sa.Text, nullable=False),
        sa.Column("printer_name", sa.Text, nullable=True),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=True),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("pages", sa.Integer, nullable=True),
        sa.Column("color", sa.Text, nullable=True),
        sa.Column("code", sa.Text, nullable=True),
        sa.Column("severity", sa.Text, nullable=True),
        sa.Column("paper_size", sa.Text, nullable=True),
        sa.Column("username", sa.Text, nullable=True),
        sa.Column("details", sa.Text, nullable=True),
        # AUTOINCREMENT در SQLite: عیناً همان DDL امروزِ برنامه (و sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "printer_counters",
        sa.Column("ip", sa.Text, primary_key=True),
        sa.Column("device_type", sa.Text, nullable=True),
        sa.Column("print_total", sa.Integer, nullable=True),
        sa.Column("full_color", sa.Integer, nullable=True),
        sa.Column("black_white", sa.Integer, nullable=True),
        sa.Column("toner_level", sa.Integer, nullable=True),
        sa.Column("manual_override", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("override_color", sa.Text, nullable=True),
        sa.Column("override_base_level", sa.Integer, nullable=True),
        sa.Column("override_start_total", sa.Integer, nullable=True),
        sa.Column("override_start_toner", sa.Integer, nullable=True),
        sa.Column("yield_per_page", sa.Integer, nullable=True, server_default=sa.text('2000')),
        sa.Column("force_estimate", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("yield_learning_failures", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("last_alert_codes", sa.Text, nullable=True),
        sa.Column("a3_total", sa.Integer, nullable=True),
        sa.Column("a4_total", sa.Integer, nullable=True),
        sa.Column("alert_codes", sa.Text, nullable=True),
        sa.Column("cartridge_id_state", sa.Text, nullable=True),
        sa.Column("updated_at", sa.Text, nullable=True),
        sa.Column("toner_overrides", sa.Text, nullable=True, server_default=sa.text('NULL')),
    )
    op.create_table(
        "security_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False, server_default=sa.text("'info'")),
        sa.Column("user_identifier", sa.Text, nullable=True),
        sa.Column("user_id", sa.Integer, nullable=True),
        sa.Column("ip_address", sa.Text, nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("endpoint", sa.Text, nullable=True),
        sa.Column("success", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("details", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
        # AUTOINCREMENT در SQLite: عیناً همان DDL امروزِ برنامه (و sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "sensor_readings",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("printer_ip", sa.Text, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("port", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("value", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True),
        sa.Column("unit", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=True),
        # AUTOINCREMENT در SQLite: عیناً همان DDL امروزِ برنامه (و sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "toner_history",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("printer_ip", sa.Text, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("print_total", sa.Integer, nullable=True),
        sa.Column("toner_level", sa.Integer, nullable=True),
        sa.Column("yield_per_page", sa.Integer, nullable=True),
        sa.Column("source", sa.Text, nullable=True, server_default=sa.text("'poll'")),
        # AUTOINCREMENT در SQLite: عیناً همان DDL امروزِ برنامه (و sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "toner_snapshots_v2",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("printer_ip", sa.Text, nullable=False),
        sa.Column("color", sa.Text, nullable=False),
        sa.Column("printer_model", sa.Text, nullable=True),
        sa.Column("cartridge_name", sa.Text, nullable=True),
        sa.Column("cartridge_key", sa.Text, nullable=True),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("print_total", sa.Integer, nullable=True),
        sa.Column("full_color", sa.Integer, nullable=True),
        sa.Column("black_white", sa.Integer, nullable=True),
        sa.Column("usage_counter", sa.Integer, nullable=True),
        sa.Column("toner_level", sa.Integer, nullable=True),
        sa.Column("raw_level", sa.Integer, nullable=True),
        sa.Column("source", sa.Text, nullable=True),
        sa.Column("valid", sa.Integer, nullable=True, server_default=sa.text('1')),
        sa.Column("reject_reason", sa.Text, nullable=True),
        # AUTOINCREMENT در SQLite: عیناً همان DDL امروزِ برنامه (و sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("username", sa.Text, nullable=False, unique=True),
        sa.Column("email", sa.Text, nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=True),
        sa.Column("google_id", sa.Text, nullable=True, unique=True),
        sa.Column("reset_token_hash", sa.Text, nullable=True),
        sa.Column("reset_token_expires", sa.Text, nullable=True),
        sa.Column("role", sa.Text, nullable=False, server_default=sa.text("'viewer'")),
        sa.Column("is_verified", sa.Integer, nullable=False, server_default=sa.text('0')),
        sa.Column("email_verified", sa.Integer, nullable=False, server_default=sa.text('0')),
        sa.Column("is_active", sa.Integer, nullable=False, server_default=sa.text('1')),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("last_login_at", sa.Text, nullable=True),
        sa.Column("allowed_offices", sa.Text, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("allowed_modules", sa.Text, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("sensor_alert_enabled", sa.Integer, nullable=False, server_default=sa.text('0')),
        sa.Column("sensor_temp_warning", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('30')),
        sa.Column("sensor_temp_critical", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('35')),
        sa.Column("sensor_hum_warning_high", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('70')),
        sa.Column("sensor_hum_critical_high", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('80')),
        sa.Column("sensor_hum_warning_low", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('20')),
        sa.Column("sensor_alert_cooldown_minutes", sa.Integer, nullable=True, server_default=sa.text('30')),
        sa.Column("sensor_alert_start_hour", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("sensor_alert_end_hour", sa.Integer, nullable=True, server_default=sa.text('24')),
        # AUTOINCREMENT در SQLite: عیناً همان DDL امروزِ برنامه (و sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "yield_samples",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("printer_ip", sa.Text, nullable=False),
        sa.Column("color", sa.Text, nullable=False),
        sa.Column("printer_model", sa.Text, nullable=True),
        sa.Column("cartridge_name", sa.Text, nullable=True),
        sa.Column("cartridge_key", sa.Text, nullable=True),
        sa.Column("start_counter", sa.Integer, nullable=True),
        sa.Column("end_counter", sa.Integer, nullable=True),
        sa.Column("start_level", sa.Integer, nullable=True),
        sa.Column("end_level", sa.Integer, nullable=True),
        sa.Column("pages_delta", sa.Integer, nullable=True),
        sa.Column("toner_drop", sa.Integer, nullable=True),
        sa.Column("estimated_yield", sa.Integer, nullable=True),
        sa.Column("counter_basis", sa.Text, nullable=True),
        sa.Column("confidence_weight", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True),
        sa.Column("accepted", sa.Integer, nullable=True, server_default=sa.text('1')),
        sa.Column("reject_reason", sa.Text, nullable=True),
        sa.Column("source", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=True),
        # AUTOINCREMENT در SQLite: عیناً همان DDL امروزِ برنامه (و sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "meta",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )

    # ─── ایندکس‌ها (۱۳ مورد، عیناً همان نام‌ها) ───
    op.create_index("idx_cartridge_state_key", "cartridge_state", ["cartridge_key"])
    op.create_index("idx_printer_ip", "logs", ["printer_ip"])
    op.create_index("idx_sec_event_type", "security_events", ["event_type"])
    op.create_index("idx_sec_ip", "security_events", ["ip_address"])
    op.create_index("idx_sec_timestamp", "security_events", ["timestamp"])
    op.create_index("idx_sec_user", "security_events", ["user_identifier"])
    op.create_index("idx_sensor_readings_ip_ts", "sensor_readings", ["printer_ip", "timestamp"])
    op.create_index("idx_sensor_readings_kind", "sensor_readings", ["kind"])
    op.create_index("idx_timestamp", "logs", ["timestamp"])
    op.create_index("idx_toner_history_ip_ts", "toner_history", ["printer_ip", "timestamp"])
    op.create_index("idx_toner_snapshots_v2_ip_color_ts", "toner_snapshots_v2", ["printer_ip", "color", "timestamp"])
    op.create_index("idx_type", "logs", ["type"])
    op.create_index("idx_yield_samples_key", "yield_samples", ["cartridge_key"])


def downgrade() -> None:
    """حذف کامل (indexها اول، سپس جدول‌ها) — `downgrade base` باید بی‌خطا کار کند."""
    op.drop_index("idx_cartridge_state_key", table_name="cartridge_state")
    op.drop_index("idx_printer_ip", table_name="logs")
    op.drop_index("idx_sec_event_type", table_name="security_events")
    op.drop_index("idx_sec_ip", table_name="security_events")
    op.drop_index("idx_sec_timestamp", table_name="security_events")
    op.drop_index("idx_sec_user", table_name="security_events")
    op.drop_index("idx_sensor_readings_ip_ts", table_name="sensor_readings")
    op.drop_index("idx_sensor_readings_kind", table_name="sensor_readings")
    op.drop_index("idx_timestamp", table_name="logs")
    op.drop_index("idx_toner_history_ip_ts", table_name="toner_history")
    op.drop_index("idx_toner_snapshots_v2_ip_color_ts", table_name="toner_snapshots_v2")
    op.drop_index("idx_type", table_name="logs")
    op.drop_index("idx_yield_samples_key", table_name="yield_samples")
    op.drop_table("meta")
    op.drop_table("yield_samples")
    op.drop_table("users")
    op.drop_table("toner_snapshots_v2")
    op.drop_table("toner_history")
    op.drop_table("sensor_readings")
    op.drop_table("security_events")
    op.drop_table("printer_counters")
    op.drop_table("logs")
    op.drop_table("cartridge_yield_profiles")
    op.drop_table("cartridge_state")
