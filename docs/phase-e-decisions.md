# Phase E.6 decisions — locked before cutover

**مخزن:** شاخه‌ی `phase-c` · **تاریخ:** ۲۰۲۶-۱۰-۰۴ · **دامنه:** E.6.1 … E.6.11
**قاعدهٔ نگارش این سند (دستور مالک):** برای هر تصمیم سه چیز نوشته شود —
**دلیل**، **جایگزینی که رد شد و چرا**، و **اثر**. هیچ تصمیمی با «به‌نظر می‌رسد»
گرفته نشد؛ هر جا عدد لازم بود، روی همین سرور اندازه‌گیری شد (PG 16.15 در سندباکس،
`printguard`، `datcollate = C`، SQLite از `logs.db` مخزن).

**وضعیت baseline پس از E.6:** SQLite **۵۵۵ passed / ۰ failed / ۰ skipped**
(۱۰۹ subtest) در هر دو نشست `TZ=UTC` و `TZ=Asia/Tehran` · PostgreSQL
**۴۸۴ passed / ۰ failed / ۷۱ skipped** (۱۰۷ subtest؛ skip‌ها marker
`sqlite_only` از E.5.2‌اند — TD-012) · golden `--check` **۱۰/۱۰ تفاضل صفر** ·
`pyflakes core web` **۳۵** · `logs.db` بی‌تغییر
(`e29d40630c45fc1d392c9a11d60b8de2`) · `public.logs` **۰ ردیف** (هنوز هیچ نوشتن
تولیدی روی PG رخ نداده) · سرِ migration: **0004**.

---

## Summary table (10 rows)

| # | موضوع | تصمیم | کامیت | اثر بر production |
|---|---|---|---|---|
| ۶.۱ | `import_db.py` روی PG | **بازنشستگی مسیر نوشتن** + اصلاح بازکردن `analyze` برای خواندن | `0ce1b33` | گارد + ۲ هلپر؛ رفتار SQLite عیناً امروز |
| ۶.۲ | بکاپ در `tools/migrate_timestamps_to_utc.py` | `pg_dump -Fc` + **تأیید** با `pg_restore --list`؛ SQLite همان کپی | `5570992` | فقط ابزار (فاز E/F)، نه مسیر اجرا |
| ۶.۳ | collation متن روی PG | **`C` می‌ماند** (هم‌ارزی با `BINARY` نتایج SQLite) | `8ef0a9f` | صفر تغییر schema؛ ۷ تست قفل + TD-013 |
| ۶.۴ | JSONB | **فقط `logs.details` ⇒ `jsonb`** روی PG (migration `0004`)؛ چهار ستون دیگر TEXT | `0a21aed` | یک migration PG-only + ۴ خط در خواننده |
| ۶.۵ | TD-001 کد مرده | **حذف** `_save_counters_to_db` (نه احیا) | `931bb42` | −۵۳ خط مرده؛ قفل بازنگشت |
| ۶.۶ | TD-002 بازتاب `next` | **allowlist** به‌جای حذف query string + بستن حفرۀ `\` | `de0a602` | `web/auth.py` + `web/__init__.py`؛ ۱۱ تست |
| ۶.۷ | TD-004 `log` تعریف‌نشده | بایندینگ `log = logging.getLogger("PrinterMonitor")` | `f3d4b7c` | `web/routes/security.py` +۷ خط؛ قفل AST |
| ۶.۸ | TD-005 / TD-006 | **ثبت و موکول**: manifest ⇒ فاز F، ساعات سکوت ⇒ فاز I | این کامیت | بدون تغییر کد |
| ۶.۹ | TD-009 `_exists_by_cols` | **بدون رفع** — زیر گارد بازنشستگی ۶.۱ می‌ماند | همراه `0ce1b33` | صفر تغییر |
| ۶.۱۰ | TD-012 تست‌های SQLite-محور | **ثبت رسمی در جدول بدهی** (مدیریت‌شده، نه رفع‌شده) | این کامیت | صفر تغییر کد |

---

## 6.1 — `import_db.py`: retirement, not translation

**تصمیم:** مسیر **نوشتن** import روی PostgreSQL بازنشسته است: گارد یکپارچه با
پیام روشن می‌ایستد و هیچ `INSERT`/`CREATE TABLE` روی PG ساخته نمی‌شود. مسیر
**خواندن** (`/api/import/analyze`) اصلاح شد تا با جانشین‌های backend-aware
هسته (`schema_columns` / `present_tables`) کار کند — یعنی روی PG فهرست جدول‌ها و
ستون‌ها را می‌بیند، ولی برای ورود فایل، همان گارد را می‌گیرد.

**دلیل:** معیار فاز E.4.4 «رفتار امروز عیناً» بود؛ ترجمۀ `INSERT OR REPLACE` به
`ON CONFLICT DO UPDATE` معنا را عوض می‌کند: `INSERT OR REPLACE` ستون‌هایی که در
`VALUES` نیامده‌اند را به **پیش‌فرض schema** برمی‌گرداند، ولی `DO UPDATE` مقدار
کهنهٔ همان ستون‌ها را **نگه می‌دارد**. این واگرایی خاموش است — هیچ خطایی نمی‌دهد و
فقط داده فرق می‌کند. ضمناً همان مسیر، `skip` را با `_exists_by_cols` می‌شکست
(TD-009) و `_columns`/`_tables` را با `PRAGMA` (TD-008).

**جایگزین ردشده:** «`(col, val) IS NOT DISTINCT FROM` + `ON CONFLICT DO UPDATE` و
معادل‌سازی `replace`». رد شد چون برای درست‌شدن باید ستون‌های نگانه‌شده را صریح
`DEFAULT` بگذاریم ⇒ ترجمۀ معنادار یعنی **بازطراحی قرارداد import**، که تصمیم دامنه‌ای
است و در اختیار این فاز نیست. جای درستش ابزار مهاجرت دادهٔ فاز F
(`tools/migrate_sqlite_to_pg.py`؛ هنوز وجود ندارد) است، جایی که منبع و مقصد هم‌زمان
دیده می‌شوند.

**اثر:** روی SQLite هیچ رفتاری عوض نشد (تست‌های `test_import_*` همان‌ها سبزند).
روی PG، `analyze` پاسخ می‌دهد و `confirm` با پیام گارد. پیام گارد در
`tests/test_import_db_guard.py` با `GUARD_MARKERS` قفل شده (تغییر متن پیام، تست را
می‌شکند — عمداً؛ تا سند و کد از هم دور نیفتند). TD-007/TD-008/TD-009 به «تصمیم‌گرفته‌شده
— موکول به فاز F» تغییر وضعیت دادند.

---

## 6.2 — Backup: `pg_dump --format=custom` + verification, instead of a file copy

**تصمیم:** در `tools/migrate_timestamps_to_utc.py`: روی PG بکاپ =
`pg_dump --format=custom` با تأیید `pg_restore --list` (ردیف‌های TOC شمرده
می‌شوند؛ جدولِ موردانتظار نبود ⇒ خطا) و روی SQLite همان کپی فیزیکی
`logs.db-<stamp>.e3bak`. پرچم‌های تازه: `--pg-dsn`، `--backup-out`،
`--backup-only` (فقط بکاپ، بدون اسکن و بدون نوشتن)، `--skip-pg-backup`.
sha256 بکاپ PG در manifest می‌نشیند (`pg_backup` با `toc_entries`/`tables`).

**دلیل:** یافتهٔ E.4.11 (بند ۷.۱ سند strictness): «بکاپ» روی PG می‌توانست بی‌صدا
**هیچ** باشد — `shutil.copy2(DB_PATH, …)` وقتی `DB_PATH` فایل نبود رشتهٔ خالی
برمی‌گرداند و برنامه همان را «بکاپ گرفته شد» گزارش می‌کرد. `pg_dump -Fc` تنها
قالبی است که **بدون اتصال به سرور** قابل بازرسی است (`--list`)، پس «بکاپ» یک
شاهدِ راستی‌آزمایی‌شدنی می‌شود نه یک عدد حجم فایل.

**جایگزین ردشده:** «کپی فایل + اگر PG بود پیام بده خودت `pg_dump` بگیر». رد شد چون
دو مسیر را از هم وا می‌کند: ابزار باید همان چیزی را تضمین کند که ادعا می‌کند؛ و
«پیام بده» در عمل یعنی اپراتور فراموش می‌کند. همچنین «dumpِ ناموفق ⇒ ادامه با
هشدار» رد شد: بکاپِ ناموفق باید اجرا را **بایساند**، نه اینکه با یک warning رد شود.

**اثر (سه نقص واقعی که همین‌جا بیرون افتاد و رفع شد):**
۱) پوشهٔ پیش‌فرض بکاپ `os.getcwd()` بود ⇒ سوئیت PG در ریشهٔ مخزن **۲۶ فایل dump**
ریخت؛ پیش‌فرض به «کنار فایل دیتابیس» تغییر کرد و `.gitignore` هم
`printguard-pg-*.dump` گرفت.
۲) `pg_dump -Fc` فایل را **پیش از** موفقیت می‌سازد ⇒ یک اجرای نیمه‌کاره، فایلی
۰ بایتی/ناقص را با نام «بکاپ» جا می‌گذاشت؛ حالا در هر دو مسیر خطا (pg_dump و
verify) فایل بی‌اعتبار حذف می‌شود.
۳) `tests/test_timestamp_migration.py` بکاپ PG را از **محیط** می‌فهمید ⇒ هر اجرای
سوئیت PG، دیتابیس زنده را dump می‌کرد؛ حالا `DB_BACKEND`/`PG_DSN` در هلپر `run_tool`
قفل است.
رمز libpq به `PGPASSWORD` منتقل و از DSN لاگ‌شدن حذف می‌شود ⇒ نه در `ps`، نه در
manifest (تست: `test_pg_dump_is_called_without_password_on_the_command_line`).

---

## 6.3 — Collation: `C` stays

**تصمیم:** collation دیتابیس/ستون‌ها روی PG **`C`** می‌ماند. ترتیب فارسیِ نمایشی
همان‌طور که امروز در UI انجام می‌شود (`localeCompare` در `web/static/js/dashboard.js`)
باقی می‌ماند. اگر روزی ترتیب الفبای فارسی در SQL لازم شد، **per-query** با
`ORDER BY col COLLATE "fa-IR-x-icu"` (یا ایندکس هم‌collation) انجام می‌شود — بدون
دست‌زدن به schema. ثبت‌شده به‌عنوان **TD-013**.

**شواهد (۱۸ رشتهٔ واقعیِ فارسی/لاتین/رقمی، همین سرور):**
`C` در برابر `fa-IR-x-icu` ⇒ **۱۵ ردیف از ۱۸** جابه‌جا شدند.
زیر `C`، ترتیب = ترتیب کدپوینت = `sorted()` پایتون = `ORDER BY … COLLATE BINARY`
در SQLite (این سه، یکی‌اند؛ در `tests/test_collation_ordering.py` قفل شده).
زیر ICU: `۲ستاره` از آخر به دوم می‌آید، `admin office` **پیش از** `Admin Office`،
و `پارسه` پیش از `ثابت` (یعنی الفبای درست فارسی).

**دلیل (و یک نکتهٔ صادقانه دربارهٔ معیار):** معیار پیشنهادی مالک «اگر تفاوت ناچیز بود،
`C` بماند» بود؛ اندازه‌گیری نشان داد تفاوت **ناچیز نیست**. با این حال `C` می‌ماند،
ولی به یک دلیل دیگر: (الف) تکلیف فاز E «هم‌ارزی دو backend» است و SQLite در این build
هیچ‌گزینهٔ ICU ندارد (`PRAGMA compile_options` ⇒ هیچ) پس اگر PG را ICU می‌کردیم،
ترتیبِ یک کوئری در دو backend فرق می‌کرد — دقیقاً در پنجرۀ دو‌سویه و بازگشت؛
(ب) تنها `ORDER BY`های متنی برنامه روی `printer_ip`/`username` (ASCII) است — فهرست
چهار محل در تست قفل شده تا اگر روزی `ORDER BY` روی ستون فارسی اضافه شود، نویسنده
مجبور شود `COLLATE` را صریح انتخاب کند؛ (ج) `=` زیر ICU همچنان به‌بزرگی‌حساس است
(`'ADMIN OFFICE' = 'admin office'` ⇒ ۰ ردیف) ⇒ ICU پاسخ «NOCASE/case-fold» هم نیست.

**جایگزین ردشده:** `ALTER DATABASE … LOCALE PROVIDER icu` یا migration سراسری
`ALTER COLUMN … COLLATE "fa-IR-x-icu"`. رد شد: بازنویسی indexهای متنی
(`users_username_key`) و تغییر ترتیب همهٔ فهرست‌ها در روز cutover، بدون اینکه هیچ
کاربری امروز ترتیب فارسیِ SQL را انتظار داشته باشد.

**اثر:** صفر تغییر در schema/کد. ۷ تست: قفلِ «PG پیش‌فرض == SQLite BINARY»،
قفلِ `datcollate = C` (اگر initdb کسی عوض کرد، تست می‌گوید)، قفلِ واگراییِ ICU
(دو جابه‌جاییِ مشخص، تا تغییر تصمیم آگاهانه باشد نه تصادفی)، و زنگِ «اگر SQLite
روزی ICU داشت، این تصمیم را بازبینی کن».

---

## 6.4 — `logs.details` ⇒ `jsonb` on PG; the rest stay TEXT

**تصمیم:** تنها ستونی که SQL روی محتوای JSON آن می‌خواند (`logs.details`) روی PG
به `jsonb` می‌رود (migration `0004_jsonb_columns.py`؛ روی SQLite no-op). چهار ستون
ـ `users.allowed_offices`، `users.allowed_modules`،
`printer_counters.cartridge_id_state`، `printer_counters.toner_overrides` ـ و
`security_events.details` **TEXT** می‌مانند.

**دلیل (معیار + عدد):** معیار مالک `grep json_extract` بود و دقیقاً یک ستون داد:
`web/routes/metrics.py` (aggregate بدون کرون زمانی ⇒ روی همهٔ ردیف‌های
`CARTRIDGE_CHANGED`) و سه کوئری dedup در `core/database.py`. هلپر `json_extract`
روی PG `(details::jsonb)->>'key'` می‌سازد، یعنی **parse در هر ردیفِ خوانده‌شده**.
اندازه‌گیری (۲۰۰٬۰۰۰ ردیف): metrics **۱۱۹٫۳ms ⇒ ۴۶٫۸ms (۲٫۵×)**؛ dedup
**۱۹٫۳ ⇒ ۱۷٫۸ms** (ناچیز، چون روی چند ردیفِ یک پرینتر است)؛ افزودن expression index
روی ستون متنی **کمکی نکرد** (۱۱۶ms؛ پلن همان seq scan) ⇒ «ایندکس بزن و رها کن»
گزینهٔ واقعی نبود؛ نوشتن ۲۰٬۰۰۰ ردیف: ۷۹ms ⇒ ۳۶ms؛ حجم جدول: هر دو ۴۰MB.
سود دوم: خطای JSON بدفرمت از **خواندن** به **نوشتن** منتقل می‌شود (امروز یک ردیف
خراب، کوئری aggregate کسی را که ربطی به آن ردیف ندارد با `22P02` می‌سوزاند).

**جایگزین‌های ردشده:**
۱) «هر پنج ستون ⇒ jsonb» (متن اولیهٔ تکلیف). رد شد، چون معیارِ خودِ تکلیف آن‌ها را
تأیید نمی‌کند: هیچ SQL روی آن‌ها JSON نمی‌خواند ⇒ سود صفر، ریسک migration و
واگرایی با SQLite (از جمله `users.allowed_offices NOT NULL DEFAULT '[]'` که روی
`jsonb` باید `'''[]'''::jsonb` شود) — «یکدستیِ بی‌دلیل». دامنه در
`tests/test_jsonb_columns.py::MigrationScopeTests` قفل شده (`JSONB_COLUMNS ==
(("logs","details"),)`).
۲) «TEXT بماند و یک TD بنویس». رد شد با عدد بالا (۲٫۵× روی کوئریِ هر-scrape) و
با این نکته که هزینهٔ تغییر **پیش از** cutover حداقل است: داده‌ای در PG نیست
(`public.logs` = ۰ ردیف) و migration تازه روی schema خالی هیچ rewrite ندارد.
۳) «NULL کردن ردیف‌های نامعتبر برای اینکه migration رد نشود». **تصمیم مالک: توقف با
شمارش.** preflight با یک تابع `pg_temp` (PG 16 هیچ `IS JSON` و هیچ try-cast ندارد)
می‌شمارد و پیامش idهای نمونه + کوئری یافتن را می‌دهد؛ رشتهٔ خالی (که به NULL
تبدیل می‌شود و تغییر داده است) فقط **هشدار با شمارش** می‌گیرد، نه سکوت.

**اثر:** کد دامنه دست‌نخورده — `json_extract()` همان cast را می‌سازد که روی ستون
`jsonb` بی‌هزینه و بی‌تغییر معناست (سنجیده شد: هر دو نوشتار یک مقدار می‌دهند)، پس
**هیچ کوئری بازنویسی نشد**. تنها `core/database.py::_row_to_dict` یاد گرفت مقدار
دیکدشده (dict) را هم بپذیرد؛ مسیر SQLite و معنای «JSON بدفرمت ⇒ خطا» عمداً عوض
نشد (تست مستقل دارد) و اولویت E.3 («ستون‌های جدول بر کلیدهای payload برنده‌اند»)
روی jsonb هم تست شد. سه ادعای کهنه در `tests/test_strictness_pg.py` به‌روز شد —
نه تضعیف: سرِ migration حالا از فایل‌ها محاسبه می‌شود
(`tests/migration_graph.py`؛ آن `0003` ثابت یک دامِ کهنه‌شدن بود)، نوع `details`
به `jsonb` تصحیح شد، و تست «JSON نامعتبر» حالا **رد در زمان نوشتن** و نبودِ ردیف
باقی‌مانده را می‌سنجد.

---

## 6.5 — TD-001: removal, not revival

**تصمیم:** `_save_counters_to_db` (۵۳ خط، بدون هیچ فراخوان) حذف شد؛ **نه** احیا.

**دلیل:** فهرست ستون‌هایش ناقص بود (چهار ستون `a3_total`/`a4_total`/
`cartridge_id_state`/`toner_overrides` را نمی‌نوشت) ⇒ اگر روزی احیا می‌شد،
`INSERT OR REPLACE` همان ستون‌ها را به پیش‌فرض می‌برد و داده را **بی‌صدا** پاک
می‌کرد. مسیر زندهٔ نوشتن شمارنده‌ها از `core/db/upsert.py` می‌گذرد و آزمون‌هایش را
دارد.

**جایگزین ردشده:** «احیا با فهرست کامل ستون‌ها» و «بگذار بقا، علامت deprecated
بزن». اولی مسیر دوگانهٔ نوشتن می‌سازد (دو منبع حقیقت برای یک جدول) و دومی کد مردهٔ
قابل‌تماس نگه می‌دارد.

**اثر:** −۵۳ خط؛ `import json` بی‌مصرفش هم رفت؛ قفل بازنگشت در
`tests/test_upsert_cross_backend.py`؛ `pyflakes core web` از ۳۷ به ۳۶ رسید.

---

## 6.6 — TD-002: allowlist for `next`, and closing the `\` gap

**تصمیم:** در پاسخ‌های ۴۰۱/۳۰۲ گارد `before_request`، `next` دیگر `request.url`
کامل نیست؛ از `request.path` + `query_string` **خام** ساخته می‌شود و اگر
`_is_safe_next_url` نپذیرد، پارامتر کلاً فرستاده نمی‌شود. ضمناً `_is_safe_next_url`
هر مقداری که `\` داشته باشد را رد می‌کند.

**دلیل:** نگه‌داشتن query string بخشی از UX است (`/logs?page=3` برگردانده می‌شود)؛
«حذف کامل query» جایزۀ یک مسئلهٔ امنیتی بود که مسئله نبود: مقدار `next` بازتابِ
ورودی خودِ کاربر است، نه دادهٔ دیگری. **اما** در همین گام یک حفرۀ واقعی پیدا شد:
`/\evil.example.com` پذیرفته می‌شد — و مرورگرها در URLهای special-scheme `\` را
معادل `/` می‌خوانند (WHATWG URL)، پس `Location: /\evil.example.com` عملاً
خارج از سایت است. guard تا پیش از این **صفر تست** داشت.

**جایگزین ردشده:** «`next` را کلاً حذف کن» (پیشنهاد اولیهٔ TD) و «`urlsplit` +
بررسی host». اولی کار را خراب می‌کرد؛ دومی در برابر `\` و `//host` همان‌قدر
 شکننده است و منطق قابل‌تستِ خالص نمی‌دهد. اکنون `same_site_next_target()`
(خواندن request) و `_same_site_next_target(path, raw_query)` (تابع خالص) از هم
جداوند؛ دلیل جداسازی: Werkzeug `request.path` را **نرمال** می‌کند، پس آزمونِ
`//evil.example.com/x` روی لایۀ request بی‌معناست و باید روی تابع خالص قفل شود.

**اثر:** `web/auth.py` + `web/__init__.py` (دو نقطۀ ساخت پاسخ)؛ ۱۱ تست در
`tests/test_auth_next_redirect.py` شامل جدول ACCEPTED/REJECTED و سه حالت بازگشت
پس از `POST /login` واقعی. `pyflakes` همان ۳۶ باقی ماند.

---

## 6.7 — TD-004: binding `log` in the error path of `/api/security/stats`

**تصمیم:** `import logging` + `log = logging.getLogger("PrinterMonitor")` در
`web/routes/security.py` (همان قرارداد ده فایل route دیگر).

**دلیل:** `except` انتهای `api_security_stats` روی `log.exception` می‌رفت، ولی
`log` در ماژول تعریف نشده بود ⇒ خودِ مسیر خطا `NameError` می‌داد: پاسخ HTML به‌جای
`500 {"error": …}` و **هیچ رکورد لاگی**. یعنی دقیقاً همان‌جا که اپراتور به شاهد
نیاز دارد، نه پاسخ درست داشتیم نه لاگ.

**جایگزین ردشده:** «`log` را در `try` محلی بساز» یا «`except` را ساده کن و بگذار
بالا برود». اولی هشت route دیگر را الگوبرداری نمی‌کند و دومی شاهد خطا را حذف می‌کند.

**اثر:** ۷ خط؛ `pyflakes core web` **۳۶ ⇒ ۳۵** (کاهش؛ ادعای «۳۶ finding» در
`docs/insert-or-replace-inventory.md` هم به‌روز شد). شدّتِ تست سنجیده شد: با حذف
خط بایندینگ، هر سه تست `tests/test_route_logging.py` قرمز می‌شوند و با بازگردانی
سبز — شامل نگهبان AST `LogBindingGuardTests.test_no_unbound_log_calls` که کل
`web/**.py` + `core/**.py` را می‌پایدد تا این اشتباه در فایل تازه تکرار نشود.

---

## 6.8 — TD-005 and TD-006: recorded and deferred (no code change)

**تصمیم:** هیچ‌کدام در E.6 رفع نشد؛ هر دو با **مقصد صریح** ثبت شدند:
TD-005 (manifest بازنویسی‌شدنی) ⇒ **فاز F** (کنار ابزار مهاجرت داده، جایی که
شاهدِ اجرای واقعی اهمیت عملی پیدا می‌کند)؛ TD-006 (ساعات سکوتِ `core/sensor_alert.py`
و `datetime.now().hour`) ⇒ **فاز I** (کنار بازبینی زمان‌محور UI/ساعت محلی، چون
پیش‌نیازش تأیید مالک است: «آیا سرور با TZ تهران اجرا می‌شود یا UTC؟»).

**دلیل:** TD-005 یک نقص *قابلیت حسابرسی* است نه داده؛ فعلاً بکاپ بایت‌به‌بایت +
هش‌های پیش/پس + دستور بازتولید (`--dry-run` روی بکاپ) شاهد را برگشت‌پذیر می‌کند —
و از E.6.2 یک field تازه (`pg_backup`) هم به manifest اضافه شد، پس «شاهدِ_PG»
دیگر وابسته به اجرای دوم نیست. TD-006 تصمیم کسب‌وکاری است: اگر `local_tz()` را
جای `now().hour` بگذاریم، رفتار هشدارهای همهٔ نصب‌ها عوض می‌شود و این در فازی که
هدفش «منطق دامنه دست‌نخورده» است، جا ندارد.

**جایگزین ردشده:** «TD-006 را همین حالا رفع کن چون سه خط است». رد شد چون تست مرزی
آن به تصمیم TZ سرور گره خورده است؛ بدون آن تصمیم، تست فقط رفتارِ انتخاب ما را قفل
می‌کرد و آزادِ تغییر را می‌گرفت.

**اثر:** صفر تغییر کد؛ دو ردیف جدول بدهی با «موکول به فاز …» و دلیل.

---

## 6.9 — TD-009 (`_exists_by_cols` ⇒ `col IS $1` on PG): not fixed, kept under a guard

**تصمیم:** رفع نشد؛ گارد یکپارچۀ ۶.۱ که کل مسیر نوشتن import روی PG را می‌بندد،
آن را هم بی‌مصرف می‌کند.

**دلیل:** `col IS $1` روی PG خطای نحو می‌دهد، ولی تنها از `mode=skip` قابل‌رسیدن
است و `skip` روی PG دیگر اجرا نمی‌شود. ترجمۀ یک‌خطی به `IS NOT DISTINCT FROM`
وسوسه‌انگیز است اما کدی را لمس می‌کرد که روی PG **هیچ‌وقت** اجرا نمی‌شود و روی
SQLite هم لازم نیست (`IS NULL` همان‌جا درست کار می‌کند) ⇒ ریسک بدون benefit.

**جایگزین ردشده:** «حذف حالت `skip` از UI/API». رد شد: `skip` روی SQLite حالت
پیش‌فرضِ پرمصرف است و حذفش، رفتار امروز را عوض می‌کرد.

**اثر:** صفر تغییر. در سند tech-debt، وضعیت TD-009 از «زیر گارد تا E.6» به
«زیر گارد بازنشستگی ۶.۱ — رفع در فاز F اگر import روی PG زنده شد» تغییر کرد.

---

## 6.10 — TD-012: official entry in the debt table

**تصمیم:** وضعیت ۵۲ تست SQLite-محور که زیر `DB_BACKEND=postgresql` می‌شکستند،
**مدیریت‌شده** ثبت شد (marker `sqlite_only` + auto-skip در `tests/conftest.py` از
E.5.2)، و ردیف TD-012 به **جدول** بدهی هم اضافه شد (تا then فقط بخش توضیحی داشت).

**دلیل:**_skip_ها خودِ راه‌حل‌اند، نه پنهان‌کاری: هر کدام یک خط marker در بالای فایل
دارند و شمارششان در سربرگ اجرای PG چاپ می‌شود. ادعای «پوشش PG» برای آن جریان‌ها
دیگر **نمی‌شود** — و همین، بدهی است؛ پس باید در فهرست زنده باشد، نه فقط در یک بند.

**جایگزین ردشده:** «تک‌تک فایل‌ها را برای PG بازنویسی کن» (هزینهٔ زیاد، ارزش کم —
خیلی‌شان ذاتاً نحو SQLite را می‌آزمایند) و «گزارش skip را بی‌صدا نگه دار»
(دقیقاً همان چیزی که `NoSilentSkipsTests` در `tests/test_strictness_pg.py`
می‌جوید).

**اثر:** صفر تغییر کد. شمارش راستی‌آزمایی‌شده در اجرای امروز (PG): **۷۱ skip**,
تفکیک بر حسب فایل: `test_golden` ۱۱ · `test_db_adapter` ۲۵ (چهار کلاس) ·
`test_ddl_bootstrap` ۵ · `test_poll_event_invariant` ۷ · `test_metrics_pg` ۳ ·
`test_db_migration_columns` ۳ · `test_smoke_routes` ۲ · `test_rbac_admin_guard` ۲ ·
`test_import_db_guard` ۱ … (بازتولید: `pytest tests/ -v` و شمردن سطرهای `SKIPPED`).
**هیچ‌یک از سه فایل تست E.6** (`test_backup_pg`، `test_jsonb_columns`،
`test_collation_ordering`) در اجرای PG skip نمی‌شود؛ skip آن‌ها فقط در نبودِ
`PG_DSN` و **با دلیل چاپ‌شده** است (`-rs`). تعداد skipهای PG در طول E.6 عوض نشد
(۷۱ ⇒ ۷۱) — یعنی هیچ پوششی بی‌صدا از دست نرفت.

---

## Final measures of phase E.6

| سنجه | عدد |
|---|---|
| SQLite (`TZ=UTC` و `TZ=Asia/Tehran`، `pytest tests/ -q`) | ۵۵۵ passed / ۰ failed / ۰ skipped · ۱۰۹ subtest |
| PostgreSQL (`DB_BACKEND=postgresql`) | ۴۸۴ passed / ۰ failed / ۷۱ skipped · ۱۰۷ subtest |
| golden `--check` | ۱۰/۱۰ «تفاضل صفر» |
| `pyflakes core web` | ۳۵ (پیش از E.6.۷: ۳۶ · پیش از E.6.۵: ۳۷) |
| `logs.db` | `e29d40630c45fc1d392c9a11d60b8de2` — بی‌تغییر |
| `public.logs` روی PG | ۰ ردیف (cutover نیفتاده) |
| سرِ migration | `0004_jsonb_columns` |
| schema ایزولهٔ تست | هر اجرا `CREATE SCHEMA` + `alembic upgrade head`؛ بدون باقیمانده |

**بازگشت:** هر هفت گام E.6 کامیت جدا هستند؛ `git revert` هر کدام مستقل برگشت‌پذیر
است، به‌جز migration `0004` که با `alembic downgrade 0003` برمی‌گردد (متن بالا:
ردیف‌های `''` در حالت بازگشت NULL می‌مانند).
