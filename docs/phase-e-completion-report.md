# گزارش کامل‌شدن فاز E — زیرساخت PostgreSQL

> مرجع نهایی فاز E.0 → E.7. سند کار: [`phase-e-plan.md`](phase-e-plan.md) · تصمیم‌ها:
> [`phase-e-decisions.md`](phase-e-decisions.md) · بدهی: [`tech-debt.md`](tech-debt.md) ·
> اجرا: [`cutover-runbook.md`](cutover-runbook.md) + [`e7-sandbox-dryrun.md`](e7-sandbox-dryrun.md).
> **این فاز از مرز cutover عبور نکرد:** هیچ نوشتن Flask روی PG آغاز نشد، هیچ بکاپی روی
> سرور مالک ساخته نشد، و **هیچ pushی** انجام نشد.

---

## ۱. سرجمع

| مورد | مقدار (سنجیده‌شده در همین session) |
|---|---|
| HEAD این گزارش | `0c4b8db` = آخرین کامیتی که اعداد برایش سنجیده شد؛ خودِ گزارش یک کامیت
  بعد نشست (فقط-سند، پس اعداد دست‌نخورده‌اند). `git status` تمیز و `git remote -v` **خالی** ⇒ هیچ pushی |
| کامیت‌ها روی شاخه | ۸۶ (از `329f3bf` = سرِ `main`) · ۱۲۴ فایل · +۲۷٬۴۶۹ / −۴۸۹ خط |
| کامیت‌های فاز E | ۷۱ (از `3fceb18` نخستین `(e0)` تا `0c4b8db`) — ۲۱ سند، ۵۰ کد/تست |
| migrationهای alembic | **۴** فایل: `0001_initial_schema` · `0002_boolean_columns` · `0003_identity_columns` · `0004_jsonb_columns` |
| سرشاخهٔ PG | `public.alembic_version = 0004` (سنجیده با `psql`) |
| تست | **۵۹۷ passed / 0 skipped / 0 failed** روی SQLite · **۵۲۶ passed + ۷۱ skipped (اعلام‌شده) / 0 failed** روی PG |
| سابتست‌ها | ۱۰۹ (SQLite) · ۱۰۷ (PG) |
| golden | ۱۰/۱۰ فایل، تفاضلِ صفر بایت‌به‌بایت |
| فایل‌های تست | ۴۳ فایل، ۱۳٬۸۷۴ خط در `tests/*.py` (۴۲ تست آخر = `tests/test_cutover_readiness.py`) |
| `tools/test_dual_backend.py` | `OK=18 / EXPECTED=1 / FAIL=0` |
| pyflakes | `core web` = **۳۵** (بدون تغییر از قبل) · فایل‌های تازهٔ E.7 = **۰** · `tools/` کلاً ۵ موردِ از پیش موجود (`get_oauth_token.py` ×۴، `record_golden.py` ×۱) که در گیت نیستند |
| `logs.db` | `md5 = e29d40630c45fc1d392c9a11d60b8de2` — بایت‌به‌بایت دست‌نخورده در تمام فاز |
| سلامت PG پس از همهٔ اجراها | `public.logs = 0` ردیف · `leftover schemas = 0` |

---

## ۲. زیربخش‌به‌زیربخش

| گام | چه چیزی نشست | کامیت(ها) |
|---|---|---|
| E.0 | سنجش واقعیت: مصرف‌کننده‌های timestamp، `sqlite_master`، `PRAGMA`، `rowid`؛ ابزار `analyze_old_events.py` و `verify_server_timezone.py`؛ حل تناقض `Z`؛ `test_pg_connection.py` · `init_pg_database.py` | `3fceb18`…`fcd3261` (۱۲ کامیت `(e0)`) |
| E.1 | وابستگی‌ها (`psycopg3` + `alembic`)، `alembic init` با resolver دوسوکولِ URL و baseline `0001` روی PG؛ `env.py` از `DB_BACKEND`/`PG_DSN` می‌خواند و **fallback بی‌صدا ندارد** | `806d69b` · `c535a7a` |
| E.2 | backend دوگانه در `core/db` (`resolve_backend`/`resolve_dsn`)، ابزار `test_dual_backend.py`، به‌روزرسانی `docs/db-adapter.md`، قفل `timezone=UTC` روی `options` | `c86998a` · `03ef70f` · `50f2236` |
| E.3 | قرارداد زمان: `utc_now_iso` در writerها، خواننده‌های UTC-aware، سبد روزانه در پایتون، مهاجرت timestampهای `logs.db`، ۱۰ fixture golden دوباره ضبط شد، TD-003 بسته شد | `05446c1`…`7d67285` |
| E.4 | strictness: سلسله‌مراتب خطای مشترک، `executescript` روی PG، schema_columns، savepoint/import guard، `json_extract` قابل‌حمل، `GROUP BY` متریک‌ها، ordering بدون `COLLATE NOCASE`، upsert مشترک، `BOOLEAN` (`0002`)، `IDENTITY` (`0003`)، `lastrowid → insert_returning_id` | `3f4a0e7`…`8b2f40e` (۱۵) |
| E.5 | ایزولاسیون تست PG با **`CREATE SCHEMA` + `options=-csearch_path=`** (هر اجرای pytest یک اسکیمای تازه، `ON COMMIT DROP` نه)، marker `sqlite_only` (۷۱ تست)، معیارهای پذیرش و عددِ هر دو backend | `f99d579`…`5cfc99b` |
| E.6 | ۹ تصمیم مستند + کد: بازنشستگی نوشتن import روی PG، بکاپ `pg_dump -Fc` + `pg_restore --list`، collation `C`، `logs.details → jsonb` (`0004`)، حذف TD-001، allowlist `next`، بایندینگ `log`؛ TD-012 ثبت، TD-013/TD-014 پدیدار | `0ce1b33`…`bbc9491` (۹) |
| E.7 | runbook عملیاتی + پیوست زمان اندازه‌گیری‌شده · `tools/check_cutover_readiness.py` با ۴۲ تست · شاهد dry-run در sandbox و ۴ اصلاح خودِ runbook | `1f23518` · `8623655` · `0c4b8db` |

---

## ۳. اعدادی که تصمیم‌ها را توجیه می‌کنند (فقط خروجیِ همان session)

| سنجه | قبل | بعد | منبع |
|---|---|---|---|
| `scrape` dashboard روی PG | ۱۱۹.۳ ms | **۴۶.۸ ms** | `logs.details → jsonb` (E.6.4) |
| dedup query | ۱۹.۳ ms | ۱۷.۸ ms | همان |
| درج ۲۰٬۰۰۰ ردیف | ۷۹ ms | ۳۶ ms | همان |
| حجم دیتابیس | ۴۰ MB | ۴۰ MB (بدون تغییر) | همان |
| شاخص GIN روی `details` | ۱۱۶ ms | بی‌فایده ⇒ **انجام نشد** | همان |
| ترتیب `event_key` با collation `fa-IR-x-icu` | — | ۱۵ از ۱۸ مورد جابه‌جا ⇒ `C` ماند | E.6.3 |
| سوئیت SQLite | — | ۵۱.۹۷ s (`TZ=UTC`) / ۵۱.۸۰ s (`TZ=Asia/Tehran`) | دروازهٔ پایانی این گزارش |
| سوئیت PG | — | ۱۱۹.۴۵ s | همان |
| `tools/record_golden.py --check` | — | ۴۴.۷۱ s | همان |
| `check_cutover_readiness.py` | — | ~۰.۸ s · `ok=9 warn=1 fail=0 skip=0` ⇒ exit 2 | همان |

---

## ۴. بدهی فنی: وضعیت ۱۵ ردیف

`docs/tech-debt.md` (TD-001…TD-015، جدول ۷ستونه — ۱۵ ردیف، همه ۸ pipe). خلاصهٔ تصمیم‌محور:

- **بسته در فاز E:** TD-001 (حذف کد مرده)، TD-002 (allowlist `next`)، TD-003 (نرمال‌ساز
  golden)، TD-004 (بایندینگ `log`).
- **با برچسب «موکول»:** TD-005 → **فاز F** (بازنشانی sequence پس از بارگذاری انبوه؛
  `setval` بدون داده معنا ندارد)، TD-006 → **فاز I** (retention/archiving).
- **تصمیمِ «همین‌طور می‌ماند»:** TD-007/TD-008/TD-009 (نوشتن import روی PG بازنشسته است؛
  جانشین = ابزار فاز F) و TD-013 (collation `C`).
- **باز و وابسته به داده:** TD-010 (اسکریپت بکاپ سرور ⇒ مسیر `logs.db` را به
  `archives/` تغییر دهد، مرحلهٔ ۳ runbook)، TD-011 (`public` هاردکد در `tools/`)،
  TD-012 (۷۱ skip با marker `sqlite_only`)، TD-014 (چهار ستون JSON دیگر ⇒ jsonb،
  مشروط به دادهٔ واقعی فاز F).
- **بدهی پذیرفته‌شده (no-action آگاهانه):** TD-015 — ۱۵ هندلر `except sqlite3.*` در ۶ فایل
  `tools/`؛ عدد با همان AST-اسکنِ تست قرارداد سنجیده شد (پیش از `d4e6cf9`: ۱۲ محل در کد
  برنامه که همه منتقل شدند، و ۱۱+۳ در ابزار/تست که عمداً ماندند).

---

## ۵. آنچه فاز F تحویل می‌گیرد (بدون زمان‌بندی)

1. `tools/migrate_sqlite_to_pg.py` — کپی واقعی SQLite → PG با dry-run/checksum/گزارش؛
   گام‌های ۴ و ۶ runbook امروز با برچسب «فاز F» بازند.
2. `tools/import_after_pg.py` — حلقهٔ کاملِ بازگشت (مرج خودکار + `migration_source`)؛
   امروز merge دستیِ خروجیِ `export_from_pg.py` است.
3. تصمیم **F.5** دربارهٔ ۲۳ ردیفِ pre-cutover با نشانهٔ `Z` (ابزار `analyze_old_events.py`
   هر بار exit 2 می‌دهد تا این تصمیم گرفته شود).
4. سنجش `setval`/sequences پس از بارگذاری انبوه (TD-005).
5. اجرای واقعی cutover روی سرور مالک — با همان گیت‌های runbook و `check_cutover_readiness.py`.

---

## ۶. سنجهٔ «صفر عبور از مرز» در پایان فاز E

```
md5sum logs.db                      → e29d40630c45fc1d392c9a11d60b8de2   (باید ثابت بماند)
psql -tAc "select count(*) from public.logs"     → 0
psql -tAc "select version_num from public.alembic_version" → 0004
psql -tAc "select count(*) from pg_namespace where nspname like 'printguard%'" → 0
git status --short                  → خالی
git remote -v                       → هیچ pushی؛ شاخهٔ phase-c روی remote نیست
```

هر شش خط بالا در پایان این فاز اجرا و تأیید شد (خروجی‌ها در همین جدول است). بازتأییدِ
نهایی پس از نشستِ این گزارش هم گرفته شد: سوئیت SQLite در `TZ=Asia/Tehran` ⇒
`597 passed, 109 subtests` در `52.24s` · golden ⇒ تفاضل صفر · `pyflakes core web` ⇒ ۳۵ ·
`check_cutover_readiness.py` ⇒ `ok=9 warn=1 fail=0 skip=0` (همان ۱ زرد = ۲۳ ردیف pre-cutover
که تصمیمشان F.5 است) · `md5 logs.db` بی‌تغییر.

---

## ۷. سه جملهٔ پایانی

فاز E **بدون تغییر منطق دامنه** تمام شد: writer/readerهای `core/` و routeهای `web/` همان
رفتار قبلی را دارند و فقط «لایهٔ اتصال» و «قرارداد زمان» دوسوکول شدند. هر عدد در این گزارش
خروجیِ یک اجرای واقعی است — از جمله چهار عددی که در نسخه‌های پیشینِ برنامه‌ریزی «تخمینی»
بودند و در نهایت یا اصلاح شدند یا به پیوستِ sandbox منتقل شدند. تنها کار باقی‌ماندهٔ فاز،
عبور از مرز است که به **فاز F** و به تصمیم مالک موکول شده است.
