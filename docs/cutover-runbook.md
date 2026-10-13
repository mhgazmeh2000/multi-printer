# Cutover Runbook — گذار به PostgreSQL

> فاز **E.7.1** از [phase-e-plan.md](phase-e-plan.md).
> محتوای راهبردی از §۳ [cutover-plan.md](cutover-plan.md) است و جزئیات فنی از
> [phase-e-decisions.md](phase-e-decisions.md).
> **این سند در گام E.7 نوشته شد و روی هیچ سرویس زنده‌ای اجرا نشد.** هیچ command در آن
> علیه سرور مالک اجرا نشده است؛ شمارهٔ گام‌ها فقط ترتیب و گیت‌ها را ثابت می‌کند.

---

## ۰. راهنمای خواندن

- **«گیت»** = شرطی که تا برآورده نشود، گام بعد **اجرا نمی‌شود**. همهٔ گیت‌ها با یک دستور
  قابل بررسی‌اند؛ نتیجه را در لاگِ اجرا (یک فایل متنی کنار `logs.db`) یادداشت کنید.
- **اعداد زمانی در بدنه نیامده‌اند.** تنها زمان‌های واقعی، در **پیوست الف** (§۸) و فقط برای
  کارهایی‌اند که در sandbox اندازه‌گیری شده‌اند؛ آن‌ها تعهد زمانی نیستند.
- جاهایی که ابزار هنوز ساخته نشده (فاز F) با برچسب **«فاز F»** مشخص است و عمداً دستور
  جانشینِ امروز هم کنارش آمده؛ در حالت جانشین، داده‌ای به PG منتقل **نمی‌شود**.
- نام فایل‌های بکاپ شامل `timestamp` است. همهٔ زمان‌ها در سند **UTC** و با پسوند `Z` نوشته
  می‌شوند؛ `set_cutover_marker.py --date` هم دقیقاً همین قالب را می‌خواهد (قالب آزاد نیست:
  ابزار آن را بررسی و رد می‌کند) و `migrate_timestamps_to_utc.py` داده‌ها را به همان قالب
  می‌برد.

**دو نکته که قبل از هر چیز باید دانست:**

1. **`POLLING_ENABLED` وجود ندارد.** این کلید در §۳ سند cutover-plan فرضی بود؛ با grep تأیید
   شد که هیچ‌جای کد نیست. توقف polling == توقف فرایند برنامه.
2. **Watchdog را فراموش نکنید.** اگر `run-watchdog.bat` باز است، هر توقف دستی
   `python run.py` پس از ~۱۰ ثانیه دوباره برنامه را بالا می‌آورد. توقف درست، از راه
   `schtasks /End` یا بستن پنجرهٔ watchdog است.

---

## ۱. پیش‌نیازها (هیچ‌کدام اختیاری نیست)

| # | پیش‌نیاز | چطور بررسی شود |
|---|---|---|
| ۱ | Windows 11 · PostgreSQL ۱۶ · Python ≥ ۳.۱۰ | `psql --version` · `python --version` |
| ۲ | درایور `psycopg` (نسخهٔ ۳) نصب باشد | `python -c "import psycopg; print(psycopg.__version__)"` |
| ۳ | کد جدید در **پوشهٔ جدا** (نه جایگزین `v4.3.6`) | پوشهٔ جدید، مثلاً `multi-printer-cutover` (اصل D-1: نسخهٔ فعلی دست‌نخورده می‌ماند) |
| ۴ | `logs.db` و `printers.json` در همان پوشهٔ جدید کپی شده باشند | `Get-FileHash logs.db -Algorithm SHA256` |
| ۵ | `.env` در ریشهٔ مخزن (gitignore شده) با `DB_BACKEND=sqlite` | فایل را باز کنید؛ تا لحظهٔ گام ۸ مقدار `sqlite` است |
| ۶ | فضای دیسک: حداقل ۴ برابر `logs.db` برای بکاپ‌ها + دیتای PG | `Get-PSDrive C \| Select-Object Free` |
| ۷ | دسترسی `CREATE`/`ALTER TABLE` روی دیتابیس PG برای کاربر برنامه | `tools/test_pg_connection.py` (گام ۳ مرحلهٔ ۰) |
| ۸ | چک‌لیست خودکار آمادگی سبز باشد | `python tools\check_cutover_readiness.py` ⇒ **exit 0** (۱ = مسدود، ۲ = فقط هشدار؛ `--strict` زرد را قرمز می‌کند، `--skip-pg` برای ماشین بدون PG، `--self-test` بدون شبکه، `--json فایل` برای ضمیمه به لاگ اجرا) |

### پیش‌نیاز اضافی — migration 0005 (هم‌راستایی اسکیمای جدول logs)

جدول `logs` در دیتابیس فعلی پروداکشن (SQLite) دارای **۱۸ ستون** است (شامل ۵ ستون legacy: `sheets`, `raw_counters`, `raw_delta`, `event_type`, `duplex_mode`). برای عبور از گارد اعتبارسنجی ابزار مهاجرت، اسکیمای PostgreSQL حتماً باید با اعمال مایگریشن 0005 با دیتابیس پروداکشن هم‌راستا شده باشد:
```powershell
$env:ALEMBIC_DB_URL="postgresql://printguard:PW@localhost:5432/printguard"
python -m alembic upgrade head
python -m alembic current
# باید مقدار 0005 (head) نمایش داده شود
```

**مقادیر `.env` که در این سند استفاده می‌شود** (نام‌ها از `docs/postgresql-setup.md` و
`alembic/env.py` گرفته شده‌اند؛ `DB_BACKEND` فقط دو مقدار `sqlite` و `postgresql` می‌پذیرد):

```ini
DB_BACKEND=sqlite
PG_DSN=postgresql+psycopg://printguard:PW@localhost:5432/printguard
```

> `alembic` هم از همین دو متغیر می‌خواند؛ اگر `DB_BACKEND=postgresql` و `PG_DSN` خالی باشد،
> با خطا متوقف می‌شود (fallback بی‌صدا ندارد).

---

## ۲. مرحلهٔ ۰ — یک روز قبل (بدون توقف سرویس)

هدف: همه‌چیز آماده و تأیید شود، در حالی‌که برنامه همچنان روی SQLite کار می‌کند.

1. **بکاپ `logs.db` روی دیسک دوم** (فایل‌ها در حال نوشتن‌اند؛ کپی در زمان idle):
   ```powershell
   New-Item -ItemType Directory -Force D:\pgbackup | Out-Null
   Copy-Item logs.db D:\pgbackup\logs-$(Get-Date -Format yyyyMMdd-HHmmss)-pre.db
   Get-FileHash D:\pgbackup\logs-*-pre.db -Algorithm SHA256
   ```
   *گیت:* SHA-256 کپی = SHA-256 فایل مبدأ در همان لحظه، و `PRAGMA integrity_check = ok`:
   ```powershell
   python -c "import sqlite3;print(sqlite3.connect('logs.db').execute('PRAGMA integrity_check').fetchone()[0])"
   ```
2. **PG روشن + DSN:**
   ```powershell
   pg_isready
   python tools\test_pg_connection.py --dsn "host=localhost dbname=printguard user=printguard"
   ```
   *گیت:* exit 0. هشدار «schema هست ولی هیچ سطری نیست» **طبیعی** است (فاز F هنوز داده نبرده).
3. **Migration روی PG** (اگر قبلاً اجرا نشده): `python -m alembic upgrade head` ⇒ گیت: `alembic current` = `0004 (head)`.
4. **تست‌های سبز، روی هر دو backend** (اعداد مورد انتظار از §۸ سند تصمیم):
   ```powershell
   python -m pytest tests/ -q                                     # SQLite ⇒ 597 passed, 0 skipped
   $env:DB_BACKEND="postgresql"; python -m pytest tests/ -q       # PG ⇒ 526 passed, 71 skipped
   Remove-Item Env:\DB_BACKEND
   ```
   *گیت:* صفر شکست در هر دو اجرا؛ ۷۱ skip روی PG **مجاز و اعلام‌شده** است (TD-012).
5. **سنجهٔ timestampها و timezone سرور:**
   ```powershell
   python tools\migrate_timestamps_to_utc.py --db logs.db --dry-run
   python tools\verify_server_timezone.py --db logs.db
   ```
   *گیت:* در dry-run عدد `converted` صفر باشد. گزارش واقعی همین مخزن:
   `rows=91 converted=0 already_standard=106 skipped=0 needs_decision=0` — یعنی مسیر
   timestampها از قبل استاندارد است.
   در مقابل `analyze_old_events.py` با **کد خروج ۲** می‌گوید «۲۳ ردیف نیازمند تصمیم/بازبینی
   دستی است»: همان ردیف‌های *پیش از cutover* که رشتهٔ timestampشان نشانهٔ `Z` دارد (cutover
   تقریبیِ خودِ ابزار = زمان کامیت `b548e99`). این ۲۳ ردیف تصمیم **F.5** را می‌خواهند و مانع
   cutover نیستند، ولی باید در لاگ اجرا ثبت شوند. `verify_server_timezone.py` هم کد خروج ۲
   می‌دهد (= هشدار، نه خطای اتصال) و حکمش روی این مخزن «TZ سرور = UTC» است؛ نتیجه را ثبت کنید.
   ```powershell
   python tools\analyze_old_events.py --db logs.db --cutover 2026-XX-XXT00:00:00Z
   ```
6. **marker cutover نوشته نشده باشد:** `python tools\set_cutover_marker.py --db logs.db --show`
   ⇒ *گیت:* «هیچ marker نوشته نشده». اگر marker از تمرین قبلی مانده، `--verify` هم بگیرید.
7. **تمرین بکاپ PG** (تصمیم **۶.۲**):
   ```powershell
   $env:PGPASSWORD="PW"; pg_dump -Fc -h localhost -U printguard -f D:\pgbackup\pg-pre-cutover.dump printguard
   pg_restore --list D:\pgbackup\pg-pre-cutover.dump | Select-String "TABLE DATA" | Measure-Object
   ```
   *گیت:* exit 0 و خروجی `pg_restore --list` غیر‌خالی (فایل ۰ بایتی = بکاپ بی‌اعتبار).
8. **تعیین `CUTOVER_DATE`** روی کاغذ: `YYYY-MM-DDT00:00:00Z` و ترجیحاً «روزِ پس از آخرین رویدادِ `logs`» — تا رویدادی بی‌مالک نماند.

---

## ۳. مرحلهٔ ۱ — روز اجرا (ده گام، به همان ترتیب)

> گام ۱ تا ۹ **هیچ داده‌ای را از بین نمی‌برند**؛ برگشت در هر لحظه ممکن است.

**گام ۱ — توقف سرویس (و polling):**
```powershell
schtasks /End /TN "PrintGuardMonitor"
```
ترتیب درست:
   1. `schtasks /End /TN "PrintGuardMonitor"` — اگر تسک نصب است (نصب: `tools\install-startup-task.bat`،
      `/SC ONLOGON`)؛ این کار درخت فرایند تسک را می‌بندد.
   2. اگر پنجرهٔ `run-watchdog.bat` باز است **همان را ببندید**، وگرنه حلقهٔ آن
      (`python run.py >> logs\watchdog.log` سپس `timeout /t 10`) برنامه را ۱۰ ثانیه بعد
      دوباره بالا می‌آورد و گیتِ گام ۱ بی‌معنی می‌شود.
   3. `stop-project.bat` — اگر تسک ندارید؛ `taskkill /F /PID` روی PIDهای پورت ۵۰۵۰ و
      پروسه‌های پایتون همین پروژه.
   4. `Get-Process python | Stop-Process` — آخرین گزینه، وقتی هیچ‌کدام از بالا کار نکرد.
   > هیچ‌کدام از این چهار مسیر سیگنالِ `SIGINT`/`SIGTERM` نمی‌فرستند، پس graceful shutdown
   > برنامه (`run.py:43-59`) اجرا **نمی‌شود** و خط «🛑 دریافت سیگنال توقف» در
   > `logs\watchdog.log` نخواهد بود. این عمدی است: گیتِ ما «پورت خالی» است، نه «لاگ تمیز».
> اگر فاز L اجرا شده و سرویس ویندوزی دارید: `Stop-Service printguard-flask` جای این دستور.

*گیت:* پورت 5050 خالی باشد:
```powershell
netstat -ano | findstr :5050
```

**گام ۲ — بکاپ دیتابیس PG (در این لحظه خالی است؛ این «بکاپِ نقطهٔ شروع» است):**
```powershell
$env:PGPASSWORD="PW"; pg_dump -Fc -h localhost -U printguard -f D:\pgbackup\pre-cutover_empty.dump printguard
pg_restore --list D:\pgbackup\pre-cutover_empty.dump | Measure-Object -Line
```
*گیت:* فایل غیر‌خالی + `pg_restore --list` موفق. توجه: `pg_dump` **دیتابیس‌محور** است نه
schema‌محور؛ در sandbox خروجی `54,628` بایت و `24` ورودی `TABLE DATA` بود (همهٔ `public`،
نه فقط جدول‌های برنامه). اگر schema جدا دارید: `pg_dump -n <schema>`.

**گام ۳ — ساخت جداول روی PG:**
```powershell
$env:DB_BACKEND="postgresql"; python -m alembic upgrade head; python -m alembic current
```
*گیت:* `0004 (head)` و در `psql`: `SELECT version_num FROM public.alembic_version;` = `0004`.

**گام ۴ — مهاجرت خشک (dry-run):**
```powershell
python tools\migrate_sqlite_to_pg.py --dry-run
```
> ⚠️ **این ابزار در فاز F ساخته می‌شود؛ امروز وجود ندارد.** تا آن زمان همین را اجرا کنید تا
> مسیر timestampها بررسی شود: `python tools\migrate_timestamps_to_utc.py --db logs.db --dry-run`.
> زمان این گام در فاز F اندازه‌گیری می‌شود (پیوست الف §۸).

*گیت:* exit 0. برای نسخهٔ جایگزینِ امروز، صفر بودن `needs_decision` و `skipped` در خط
`مجموع:` گزارش کافی است. (گزارش ابزار فاز F هنوز تعریف نشده ⇒ شکل نهایی این گیت در **F.2**
بسته می‌شود؛ آن‌جا عددِ «ردیف‌های جابه‌جاشده» هم ثبت می‌شود.)

**گام ۵ — checksum قبل از انتقال (سمت SQLite):**
```powershell
python -c 'import sqlite3;c=sqlite3.connect("file:logs.db?mode=ro",uri=True);[print(t,c.execute("select count(*) from "+t).fetchone()[0]) for t in (r[0] for r in c.execute("select name from sqlite_master where type=? and name not like ?",("table","sqlite_%")))]'
```
*گیت:* خروجی **۱۲ سطر** باشد (۱۱ جدول برنامه + `alembic_version`)؛ شمارش `logs` را جدا
یادداشت کنید — مبنای مقایسه در گام ۷ همین است. §۳ سند cutover-plan «۱۰ جدول» نوشته که
`meta` و `alembic_version` را نمی‌شمارد؛ عدد درستِ همین مخزن **۱۱ جدول برنامه** است
(`core/db/schema.py::APP_TABLES`).

**گام ۶ — اجرای واقعی مهاجرت:**
```powershell
python tools\migrate_sqlite_to_pg.py
```
> ⚠️ **فاز F.** تا ساخت این ابزار، گام‌های ۶ و ۷ اجرا **نمی‌شوند** و cutover معنای «انتقال داده»
> ندارد؛ فقط می‌توانید مسیر آماده‌سازی (گام ۱ تا ۵ و ۸ تا ۱۰) را تمرین کنید.
> زمان این گام در فاز F اندازه‌گیری می‌شود.

*گیت:* exit 0 و برابری شمارشِ تک‌تک جدول‌ها بین PG و SQLite (دستور گام ۵ در برابر همان
دستور با `mode=ro` روی PG یا `psql -c "SELECT count(*) FROM <table>;"`). اگر F.2 شمارش
نهایی‌کردن ردیف‌ها را در گزارش خود آورد، همین گیت خودکار می‌شود.

**گام ۷ — checksum بعد از انتقال (سمت PG) و مقایسه:**
```powershell
psql "host=localhost dbname=printguard user=printguard" -c "SELECT count(*) FROM public.logs;"
```
*گیت:* تساوی **دقیق** شمارش‌ها با گام ۵ (همین جدول‌ها، تک‌تک). اختلاف ⇒ اجرا را متوقف کنید و
به **§۵ (Rollback)** بروید؛ «تقریباً برابر» پذیرفته نیست.
*شاهدِ مستقل (سنجهٔ §۷ `phase-e-plan.md`):* شمارشگرهای PG را **قبل و بعد** از مهاجرت بگیرید و
`delta` را ببینید؛ `n_tup_ins` باید دقیقاً به اندازهٔ ردیف‌های منتقل‌شده رشد کند و
`n_tup_upd`/`n_tup_del` روی جدول‌های برنامه صفر بمانند:
```powershell
psql "host=localhost dbname=printguard user=printguard" -c "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables WHERE schemaname='public' AND relname <> 'alembic_version' ORDER BY relname;"
```
سه نکته که در sandbox اندازه‌گیری شد (`docs/e7-sandbox-dryrun.md` §۳):
- **`alembic_version` را مستثنی کنید**: خودِ `alembic upgrade head` روی اسکیمای خالی
  `1 ins / 3 upd` می‌سازد (درج `0001` و سه به‌روزرسانی `0002→0004`)؛ بدون این فیلتر گیت
  **همیشه** قرمز است.
- **مقایسهٔ delta، نه مقدار مطلق**: `pg_stat_reset()` با نقش برنامه کار نمی‌کند
  (`permission denied for function pg_stat_reset`) ⇒ شمارشگرها تجمعی‌اند.
- اگر می‌خواهید صفرِ مطلق ببینید، reset را با superuser انجام دهید و بعد اندازه بگیرید.

**گام ۸ — تغییر `.env` به `postgresql`:**
مقدار `DB_BACKEND=postgresql` را ست کنید (فقط همین یک خط؛ `PG_DSN` از قبل ست است).
*گیت — بدون اتصال زنده، backend مؤثر را بخوانید:*
```powershell
python -c 'import config.settings; from core.db.connection import resolve_backend, resolve_dsn; b=resolve_backend(); print("backend =", b, "| dsn_set =", bool(resolve_dsn(b)))'
```
باید چاپ کند: `backend = postgresql | dsn_set = True`. (رمز عبور چاپ نمی‌شود.)
> اگر `PG_DSN` ست نشده باشد همین دستور `core.db.exceptions.ConfigurationError`
> می‌دهد (سنجیده‌شده در sandbox) — آن خطا یعنی گیت **رد شده** است، نه «هشدار»: fallback
> بی‌صدا به SQLite در کار نیست.

**گام ۹ — شروع مجدد سرویس:**
```powershell
schtasks /Run /TN "PrintGuardMonitor"
```
جایگزین‌ها: اجرای `run-watchdog.bat` (حالت عادیِ نصب شما) یا `python run.py` برای یک اجرای
مستقیمِ کوتاه.
> اگر سرویس ویندوزی فاز L نصب شده: `Start-Service printguard-flask`.

**گام ۱۰ — تأیید سلامت:**
```powershell
curl.exe -s http://localhost:5050/api/status
```
> `/metrics` هم روی همان **پورت Flask (5050)** است، نه پورت ۵۴۳۲.
درست: `/api/status` ⇒ JSON با `"status": "running"` و تازه‌شدن `count` / `last` در همان
پاسخ (این چهار کلید از `core/store.py::poll_stats` می‌آیند: `count`, `last`, `errors`,
`is_polling`). `errors` باید صفر بماند و بین دو فراخوانی، `count` یکی اضافه شود
(`POLL_INTERVAL = 60` ثانیه ⇒ یک چرخه یک دقیقه است). این endpoint عمداً `host_ip` را افشا نمی‌کند و
**`db_backend` را هم در پاسخ ندارد**، پس backend از پاسخِ API قابل تأیید نیست — برای همین گیتِ
«رشد `public.logs`» پایین آمده است. `/metrics` روی همان پورت Flask ⇒ خطوط `printguard_` غیر‌خالی:
```powershell
curl.exe -s http://localhost:5050/metrics | Select-String "^printguard_" | Measure-Object -Line
```
*گیت‌های تأییدِ backend (چون API آن را چاپ نمی‌کند):*
1. شمارش `public.logs` در PG پس از یک چرخهٔ polling افزایش یابد، در حالی‌که `logs` در `logs.db`
   ثابت مانده است:
   ```powershell
   python -c "import sqlite3;print(sqlite3.connect('file:logs.db?mode=ro',uri=True).execute('select count(*) from logs').fetchone()[0])"
   ```
2. پس از تأیید، marker را بنویسید (همین خط، cutover را رسماً «انجام‌شده» می‌کند و ورودی‌های
   legacy را می‌بندد):
   ```powershell
   python tools\set_cutover_marker.py --db logs.db --date 2026-XX-XXT00:00:00Z --commit <sha>
   python tools\set_cutover_marker.py --db logs.db --verify
   ```
   *گیت:* `--verify` ⇒ exit 0.

---

## ۴. مرحلهٔ ۲ — بیست‌وچهار ساعت بعد

سنجه‌ها از §۷ [cutover-plan.md](cutover-plan.md) است؛ «چگونه اندازه‌گیری شود» به نسخهٔ PG
سازگار شده:

| معیار | چطور بررسی شود (PG) | آستانهٔ هشدار | اقدام در صورت هشدار |
|---|---|---|---|
| شمارش `logs` | `SELECT count(*) FROM public.logs;` هر ساعت | رشد صفر در ۲۴ ساعت | بررسی `polling_paused` و لاگ `PrinterMonitor` |
| `pg_stat_activity` | `SELECT count(*) FROM pg_stat_activity WHERE datname=current_database();` | > ۲۰ | نشتی connection ⇒ pool/`pool_pre_ping` (TD-003) |
| `Failed to write alert` | جست‌وجو در لاگ Flask | غیر‌صفر | مسیر نوشتن alert (تصمیم **۶.۷**؛ bind دیگر `%s` را نمی‌شکند) |
| Response time داشبورد | `GET /api/metrics/dashboard` | محسوساً کندتر | `EXPLAIN ANALYZE` روی `public.logs`؛ `details::text` را در CAST ببینید (§۶) |
| Golden fixtures | `python tools\record_golden.py --check` | تفاصل ≠ صفر | توقف و بررسی؛ snapshotهای DB در تست‌ها انجام می‌شود، پس اجرای این دستور روی سرور **بی‌خطر** است |
| `integrity_check` سمت PG | `SELECT count(*) FROM information_schema.tables WHERE table_schema='public';`) = **۱۲** (۱۱ + `alembic_version`) + `ANALYZE` | جدول کم است | migration ناقص ⇒ `alembic current` |

اگر همه سبز بود: **تصمیم نهایی** — SQLite فقط archive می‌شود (مرحلهٔ ۳).

---

## ۵. Rollback — بدون فشار زمان

**حالت A — قبل از نوشتن marker (گام ۱۰):** هیچ داده‌ای در PG معنادار نیست.
```powershell
schtasks /End /TN "PrintGuardMonitor"      # همان دستورهای گام ۱
# .env: DB_BACKEND=sqlite                  # بازگردانی یک خط
schtasks /Run /TN "PrintGuardMonitor"      # همان دستورهای گام ۹
```
`downgrade` لازم نیست: جداول PG رها می‌شوند و `logs.db` هنوز مبدأ حقیقت است (تصمیم **۶.۱**
تا فاز F، مسیر نوشتن import روی PG را بازنشسته کرده، اما SQLite کامل دست‌نخورده است).

**حالت B — بعد از cutover (marker نوشته شده):** رویدادهای دوران PG را برگردانید.
```powershell
schtasks /End /TN "PrintGuardMonitor"
python tools\export_from_pg.py --dsn "host=localhost dbname=printguard user=printguard" --out D:\pgbackup\after-cutover.db --manifest D:\pgbackup\after-cutover-manifest.json
# یا نسخهٔ portable برای import دستی:
python tools\export_from_pg.py --dsn "..." --mode sql --out D:\pgbackup\after-cutover.sql --force
```
*گیت:* در `manifest` مقدار `row_counts` را با شمارش PG مقایسه کنید (manifest رمز DSN را
`***` می‌کند، پس قابل commit است). سپس `INSERT`های `after-cutover.sql` را در `logs.db` اجرا
کنید، `.env` را به `sqlite` برگردانید و سرویس را بالا بیاورید.
> **هشدار (اندازه‌گیری‌شده):** `export_from_pg.py` جدول `alembic_version` را هم می‌بَرَد
> (`row_counts` آن `= 1`). سطرش را در merge دستی وارد `logs.db` **نکنید** — وگرنه دو head
> هم‌زمان در یک فایل SQLite دارید؛ head را همان `alembic current`ِ خودِ مخزن بگذارید.
> حلقهٔ کاملِ «import back» (مرج خودکار + ستون `migration_source`) در **F.6** و با ابزار
> `tools/import_after_pg.py` انجام می‌شود؛ امروز merge دستی است — همان چیزی که
> `docs/cutover-plan.md` §۶ پیش‌بینی کرده است.

**بستن marker اگر cutover لغو شد:** با `git revert` marker را بردارید و
`suggested_path` را از `backups/` پاک نکنید (فایل `meta` باید با history هم‌راستا بماند).

---

## ۶. نکات پایداری که در اجرا گریبان‌گیر می‌شوند

- **`public` را hard-code نگه دارید تا TD-011 بسته شود.** اگر `PG_DSN` شامل
  `options=-csearch_path=<schema>` باشد، جدول‌ها در همان schema ساخته می‌شوند و **`public.logs`
  خالی می‌ماند**؛ `tools/export_from_pg.py` عمداً `public.logs` را می‌خواند. برای اطمینان:
  `SELECT count(*) FROM logs;` (بدون schema) در همان DSN.
- **collation `C` است** (تصمیم **۶.۳**): `ORDER BY event_key` روی PG همان ترتیب SQLite را
  می‌دهد؛ با `fa-IR-x-icu` ترتیب عوض می‌شد (۱۵ از ۱۸ مورد) — عمداً تغییر نکنید.
- **`logs.details` jsonb است** (تصمیم **۶.۴**): در `ORDER BY`/`LIKE` باید `details::text`
  بنویسید؛ چهار ستون JSON دیگر هنوز `TEXT` هستند (TD-014).
- `PRAGMA table_info(...)` روی PG معنا ندارد؛ اینتروسپکشن PG ⇒ `information_schema`.
- `set_cutover_marker` روی PG عمداً **no-op** است (marker فقط در `meta` دیتابیس جاری/SQLite
  نوشته می‌شود؛ TD-012).
- بکاپ‌ها را **روی دیسک دوم** نگه دارید؛ `logs.db` هرگز پاک نمی‌شود (مرحلهٔ ۳ فقط انتقال به
  `archives/` است، نه حذف).

---

## ۷. تست‌شده در sandbox (E.7.3)

این runbook و ابزار `tools/check_cutover_readiness.py` در sandbox لینوکسی **اجرا شده‌اند**؛
لاگ کامل و اعداد در [`docs/e7-sandbox-dryrun.md`](e7-sandbox-dryrun.md) است. خلاصه:

- سه کد خروج (۰/۱/۲) هر سه دیده شد: روی کپی `logs.db` ⇒ `2` (فقط هشدار ۲۳ ردیف pre-cutover)،
  با `--dsn host=127.0.0.1 port=1` ⇒ `1` و چهار ردیف قرمز، روی محیط سالم ⇒ `0` با `--skip-pg`
  اگر هشدارهای دیگری نباشد.
- `alembic upgrade head` روی اسکیمای موقت: ۴ migration، `0.75s`، `0004`، ۱۲ جدول.
- `export_from_pg.py` (تمرین rollback) ⇒ فایل `81,920` بایتی با manifestِ DSN‌ماسک‌شده.
- **صفر ردپا:** `md5 logs.db` بی‌تغییر، `public.logs = 0` ردیف، صفر اسکیمای باقی‌مانده،
  `git status` تمیز.
- **تمرین‌نشده:** گام‌های ۴ و ۶ واقعی (ابزارش فاز F است) و گام‌های ۱/۹ ویندوزی
  (`schtasks`، `/api/status`، رشد polling) — این‌ها در sandbox معنا نداشتند.

---

## ۸. پیوست الف — زمان‌های اندازه‌گیری‌شده در sandbox

**ماشین اندازه‌گیری:** sandbox لینوکسی — ۲ vCPU Intel Xeon @ 2.60 GHz · ۱ GB RAM · ۲۰ GB فضای
آزاد · PostgreSQL 16.15 · Python 3.13.14 · `logs.db` = ۱۶۷٬۹۳۶ بایت (۷ ردیف در `logs`).
**همهٔ اعداد خروجیِ همان session هستند** (`date +%s%N` دور هر دستور)؛ مقیاس‌دهی به سرور شما
نشده است — سرور شما روی این داده‌ها (چند صد کیلوبایت) احتمالاً سریع‌تر است و روی دیتای واقعی
باید خودتان اندازه بگیرید.

| کار | زمان در sandbox | یادداشت |
|---|---|---|
| `pytest tests/ -q` روی SQLite (۵۹۷ تست + ۱۰۹ سابتست) | ~۵۵ ثانیه | ۵۵.۵s در `TZ=UTC` و ۵۳.۱s در `TZ=Asia/Tehran` (شمارش پس از افزودن ۴۲ تستِ ابزار E.7.2) |
| `pytest tests/ -q` روی PG (۵۲۶ + ۷۱ skip) | ~۱۱۴ ثانیه | ۱۱۴.۰s؛ هر تست PG یک schema تازه می‌سازد |
| `tools/record_golden.py --check` (۱۰ فیچر) | ~۴۵ ثانیه | ۴۴.۸s؛ سنگین‌ترین گیتِ خودکارِ ما |
| `tools/test_pg_connection.py --quiet` | < ۱ ثانیه | ۰.۲۷s (با هشدار «بدون سطر» — طبیعی) |
| `PRAGMA integrity_check` + شمارش `logs` | < ۱ ثانیه | ۰.۰۲s روی ۱۶۴ KB |
| کپی سرد `logs.db` + SHA-256 | < ۱ ثانیه | ۰.۰۳s (روی ویندوز `Get-FileHash` همان کار را می‌کند) |
| `pg_dump -Fc` + `pg_restore --list` روی DB خالی | < ۱ ثانیه | ۰.۲۶s · خروجی ۲۶ KB · ۱۲ ورود TOC |
| `alembic upgrade head` روی schema خالی (۰۰۰۱→۰۰۰۴) | < ۱ ثانیه | چهار migration، بدون داده |
| `tools/migrate_timestamps_to_utc.py --dry-run` روی کپی `logs.db` | < ۱ ثانیه | ۰.۰۹s · `rows=91 converted=0 already_standard=106 needs_decision=0` |
| `tools/export_from_pg.py --mode db` روی `public.logs` خالی | < ۱ ثانیه | ۰.۳۱s · فایل ۸۰ KB (تمرین rollback) |
| `tools/test_dual_backend.py` | < ۱ ثانیه | ۰.۳۹s · `OK=18 / EXPECTED=1 / FAIL=0` |
| `check_cutover_readiness.py` (۱۰ ردیف، با PG زنده) | < ۱ ثانیه | ~۰.۸s؛ سنگین‌ترین بخشش فراخوانی `analyze_old_events.py` است |
| **گام ۴ و ۶ (`migrate_sqlite_to_pg.py`)** | **۱.۲۲ ثانیه روی ۳۰MB** | ثبت‌شده در تمرین F.4؛ برآورد دیتای ۳۱۳MB پروداکشن ~۱۳s (رجوع به بند ۸.۱) |
| **گام ۷ تا ۱۰ (تغییر `.env` + start + تأیید)** | < ۵ ثانیه | به زمان start Flask و اولین polling وابسته است |

**چطور روی سرور خودتان اندازه بگیرید** (PowerShell):
```powershell
Measure-Command { python -m pytest tests/ -q | Out-Host }
Measure-Command { pg_dump -Fc -h localhost -U printguard -f $env:TEMP\t.dump printguard }
```
قاعدهٔ کلی برای زمانِ توقف سرویس (گام ۱ تا ۹ بدون مهاجرت داده): چند دقیقه — چون هیچ‌کدام از
دستورات بالا روی این مقیاس بیش از چند ثانیه طول نمی‌کشد و کارِ سنگین (گام ۶) در فاز F اندازه
می‌شود.

---

## ۸.۱. نتایج تمرین Cutover با داده‌ی مصنوعی ۳۰MB (فاز F.4)

در جریان زیرفاز F.4، تمرین کامل Cutover با دیتابیس مصنوعی ۳۰ مگابایتی (`tests/fixtures/cutover_synthetic.db`) شامل **۱۵۷٬۱۸۷ سطر در ۱۱ جدول** که بر مبنای توزیع و ساختار واقعی دیتابیس پروداکشن (از جمله timestampهای بدون Z) توسط `tools/generate_cutover_fixture.py` تولید شد، در آزمون خودکار `tests/test_cutover_rehearsal.py` به اجرا درآمد.

### جدول زمان‌های واقعی اندازه‌گیری‌شده (Rehearsal Measurements)

| مرحله عملیات Cutover | استراتژی / ابزار | تعداد سطرها / جزئیات | زمان ثبت‌شده در Sandbox |
|---|---|---|---|
| **Dry-Run (گام ۴)** | `migrate_sqlite_to_pg.py --dry-run` | بررسی ساختار ۱۱ جدول بدون نوشتن | **۰.۰۴۵ ثانیه** (۴۵ میلی‌ثانیه) |
| **مهاجرت `logs`** | استراتژی `COPY FROM STDIN` | ۶۱٬۵۰۰ سطر (حجم ~۱۵MB) | **۰.۵۹ ثانیه** |
| **مهاجرت `sensor_readings`** | استراتژی `COPY FROM STDIN` | ۴۱٬۰۰۰ سطر (حجم ~۵MB) | **۰.۱۷ ثانیه** |
| **مهاجرت `toner_history`** | استراتژی `COPY FROM STDIN` | ۲۴٬۰۰۰ سطر (حجم ~۳MB) | **۰.۱۲ ثانیه** |
| **مهاجرت `toner_snapshots_v2`** | استراتژی `COPY FROM STDIN` | ۲۰٬۰۰۰ سطر (حجم ~۴MB) | **۰.۰۹ ثانیه** |
| **مهاجرت `yield_samples`** | استراتژی `Batch 1000` | ۱۰٬۰۰۰ سطر (حجم ~۲MB) | **۰.۱۸ ثانیه** |
| **مهاجرت ۶ جدول کوچک تنظیمی** | استراتژی `Batch 1000` | ۶۸۷ سطر (`users`, `counters`, ...) | **۰.۰۲ ثانیه** |
| **همگام‌سازی توالی‌های IDENTITY** | `SELECT setval(...)` (ریسک R-1) | ۷ جدول کلید اصلی | **۰.۰۰۵ ثانیه** |
| **اعتبارسنجی کامل داده‌ها** | گزینه `--verify` (چک‌سام و زمان) | تطابق ۱۰۰٪ سطرها و SUM/MAX | **۰.۰۴ ثانیه** |
| **مجموع زمان اجرای مهاجرت واقعی (گام ۶)** | `migrate_sqlite_to_pg.py` کامل | **۱۵۷٬۱۸۷ سطر (~۳۰.۲۲ MB)** | **۱.۲۲ ثانیه** |
| **برگشت اضطراری (Rollback)** | `TRUNCATE TABLE ... RESTART IDENTITY` | تخلیه کامل و بازنشانی توالی‌ها | **۰.۰۵۵ ثانیه** (۵۵ میلی‌ثانیه) |
| **اعتبارسنجی درج مستقل R-1** | `INSERT INTO logs ... RETURNING id` | تضمین تولید خودکار شناسه معتبر | **موفق (✔)** |

---

### تخمین خطی برای دیتابیس ۳۱۳ مگابایتی پروداکشن

با توجه به اینکه معماری ابزار مهاجرت از روش استریم خطی `cursor.copy()` با بافرینگ پایتون و تراکنش واحد بهره می‌برد، رفتار ابزار کاملاً خطی ($O(N)$) است:

- **ضریب مقیاس:** $\frac{313 \text{ MB}}{30.22 \text{ MB}} \approx 10.36 \approx 10.4$
- **تعداد کل تخمینی سوابق:** حدود ۱.۶ میلیون سطر
- **زمان برآورد شده برای انتقال داده‌ها:**
  $$1.22 \text{ s} \times 10.36 \approx 12.6 \text{ ثانیه}$$
- **زمان با احتساب ضریب اطمینان ۲ برابری (سخت‌افزار/دیسک):** **۲۵ الی ۳۰ ثانیه**.

---

### نتیجه‌گیری: آیا زیر ۳ دقیقه می‌ماند؟

**کاملاً بله؛ با حاشیه اطمینان بسیار بالا.**
- سقف زمانی تعیین‌شده در معیارهای پذیرش فاز F برابر ۱۸۰ ثانیه (۳ دقیقه) بود. زمان واقعی پیش‌بینی‌شده برای دیتابیس ۳۱۳ مگابایتی حدود **۱۳ الی ۳۰ ثانیه** است.
- کل زمان خاموشی (Downtime) پروداکشن شامل توقف واچ‌داگ، اجرای `migrate_sqlite_to_pg.py --convert-naive-timestamps --verify`، تغییر تنظیمات `.env` و راه‌اندازی مجدد سرویس، **کمتر از ۱ دقیقه** خواهد بود.

---

### توصیه‌های عملی برای سرور مالک (Production Recommendations)

۱. **اجرای حتمی Dry-Run:** قبل از اقدام به مهاجرت اصلی، دستور زیر را اجرا کنید تا از اتصال، دسترسی‌ها و تعداد سطرها اطمینان حاصل شود:
   ```powershell
   python tools\migrate_sqlite_to_pg.py --source logs.db --dry-run
   ```
۲. **استفاده از پرچم تبدیل زمان (`--convert-naive-timestamps`):** با توجه به اینکه داده‌های واقعی پروداکشن (طبق یافته‌های F.1) فاقد پسوند `Z` هستند، حتماً از این پرچم استفاده کنید تا لاگ‌ها به درستی با تبدیل UTC در PostgreSQL درج شوند:
   ```powershell
   python tools\migrate_sqlite_to_pg.py --source logs.db --convert-naive-timestamps --verify
   ```
۳. **صفر ریسک برای SQLite:** ابزار مهاجرت با URI وضعیت `mode=ro&immutable=1` به فایل `logs.db` متصل می‌شود؛ بنابراین فایل دیتابیس قدیمی هرگز دستکاری نمی‌شود.
۴. **بازگشت بدون دردسر در صورت بروز هرگونه پیشامد غیرمنتظره:** در صورتی که پس از اجرای مهاجرت خطایی مشاهده کردید، کافی است در فایل `.env` مقدار `DB_BACKEND=sqlite` را برگردانید و پروسه را ری‌استارت کنید (کل زمان بازگشت زیر ۳۰ ثانیه).

---

## ۹. چک‌لیست نهایی (همین را چاپ کنید)

```
[ ] بکاپ logs.db روی دیسک دوم + SHA-256 ثبت شد
[ ] integrity_check = ok
[ ] pg_isready + test_pg_connection.py سبز
[ ] alembic current = 0005 (head)
[ ] pytest SQLite: 597 passed, 0 skipped
[ ] pytest PG: 526 passed, 71 skipped, 0 failed
[ ] dry-run: converted = 0 و 23 ردیف نیازمند تصمیم در لاگ ثبت شد (F.5)
[ ] verify_server_timezone اجرا و نتیجه ثبت شد
[ ] set_cutover_marker --show: بدون marker
[ ] pg_dump -Fc + pg_restore --list سبز (dump = دیتابیس‌محور)
[ ] CUTOVER_DATE (ISO UTC) تعیین شد
— روز اجرا —
[ ] پورت 5050 خالی است (watchdog بسته شد)
[ ] بکاپ pre-cutover_empty.dump + تأیید لیست
[ ] migrate --dry-run: failed = 0
[ ] checksum SQLite ثبت شد (۱۱ جدول)
[ ] checksum PG = checksum SQLite (دقیق)
[ ] delta n_tup_ins = ردیف‌های منتقل‌شده (با فیلتر alembic_version)
[ ] .env: backend = postgresql | dsn_set = True
[ ] /api/status running + /metrics دارای printguard_
[ ] public.logs رشد کرد و logs.db ثابت ماند
[ ] marker نوشته و --verify شد
— ۲۴ ساعت بعد —
[ ] جدول §۴ پر شد
— یک هفته بعد —
[ ] logs.db به archives/ منتقل شد (حذف نه)
```
