# Consumers of time-series tables and the retention proposal

**دامنه:** سه جدول `toner_snapshots_v2`، `sensor_readings`، `toner_history`
**هدف:** پیش از افزودن هر مکانیزم retention، مشخص شود چه کدی از این جدول‌ها می‌خواند و چه بازه‌ای لازم دارد.
**وضعیت این سند:** فقط مستندات — هیچ کد، schema یا مهاجرتی تغییر نکرده است.
**مبنا:** `main` + شاخه‌ی `phase-c` (کارهای ۱–۴)، تاریخ بررسی: ۲۰۲۶-۱۰-۰۳.

---

## Executive summary (4 points)

۱. **هیچ‌کدام از سه جدول خواننده‌ی «درون‌برنامه‌ای» (web/API/داشبورد) ندارند** — به‌جز یک استثنا: `sensor_readings` که `/api/stats/sensor/daily` از آن می‌خواند (`web/routes/stats.py:77`). یعنی حذف/کوتاه‌کردن دو جدول دیگر، چارت‌های داشبورد را نمی‌شکند.
۲. **`toner_snapshots_v2` عملاً write-only است** (نویسنده: Yield Engine، `core/yield_engine.py:445`) و تنها مصرف‌کننده‌ی واقعی آن **ابزار ممیزی** است (`tools/audit_pages.py`). بنابراین retention این جدول باید بر اساس **نیاز ممیزی** تعیین شود، نه یادگیری — این با فرض اولیه‌ی درخواست («Yield Engine برای یادگیری مقاوم ۹۰ روز لازم دارد») متفاوت است؛ یادگیری از `cartridge_state`/`yield_samples` تغذیه می‌شود (`core/yield_engine.py:367,388,556`، `:1226`).
۳. **ممیزی به granularity خام نیاز دارد، نه rollup:** تشخیص «شکاف پایش» آستانه‌ی ۹۰۰ ثانیه دارد (`tools/audit_pages.py:43,105-117`) و «لنگر شکاف‌آگاه» یک ردیف **قبل از** شروع پنجره می‌خواهد (`tools/audit_pages.py:101`). rollup ساعتی هر دو را از بین می‌برد ⇒ **rollup جایگزین raw نیست** و فقط برای نمودارهای بلندمدت است.
۴. **دو مسیر retention موجود هر دو مرده‌اند:** `prune_old_print_logs()` (`core/database.py:962`) و `cleanup_old_events()` (`core/security_audit.py:269`) هیچ **call site**ی ندارند (`prune_old_print_logs` فقط در `run.py:31` import شده و هرگز صدا زده نمی‌شود). هیچ scheduler دوره‌ای برای pruning وجود ندارد.

---

## Table 1 — `toner_snapshots_v2`

**نویسنده‌ها (write path):**

| نقش | فایل:خط | یادداشت |
|---|---|---|
| ساخت جدول | `core/yield_engine.py:293` | ستون‌ها: `printer_ip, color, printer_model, cartridge_name, cartridge_key, timestamp, print_total, full_color, black_white, usage_counter, toner_level, raw_level, source, valid, reject_reason` |
| ایندکس | `core/yield_engine.py:345` | `(printer_ip, color, timestamp)` — برای کوئری‌های بازه‌ای ابزار ممیزی کافی است |
| تابع نوشتن | `core/yield_engine.py:439` → `INSERT` در `core/yield_engine.py:445` | `_record_snapshot()` |
| ۹ نقطه‌ی فراخوانی | `core/yield_engine.py:791, 856, 885, 907, 953, 962, 988, 999, 1084` | همه داخل `process_cartridge_snapshot` (`:723`) — مسیرهای پذیرش/رد نمونه |
| مسیر دوره‌ای | `core/yield_engine.py:1093` → `:1110` → فراخوان از `core/enhanced_collector.py:1589-1590` | در هر پولینگ برای هر رنگ اجرا می‌شود |

**مصرف‌کننده‌ها (read path):**

| مصرف‌کننده | فایل:خط | بازه‌ی موردنیاز | یادداشت |
|---|---|---|---|
| ابزار ممیزی لاگ/شمارنده | `tools/audit_pages.py:80-88` | پنجره‌ی کاربر: `--days`/`--since`/`--until`؛ پیش‌فرض = **کل بازه** (`:441` + `_window_args` در `:55-70`) | SELECT ردیف‌های `color='black'` با `print_total` غیرNULL برای محاسبه‌ی دلتا |
| ابزار ممیزی — «لنگر» قبل از پنجره | `tools/audit_pages.py:101` | **یک ردیف قدیمی‌تر از t0** (هر چقدر عقب‌تر) | دلیل مستند در `:91-98` (مورد واقعی خاموشی ۸۹ ساعته) |
| ابزار ممیزی — جدیدترین timestamp | `tools/audit_pages.py:440` | بی‌نهایت (فقط `MAX`) | مبنای محاسبه‌ی `--days` |
| ابزار ممیزی — فهرست دستگاه‌ها | `tools/audit_pages.py:447` | بی‌نهایت (`DISTINCT`) | دستگاه‌هایی که فقط در بازه‌ی حذف‌شده داده دارند، از ممیزی می‌افتند |
| ابزار ممیزی — probe «سرور روشن بوده؟» | `tools/audit_pages.py:455` | همان پنجره (`_SERVER_PROBE_MIN_HITS=20` در `:250`) | حداقل ۲۰ ردیف در پنجره لازم است |
| تسک هفتگی ویندوز | `tools/install-weekly-audit.bat:33` | **۷ روز** (هر شنبه ۰۸:۰۰) | `audit_pages.py --days 7 --md audit-weekly.md` |
| مسیر import/export دیتابیس | `web/routes/import_db.py:66-73` و `:82` (`YIELD_TABLES`) | کل جدول (dedupe بر `printer_ip,color,timestamp,usage_counter,toner_level`) | Import دستی می‌تواند تاریخچه را برگرداند ⇒ retention قابل جبران با backup |
| رابط کاربری import | `web/static/js/dashboard.js:3592,3597` | کل جدول | انتخاب جدول‌های قابل import |
| آزمون‌ها (فقط زیرساخت تست) | `tests/golden_harness.py:44,52` | — | `db_state_after` در fixtureها |

**بازه‌ی موردنیاز نهایی:** ≥ **۹۰ روز** برای ابزار ممیزی (۷ روز خودکار + امکان ممیزی دستی چندماهه)؛ «لنگر» فقط به **یک** ردیف قدیمی نیاز دارد، پس با حذف داده‌های قدیمی، ممیزی پنجره‌های ابتدای تاریخ از دست می‌رود ولی نمی‌شکند.

---

## Table 2 — `sensor_readings`

**نویسنده‌ها:**

| نقش | فایل:خط | یادداشت |
|---|---|---|
| ساخت جدول + ایندکس | `core/database.py:305`، `:317-319` | ایندکس‌های `(printer_ip, timestamp)` و `(kind)` |
| تابع نوشتن | `core/database.py:1127` → `INSERT` در `core/database.py:1158` | `record_sensor_readings()` |
| فراخوان | `core/collectors/sensor.py:249` | در هر پولینگ، برای هر پورت/نوع (`SENSOR_PORT_COUNT` پیش‌فرض ۲ در `core/collectors/sensor.py:32`) |
| حجم تخمینی | — | ۴ ردیف در هر پولینگ برای هر سنسور ⇒ ≈ **۵٬۷۶۰ ردیف/روز** برای هر سنسور |

**مصرف‌کننده‌ها:**

| مصرف‌کننده | فایل:خط | بازه‌ی موردنیاز | یادداشت |
|---|---|---|---|
| API میانگین روزانه‌ی سنسور | `web/routes/stats.py` (`api_sensor_daily_stats`) | `days` پارامتر: پیش‌فرض **۳۰**، سقف **۳۶۵** | از E.3.4: `SELECT timestamp, kind, value` + میانگین‌گیری روزانه در پایتون (`to_local_day`، روزِ Asia/Tehran) |
| رابط کاربری داشبورد | `web/static/js/dashboard.js:3175` | **۳۰ روز** (`days=30`) | تنها مصرف‌کننده‌ی واقعی این جدول در UI |
| fallback زنده (بدون خواندن جدول) | `web/routes/stats.py:94-105` | — | اگر جدول داده نداشت، از snapshot حافظه (`store.printer_data`) استفاده می‌کند |
| مسیر import/export دیتابیس | `web/routes/import_db.py:45-52` | کل جدول | dedupe بر `printer_ip,timestamp,port,kind` |
| رویدادهای SENSOR_CHANGE (مسیر جدا) | `core/collectors/sensor.py:133-172` → `add_event` | از جدول `logs` می‌خواند، نه این جدول | «تغییر معنی‌دار» ≥۱°C یا ≥۵٪ (`:48-52`) |

**بازه‌ی موردنیاز نهایی:** UI **۳۰ روز** می‌خواهد ولی API تا **۳۶۵ روز** را می‌پذیرد ⇒ اگر raw کمتر از ۳۶۵ روز شود، درخواست‌های `days>raw` **بی‌صدا کوتاه** می‌شوند (نه خطا). یا raw ≥ ۳۶۵ روز بماند، یا API به rollup روزانه سوییچ کند (پیش‌نیاز در بخش پیشنهاد).

---

## Table 3 — `toner_history`

**نویسنده‌ها:**

| نقش | فایل:خط | یادداشت |
|---|---|---|
| ساخت جدول + ایندکس | `core/database.py:292`، `:302` | `(printer_ip, timestamp)` |
| تابع نوشتن | `core/database.py:1167` → `INSERT` در `core/database.py:1177` | `record_toner_snapshot()` |
| فراخوان | `core/store.py:140` داخل `PrevStore.set` | **throttle:** فقط هنگام تغییر `print_total`/`toner_level` یا هر **۱۸۰۰ ثانیه** (`core/store.py:123-138`) ⇒ حداکثر ۴۸ ردیف/روز/دستگاه در حالت idle |

**مصرف‌کننده‌ها:**

| مصرف‌کننده | فایل:خط | بازه‌ی موردنیاز | یادداشت |
|---|---|---|---|
| تخمین yield تاریخی (legacy) | `core/database.py:1193` (`estimate_yield_from_history`، پارامتر `days=7`)، کوئری در `core/database.py:1201` | **۷ روز** | `min_points=3`, `min_pages=500` |
| بوت‌استرپ yield در اولین poll | `core/collectors/base.py:79-85` (`_bootstrap_yield_from_history`) | **۷ روز** | فقط وقتی `yield_per_page` هنوز مقدار پیش‌فرض باشد (`:81-82`) |
| مسیر import/export دیتابیس | `web/routes/import_db.py:38-44` | کل جدول | برچسب خودِ کد: «تاریخچه تونر legacy» |
| مستندات (نه کد) | `README.md:316`، `README.en.md:314`، `docs/architecture.html:401,711` | — | به‌عنوان جدول قابل import معرفی شده است |

**بازه‌ی موردنیاز نهایی:** فقط **۷ روز** و تنها در مسیر بوت‌استرپ. Yield Engine مدرن مستقل از این مسیر است (کامنت صریح در `core/yield_engine.py:14`). بنابراین «حذف» امکان‌پذیر است، اما **مشروط** به حذف/جایگزینی `_bootstrap_yield_from_history` — وگرنه اولین poll دستگاه‌های جدید بی‌صدا بوت‌استرپ نمی‌شوند (رجوع به بخش پیش‌نیازها).

---

## Retention proposal (not executed)

| جدول | raw | rollup ساعتی | rollup روزانه | پیش‌نیاز / ریسک |
|---|---|---|---|---|
| `toner_snapshots_v2` | **۹۰ روز** | ۱ سال | ۳ سال | rollup ساعتی/روزانه **جایگزین ممیزی نیست** (نیاز به granularity خام: `tools/audit_pages.py:43,105-117` و لنگر `:101`) — rollup فقط برای تحلیل/نمودار بلندمدت |
| `sensor_readings` | **۹۰ روز** (به‌جای ۳۰) | ۱ سال | ۵ سال | برای حفظ قرارداد API تا ۳۶۵ روز (`web/routes/stats.py:67`) یا raw ≥ ۳۶۵ روز لازم است یا سوییچ API به rollup روزانه — در غیر این صورت پاسخ‌های `days>90` بی‌صدا کوتاه می‌شوند |
| `toner_history` | **۳۰ روز (موقت)** | — | — | حذف کامل فقط بعد از بازنشستگی `_bootstrap_yield_from_history` (`core/collectors/base.py:79-85`) و `estimate_yield_from_history` (`core/database.py:1193`) انجام شود |

**دلایل عددها (خلاصه):**
- `toner_snapshots_v2`: تسک هفتگی ۷ روز می‌خواهد (`tools/install-weekly-audit.bat:33`)، ولی ممیزی دستی می‌تواند بازه‌ی چندماهه بخواهد و پیش‌فرض ابزار «کل بازه» است (`tools/audit_pages.py:441,55-70`) ⇒ ۹۰ روز سازش معقول بین حجم و قابلیت ممیزی.
- `sensor_readings`: تنها مصر‌ف‌کننده‌ی UI ۳۰ روز می‌خواهد (`web/static/js/dashboard.js:3175`) ولی سقف API ۳۶۵ است (`web/routes/stats.py:67`) ⇒ ۹۰ روز بدون rollup، درخواست‌های بلندتر را می‌شکند.
- `toner_history`: کل نیازش ۷ روز است (`core/database.py:1193`) و volume آن throttled است (`core/store.py:123-138`) ⇒ نگه‌داشتن ۳۰ روز هزینه‌ی ناچیزی دارد و فضای بازنشستگی تدریجی می‌دهد.

---

## Existing retention mechanisms (actual status)

| مکانیزم | فایل:خط | دامنه | وضعیت اجرا |
|---|---|---|---|
| `prune_old_print_logs(days=90)` | `core/database.py:962` (DELETE در `:967`) | فقط `logs.type='PRINT'` | **هرگز صدا زده نمی‌شود** — تنها ارجاع، import در `run.py:31` است و هیچ call siteی وجود ندارد |
| `cleanup_old_events(days=90)` | `core/security_audit.py:269` (DELETE در `:282`) | `security_events` | **هیچ call site** مشاهده نشد |
| حذف دستی کل لاگ‌ها | `core/database.py:929` (`DELETE FROM logs`) | `logs` | فقط از مسیر API/دستی |
| حلقه‌ی پولینگ | `core/poller.py:375` (`polling_loop`) | — | هیچ منطق retention ندارد |

**نتیجه:** هیچ مسیر خودکاری برای پاکسازی سه جدول سری‌زمانی (و حتی لاگ‌ها) در حال اجرا نیست؛ این توضیح‌دهنده‌ی رشد حجم دیتابیس در نصب واقعی است.

---

## Open questions (need a decision before implementation)

۱. **آیا rollup ها در پستگرس ماده‌ای (materialized view / جدول تجمیعی با job دوره‌ای) ساخته می‌شوند یا به‌صورت continuous aggregation؟** در معماری هدف (بدون Prometheus/Timescale) این انتخاب روی طراحی جدول‌ها اثر دارد.
۲. **برای `sensor_readings` آیا سقف ۳۶۵ روز API حفظ می‌شود؟** اگر بله، یا raw باید ۳۶۵ روز بماند یا endpoint باید به rollup روزانه سوییچ کند (کاری که در این فاز انجام نشد).
۳. **ابزار ممیزی روی پستگرس چطور اجرا می‌شود؟** فعلاً `tools/audit_pages.py` مستقیماً `sqlite3.connect` می‌زند (`tools/audit_pages.py:438`) و با `--days 7` هفتگی اجرا می‌شود؛ در مهاجرت باید به درایور پستگرس منتقل شود (وگرنه ممیزی از کار می‌افتد).
۴. **آیا `toner_history` در نصب واقعی داده‌ی تاریخی مهمی دارد؟** تصمیم «حذف» بهتر است بعد از شمارش ردیف‌ها و بررسی بازه‌ی پوشش گرفته شود (`SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM toner_history`).

---

## How to reproduce (grep commands used)

```bash
# ۱) همه‌ی ارجاع‌های هر جدول در کد پایتون
grep -rn "toner_snapshots_v2" --include="*.py" . | grep -v "^./.venv"
grep -rn "sensor_readings"    --include="*.py" . | grep -v "^./.venv"
grep -rn "toner_history"      --include="*.py" . | grep -v "^./.venv"

# ۲) فقط عملیات داده‌ای (خواندن/نوشتن) روی سه جدول
grep -rnE "(SELECT|INSERT|DELETE|FROM|UPDATE)[^\n]*(toner_snapshots_v2|sensor_readings|toner_history)" \
  --include="*.py" . | grep -v "^./.venv" | grep -v "^./tests/"

# ۳) توابع نوشتن/خواندن و همه‌ی فراخوان‌ها
grep -rn "record_toner_snapshot\|record_sensor_readings\|estimate_yield_from_history" \
  --include="*.py" . | grep -v "^./.venv"

# ۴) مکانیزم‌های retention
grep -rn "def prune\|def cleanup\|DELETE FROM" core/*.py
grep -rn "prune_old_print_logs(\|cleanup_old_events(" --include="*.py" . | grep -v "^./.venv"   # ⇒ هیچ call site

# ۵) مصرف‌کننده‌های غیرپایتونی (JS/HTML/MD) و زمان‌بندی ممیزی
grep -rn "toner_snapshots_v2\|sensor_readings\|toner_history" --include="*.js" --include="*.html" --include="*.md" .
grep -rn "audit_pages.py" --include="*.bat" --include="*.md" .
```

---

*این سند هیچ تغییری در کد، schema یا داده ایجاد نمی‌کند و صرفاً مبنای تصمیم برای فاز retention است.*
