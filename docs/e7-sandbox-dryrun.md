# E.7.3 — نتیجهٔ dry-run در sandbox

> افزونهٔ [`docs/cutover-runbook.md`](cutover-runbook.md). این فایل **شاهدِ اجرا** است، نه
> دستورالعمل؛ اعداد و خروجی‌ها عیناً از همان session گرفته شده‌اند.
> **دامنه:** sandbox لینوکسی (۲ vCPU · ۱ GB RAM · PostgreSQL 16.15 · Python 3.13.14).
> **صفر نوشتن روی سرور مالک، صفر push، صفر تغییر در کد production.**
> اجرا: `2026-10-05T05:53:20Z → 05:53:22Z` (کار واقعیِ ابزار: ~۲ ثانیه + اجرای alembic/golden).

---

## ۱. چه چیزی تمرین شد و چه چیزی نشد

| وضعیت | مورد | چرا |
|---|---|---|
| ✅ تمرین شد | کپی `logs.db` + SHA-256، `PRAGMA integrity_check`، شمارش جدول‌ها، `alembic upgrade head` روی اسکیمای موقت، گیت‌های ابزار `check_cutover_readiness.py` (حالت سبز و حالت مسدود)، `migrate_timestamps_to_utc.py --dry-run`، `pg_dump -Fc` + `pg_restore --list`، `export_from_pg.py` (تمرین rollback)، گیت `.env`/`resolve_backend` | همه با ابزار موجود و بدون نیاز به ویندوز/سرویس زنده |
| ⛔ تمرین‌ناشدنی | گام‌های ۴ و ۶ واقعی (کپی داده به PG) | ابزار `tools/migrate_sqlite_to_pg.py` **در فاز F ساخته می‌شود**؛ طبق دستور مالک در این گام فقط runbook + چک آمادگی اجرا می‌شود |
| ⛔ تمرین‌ناشدنی | گام‌های ۱ و ۹ روی ویندوز (`schtasks /End`، `/Run`)، خالی‌بودن پورت 5050، `/api/status`، رشد `public.logs` پس از یک چرخهٔ polling | به سرویس Flask روی ماشین مقصد نیاز دارند؛ در sandbox هیچ Flask زنده‌ای نبود |

---

## ۲. ردیف‌به‌ردیف، با خروجی واقعی

| # | گام | خروجی واقعی (از لاگ اجرا) | exit |
|---|---|---|---|
| ۰ | نقطهٔ شروع | `md5 logs.db = e29d40630c45fc1d392c9a11d60b8de2` · `public.logs rows = 0` · `schemas printguard% = 0` | 0 |
| ۱ | کپی سرد + SHA-256 | `a1faa94c97bd72a2…` برای مبدأ و مقصد **یکسان** · `167,936` بایت | 0 |
| ۲ | `CREATE SCHEMA printguard_e73_…` + `alembic upgrade head` | `migrations applied: 4` · `0.75s` · `alembic_version = 0004` · `tables in scratch schema = 12` | 0 |
| ۳ | `check_cutover_readiness.py --db /tmp/e73/logs.db` | ۹ سبز + ۱ زرد + ۰ قرمز ⇒ «آماده با هشدار» · `integrity=ok \| logs=7 \| 12 جدول` | **2** |
| ۴ | همان ابزار با `--dsn host=127.0.0.1 port=1` | ۴ قرمز: سرویس (`no response`)، DSN (`Connection refused`)، head، پورت ⇒ «⛔ cutover مجاز نیست» | **1** |
| ۵ | checksum دو طرف | SQLite: `logs 7 · meta 1 · toner_snapshots_v2 47 · yield_samples 13 · security_events 7 · toner_history 8 · cartridge_state 4 · users 3 · printer_counters 1 · sensor_readings 0 · cartridge_yield_profiles 0 · alembic_version 0` — PG (اسکیمای موقت): `logs|0 \| meta|0` | 0 |
| ۶ | گیت «صفر نوشتن» (`n_tup_*`) | **بدون فیلتر:** `1 ins / 3 upd / 0 del` — **با فیلد `relname <> 'alembic_version'`:** `0 ins / 0 upd / 0 del` | 0 |
| ۷ | `pg_dump -Fc` + `pg_restore --list` | `0.11s` · `54,628` بایت · `TOC TABLE DATA entries: 24` · verify `exit=0` در `0.04s` | 0 |
| ۸ | `migrate_timestamps_to_utc.py --dry-run` روی کپی | `rows=91 converted=0 already_standard=106 skipped=0 needs_decision=0` · «✔ dry-run: هیچ تغییری اعمال نشد» · **`backups/` ساخته نشد** (۰ فایل) · `0.07s` | 0 |
| ۹ | گیت backend (`resolve_backend`) | با DSN: `backend = postgresql \| dsn_set = True` — با `env -u PG_DSN`: `core.db.exceptions.ConfigurationError: backend=postgresql انتخاب شده ولی نشانی اتصال خالی است` ⇒ **fallback بی‌صدا وجود ندارد** | 0 / خطای صریح |
| ۱۰ | `export_from_pg.py` (rehearsal بازگشت) | `12 جدول، 1 سطر` · artifact `81,920` بایت · `manifest.row_counts` همه صفر جز `alembic_version: 1` · `dsn masked = True` · `0.27s` | 0 |
| ۱۱ | پاک‌سازی | `DROP SCHEMA printguard_e73_… CASCADE` · `schemas printguard% = 0` · `public.logs rows = 0` · `md5 logs.db` بی‌تغییر · `git status` تمیز | 0 |

---

## ۳. چهار یافته که خودِ runbook را اصلاح کرد

1. **گیت `n_tup_*` باید `alembic_version` را مستثنی کند.** روی یک اسکیمای کاملاً خالی،
   `alembic upgrade head` به‌خودی‌خود `1 ins / 3 upd` تولید می‌کند (درج `0001` و سه
   `UPDATE` برای `0002→0003→0004`)؛ اگر `relname <> 'alembic_version'` فیلتر شود، شمارش
   `0/0/0` است. سند cutover-plan این را نمی‌گفت؛ در runbook §۳ (گام ۷) اعمال شد — وگرنه همان گیت روی
   سرور مالک **همیشه** قرمز می‌شد.
2. **`pg_stat_reset()` برای نقش برنامه مجاز نیست** (`ERROR: permission denied for function
   pg_stat_reset`). پس شمارشگرها «تجمعی»اند و نباید «صفر مطلق» انتظار داشت؛ مقایسهٔ
   قبل/بعد (delta) درست است، نه مقدار مطلق. روی سرور مالک یا با superuser reset کنید یا
   delta بگیرید (در گام ۷ و §۴ runbook نوشته شده).
3. **`pg_dump` دیتابیس‌محور است، نه schema‌محور.** تمرین روی اسکیمای موقت، خروجی کل
   `printguard` را گرفت (`24` ورودی `TABLE DATA`)؛ اگر فاز F خواست فقط یک schema بکاپ شود،
   `-n <schema>` لازم است. size مرجع برای دیتای امروزِ مخزن: `54,628` بایت.
4. **`export_from_pg.py` در حالت `db` همان‌طور که انتظار می‌رفت `alembic_version` را هم
   می‌بَرَد** (`row_counts = {… 'alembic_version': 1}`)؛ پس فایل بازگشت، head را هم برمی‌گرداند
   و برای merge دستیِ پس از cutover باید این سطر از `logs.db` مقصد نادیده گرفته شود
   (در غیر این صورت head دوتایی می‌شود).

---

## ۴. بازتولید (برای مالک، روی هر ماشین)

```powershell
# ۰) نقطهٔ شروع
python -c "import sqlite3;print(sqlite3.connect('logs.db').execute('PRAGMA integrity_check').fetchone()[0])"
# ۱) چک آمادگی (قبل از روز اجرا)
python tools\check_cutover_readiness.py --json D:\pgbackup\ready.json
# ۲) تمرین روی کپی — هر چیزی که در مخزن دست‌نخورده بماند
Copy-Item logs.db $env:TEMP\e73-logs.db -Force
python tools\migrate_timestamps_to_utc.py --db $env:TEMP\e73-logs.db --dry-run
# ۳) بلوک‌کردن عمدی (باید exit 1 بدهد)
python tools\check_cutover_readiness.py --dsn "host=127.0.0.1 port=1 dbname=x user=x"
# ۴) تمرین بازگشت
python tools\export_from_pg.py --dsn "host=localhost dbname=printguard user=printguard" --out $env:TEMP\after.db --manifest $env:TEMP\m.json --quiet
```

انتظار: ۲ ⇒ `0` یا `2` (بسته به ردیف‌های pre-cutover)، ۳ ⇒ `1`، ۴ ⇒ `0` با `row_counts` در manifest.

---

## ۵. نتیجه

runbook و ابزار آمادگی در sandbox **اجرا شدند و رفتارشان مطابق سند بود**: سه کد خروج
(۰/۱/۲) هر سه دیده شدند، گیت‌های «فایل خراب/DSN خراب/پورت بسته» واقعاً قرمز شدند، و هیچ
نوشتنی از این اجرا باقی نماند (`logs.db` بی‌تغییر، `public.logs = 0`، صفر اسکیمای
باقی‌مانده، `git status` تمیز). چهار یافتهٔ §۳ در runbook اعمال شد؛ دو گام (۴ و ۶ واقعی)
منتظر ابزار فاز F‌اند و در سند با همان برچسب باز هستند.
