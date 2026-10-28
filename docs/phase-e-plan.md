# Execution plan for phase E — PostgreSQL + Alembic + SNMP Exporter + Dashboard

**دامنه:** فاز E روی شاخه‌ی `phase-c`؛ هدف نهایی: انتقال منبع حقیقت از SQLite به
**PostgreSQL 16** روی همان ویندوزسرور، بدون تغییر منطق دامنه، با برگشت‌پذیری کامل تا
لحظه‌ی cutover.
**مرجع‌های وابسته:** `docs/cutover-plan.md` (تعریف cutover و ۷ گام اجرایی)،
`docs/postgresql-strictness.md` (۱۷ مورد سخت‌گیری)، `docs/sql-paths-inventory.md`
(۱۲۱ محل SQL خام)، `docs/timestamp-consumers.md` §۶ (الزام آگاه-UTC)،
`docs/postgresql-setup.md` (نصب موجود مالک)، `docs/db-adapter.md` (لایه‌ی `core/db/`).
**قیدهای ثابت کل فاز:** PostgreSQL ۱۶ تنها منبع حقیقت · بدون Prometheus/Grafana ·
بدون Linux/Docker/WSL (بومی ویندوزسرور) · UI فارسی/RTL · بدون حذف `/metrics` یا تغییر
۶۷ روت · منطق دامنه دست‌نخورده · هر گام = یک کامیت.

**قواعد مشترک هر گام (بدون استثنا):**

۱. معیار پذیرش هر گام: **۲۸۱ تست سبز در هر دو TZ** (`UTC` و `Asia/Tehran`)،
   **۰ skipped**، و **۱۰/۱۰ golden با تفاضل صفر**.
۲. اجرای تست‌ها در **شل تمیز**: `env -u DB_BACKEND -u PG_DSN` (تا گام E.2 که خودِ
   backend هدفِ سنجش می‌شود).
۳. هیچ گامی منطق دامنه را عوض نمی‌کند؛ تغییرات فقط در لایه‌ی persistence/adapter،
   migrationها، ابزارها و اسناد است.
۴. هر گام یک کامیت با پیام انگلیسی به سبک موجود + گزارش کوتاه فارسی در پایان.
۵. در ابهام یا برخورد با قید فاز ⇒ **توقف و پرسش**؛ هیچ تصمیمی حدسی گرفته نمی‌شود.
۶. پایان هر گام: `git status --short` بدون `logs.db`، و مسیر برگشت مستند شده است.

---

## 1. Status of E.1 — closed (baseline)

| # | گام | کامیت | خروجی |
|---|---|---|---|
| E.1.1 | راهنمای نصب PG برای نصب موجود مالک | `3d59577` | `docs/postgresql-setup.md` |
| E.1.2 | وابستگی‌ها: `psycopg[binary]>=3.1,<4` + `alembic>=1.13,<2` | `806d69b` | `requirements.txt` |
| E.1.3 | `alembic init` + resolver دوبکندی (بدون ORM) | `c535a7a` | `alembic.ini`, `alembic/env.py` |
| E.1.4 | migration اولیه: ۱۰ جدول برنامه + `meta` | `bc51307` | `alembic/versions/0001_initial_schema.py` |
| E.1.5 | ابزار آزمون اتصال/تنظیمات/schema روی PG | `842bd61` | `tools/test_pg_connection.py` |
| E.1.6 | بررسی دسترس‌پذیری DB در `start.bat` پیش از Flask | `daf7b1a` | بلوک «بررسی دیتابیس» |
| E.1.7 | ابزار ساخت idempotent دیتابیس/نقش | `c7b4e7b` | `tools/init_pg_database.py` |
| E.1.8 | همین سند | — | `docs/phase-e-plan.md` |

**شواهد پذیرش E.1 (اندازه‌گیری‌شده):**

- **SQLite:** `upgrade head` ⇒ ۱۲ جدول (`alembic_version` + ۱۱)؛ `alembic_version=[('0001',)]`؛
  چرخه‌ی `upgrade → downgrade base → upgrade` کامل؛ DDL `logs`:
  `id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT`. مقایسه‌ی ساختاری با دیتابیسِ ساخته‌ی
  خود اپلیکیشن: نوع/ترتیب/پیش‌فرض/PK و ۱۳ ایندکس صریح و ۶ autoindex یکسان؛ تنها تفاوت
  عمدی = ۹ ستون `NOT NULL` صریح (۷ `id` + `printer_counters.ip` +
  `cartridge_yield_profiles.cartridge_key`) که در docstring همان migration مستند شده است.
- **PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2):** `upgrade head` ⇒ ۱۱ جدول؛ idها `BIGINT`
  با `nextval(...)`؛ `downgrade base` ⇒ تنها `alembic_version` با ۰ سطر؛ حالت offline
  (`--sql`) ⇒ ۲۵ دستور `CREATE` با `BIGSERIAL`/`DOUBLE PRECISION`.
- **اتصال و schema:** `tools/test_pg_connection.py --quiet` ⇒ exit 0 با
  `[DB] PG 16.15 | db=printguard | user=printguard | tz=UTC | tables=10/10`.
- **دیتابیس هدف:** `printguard` — `ENCODING=UTF8`, `LC_COLLATE=C`, `LC_CTYPE=C`,
  `TimeZone=UTC`؛ خوشه‌ی `16/main` روی `127.0.0.1:5432`؛ احراز هویت `scram-sha-256`؛
  نقش `printguard` بدون `CREATEDB`/`SUPERUSER` (کمترین-مجوز).
- **دیتابیس‌های موجود روی خوشه:** `postgres`, `template0`, `template1`, `printguard`
  (دیتابیس‌های موقت آزمون در پایان همان آزمون‌ها حذف شدند).
- **نکته‌ی عملیاتی:** چون نقش برنامه مجوز `CREATEDB` ندارد، ساخت دیتابیس فقط با نقش
  ادمین ممکن است؛ `tools/init_pg_database.py` همین را می‌پذیرد (`--admin-dsn` /
  `PG_ADMIN_DSN` / کاربر `postgres`).
- **دو تفاوت عمدی و مستندشده‌ی migration:** (الف) ۹ ستون `NOT NULL` سخت‌گیرانه‌تر؛
  (ب) `BIGSERIAL` به‌جای `IDENTITY` — ارتقا به `IDENTITY` کار گام E.4 (بند ۳.۵).
- **وضعیت backend:** `DB_BACKEND=postgresql` در E.1 **فعال نشد** (قید فاز)؛ اجرای
  برنامه و تست‌ها همچنان SQLite است.

**مسیر برگشت E.1:** هیچ کد production تغییر نکرده است؛ `git revert` هر کامیت + حذف
`alembic/` و دو ابزار، و بازگشت `requirements.txt` — بدون اثر روی داده.

---

## 2. E.2 — enabling the dual backend

**هدف:** `DB_BACKEND=postgresql` بدون تغییر منطق دامنه کار کند؛ `sqlite` همچنان پیش‌فرض
و سالم بماند.

**کار:**
- برداشتن گارد `UnsupportedBackendError` برای مقدار `postgresql` در
  `core/db/connection.py` (تابع انتخاب backend و `connect()`)، با حفظ همان مسیر برای
  مقدار `sqlite`؛
- اتصال PG با `psycopg` + تنظیم صریح `TimeZone=UTC` روی اتصال (✔ انجام شد)؛
  **انتقال مورد PRAGMA به E.4:** قید گام E.2 تغییر `core/database.py` را ممنوع کرده است،
  پس حذف PRAGMAهای `core/database.py:224/228/234` در مسیر PG و جانشین‌کردنشان با
  `lock_timeout`/`statement_timeout` (بند ۱۲ سند سخت‌گیری) در E.4 انجام می‌شود — جایی که
  تغییر آن فایل مجاز است. (`connect_sqlite` هم PRAGMA را فقط روی مسیر sqlite می‌زند.)
- نگه‌داشتن ترجمه‌ی placeholderها (`?` → `%s`) در `core/db/placeholders.py` — امروز مخزن
  هیچ placeholder نام‌داری ندارد (فهرست SQL: ۱۲۱ محل، همه qmark)؛
- `ALEMBIC_DB_URL`/`PG_DSN` و اولویت resolver در `alembic/env.py` دست‌نخورده می‌ماند
  (E.1.3) — ابزارها و migration و اپلیکیشن **یک** منبع برای آدرس داشته باشند.

**خروج از گام:** اجرای برنامه با `DB_BACKEND=postgresql` برای **مسیرهای کاربری از پیش
ساخته‌شده و دست‌نخورده** (بند ۷ سند سخت‌گیری: `TEXT`→`TIMESTAMPTZ` قطعی نیست، پس در این
گام فقط خواندن و مسیرهای ساده‌ی نوشتن بررسی می‌شود و ریسک‌دارها به E.3/E.4 واگذار می‌شوند).

**معیار پذیرش:** `DB_BACKEND=sqlite` ⇒ ۲۸۱ passed؛ `DB_BACKEND=postgresql` ⇒ همان
۲۸۱ بدون skip و بدون تست «فقط SQLite»؛ اجرای دستی دیتابیسِ `printguard` پس از
`alembic upgrade head` بدون خطای اتصال/ترجمه.

**مسیر برگشت:** `DB_BACKEND=sqlite` ⇒ بازگشت بی‌هزینه (هیچ داده‌ی تازه‌ای در PG در این
گام تولید نمی‌شود؛ شرط با `n_tup_*` سنجیده می‌شود — §۳.۵ سند cutover).

---

## 3. E.3 — UTC-aware writers ✅ **closed (2026-10-04)**

**هدف:** اجرای **الزام آگاه-UTC** (`docs/timestamp-consumers.md` §۶)؛ این گیتِ سختِ
پیش از cutover است.

**نتیجه:** هشت گام (به‌جای هفت)، هر گام یک کامیت — گزارش کامل: پیام هر کامیت و
`docs/timezone-contract.md` §۹:

| گام | کامیت | خلاصه |
|---|---|---|
| E.3.1 | `4540d31` | `core/time_utils.py` (فرمت `…ffffffZ`) + ۱۸ تست + `tzdata` |
| E.3.2 | `05446c1` | ابزار مهاجرت + ۲۰ تست؛ دو باگ واقعی همان‌جا لو رفت و رفع شد |
| E.3.3 | `d806324` | همهٔ نویسندگان ⇒ `utc_now_iso` (۲۰ فایل) |
| E.3.4 | `9902c9b` | خوانندگان آگاه-UTC + ۳ هِلپر + گروه‌بندی روزی در پایتون + نمایش محلی |
| E.3.5 | `46b193f` | مهاجرت `logs.db` (۱۰۶ سلول، بکاپ، بازبینی ۵‌ادعایی، idempotent) |
| E.3.6 | `5f05173` | قفل ساعتِ `utc_now_iso` در هارنس + بازضبط ۱۰ golden |
| E.3.7 | `4791315` | به‌روزرسانی گاردها و تست‌های وابسته به فرمت + ۱۱ تست جدید |
| TD-003 | `f5deb7a` | نرمال‌ساز golden دیگر `N/A` را خراب نمی‌کند |
| E.3.8 | همین کامیت | مستندسازی + ثبت TDهای تازه‌کشف |

**معیار پذیرش — محقق‌شده:** ۳۶۰ تست سبز در هر دو TZ (با PG فعال؛ بدون PG همان
منها ۹ تست قرارداد PG)، ۱۰/۱۰ golden تفاضل-صفر، `logs.db` مهاجرت‌شده با بکاپ
بایت‌به‌بایت و بازبینی مستقل، صفر تغییر schema/Alembic، صفر هشدار جدید pyflakes.
⚠ عدد تست در بند «قواعد مشترک» ۲۸۱ بود؛ با تست‌های افزودهٔ E.0.7/E.1/E.2/E.3 به
۳۶۰ رسیده است (رشد آزمایشی، نه حذف/غیرفعال‌سازی).

**کار:**
- همه‌ی writers به `datetime.now(timezone.utc)` مهاجرت کنند — ۲۶ فراخوانی از مسیر
  پیش‌فرض `core/database.py:658` + مسیرهای صریح فهرست‌شده در جدول §۱.۱ همان سند
  (`core/collectors/base.py`, `core/poller.py`, `web/routes/logs.py`,
  `web/routes/printers.py`, `core/enhanced_collector.py`)؛
- مصرف‌کنندگانِ مقایسه‌ای (۵ debounce، ۲ فیلتر بازه، ۱ prune) با همان مرجع آگاه-UTC
  هم‌تراز شوند تا رفتار یکسان بماند؛
- `td-003` (نرمال‌ساز golden: `N/A` → `N<PATH>`) در همین گام و همراه بازضبط golden
  حل شود (فهرست بدهی فنی: `docs/tech-debt.md`).

**معیار پذیرش:** همان ۲۸۱ تست در دو TZ؛ golden فقط با **re-record صریح** و
diff قابل‌توجیه (fixtureها بایت‌به‌بایت تغییر نکنند مگر PII یا بازضبط اعلام‌شده)؛
هیچ سطری با timestamp naive در دو مسیر نوشتنِ آزمون‌شده نماند.

**مسیر برگشت:** یک کامیت؛ `revert` آن E.3 را برمی‌گرداند (schema دست‌نخورده).

---

## 4. E.4 — PG strictness: handling of 🔴 and 🟡 items — ✅ **closed (14 code commits + this documentation step)**

**وضعیت E.4: بسته** — `3f4a0e7 … 56710ea` (۱۴ کامیت، سنجیده‌شده با
`git rev-list --count 3f4a0e7^..56710ea` ⇒ ۱۴) + این گام مستندات. جدول ۱۷ ردیفی با وضعیت هر ردیف در
`docs/postgresql-strictness.md` بند ۷؛ اعداد خطاهای `except` در
`docs/sqlite-excepts-inventory.md` بند ۶.

| گام | کامیت | محتوا |
|---|---|---|
| E.4.1 | `3f4a0e7` | migration `0002` — پنج ستون `BOOLEAN` (+ `DEFAULT` بولی، no-op روی SQLite) |
| E.4.2 | `52cf521` | migration `0003` — هفت `id` ⇒ `IDENTITY` (ترتیب `DROP DEFAULT` ⇒ `DROP SEQUENCE` ⇒ `ADD IDENTITY` ⇒ `RESTART`) |
| E.4.3 | `e12aeb6` | `insert_returning_id` در adapter (PG ⇒ `RETURNING id`؛ صفر ردیف ⇒ `None`) |
| E.4.5 | `fae0330` | DDL backend-aware: `core/db/schema.py` + **no-op** روی PG (Alembic تنها منبع DDL) + `tools/test_ddl_bootstrap.py` |
| E.4.6 | `9b26ee2` | حذف `cursor()` خام از کد برنامه (۴ محل) + گارد AST |
| E.4.7 | `f97ce97` | `executescript` روی PG با `core/db/scripts.py::split_sql_statements` (با walker؛ عمداً نه `str.split(";")`) |
| E.4.4 | `d4e6cf9` | خانوادهٔ مشترک خطا: `_translate_error` + ۵ کلاس + مهاجرت ۱۳ محل + گارد AST (صفر `except sqlite3.*` در `core/`+`web/`) |
| E.4.8 (پیش‌نیاز) | `282eb2d` | `core/db/schema.py::schema_columns` — فهرست ستون‌ها، cross-backend |
| E.4.8 | `51e72a0` | **SAVEPOINT هر ردیف** در `import_db` + `_prepare_row` (بولی) + کلاس `UnsupportedFeatureError` |
| E.4.8b | `63a33fa` | **گارد یکپارچهٔ import روی PG** (به‌جای سه گارد: ردیف ۱۱ + TD-007 + TD-009) |
| E.4.9 | `07502f9` | `ConnectionAdapter.json_extract(...)` + مهاجرت ۱۰ نقطه (`::jsonb` چون `details` روی PG `TEXT` است) |
| E.4.9b | `cbd6d67` | `/metrics`: `MAX(printer_name)` برای `GROUP BY` سخت PG (تصمیم: حفظ تعداد سری) ⇒ TD-010 |
| E.4.10 | `b25c761` | `tests/test_strictness_pg.py` — قفل پذیرش + `NoSilentSkipsTests` («با PG، صفر skip») |
| E.4.11a | `56710ea` | `list_users`: `COLLATE NOCASE` ⇒ `lower(username)` (ردیف ۳؛ روی PG `[]` بی‌صدا می‌داد) |
| E.4.11 | همین کامیت | مستندات: جدول ۱۷ ردیفی وضعیت + شمارش جدید `except`ها + TD-011 |

**معیار پذیرش E.4 (سنجیده‌شده در آخرین اجرا):** ۴۶۹ تست سبز / **۰ skip** در هر دو
backend و دو TZ (مرجع برنامه: ۲۸۱؛ افزایش‌ها از E.1 تا E.4 عمدی و در پیام هر کامیت
مستند است)؛ golden ۱۰/۱۰ تفاضل‌صفر؛ هر ردیف ۱۷تایی یا «بسته» یا «⬜ no-action با دلیل»
یا «⏳ E.6»؛ `logs.db` بایت‌به‌بایت یکسان.

**هدف:** بستن ۱۷ مورد `docs/postgresql-strictness.md` به‌صورت ترجمه‌ی صریح (نه آزمون‌وخطا).

| مورد سند | کار E.4 |
|---|---|
| ۳ (`COLLATE NOCASE`) | `ORDER BY lower(username)` (سازگار با `C`) یا تصمیم collation در E.6 |
| ۸ (`AUTOINCREMENT`) | ارتقای `BIGSERIAL` ⇒ `BIGINT GENERATED BY DEFAULT AS IDENTITY` در migration + تصمیم `setval` پس از مهاجرت فاز F |
| ۹ (`lastrowid`) | `INSERT … RETURNING id` در `core/database.py:510` |
| ۱۱ (`sqlite_master`/`PRAGMA table_info`) | `information_schema`/`pg_catalog` برای `web/routes/import_db.py` |
| ۱۲ (PRAGMAها) | جایگزینی با `lock_timeout`/`statement_timeout` — **کامل در E.4** (قید E.2 تغییر `core/database.py` را ممنوع کرده بود) |
| ۱۳ (`rowid` ضمنی) | `ORDER BY id` در `tools/analyze_old_events.py` |
| ۱۴ (`INSERT OR REPLACE`، ۶ محل) | `ON CONFLICT … DO UPDATE` طبق `docs/insert-or-replace-inventory.md` |
| ۱۵ (`json_extract`، ۱۰ نقطه) | `JSONB` + `->>` یا حفظ `TEXT` — تصمیم در E.6، اجرا در E.4 |
| ۱۶ (`except sqlite3.*`، ۲۱ محل) | SAVEPOINT / تقسیم `executescript` |
| ~~۱ (`substr(timestamp,1,10)`)~~ | **رفع شد در E.3.4** — هیچ `substr`ی نماند؛ مرز روز/سبد روزانه در پایتون (`to_local_day`/`_local_day_start`). در E.4 فقط بازبینی می‌شود که با `TIMESTAMPTZ` کوئری‌ها همان‌طور کار کنند. |
| ۵ (boolean 0/1) | تصمیم E.6 (`BOOLEAN` یا `int(...)` پیش از bind) |
| ۱۰ (`executemany`) | مرز تراکنش صریح + جلوگیری از abort کل تراکنش با یک سطر بد |

**معیار پذیرش (متن برنامه، پیش از اجرا):** ۲۸۱ تست در هر دو backend و دو TZ؛ golden
۱۰/۱۰؛ هر مورد سند سخت‌گیری یا «بسته» یا «آگاهانه به تأخیر افتاده با دلیل مستند».
نتیجهٔ سنجیده‌شده در جدول بالا ثبت شده است.

**مسیر برگشت:** هر ترجمه در یک کامیت مستقل با test پوشش‌دهنده؛ `revert` تک‌کامیتی.

---

## 5. E.5 — full test run on **both** databases — ✅ **closed (3 code/test commits + this documentation step)**

**هدف (تصمیم مالک، گزینهٔ ۱):** صفر شکست روی هر دو backend + ترجمهٔ سه محل زندهٔ
`INSERT OR REPLACE`، با هِلپر در `core/db/` و ایزولاسیون در `tests/conftest.py`.

### ۵.۱ آنچه انجام شد

| کامیت | محتوا |
|---|---|
| `08c9025` | `core/db/upsert.py::upsert_sql` + ترجمهٔ سه محل زنده + `tests/test_upsert_cross_backend.py` (۲۲ تست) + `ensure_printer_counters_columns()` روی PG بی‌عمل |
| `f99d579` | `tests/conftest.py`: اسکیمای اختصاصی هر اجرا (`printguard_test_<uuid12>`) + `alembic upgrade head` رویش + `DROP … CASCADE` + بازنشانی داده پیش از هر تست |
| `03f140b` | marker `sqlite_only` (ثبت در `pytest.ini`) + auto-skip و شمارش skip در خلاصهٔ پایانی — تصمیم مالک: گزینهٔ «ج» |

**قاعدهٔ ترجمه:** SQLite ⇒ `INSERT OR REPLACE` (بایت‌به‌بایت همان جملهٔ تاریخی)؛
PG ⇒ `INSERT … ON CONFLICT (کلید) DO UPDATE SET c = EXCLUDED.c`. ترجمه فقط وقتی مجاز
است که جمله **همهٔ** ستون‌ها را بنویسد (§۳.۱ `docs/insert-or-replace-inventory.md`)؛
این شرط در `ColumnCompletenessTests` با `schema_columns()` قفل شده است.

**سه محل منتقل‌شده:** `core/database.py:save_printer_counters` (کلید `ip`)،
`core/yield_engine.py:_upsert_state` (`printer_ip, color`)،
`core/yield_engine.py:_upsert_profile_if_high` (`cartridge_key`).
**no-action (تصمیم صریح):** `core/enhanced_collector.py` (کد مرده، ۱۳ از ۲۱ ستون) و
`tests/golden_harness.py`؛ `web/routes/import_db.py` زیر گارد E.4.8ب تا E.6.
allow-list سه‌تاییِ `SourceContractTests` نمی‌گذارد چهارمین جملهٔ دستی بی‌صدا اضافه شود.

### ۵.۲ معیار پذیرش و اعداد سنجیده‌شده

**معیار (نسخهٔ به‌روز‌شدهٔ E.5):**
۱. `python -m pytest tests/ -q` با `PG_DSN` ⇒ **همه سبز، ۰ skipped** در `TZ=UTC` و
`TZ=Asia/Tehran`؛
۲. `DB_BACKEND=postgresql python -m pytest tests/ -q` ⇒ **۰ شکست**؛ skipهای مجاز
فقط دو دسته‌اند: تست‌های PGِ بدون `PG_DSN` و تست‌های اعلام‌شدهٔ `sqlite_only`
(هیچ skip بی‌دلیلی نباید بماند — `tests/test_strictness_pg.py::NoSilentSkipsTests`).
۳. golden ۱۰/۱۰ تفاضل‌صفر و `logs.db` بایت‌به‌بایت؛ `tools/test_dual_backend.py` ⇒
OK=18 / EXPECTED=1 / FAIL=0؛ `pyflakes core web` روی ۳۶ باقی می‌ماند.

| حالت اجرا | نتیجه |
|---|---|
| `TZ=UTC` SQLite (با `PG_DSN`) | **491 passed / 0 failed / 0 skipped**، ۸۲ subtest |
| `TZ=Asia/Tehran` SQLite (با `PG_DSN`) | **491 passed / 0 failed / 0 skipped** |
| `TZ=UTC DB_BACKEND=postgresql` (با `PG_DSN`) | **419 passed / 0 failed / 72 skipped** — هر ۷۲ skip اعلام‌شده (skip: 72) |
| SQLite بدون `PG_DSN` | 441 passed + 50 skipped (تست‌های PG، skip با پیام در `skipUnless`) |
| golden / dual-backend / pyflakes / `logs.db` | ۱۰/۱۰ · 18/1/0 · ۳۶ · `e29d40630c45fc1d392c9a11d60b8de2` |

ترتیب اعداد PG: پیش از ایزولاسیون `95 failed / 396 passed` ⇒ با `conftest`
`52 failed / 439 passed` ⇒ با marker `0 failed / 419 passed / 72 skipped`.

### ۵.۳ `sqlite_only` — تصمیم، فهرست و دلیل هر محل

**تصمیم مالک (گزینهٔ «ج» + auto-skip):** فایل‌ها و کلاس‌هایی که SQL یا اسکیمای
*مختص SQLite* می‌نویسند، زیر `DB_BACKEND=postgresql` **رد** می‌شوند — نه اینکه
تست‌ها بازنویسی شوند. قاعده در `tests/conftest.py::pytest_collection_modifyitems`
است و marker در `pytest.ini` ثبت شده؛ روی مسیر SQLite هیچ اثری ندارد (پس «۰
skipped» آن مسیر دست‌نخورده می‌ماند). شمارش skip در خلاصهٔ پایانی چاپ می‌شود تا
هیچ رد‌شدنی بی‌صدا نباشد.

| محل | سطح | دلیل |
|---|---|---|
| `tests/test_golden.py` | فایل | fixtureهای golden با SQLite تعریف شده‌اند (`golden_harness` همان `INSERT OR REPLACE` no-action را دارد) |
| `tests/test_db_migration_columns.py` | فایل | مهاجرت ALTER روی *فایل* SQLite؛ روی PG منبع DDL فقط Alembic است (قاعدهٔ E.4.5) |
| `ConnectionAdapterTests` · `WireEquivalenceTests` · `InsertReturningIdTests` · `ExistingCodeCompatibilityTests` · `BackendSelectionTests` در `tests/test_db_adapter.py` | کلاس | `CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT…)` و `INSERT OR REPLACE INTO logs …` درون خودِ تست |
| `SqliteBootstrapTests` در `tests/test_ddl_bootstrap.py` | کلاس | آزمونِ همان DDL تاریخی SQLite؛ دو کلاس PGِ همان فایل (**`PostgresBootstrapNoDdlTests` / `PostgresBootstrapLiveTests`**) دست‌نخورده و زیر PG اجرا می‌شوند |
| `TestPrintInvariant` · `TestPrintDedup` · `TestCounterResetDedup` · `TestToshibaPaperSplit` در `tests/test_poll_event_invariant.py` | کلاس | فرض «هر `chdir` یک DB خالی»؛ `TestPollSerialization` سرجای خود اجرا می‌شود |
| `test_stats_daily_route` · `test_stats_daily_skips_unmigrated_rows` · `test_import_analyze_route` در `tests/test_smoke_routes.py` | متد | دو اول: DB خالی هر chdir؛ سومی: `/api/import/analyze` روی PG هنوز `PRAGMA`/`sqlite_master` می‌زند (TD-008، ترجمه در E.6 با TD-007) |
| `test_4_…` · `test_5_…` در `tests/test_rbac_admin_guard.py` | متد | «اولین ثبت‌نام ⇒ admin» روی PG برقرار نیست، چون `setUp` همان کلاس ۴ کاربر در اسکیمای جلسه ساخته است |
| `SqliteMetricsSeriesTests` در `tests/test_metrics_pg.py` | کلاس | `json_extract()` خام در SQLِ تست (کلاس PGِ همان فایل اجرا می‌شود) |
| `ImportEndpointSqliteTests` در `tests/test_import_db_guard.py` | کلاس | شاخهٔ SQLite گارد؛ سه کلاس PGِ همان فایل اجرا می‌شوند |

**انتخاب سطح (کلاس/متد به‌جای فایل، جز دو فایل اول):** دستور «یک خط در هر فایل» حفظ
شد، ولی در فایل‌های *مختلط* دکوریتور روی همان کلاس/متد نشست تا پوشش PGِ کلاس‌های
PG-محور آن فایل‌ها (مثل `PostgresBootstrapLiveTests`) بی‌دلیل از دست نرود.

**محدودیت صریح و دانسته:** markerها فقط در `pytest` اثر دارند. مسیر دوم CI
(`python -m unittest discover`) **همیشه SQLite** است و در `.github/workflows/tests.yml`
هیچ `DB_BACKEND`/`PG_DSN` به آن داده نمی‌شود؛ اجرای `unittest discover` با
`DB_BACKEND=postgresql` همچنان `47 failures + 33 errors` می‌دهد و **پشتیبانی نمی‌شود**
(سندسازی شد؛ رفعش همان بازنویسی تست‌هاست که در گزینهٔ «ب» بود).

**مسیر برگشت:** `git revert` هر یک از سه کامیت مستقل است؛ رفتار SQLite در هیچ‌کدام
تغییر نکرده (golden ۱۰/۱۰ و ۴۹۱ تست سبز گواه همان است).

## 6. E.6 — decisions (locked before cutover) — ✅ **closed (7 code/test commits + 2 documentation commits, 2026-10-04)**

طبق بند ۲ سند cutover، این موارد باید **صریح و مکتوب** شوند:

| # | تصمیم | گزینه‌ها | مرجع |
|---|---|---|---|
| ۱ | مرز روز | نیمه‌شب تهران (`AT TIME ZONE 'Asia/Tehran'`) | بند ۱ |
| ۲ | collation سرور | `C` (وضع فعلی دیتابیس) یا ICU/`fa-IR` | بند ۴ |
| ۳ | boolean ستون‌ها | `BOOLEAN` یا `int(...)` پیش از bind | بند ۵ |
| ۴ | `TimeZone` اتصال | تثبیت `UTC` | بند ۷ |
| ۵ | idها | `IDENTITY` + `setval` پس از مهاجرت | بند ۸ |
| ۶ | `RETURNING id` | اجرا در E.4 | بند ۹ |
| ۷ | `import_db.py` | بازنشستگی یا ترجمه (تصمیم محصولی) | بند ۱۱ |
| ۸ | `logs.details` | `JSONB` یا `TEXT` | بند ۱۵ |

**وضعیت:** هر هشت سطر بالا در E.6 تصمیم گرفته شد؛ به‌علاوهٔ شش بندِ
دستور مالک (۶.۱ … ۶.۱۰). سند کامل تصمیم‌ها با دلیل، جایگزینِ ردشده و
اثر: **`docs/phase-e-decisions.md`**. اعداد و کامیت‌ها:
`.1 import_db` `0ce1b33` · `.2 pg_dump` `5570992` · `.3 collation` `8ef0a9f` ·
`.4 JSONB` `0a21aed` · `.5 TD-001` `931bb42` · `.6 TD-002` `de0a602` ·
`.7 TD-004` `f3d4b7c` · `.8/.10/.11` مستندات (این گام و کامیت بدهی فنی).
تنها دو بند، کد production را لمس کردند (۶.۲ در `tools/`، ۶.۴ در migration و
چهار خط خواننده)؛ بقیه گارد/تست/سند است.

**معیار پذیرش (برقرار شد):** سند تصمیم با انتخاب + دلیل + جایگزین ردشده + اثر
بازگشت؛ هر تصمیمِ مالک-محور پرسیده شد (دامنۀ JSONB و رفتار ردیف نامعتبر،
پاسخ مالک: فقط `logs.details` + توقف با شمارش). SQLite و PG هر دو صفرشکست،
golden ۱۰/۱۰.
**مسیر برگشت:** تصمیم‌ها سند‌اند؛ تغییرشان تا پیش از cutover آزاد است. تنها
تغییرِ schema (مهاجرت `0004`) با `alembic downgrade 0003` برمی‌گردد.

---

## 7. E.7 — cutover preparation (without crossing the boundary)

**هدف:** همه‌ی پیش‌نیازها آماده و سنجیده شوند، ولی **cutover در فاز E رخ نمی‌دهد**
(طبق `docs/cutover-plan.md`: cutover = اولین نوشتن Flask روی PG، و گام‌های ۳ تا ۷ در فاز F).

**کار:**
- گام ۱ سند cutover: dry-run فقط-خواندنی با writers خاموش + ثبت `n_tup_*` ابتدا/انتها
  (اختلاف باید صفر باشد)؛
- گام ۲: همان خروجی E.5 به‌عنوان شاهد؛
- فهرست بازبینی بند ۸ سند cutover تکمیل و امضا شود؛
- آماده‌سازی `archives\logs.db.bak` (گام ۳) به‌عنوان بخشی از چک‌لیست عملیاتی — تولید واقعی
  در فاز F؛
- `tools/set_cutover_marker.py --verify` روی دیتابیس هدف؛
- گزارش «آمادگی cutover» با سنجه‌های عددی، بدون هیچ زمان‌بندی تخمینی.

**معیار پذیرش:** چک‌لیست بند ۸ کامل؛ صفر نوشتن در PG؛ SQLite همچنان منبع حقیقت.
**مسیر برگشت:** بی‌اثر (هیچ نوشتنی رخ نداده).

**وضعیت E.7 — بسته (۴ کامیت):**

| گام | خروجی | کامیت |
|---|---|---|
| E.7.1 | [`cutover-runbook.md`](cutover-runbook.md) — بدنه بدون هیچ عدد زمانی؛ زمان‌ها فقط در «پیوست الف» (اندازه‌گیری‌شده در sandbox) | `1f23518` |
| E.7.2 | `tools/check_cutover_readiness.py` — ۹ سنجه + چک‌لیست رنگی + exit 0/1/2، با ۴۲ تست | `8623655` |
| E.7.3 | [`e7-sandbox-dryrun.md`](e7-sandbox-dryrun.md) — شاهد اجرای sandbox و ۴ اصلاح خودِ runbook | `0c4b8db` |
| E.7.4 | [`phase-e-completion-report.md`](phase-e-completion-report.md) — گزارش کامل‌شدن فاز E | (این کامیت) |

گام‌های ۴ و ۶ runbook که به `tools/migrate_sqlite_to_pg.py` نیاز دارند با برچسب «فاز F» باز
هستند (ابزار ساخته نشده — طبق دستور مالک در این فاز فقط runbook + چک آمادگی). دو اصلاح
واقعیتی که در این سند و §۳ `cutover-plan.md` لازم شد: کلید `POLLING_ENABLED` در کد وجود
ندارد، و شمارش جدول‌ها ۱۰ نیست (۱۱ جدول برنامه + `alembic_version`). **عبور از مرز انجام
نشد:** صفر نوشتن روی سرور مالک، صفر push، `logs.db` بایت‌به‌بایت دست‌نخورده.

---

## 8. Open items related to phase E

| مورد | وضعیت | یادداشت |
|---|---|---|
| push شاخه‌ی `phase-c` (E.0.9) | **باز — نیازمند تصمیم مالک** | اعتبارنامه‌ی GitHub در محیط توسعه موجود نیست؛ طبق دستور مالک هیچ توکنی گرفته نمی‌شود. |
| TD-001 کد مرده‌ی `_save_counters_to_db` | **✅ بسته (E.6.5، `931bb42`)** | حذف شد؛ قفل در `tests/test_upsert_cross_backend.py` |
| TD-002 بازتاب `next` در ۴۰۱ | **✅ بسته (E.6.6، `de0a602`)** | allowlist + بستن حفرۀ `\`؛ ۱۱ تست |
| TD-003 نرمال‌ساز golden (`N/A` → `N<PATH>`) | **بسته شد (E.3، `f5deb7a`)** | — |
| TD-004 `log` تعریف‌نشده در `web/routes/security.py` | **✅ بسته (E.6.7، `f3d4b7c`)** | بایندینگ logger + نگهبان AST؛ pyflakes ۳۶⇒۳۵ |
| TD-005 بازنویسی manifest ابزار مهاجرت در اجرای بعدی | باز | کشف در E.3؛ شاهد apply فقط با اجرای dry-run روی بکاپ بازتولیدشدنی است |
| TD-006 `sensor_alert` به ساعت محلی سرور تکیه دارد | باز | کشف در E.3؛ تصمیم «ساعات سکوت» است نه timestamp ذخیره‌شده |
| `import_db.py` | ✅ **تصمیم بسته (E.6.1، `0ce1b33`)** | بازنشستگی نوشتن روی PG + `analyze` خواندنی؛ ترجمه ⇒ ابزار فاز F |
| TD-007 `INSERT OR REPLACE` در import روی PG | باز — **تصمیم E.6.1: بازنشستگی** | `ON CONFLICT DO UPDATE` رد شد (معنای ستون‌های نگانه‌شده فرق می‌کرد)؛ رفع ⇒ فاز F |
| TD-008 `_columns`/`_tables` در import (SQLite-only) | باز | جانشین آماده: `schema_columns`/`present_tables` |
| TD-009 `_exists_by_cols` ⇒ `col IS $1` روی PG | باز — **بدون رفع (E.6.9)** | زیر گارد بازنشستگی ۶.۱ بی‌مصرف است؛ `skip` روی SQLite دست‌نخورده |
| TD-010 `printer_name` در `/metrics` با `MAX()` | باز (تصمیم ثبت‌شده) | «نام تاریخی» است؛ نام زنده ⇒ تغییر تعداد سری |
| TD-011 نام `UniqueViolation` برای کل خانوادهٔ integrity | باز | پایهٔ `IntegrityError` + زیرکلاس‌ها در فاز بعد |

| collation متن روی PG | ✅ **تصمیم E.6.3: `C` می‌ماند** | واگرایی با SQLite `BINARY` و نبود ICU در SQLite؛ ترتیب فارسی per-query ⇒ TD-013 |
| JSONB برای ستون‌های JSON | ✅ **تصمیم E.6.4** | `logs.details` ⇒ `jsonb` روی PG؛ چهار ستون دیگر TEXT ⇒ TD-014 |
| SNMP Exporter + Dashboard | فاز بعد | خارج از دامنه‌ی E.1–E.7؛ PostgreSQL تنها منبع حقیقت آن است |

---

*این سند دستور کار فاز E است. E.1 تا E.4 بسته شده‌اند (E.4 با ۱۴ کامیت، بند ۴)؛
گام‌های E.5 تا E.7 در انتظار اجرای ترتیبی‌اند و هر گام فقط با معیارهای بند «قواعد
مشترک» پیش می‌رود. تنها کار بازِ بیرونی: push شاخهٔ `phase-c` (E.0.9 — تصمیم مالک).*
