# Inventory of `except sqlite3.*` and PostgreSQL equivalents — phase E.0, task E.0.5

**وضعیت:** فقط مستندات + یک ابزار تشخیصی. هیچ کد production، هیچ schema و هیچ داده‌ای تغییر نکرده است.
**ابزار:** `tools/find_sqlite_excepts.py` (اسکن **AST**-محور؛ SQL پویا را هم با بافتِ حلقه/تابع تک‌منبع حل می‌کند).
**مبنا:** شاخه‌ی `phase-c` پس از `e220825`؛ تاریخ بررسی: ۲۰۲۶-۱۰-۰۳.

---

## 1. Why this inventory is needed

استثناهای `sqlite3.*` بعد از مهاجرت **دیگر پرتاب نمی‌شوند**. اگر یک `except sqlite3.OperationalError` باقی بماند، خطای پستگرس از کنارش رد می‌شود و به کاربر ۵۰۰ می‌رسد؛ اگر هم نوعی که کد جست‌وجو می‌کند عوض شود، مسیرهای «تحمل خطا» (مثل `pass` روی ALTER تکراری) بی‌اثر می‌شوند.

سیاست پروژه (فاز D): ترجمه‌ی نوع خطا **فقط** کار adapter است؛ مصرف‌کننده‌ها باید با انواع مشترک (`core.db.DatabaseError`, `core.db.DatabaseConnectionError`) کار کنند. پس محلِ `sqlite3.connect` و مهارهای درون adapter از دامنه‌ی این فهرست بیرون‌اند (فقط علامت‌گذاری می‌شوند).

### جدول نگاشت (مبنای تصمیم‌ها)

| SQLite | معادل PostgreSQL (psycopg 3) | چه زمانی |
|---|---|---|
| `sqlite3.OperationalError` روی `ALTER TABLE ADD COLUMN` | `psycopg.errors.DuplicateColumn` | ستون از قبل هست (کد idempotent) |
| `sqlite3.OperationalError` روی `SELECT` از جدول/ستون نبود | `psycopg.errors.UndefinedTable` / `UndefinedColumn` | DB قدیمی/نیم‌مهاجرت |
| `sqlite3.IntegrityError` | `psycopg.errors.UniqueViolation` | تعارض کلید یکتا |
| `sqlite3.DatabaseError` / `sqlite3.Error` (عمومی) | `core.db.DatabaseError` (مشترک پروژه) | مسیرهای تحمل خطای عمومی |
| `sqlite3.OperationalError` (قفل/دیسک/اتصال) | `psycopg.OperationalError` | خطاهای اجرایی؛ **بررسی پیام باید به SQLSTATE تبدیل شود** |
| `PRAGMA` | معادل ندارد | کار adapter: no-op یا `SET` |

---

## 2. Full list (21 locations — none left without a decision)

> ⚠ **این جدول snapshot زمان E.0.5 است** (۲۱ محل). شمارش امروز، پس از E.4.4:
> `جمع: 16 محل (کد برنامه/ابزار/تست: 13، adapter: 3) | نیاز به بازبینی: 0` و
> **صفر** `except sqlite3.*` در `core/` و `web/`. جزئیات در بند ۶.

| فایل:خط | exception | عملیات داخل try | معادل PostgreSQL | تصمیم فاز E |
|---|---|---|---|---|
| `core/database.py:229` | `sqlite3.OperationalError` | `c.execute("PRAGMA journal_mode=DELETE")` (خط `228`) | معادل ندارد ⇒ no-op در adapter | معادل دقیق psycopg + ⚠ یادداشت |
| `core/database.py:235` | `sqlite3.OperationalError` | `c.execute("PRAGMA synchronous=FULL")` (خط `234`) | معادل ندارد ⇒ no-op در adapter | معادل دقیق psycopg + ⚠ یادداشت |
| `core/database.py:259` | `sqlite3.OperationalError` | `ALTER TABLE logs ADD COLUMN paper_size TEXT` | `psycopg.errors.DuplicateColumn` | معادل دقیق psycopg |
| `core/database.py:263` | `sqlite3.OperationalError` | `ALTER TABLE logs ADD COLUMN username TEXT` | `psycopg.errors.DuplicateColumn` | معادل دقیق psycopg |
| `core/database.py:282` | `sqlite3.OperationalError` | حلقه روی `_counter_column_alters()` — ۲۰ ستون `printer_counters` (خط `279`) | `psycopg.errors.DuplicateColumn` | معادل دقیق psycopg |
| `core/database.py:361` | `sqlite3.OperationalError` | حلقه روی ۲۲ `ALTER TABLE users …` | `psycopg.errors.DuplicateColumn` | معادل دقیق psycopg |
| `core/database.py:950` | `sqlite3.OperationalError` | همان ۲۰ ستون `printer_counters` (حلقه در خط `947`، تابع `ensure_printer_counters_columns`) | `psycopg.errors.DuplicateColumn` | معادل دقیق psycopg |
| `core/db/connection.py:249` | `sqlite3.Error` | `PRAGMA busy_timeout=…` (خط `248`) | — (کار adapter) | **خارج از دامنه (adapter)** |
| `create_admin.py:34` | `sqlite3.OperationalError` (+`DatabaseConnectionError`) | `init_db()` | `psycopg.OperationalError` | معادل دقیق psycopg + ⚠ یادداشت |
| `tests/golden_harness.py:256` | `sqlite3.OperationalError` | `SELECT * FROM {t}` (جدول ممکن است نباشد) | `UndefinedTable` / `UndefinedColumn` | معادل دقیق psycopg |
| `tests/golden_harness.py:295` | `sqlite3.OperationalError` | `SELECT * FROM {t} ORDER BY … LIMIT …` | `UndefinedTable` / `UndefinedColumn` | معادل دقیق psycopg |
| `tests/test_db_adapter.py:68` | `sqlite3.OperationalError` | `raw.execute(q)` — SQL پویا (`q`) | `UndefinedTable` / `UndefinedColumn` | معادل دقیق psycopg |
| `tools/analyze_old_events.py:139` | `sqlite3.Error` | `SELECT COUNT(1) FROM {table}` | `core.db.DatabaseError` (مشترک) | تبدیل به DatabaseError مشترک |
| `tools/analyze_old_events.py:150` | `sqlite3.Error` | `SELECT … FROM {table} ORDER BY rowid DESC LIMIT 5000` | `core.db.DatabaseError` (مشترک) | تبدیل به DatabaseError مشترک |
| `tools/analyze_old_events.py:181` | `sqlite3.Error` | `sqlite3.connect(mode=ro)` | `core.db.DatabaseError` (مشترک) | تبدیل به DatabaseError مشترک |
| `tools/find_insert_or_replace.py:204` | `sqlite3.Error` | `PRAGMA table_info({t})` | معادل ندارد ⇒ no-op در adapter | معادل دقیق psycopg |
| `tools/verify_server_timezone.py:200` | `sqlite3.Error` | `sqlite3.connect(mode=ro)` | `core.db.DatabaseError` (مشترک) | تبدیل به DatabaseError مشترک |
| `tools/verify_server_timezone.py:211` | `sqlite3.Error` | `SELECT timestamp, type FROM logs ORDER BY id DESC LIMIT ?` | `core.db.DatabaseError` (مشترک) | تبدیل به DatabaseError مشترک |
| `web/routes/import_db.py:201` | `sqlite3.IntegrityError` | `target_conn.execute(insert_sql, row)` — SQL پویا | `psycopg.errors.UniqueViolation` | معادل دقیق psycopg |
| `web/routes/stats.py` (`api_sensor_daily_stats`) | `sqlite3.OperationalError` | کل بلوک آماری سنسور (از E.3.4 دیگر `substr` ندارد؛ گروه‌بندی در پایتون) | `UndefinedTable` / `UndefinedColumn` | معادل دقیق psycopg |
| `web/routes/stats.py:131` | `sqlite3.DatabaseError` | همان بلوک آماری سنسور | `core.db.DatabaseError` (مشترک) | تبدیل به DatabaseError مشترک |

**جمع:** ۲۱ محل (کد برنامه/ابزار/تست: ۲۰، adapter: ۱) — **نیاز به بازبینی: ۰**.
توزیع تصمیم: ۱۴ «معادل دقیق psycopg»، ۶ «تبدیل به DatabaseError مشترک»، ۱ «خارج از دامنه (adapter)».
توزیع معادل: ۶ × `DatabaseError` مشترک، ۵ × `psycopg.OperationalError`، ۳ × `PRAGMA→no-op`، ۳ × `UndefinedTable/Column`، ۲ × `DuplicateColumn`، ۱ × `UniqueViolation`، ۱ × adapter.

---

## 3. Notes item by item

### ۳.۱ مهارهایی که «تحمل خطا»ی معنادار دارند (باید دقیق ترجمه شوند)

| محل | رفتار امروز | در پستگرس چه می‌شود |
|---|---|---|
| `database.py:259/263/282/361/950` | `pass` روی ALTER تکراری (idempotent) | باید `DuplicateColumn` را بگیرد؛ اگر نوع دیگری بیاید **نباید** قورت داده شود. راه امن: adapter کلاس `ColumnAlreadyExists` مشترک بدهد و کد همان را بگیرد. |
| `web/routes/import_db.py:201` | `IntegrityError` ⇒ `skipped += 1` (ادامهٔ حلقهٔ درج) | `UniqueViolation` ⇒ همان رفتار (شمارش skipped). نکته: در پستگرس، پس از خطا درون یک تراکنش، **ادامهٔ کار در همان تراکنش ممکن نیست** (تراکنش abort می‌شود). امروز همهٔ جدول‌ها در **یک** تراکنش درج می‌شوند (`web/routes/import_db.py:291`، `with db_connection(commit=True)`) ⇒ در فاز E باید یا `SAVEPOINT` به‌ازای هر ردیف بگذاریم یا تراکنش را به‌ازای هر ردیف بشکنیم؛ این یکی از دو موردی است که مهاجرت **باید** بازنویسی منطقی داشته باشد. |
| `web/routes/stats.py:127/131` | نمودار سنسور در نبود جدول، آرایه‌ی خالی می‌دهد | `UndefinedTable` / `DatabaseError` مشترک ⇒ همان رفتار |

### ۳.۲ ⚠ بررسی پیام خطا (سه محل)

`core/database.py:229`، `:235` و `create_admin.py:34` مقدار خطا را **رشته‌ای** می‌سنجند:

```python
if "locked" not in str(exc).lower():   # database.py:230، :236
    raise
if "full" in str(e).lower():           # create_admin.py:40
```

این بررسی‌ها اختصاصی SQLite‌اند (پیام «database is locked» / «database or disk is full»). در پستگرس باید با **نوع/SQLSTATE** جایگزین شوند (`psycopg` شیء `exc.sqlstate` می‌دهد؛ مثلاً `55P03` = lock_not_available). تا آن زمان، این سه محل «معادل دقیق psycopg + یادداشت» علامت خورده‌اند.

### ۳.۳ PRAGMA ها (سه محل + adapter)

| محل | PRAGMA | جایگزین |
|---|---|---|
| `core/database.py:228` (مهار در `:229`) | `journal_mode=DELETE` | ندارد؛ در پستگرس WAL/تنظیم سرور است ⇒ no-op |
| `core/database.py:234` (مهار در `:235`) | `synchronous=FULL` | ندارد ⇒ no-op |
| `core/db/connection.py:248` (مهار در `:249`) | `busy_timeout` | ندارد ⇒ no-op (کار adapter) |
| `tools/find_insert_or_replace.py:201` (مهار در `:204`) | `table_info` | `information_schema.columns` (فاز F) |

یک PRAGMA دیگر هم بدون مهار اجرا می‌شود: `core/database.py:224` (`busy_timeout=30000`) — در فهرست بالا نیست چون `except` ندارد، ولی مهاجرت باید به آن هم فکر کند.

**تصمیم:** محل‌های PRAGMA در فاز E با محافظ «اگر backend = sqlite» اجرا شوند (یا adapter خودش no-op کند). هر دو راه در سند `docs/db-adapter.md` مجازند؛ انتخاب نهایی در فاز E.

### ۳.۴ محل‌های خارج از کد برنامه

شش ردیف در `tools/` (که خودمان در همین فاز ساختیم) و سه ردیف در `tests/` هستند:

- ابزارهای E.0 (`verify_server_timezone`, `analyze_old_events`, `find_insert_or_replace`) عمداً **مستقیم** با `sqlite3` و `mode=ro` کار می‌کنند: این ابزارها باید روی نصب فعلی (SQLite) کار کنند و به adapter گره نخورند. تصمیم آن‌ها «تبدیل به DatabaseError مشترک» در حد **مسیر خطای خودشان** است، نه بازنویسی با adapter.
- `tests/golden_harness.py` و `tests/test_db_adapter.py` در فاز E هم SQLite-محور می‌مانند (golden روی SQLite ضبط شده)؛ ولی اگر golden روی پستگرس هم اجرا شود، این مهارها باید نوع مشترک را بگیرند.

---

## 4. Summary and links

- ✅ **انجام شد (E.4.4، کامیت `d4e6cf9`):** در `core/` و `web/` هیچ `except sqlite3.*` نمانده — صفر، و این با گارد AST در `tests/test_exceptions_pg.py::AppCodeContractTests` قفل شده است (اجرای تست، نه grep). همه به نوع مشترک `core.db.DatabaseError` یا کلاس‌های زیرمجموعه‌اش (`UniqueViolation`, `UndefinedTable`, `UndefinedColumn`, `ColumnAlreadyExists`, `DatabaseOperationalError`) رفته‌اند؛ ترجمه در خود adapter است (`_translate_error`) و **هیچ `except` دوگانه‌ای** نوشته نشد.
- دو مورد **بازنویسی منطقی** (نه فقط تغییر نوع) لازم دارند:
  ۱. سه بررسی پیام رشته‌ای (§۳.۲) ⇒ SQLSTATE؛
  ۲. مسیر `IntegrityError` در `import_db.py` (§۳.۱) ⇒ `SAVEPOINT` یا تراکنش به‌ازای هر ردیف.
- این سند منبع بخش «except sqlite3» در `docs/db-adapter.md` و ورودی سند سخت‌گیری پستگرس (گام E.0.10) است.

---

## 5. Reproduction

```bash
python tools/find_sqlite_excepts.py                    # گزارش خوانا (کد خروج 0 = همه تصمیم دارند)
python tools/find_sqlite_excepts.py --md /tmp/exc.md   # جدول Markdown
python tools/find_sqlite_excepts.py --json /tmp/exc.json
python tools/find_sqlite_excepts.py --include-adapter  # نمایش ردیف‌های adapter
```

ابزار SQL پویا را هم حل می‌کند: حلقه روی `_counter_column_alters()` ⇒ ستون‌ها از ثابت‌های `_COUNTER_BASE_COLUMNS + _COUNTER_ADDED_COLUMNS` باز می‌شود؛
و `execute(q)` با متغیر ⇒ به‌عنوان SQL پویا علامت می‌خورد (نه نادیده).

---

*این سند فقط مستندات است؛ هیچ تغییری در کد، schema یا داده ایجاد نمی‌کند.*

---

## 6. Status after E.4.4 (2026-10-04)

### ۶.۱ ۱۳ محلی که منتقل شدند (گروه A — مصرف‌کنندهٔ adapter)

| فایل | تعداد | نوع مشترک پذیرفته‌شده |
|---|---|---|
| `core/database.py` | ۷ | `ColumnAlreadyExists` (حلقهٔ `ALTER TABLE`) · `DatabaseOperationalError` (PRAGMAها) |
| `create_admin.py` | ۱ | `DatabaseOperationalError` + `DatabaseConnectionError` (راهنمای «دیسک پر» با `sqlstate == '53100'`) |
| `web/routes/stats.py` | ۲ | `UndefinedTable` / `UndefinedColumn` + `DatabaseError` |
| `web/routes/import_db.py` | ۱ | `UniqueViolation` (skip و ادامه — رفتار عیناً همان قبل) |
| `tests/golden_harness.py` | ۲ | `DatabaseError` مشترک |
| **جمع** | **۱۳** | `import sqlite3` از همهٔ این فایل‌ها حذف شد |

### ۶.۲ ۱۲ محلی که **آگاهانه دست‌نخورده** ماندند — ⬜ no-action، ابزار SQLite-only

این‌ها با `sqlite3.connect` خام کار می‌کنند (نه adapter)، پس نوع خطایشان واقعاً
`sqlite3` است و بازنویسی‌شان بی‌صدا می‌شکست (تصمیم مالک در E.4.4):

| فایل | خطوط |
|---|---|
| `tools/analyze_old_events.py` | ۱۳۹ · ۱۵۰ · ۱۸۱ |
| `tools/find_insert_or_replace.py` | ۲۰۴ |
| `tools/migrate_timestamps_to_utc.py` | ۲۲۲ · ۲۳۶ |
| `tools/set_cutover_marker.py` | ۱۴۶ · ۱۹۴ · ۲۳۹ |
| `tools/verify_server_timezone.py` | ۲۰۰ · ۲۱۱ |
| `tests/test_db_adapter.py` (استاب DB فیزیکی موقت) | ۷۲ |

فهرست مجازِ استثناها خودِ همین پنج فایل tool در تست
`AppCodeContractTests::test_sqlite_only_tools_are_explicitly_exempt` قفل شده است؛
اگر فایل ششمی به این فهرست اضافه شود، تست می‌شکند — یعنی استثنا آگاهانه می‌ماند،
نه فراموش‌شده.

### ۶.۳ شمارش رسمی امروز

```
$ python tools/find_sqlite_excepts.py --include-adapter
جمع: 16 محل (کد برنامه/ابزار/تست: 13، adapter: 3) | نیاز به بازبینی: 0
```

توضیح عدد ۱۶ (سنجش AST در E.4.11): ۱۱ `tools/` + ۱ `tests/` + ۱ هندلر
`except DatabaseError` در `web/routes/stats.py:184` (شکل جدیدِ همان خطای منتقل‌شده)
+ ۳ هندلر ترجمه‌گر در `core/db/connection.py` (`execute` / `executemany` /
`executescript`). شمارش `except sqlite3.*` خالص: **۱۲**، همگی بیرون از کد برنامه.
