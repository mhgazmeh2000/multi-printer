# طرح cutover — SQLite → PostgreSQL (فاز E)

**دامنه:** این سند فقط **دستور کار** است، نه اجرا. در فاز E.0 هیچ‌چیز از این سند اجرا نمی‌شود.
**پیش‌نیازها:** `docs/db-adapter.md` (لایه‌ی adapter)، `docs/postgresql-strictness.md` (سخت‌گیری‌های PG)، `docs/timestamp-consumers.md` §۶ (الزام آگاه-UTC)، `docs/sql-paths-inventory.md` (مسیرهای SQL خام).
**قید محصول:** بومی ویندوزسرور — بدون Linux/Docker/WSL. PostgreSQL 16 تنها منبع حقیقت در فاز E.

---

## ۱. تعریف cutover

> **cutover = اولین نوشتنِ Flask روی PostgreSQL.**

نه اولین اتصال، نه اولین خواندن، نه سبز شدن تست‌ها. تا وقتی که **هیچ سطری** توسط
اپلیکیشن در PG نوشته نشده، می‌توان بدون هیچ ابزار خاصی به عقب برگشت؛ بعد از اولین
`INSERT`/`UPDATE`، «دیتابیس حقیقت» رسماً PG است و SQLite از آن لحظه **کهنه** می‌شود.

پیامد مستقیم این تعریف در دو حالت:

| لحظه | مسیر بازگشت | شرایط |
|---|---|---|
| **قبل از cutover** | `DB_BACKEND=sqlite` + ری‌استارت سرویس | هیچ داده‌ی تازه‌ای در PG نباشد (شرط قابل سنجش — §۳.۵) |
| **بعد از cutover** | `tools/export_from_pg.py` ⇒ یک SQLite تازه ⇒ `DB_BACKEND=sqlite` + ری‌استارت | برون‌بری + بازبینی checksum/شمارش سطرها موفق باشد (§۶) |

---

## ۲. چیزهایی که باید **قبل** از cutover تصمیم‌گیری و قفل شود

تاریخچهٔ «مرجع»: سطرهای ۲ تا ۸ این جدول به «بند N همان سند» ارجاع می‌دادند، درحالی‌که
بندها در `docs/postgresql-strictness.md`‌اند نه در این سند — اصلاح شد (E.6.11).
همۀ تصمیم‌های این جدول در `docs/phase-e-decisions.md` بسته شده‌اند.

| # | تصمیم | مرجع | وضعیت |
|---|---|---|---|
| ۱ | مرز روز = نیمه‌شب تهران (`AT TIME ZONE 'Asia/Tehran'`) | `docs/postgresql-strictness.md` بند ۱ | ✅ اعمال‌شده (E.3.4؛ قرارداد در `docs/timezone-contract.md`) |
| ۲ | collation سرور (`C` یا ICU/`fa-IR`) | `docs/postgresql-strictness.md` بند ۴ | ✅ **`C` می‌ماند** (E.6.3، `8ef0a9f`) — دلیل و عدد: `docs/phase-e-decisions.md` §۶.۳ |
| ۳ | boolean ستون‌ها `BOOLEAN` (نه INTEGER) | بند ۵ همان سند | ✅ اعمال‌شده (E.4.1، `0002`) |
| ۴ | `TimeZone=UTC` روی اتصال (`options=-c timezone=UTC`) | بند ۷ همان سند | ✅ اعمال‌شده (E.2/E.3؛ تست سخت‌گیری `SHOW TimeZone`) |
| ۵ | `IDENTITY` برای idها + `setval` پس از مهاجرت | بند ۸ همان سند | ✅ `0003` اعمال‌شده؛ `setval` ⇒ گام ۴ سند جاری (فاز F) |
| ۶ | `RETURNING id` برای `core/database.py:510` | بند ۹ همان سند | ✅ کد انجام‌شده (E.4.3، `e12aeb6`) |
| ۷ | `import_db.py` (ورود فایل `.db` دیگر): بازنشستگی یا ترجمه | `docs/postgresql-strictness.md` بند ۱۱ | ✅ **بازنشستگی نوشتن روی PG** (E.6.1، `0ce1b33`)؛ ترجمه ⇒ ابزار فاز F |
| ۸ | JSONB یا TEXT برای `logs.details` | بند ۱۵ همان سند | ✅ **JSONB روی PG** (`0004`، E.6.4، `0a21aed`)؛ SQLite همان TEXT |
| ۹ | **الزام آگاه-UTC:** همه‌ی writers فاز E به `datetime.now(timezone.utc)` | `docs/timestamp-consumers.md` §۶ | **الزام کد** |
| ۱۰ | نشانه‌ی cutover در جدول `meta` (`timezone_cutover_at`) | `tools/set_cutover_marker.py` (E.0.13) | آماده |

> ⛔ **گیت سخت:** تا وقتی الزام ۹ اجرا نشده، cutover **ممنوع** است. دلیل: ستون
> `TIMESTAMPTZ` هر مقدار naive را با TimeZone نشست تفسیر می‌کند؛ با writers محلی،
> هر نوشتن تازه ۳:۳۰ دقیقه خطا می‌شود (همان باگ D.6، این بار خاموش و در داده).

---

## ۳. روش اجرا — ۷ گام

### گام ۱ — dry-run بدون نویسنده
هدف: اثبات اینکه خواندن‌ها کار می‌کنند، **بدون** اینکه چیزی در PG نوشته شود.
- اجرای مهاجرت (کپی داده) در حالت فقط‌خواندنی/تک‌شات؛
- اجرای اپلیکیشن با `DB_BACKEND=postgresql` اما بدون poller؛ **کلید `POLLING_ENABLED` در کد
  وجود ندارد** (با grep تأیید شد) ⇒ توقف poller همان توقف فرایند برنامه است
  (`schtasks /End /TN "PrintGuardMonitor"`؛ دستورهای دقیق در `docs/cutover-runbook.md` §۳)،
  فقط برای گردش صفحات و APIها؛
- ثبت `n_tup_ins`/`n_tup_upd` جدول‌های PG در ابتدا و انتهای گام؛ **اختلاف باید صفر باشد**.
- خروجی گام: گزارش کوتاه «کدام فهرست/pages/APIها بازبینی شد + صفر نوشتن».

### گام ۲ — تأیید ۲۷۳ تست روی **هر دو** دیتابیس
```powershell
# مسیر فعلی (SQLite) — همان معیار پذیرش امروز
python -m pytest -q                                   # انتظار: 273 passed، 0 skipped

# پس از افزودن backend در فاز E
$env:DB_BACKEND="postgresql"; python -m pytest -q      # همان ۲۷۳، بدون skip
$env:DB_BACKEND="sqlite";     python -m pytest -q      # دوباره سبز (بدون رگرسیون)
python tools/record_golden.py --only all --check       # ۱۰/۱۰ تفاضل-صفر
```
معیار: **۲۷۳ passed در هر دو backend، ۰ skipped، ۰ xfail** و goldenها دست‌نخورده.
هیچ تستی نباید «فقط SQLite» یا «فقط PG» باشد؛ هر تفاوت، بند ۲ را باطل می‌کند.

### گام ۳ — توقف Flask
- توقف سرویس وب (و poller) تا هیچ نویسنده‌ای فعال نباشد؛
- بکاپ اجباری SQLite قبل از هر کار:
  ```
  archives\logs.db.bak        ← کپی سردِ دیتابیس SQLite در همین لحظه
  ```
  («سرد» = پس از توقف کامل؛ کپی داغِ WAL می‌تواند ناقص باشد.)
- ثبت اثر انگشت بکاپ: `certutil -hashfile archives\logs.db.bak SHA256`.

### گام ۴ — مهاجرت + checksum
- اجرای مهاجرت (فاز F) از `archives\logs.db.bak` به PG؛
- پس از مهاجرت، سه‌گانه‌ی بازبینی:
  | سنجه | روش |
  |---|---|
  | شمارش سطر هر جدول | `SELECT COUNT(*)` در دو طرف (۱۱ جدول برنامه + `alembic_version`) — شمارش «۱۰» این سند پیش از افزودن `meta`/`toner_snapshots_v2` نوشته شده بود؛ مرجع: `core/db/schema.py::APP_TABLES` |
  | برد بازه‌ی زمانی | `MIN/MAX(timestamp)` روی `logs`, `toner_history`, `sensor_readings`, `security_events` |
  | جمع کنترلی | `SUM(pages)` روی `logs`، `SUM(value)` روی `sensor_readings` |
- خروجی گام: جدول «مقایسه‌ی دو طرف» با اعداد، در گزارش cutover.
- **ثبت `n_tup_ins`/`n_tup_upd` پس از مهاجرت** به‌عنوان مبنای گام ۵ و ۳.۵.

### گام ۵ — کلید زدن `DB_BACKEND=postgresql`
```powershell
$env:DB_BACKEND="postgresql"
```
- این نقطه، لحظه‌ی «آماده‌سازی» است؛ خودِ نوشتن در گام ۶ رخ می‌دهد؛
- اجرای `tools/set_cutover_marker.py` برای ثبت `timezone_cutover_at` در `meta`
  (نشانه‌ی زمانِ مرجع در دیتابیس؛ مهاجرت فاز F و ابزارهای بعدی از آن می‌خوانند).

### گام ۶ — راه‌اندازی و اولین نوشتن (این همان cutover است)
- استارت سرویس وب + poller؛
- **اولین چرخه‌ی poll** که یک ردیف در PG می‌نویسد = عبور از مرز.
- تأیید فوری پس از عبور: یک سطر تازه در `logs`/`toner_history` با timestamp
  **آگاه-UTC** و درست (اختلاف با UTC واقعی نزدیک صفر، نه ۳:۳۰).

### گام ۷ — تأیید ۲۴ ساعت
پنجره‌ی پایش ۲۴ ساعته با این سنجه‌ها:
| سنجه | حد پذیرش |
|---|---|
| خطای نوشتن/خواندن در لاگ | صفر خطای تکرارشونده |
| انحراف زمانی سطرهای تازه | نزدیک صفر (هیچ سطر ۳:۳۰± جابه‌جا) |
| شمارش سطرها در مقایسه با نرخ معمول | در محدوده‌ی نوسان معمول، نه صفر و نه جهش |
| صفحات کلیدی UI (داشبورد/آمار/گزارش) | باز و درست |
| `/metrics` و ۶۷ روت | بدون تغییر ساختار؛ همان رفتار |

اگر هر سنجه شکست خورد ⇒ **بازگشت (§۶)** و ثبت علت در گزارش فاز.

---

## ۳.۵ شرط «هیچ داده‌ی تازه در PG» (درجه‌سنجی گام ۱)

```sql
-- در ابتدا و انتهای گام ۱ (و بعد در گام ۴/۵) اجرا و مقایسه شود:
SELECT relname, n_tup_ins, n_tup_upd, n_tup_del
FROM pg_stat_user_tables ORDER BY relname;
```
- اگر `n_tup_ins`/`n_tup_upd`/`n_tup_del` بین دو خوانش **بدون تغییر** باشد، بازگشتِ
  پیش‌از-cutover (فقط `DB_BACKEND=sqlite` + ری‌استارت) بی‌خطر و بدون ابزار است.
- از لحظه‌ای که این اعداد بالا می‌روند (گام ۶)، بازگشت دیگر بی‌ابزار نیست (§۶).

---

## ۴. چه چیزی cutover **نیست** (اشتباه‌های رایج)

- «اپلیکیشن به PG وصل شد» ⇒ نه؛ اتصال/خواندن، برگشت‌پذیرِ بی‌هزینه است.
- «تست‌ها روی PG سبز شد» ⇒ نه؛ آن گام ۲ است.
- «مهاجرت داده انجام شد» ⇒ نه؛ تا وقتی Flask در PG **نمی‌نویسد**، SQLite هنوز حقیقت است
  و می‌توان با خیال راحت مهاجرت را پاک کرد و از نو زد.
- «`DB_BACKEND` را عوض کردم ولی poller خاموش است» ⇒ نزدیک مرز ولی نه cutover؛
  عبور از مرز = اولین نوشتن.

---

## ۵. بکاپ‌ها و مسیرها

| مورد | مسیر | توضیح |
|---|---|---|
| بکاپ سرد SQLite (گام ۳) | `archives\logs.db.bak` | منبع مهاجرت؛ با sha256 ثبت می‌شود |
| بکاپ‌های زمان اجرا | کنار `logs.db` با الگوی `logs.db.bak_YYYYmmdd-HHMMSS` | ساخته‌ی ابزارهای موجود |
| بکاپ تأییدشدهٔ PostgreSQL | کنار فایل دیتابیس، الگوی `printguard-pg-<stamp>.dump` | `pg_dump --format=custom` + تأیید با `pg_restore --list` (E.6.2 در `tools/migrate_timestamps_to_utc.py`)؛ `--backup-only` = فقط بکاپ، بدون هیچ نوشتنی؛ sha256 در manifest (`pg_backup`) |
| بکاپ سرد SQLite در ابزار مهاجرت زمان | کنار `logs.db`، الگوی `logs.db-<stamp>.e3bak` | کپی فیزیکی + مقایسهٔ sha256 (پیش‌فرض ابزار؛ بدون تغییر رفتار) |
| خروجی بازگشت (اگر لازم شد) | `archives\logs-after-cutover.db` | ساخته‌ی `tools/export_from_pg.py` |
| مانیتور برون‌بری | `archives\logs-after-cutover.db.manifest.json` | شمارش سطر + sha256 + شمارش timestampهای naive |

---

## ۶. بازگشت پس از cutover (نقش `tools/export_from_pg.py`)

```powershell
# ۱) توقف Flask (تا لحظه‌ی بازگشت هیچ نویسنده‌ای روی PG نباشد)
# ۲) برون‌بری PG → SQLite
python tools\export_from_pg.py --out archives\logs-after-cutover.db --manifest
#    ⚠ اگر خروجی گفت «timestampهای naive» ⇒ الزام آگاه-UTC رعایت نشده؛
#      قبل از بازگشت گزارش بدهید (نه اینکه بی‌صدا جلو بروید).
# ۳) بازبینی: شمارش سطرها + sha256 در فایل manifest
# ۴) کلید برگشت و ری‌استارت
$env:DB_BACKEND="sqlite"
```
قواعد:
- خروجی برون‌بری **جایگزین** دیتابیس زنده نمی‌شود؛ اول به مسیری جدا نوشته و بازبینی
  می‌شود، بعد (با تصمیم صریح) جای `logs.db` را می‌گیرد؛
- ترتیب جدول‌ها اهمیت ندارد؛ کلید اصلی چندستونی (`printer_ip, color`) پشتیبانی می‌شود؛
- boolean → `0/1`، `TIMESTAMPTZ` → ISO آگاه-UTC، `JSONB` → متن JSON؛ یعنی همان چیزی
  که کد امروزِ SQLite انتظار دارد؛
- در حالت `--mode sql` یک فایل `restore.sql` با `INSERT`های لیترالی ساخته می‌شود که
  روی دیتابیسِ ساخته‌شده با خود اپلیکیشن قابل اجراست (schema کارِ اپلیکیشن است).
- اگر `psycopg` نصب نباشد یا خروجی موجود باشد و `--force` نداشته باشید، ابزار با
  کد خروج ۲ («نیازمند اقدام انسانی») برمی‌گردد — نه با شکست مبهم.

---

## ۷. نشانه‌ی cutover در دیتابیس

`tools/set_cutover_marker.py` (E.0.13 — **ساخته شد**) جدول `meta(key, value, updated_at)`
را idempotent می‌سازد و رکورد `timezone_cutover_at` را با ISO UTC می‌نویسد:

```powershell
python tools\set_cutover_marker.py --show                       # نمایش مقدار فعلی (فقط‌خواندنی)
python tools\set_cutover_marker.py                              # پیش‌فرض: زمان کامیت b548e99
python tools\set_cutover_marker.py --date 2026-10-05T00:00:00Z  # گام ۵: لحظه‌ی واقعی
python tools\set_cutover_marker.py --db archives\logs.db.bak --date 2026-10-05T00:00:00Z
python tools\set_cutover_marker.py --verify                     # exit 1 اگر نشانه نباشد/نامعتبر باشد
```

- پیش‌فرض: زمان کامیت `b548e99` از `git show -s --format=%cI b548e99` (پایان فاز C، مبنای
  قرارداد زمانی)؛ اگر git در دسترس نباشد با کد خروج ۲ و پیام روشن متوقف می‌شود تا
  تاریخ صریح بدهید (هیچ مقدار حدسی نوشته نمی‌شود)؛
- `--date` **فقط ISO آگاه-UTC** را می‌پذیرد؛ naive یا دارای offset محلی با پیام روشن رد
  می‌شود (چون مرجع اشتباه = ۳:۳۰ خطا در کل مهاجرت فاز F)؛
- اجرای دوباره = `UPDATE` (هیچ سطر تکراری‌ای ساخته نمی‌شود): آپسرت با
  `INSERT ... ON CONFLICT(key) DO UPDATE` نوشته شده که **همین حالا هم در PG معتبر است**؛
- این جدول **تنها افزوده‌ی schema در فاز E.0** است (قید فاز) و در فاز E به PG منتقل
  می‌شود؛ migration فاز F از همان مقدار می‌خواند؛
- امنیت اجرا: اگر فایل دیتابیس نباشد خطا می‌دهد (مگر `--allow-create`) و `--show/--verify`
  فقط با `mode=ro` باز می‌کنند.

---

## ۸. فهرست بازبینی نهایی (قبل از اعلام پایان cutover)

- [ ] گام ۱: صفر نوشتن در PG (`n_tup_*` بی‌تغییر) و گزارش صفحات بازبینی‌شده
- [ ] گام ۲: ۲۷۳ passed در هر دو backend + golden ۱۰/۱۰
- [ ] گام ۳: `archives\logs.db.bak` + sha256 ثبت‌شده
- [ ] گام ۴: جدول مقایسه‌ی شمارش/بازه/جمع کنترلی، همه منطبق
- [ ] گام ۵: `DB_BACKEND=postgresql` + نشانه‌ی `meta`
- [ ] گام ۶: اولین نوشتن + timestamp آگاه-UTC درست
- [ ] گام ۷: ۲۴ ساعت پایش بدون شکست سنجه‌ها
- [ ] الزام آگاه-UTC (بند ۲.۹) پیش از گام ۶ اجرا شده
- [ ] تصمیم‌های بند ۲ (collation، JSONB، import_db، identity) ثبت‌شده

---

## ۹. کارهای باز مرتبط

| مورد | وضعیت |
|---|---|
| push شاخه‌ی `phase-c` (E.0.9) — **باز، نیازمند تصمیم مالک** | اعتبارنامه‌ی GitHub در محیط توسعه موجود نیست؛ تغییرات فقط محلی‌اند. تا پیش از push، هر مهاجرت/بازگشتی صرفاً روی همین ماشین معنا دارد. |
| `import_db.py` (بند ۲.۷) | تصمیم محصولی: بازنشستگی یا ترجمه |
| TD-001 (کد مرده‌ی `_save_counters_to_db`) | در فاز E همراه با writers رفع می‌شود |
| TD-002 (بازتاب `next` در ۴۰۱) | در فاز E همراه با بازبینی auth |
| TD-003 (نرمال‌ساز golden: «N/A» → «N<PATH>») | فاز E، همراه بازضبط پس از انتقال writers به UTC |

*این سند فقط دستور کار است؛ در فاز E.0 هیچ‌کدام از گام‌ها اجرا نشده و هیچ کدی تغییر نکرده است.*
