# Cutover execution log — SQLite → PostgreSQL (real run, owner's server)

> افزونهٔ [`docs/cutover-runbook.md`](cutover-runbook.md). این فایل **شاهدِ اجرا** است، نه
> دستورالعمل: هر ردیف، خروجی واقعیِ همان session است. مبنای مقایسه با سندباکس:
> [`docs/e7-sandbox-dryrun.md`](e7-sandbox-dryrun.md).
>
> **محیط:** ویندوز (سرور مالک) · Python 3.14.7 · psycopg 3.3.6 · PostgreSQL 17.5
> (x86_64-windows) · دیتابیس `printguard` با `datcollate = datctype = C` · نقش `postgres` ·
> `logs.db` = ۳۹۴٬۶۸۶٬۴۶۴ بایت و ۱۶٬۴۸۲ ردیف `logs`.
> **زمان:** ۲۰۲۶-۱۰-۱۳، ۰۸:۱۵Z تا ۱۴:۰۵Z.
> **دامنه:** گام‌های ۹ و ۱۰ (ری‌استارت سرویس و نوشتن marker) **اجرا نشد** — تصمیم
> go/no-go مالک است. `logs.db` دست‌نخورده ماند (SHA-256 در آغاز و پایان یکسان:
> `e682c5989900dc45aa280a94ce37af7b77f72bea90bbf84fdc1605e9fe30bbbc`).
> گزارش ماشینی چک‌لیست آمادگی: [`caches_readiness.json`](../caches_readiness.json).

## 1. Status summary

| گام (runbook) | نتیجه | شاهد |
|---|---|---|
| ۰ — بکاپ + SHA-256 + `integrity_check` | ✔ | `backups/logs-20261013-081509-pre.db`، SHA برابر مبدأ، `integrity=ok` |
| ۱ — چک‌لیست آمادگی (پیش از alembic) | ✘ مسدود (۱ ردیف قرمز: head روی PG خوانده نشد) | خروجی ابزار، ۷✔/۲!/۱✘ |
| ۱ — `alembic upgrade head` روی PG | ✔ | `alembic_version = 0005` |
| ۱ — چک‌لیست آمادگی (پس از alembic) | ! آماده با هشدار (۸✔/۲!/۰✘، exit 2) | همان فایل JSON بالا |
| ۴ — dry-run مهاجرت | ✔ (کد خروج ۳) | ۱٬۸۴۵٬۳۲۱ ردیف / ۱۱ جدول / ۷ توالی، صفر نوشتن |
| ۶ — مهاجرت واقعی | ✔ (کد خروج ۰) | کپی ۱٬۸۴۵٬۳۲۱ ردیف، صفر ردیف ردشده |
| ۶ — `--verify` روی هر دو سو | ✔ | تطابق ۱:۱ همهٔ جدول‌ها + چک‌سام `SUM(id)` |
| ۷ — checksum مستقل (خارج از ابزار) | ✔ | ۰ ناهم‌خوانی، `n_tup_upd = n_tup_del = 0` روی جدول‌های برنامه |
| ۸ — گیت backend | ✔ | `backend = postgresql \| dsn_set = True` |
| ۸ — گیت تست‌ها | ⛔ کامل سبز نشد | §۵ — دلایل محیطی، نه مهاجرت |
| ۹ — ری‌استارت سرویس | — اجرا نشد | تصمیم مالک (cutover رسمی) |
| ۱۰ — marker cutover | — نوشته نشد | «وضعیت درست پیش از روز اجرا» |

## 2. Step 0 — Backup and prerequisites

```text
mkdir -p backups && cp logs.db backups/logs-20261013-081509-pre.db
sha256sum logs.db backups/logs-20261013-081509-pre.db   ⇒ هر دو e682c598…bbbc
python -c "sqlite3 … PRAGMA integrity_check"            ⇒ ok
```

- **انحراف از runbook:** این ماشین درایو `D:` ندارد؛ بکاپ در `backups/` کنار مخزن
  نگه داشته شد (runbook پوشهٔ دیگری پیشنهاد می‌کند).
- `tools/set_cutover_marker.py --verify` ⇒ **marker نوشته نشده** (وضعیت درست پیش از cutover).
- ابزارهای CLI پستگرس (`psql`/`pg_dump`/`pg_restore`/`pg_isready`) روی `PATH` **نیستند**؛
  همهٔ اتصال‌ها از راه `psycopg` انجام شد (همین، در §۵ دو دسته شکست تست را توضیح می‌دهد).

### چک‌لیست آمادگی — پیش و پس از alembic

```text
پیش:  [✔]×7  [!]×2  [✘]×1  ⇒ «مسدود ✘» — alembic head روی PG: InFailedSqlTransaction
پس:   [✔]×8  [!]×2  [✘]×0  ⇒ «آماده با هشدار !» (exit 2) — version_num=0005 | schema=public
```

دو هشدار باقی‌مانده (پذیرفته‌شده و ثبت‌شده، مطابق §۲.۵ runbook):

1. `logs.db` **۱۰ جدول** برنامه دارد و جدول `meta` در آن نیست (اسکیمای مقصد ۱۱ جدول دارد).
   `meta` روی SQLite هرگز ساخته نشده؛ روی PG با migration ساخته می‌شود و صفر ردیف ماند.
2. **۳ ردیف** با timestamp نیازمند تصمیم/بازبینی دستی است (ردیف‌های پیش از cutover).
   تصمیم این ردیف‌ها کار **F.5** است و cutover را متوقف نمی‌کند.

## 3. Step 1 — Migration (steps 4 and 6)

```text
python tools/migrate_sqlite_to_pg.py --source logs.db --target "host=127.0.0.1 dbname=printguard …" --dry-run
⇒ کد خروج ۳ (DRY-RUN ONLY) · ۱۱ جدول · ۱٬۸۴۵٬۳۲۱ ردیف · ۷ توالی · «هیچ داده‌ای نوشته نشد»

python tools/migrate_sqlite_to_pg.py … --convert-naive-timestamps --verify
⇒ کد خروج ۰ · پایان مهاجرت · همگام‌سازی توالی‌های IDENTITY موفق
```

> توجه: در این محیط نقش `printguard` وجود ندارد (نقش سرور `postgres` است) و DSN پیش‌فرضِ
> سندباکسِ ابزار هم کار نمی‌کند؛ به همین دلیل `--target` **صریح** داده شد. این،
> رفتار مورد انتظار است (ابزار fallback بی‌صدا ندارد).

**تبدیل timestamp بدون `Z`:** ۱۶٬۳۸۵ ردیف با پرچم `--convert-naive-timestamps` تبدیل شد و
ابزار خودش هشدار «ثبت در manifest» داد. صفر ردیف ردشده.

### تأیید خودکار `--verify`

| جدول | SQLite | PG | MAX(id) | نتیجه |
|---|---|---|---|---|
| users | 5 | 5 | 6/6 | ✔ |
| printer_counters | 26 | 26 | — | ✔ |
| cartridge_state | 51 | 51 | — | ✔ |
| cartridge_yield_profiles | 6 | 6 | — | ✔ |
| logs | 16,482 | 16,482 | 17136/17136 | ✔ |
| toner_history | 23,241 | 23,241 | 23241/23241 | ✔ |
| yield_samples | 31,921 | 31,921 | 31921/31921 | ✔ |
| toner_snapshots_v2 | 1,584,144 | 1,584,144 | 1584144/1584144 | ✔ |
| sensor_readings | 189,336 | 189,336 | 189336/189336 | ✔ |
| security_events | 109 | 109 | 109/109 | ✔ |

```text
SUM(id) لاگ‌ها      : SQLite=145792874 | PG=145792874 | ✔
MAX(timestamp) logs : SQLite=2026-10-12T11:42:26.819226 | PG=…819226Z | ✔
لاگ‌های ۳۰ روز اخیر  : SQLite=10012 | PG=10012 | ✔
```

## 4. Independent verification (outside the migration tool)

پس از همهٔ اجراهای تست، مستقیماً از `psycopg` + `sqlite3` (read-only) پرسیده شد:

```text
alembic head = 0005
ناهم‌خوانی شمارش در ۱۰ جدول مشترک = 0
public: n_tup_upd = 0 | n_tup_del = 0   (فقط insert مهاجرت)
```

## 5. Test gate (step 8) — and honestly: not fully green

سه حالت اجرا شد. علت هر شکست ریشه‌یابی شد و در پایان، باگ‌های واقعی **اصلاح و بازآزمایی** شدند.

| حالت | دستور | نتیجه |
|---|---|---|
| A — فقط SQLite | `DB_BACKEND=sqlite PG_DSN= python -m pytest tests/ -q` | ۷۴۵ pass / ۳۲ fail / ۷۶ skip (۱۲:۱۱) |
| B — PG روی «دیتابیس قرنطینه» | `DB_BACKEND=postgresql`, `PG_DSN → printguard_*` (تازه‌ساخته) | ۲۴۲ pass / ۲ fail / ۴۶ skip (۴:۵۹) |
| C — PG روی همان `printguard` (اسکیمای ایزولهٔ هر اجرا) | `DB_BACKEND=postgresql python -m pytest <۱۷ فایل مرتبط> -q` | §۵.۳ |

### ۵.۱ دسته‌بندی شکست‌ها (حالت A)

| علت ریشه‌ای | تعداد | نمونه | حکم |
|---|---|---|---|
| هندل باز SQLite روی ویندوز در لحظهٔ پاک‌سازی پوشهٔ موقت (`WinError 32`) | ۸ | `test_db_migration_columns::{1,2,3}`، `test_db_adapter::test_adapter_exit_rolls_back_and_reraises_on_error` | نقص تست/محیط — **خودِ assert سبز است**؛ فقط `tmp.cleanup()` می‌افتد. روی لینوکس (CI) دیده نمی‌شود چون حذف فایلِ باز مجاز است |
| نبود ابزار کلاینت پستگرس روی `PATH` | ۳ | `test_backup_pg::VerifyDumpTests::*` ⇒ «ابزار `pg_restore` در PATH نیست» | محیطی — نصب `postgresql-client` لازم است |
| برگشت به DSN سندباکس وقتی `PG_DSN` خالی است ⇒ `role "printguard" does not exist` | ۱۵ | `test_migrate_sqlite_to_pg::*` (خروج ۲ به‌جای ۰/۱/۳) | محیطی — در حالت B/C این‌ها سبزند |
| mock قدیمی تست با `PRAGMA table_info` (ستون generated مجازی `raw_delta` را نمی‌بیند) | ۲ | `TestEndToEnd::test_dry_run_counts_all_tables` ⇒ «ناهم‌خوانی ستون‌ها: {raw_delta}» | نقص تست (نه ابزار؛ خودِ ابزار درست می‌خواند) |
| شدت `warn` چک‌لیست، چون cutover هنوز علامت‌گذاری نشده | ۱ | `test_cutover_readiness::test_after_cutover_z_is_green` | مورد انتظار پیش از گام ۱۰ |
| پر شدن دیسک در میانهٔ اجرا (`OSError: [Errno 28]`) | ۵ | `test_oid_matrix::*`، `test_offline_resilience::*` | آلودگی اجرا — §۶ |

### ۵.۲ حالت B — دو شکست، هر دو از خودِ روش قرنطینه

`test_collation_ordering` دو تست را رد کرد چون **collation دیتابیس تازه‌ساخته** روی این سرور
`English_United States.1252` است (از `template1`)، در حالی که بازوی اصلی پروژه `printguard` با
`datcollate = datctype = C` ساخته شده است:

```text
AssertionError: 'English_United States.1252' != 'C'
('printguard', 'C', 'C')  vs  ('template1', 'English_United States.1252', 'English_United States.1252')
```

اجرای همان فایل روی `printguard` واقعی: **`7 passed`** — پس شکست، مصنوعِ قرنطینه است، نه رگرسیون.
(قرنطینه برای این انتخاب شد که تست تمرین F.4 در خودش `TRUNCATE` دارد و نباید به دادهٔ مهاجرت‌شدهٔ
`public` برسد.)

### ۵.۳ حالت C — گیت نهایی روی `printguard` واقعی

```text
DB_BACKEND=postgresql python -m pytest <۱۷ فایل مرتبط با مهاجرت/آداپتر/timestamp> -q
```

نتیجه: **۲۴۴ pass / ۰ fail / ۴۶ skip (+۷۲ زیر‌تست) در ۴:۰۱ دقیقه، کد خروج ۰**.
(۴۶ skip همان تست‌های `sqlite_only` است — یعنی اجرا واقعاً در حالت PG بوده.)

شاهدِ «دست‌نزدن به `public`» پس از همین اجرا:

```text
alembic head = 0005            ناهم‌خوانی شمارش جدول‌ها = 0
public: n_tup_upd = 0 | n_tup_del = 0
اسکیمای باقی‌ماندهٔ printguard_test_* = []      دیتابیس قرنطینهٔ باقی‌مانده = []
SHA-256 logs.db = e682c598…bbbc  (بدون تغییر از آغاز اجرا)
```

توضیح حالت B و C: در حالت C، تست `test_cutover_rehearsal` هم اجرا و **سبز** شد؛
`TRUNCATE` آن فقط به اسکیمای ایزولهٔ همان اجرا خورد (اسکیمای موقت در پایان پاک شد) و به
دادهٔ مهاجرت‌شدهٔ `public` نرسید — همین، دلیل انتخاب این حالت برای گیت نهایی است.

### ۵.۴ باگ‌هایی که یافت شد و **اصلاح** شدند

1. **`web/routes/import_db.py` — هندل باز روی ویندوز.** `ConnectionAdapter.__exit__` فقط
   commit/rollback می‌کند و فایل را نمی‌بندد؛ در نتیجه `os.remove(temp_path)` با `WinError 32`
   می‌افتاد: `/api/import/analyze` به‌جای JSON خطا، ۵۰۰ می‌داد و `temp_import_*.db` روی دیسک
   می‌ماند. اصلاح: اتصال‌های فایل موقت با `contextlib.closing(...)` بسته می‌شوند.
   *اعتبارسنجی:* `tests/test_import_db_guard.py` + `tests/test_collation_ordering.py` ⇒ ۳۰ pass.
2. **`tests/test_collation_ordering.py` — جداکنندهٔ مسیر.** تست کلیدهای `ALLOWED_TEXT_ORDER_SITES`
   را با `/` می‌سازد ولی `os.path.relpath` روی ویندوز `\` می‌دهد ⇒ هر ۴ مسیر «تازه» شمرده
   می‌شدند. اصلاح: `.replace(os.sep, "/")`. *اعتبارسنجی:* همان فایل ⇒ `7 passed`.
3. **`tools/generate_cutover_fixture.py` — تولید fixture تمرین F.4 روی اسکیمای واقعی.**
   چهار نقص پشت‌سرهم: (الف) `PRAGMA table_info` ستون generated مجازی `raw_delta` را نمی‌بیند و
   `ADD COLUMN` با «duplicate column name» می‌افتد؛ (ب) جدول `meta` در `logs.db` نیست؛
   (ج) درج `VALUES` بدون فهرست ستون به ستون generated می‌خورد؛ (د) کپی `logs.db` پروداکشن
   (۳۹۴MB) بدون `VACUUM` جمع نمی‌شد. اصلاح: `table_xinfo` + ساخت `meta` + درج ستون‌محور +
   `VACUUM` بعد از تخلیه + کالیبراسیون اندازه تا در بازهٔ ۲۵–۳۵MB بماند.
   *اعتبارسنجی:* fixture = ۳۳MB با ۱۲ جدول (۱۱ برنامه + `sqlite_sequence`) و ۱۵۷٬۱۸۷ ردیف؛
   و **`tests/test_cutover_rehearsal.py` در یک دیتابیس قرنطینه ⇒ `1 passed` در ۱۷.۷s**
   (dry-run، کپی، `setval`، verify و rollback کامل).

## 6. Operational incident: disk full (recorded and cleaned up)

اجرای سوئیت، در `tmp` پایتون **۱۸GB** جمع کرد — چون چند تست `shutil.copyfile("logs.db", …)`
می‌کنند و `logs.db` این ماشین ۳۹۴MB است (در سندباکس ۱۶۷KB بود). نتیجه:

```text
C: 100G  100G  0  100%      ⇒  sqlite3.OperationalError: database or disk is full
                                OSError: [Errno 28] No space left on device
```

- پاک‌سازی انجام شد: `/tmp/pytest-of-*` و نسخه‌های موقت ⇒ **بازگشت به ۱۹GB آزاد**؛
  دیتابیس‌های قرنطینه هم `DROP` شدند.
- **آسیب جانبی و ترمیم:** تست `test_oid_matrix` ابزار `build_oid_matrix.py --write` را با
  دسترسی `--write` اجرا می‌کند و همان ابزار، `docs/oid-matrix.md` را بازنویسی می‌کند؛ با پر شدن
  دیسک فایل **صفر بایت** شد. با `git checkout -- docs/oid-matrix.md` به نسخهٔ HEAD بازگردانده شد
  (۳۰۳ خط، `git status` تمیز). **درس:** پیش از اجرای سوئیت روی سرور مالک، یا `logs.db` سبک
  باشد یا `TMPDIR` روی درایو پرحجم/محدودشده بماند؛ وگرنه یک تست می‌تواند دیسک سیستم را پر کند.
- به همین دلیل ۵ شکستِ `test_oid_matrix`/`test_offline_resilience` «آلودگی اجرا» شمرده
  می‌شوند و باید پس از پاک‌سازی دوباره اجرا شوند.

## 7. Steps not executed and remaining risk

1. **گام ۹ (ری‌استارت سرویس)** و **گام ۱۰ (marker + رشد `public.logs` در یک چرخهٔ polling)**
   اجرا نشده‌اند: تا این لحظه **هیچ نوشتنی از Flask در PG ثبت نشده** (گیت `n_tup_upd/del` و
   شمارش‌ها همین را می‌گویند). تعریف cutover در §۱ سند طرح هنوز محقق نشده و بازگشت به
   `DB_BACKEND=sqlite` بی‌هزینه است.
2. **گیت تست سبز کامل نیست** (تفصیل §۵). هیچ‌یک از شکست‌ها به خودِ مهاجرت وصل نیست، ولی
   «سبز بودن گیت گام ۸» باید با نصب `postgresql-client` روی `PATH` و ساخت نقش سندباکس
   (`printguard`) یا تنظیم `PG_DSN` در محیط تست، دوباره اندازه‌گیری شود.
3. **۳ ردیف timestamp** منتظر تصمیم F.5 و **۱۶٬۳۸۵ تبدیل`Z`** منتظر ثبت در manifest نهایی.
4. تست `test_cutover_rehearsal` روی این ماشین فقط در **دیتابیس قرنطینه** اجرا شد
   (پیش‌نیاز: `PG_DSN` به دیتابیسی با collation `C` یا صرف‌نظر از دو تست ترتیب).
