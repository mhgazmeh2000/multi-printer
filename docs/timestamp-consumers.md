# Consumers of timestamps and the unification decision — phase E.0, task E.0.0

**وضعیت:** فهرستِ شناسایی (E.0.0) — **اجرا شد در فاز E.3**؛ نگاشتِ هر گروهِ
مصرف‌کننده به تغییرِ انجام‌شده در **بخش ۷** همین سند (و جزئیات کامل در
`docs/timezone-contract.md` §۹).
**ابزار تولید فهرست:** `tools/find_timestamp_consumers.py` (فقط منبع کد را می‌خواند؛ به هیچ DBی وصل نمی‌شود).
**مبنا:** شاخه‌ی `phase-c` پس از کار D.6 (کامیت `2cee9d3`)، تاریخ بررسی: ۲۰۲۶-۱۰-۰۳.
**چرا این سند؟** پیش از مهاجرت به PostgreSQL باید بدانیم در ستون `logs.timestamp` و فیلد `details["timestamp"]` چه فرمت‌هایی وجود دارد، چه کدی آن‌ها را می‌خواند، و هر مصرف‌کننده با هر فرمت چه می‌کند. تصمیم یکسان‌سازی writers هم در بخش ۶ همین سند ثبت شده است.

---

## Executive summary (5 points)

۱. **پیش‌فرض نویسنده‌ها naive local است:** در کد برنامه (core+web) **۲۷ فراخوانی واقعی `add_event`** وجود دارد؛ تنها **۱** مورد timestamp صریح می‌فرستد (مسیر موتور کارتریج، `core/collectors/base.py:800`) و ۲۶ مورد دیگر مقدار پیش‌فرض `datetime.now().isoformat()` می‌گیرند (`core/database.py:658`). در تست‌ها هم ۱۸ فراخوانی دیگر همین پیش‌فرض را دارند.)

۲. **`logs.timestamp` پس از D.6 مختلط است — حتی درون یک نوع رویداد:** رویداد `CARTRIDGE_CHANGED` که مسیر موتور عمومی می‌سازد **UTC با `Z`** است، ولی چهار پیلود قدیمیِ همان نوع رویداد (chip_id قدیمی، reset شمارش، تغییر نام supply، پرش تونر) کلید `timestamp` ندارند ⇒ **naive local**. یعنی تفکیک «UTC = کارتریج، naive = بقیه» نادرست است.

۳. **اثبات تجربی اختلاف ۲۱۰ دقیقه:** برای «همان لحظه»، رشته‌ی UTC `2026-10-03T07:09:06Z` و رشته‌ی naive محلی `2026-10-03T10:39:06Z` تولید می‌شود (`TZ=Asia/Tehran`). مرتب‌سازی رشته‌ای SQL ردیف UTC را ۳ ساعت و ۳۰ دقیقه «قدیمی‌تر» می‌بیند ⇒ **ترتیب رویدادها در UI برای ردیف‌های مسیر موتور غلط است**.

۴. **همه‌ی خواننده‌ها به naive local گره خورده‌اند:** ۵ مقایسه‌ی debounce + ۲ فیلتر بازه + ۱ prune (cutoff همه با `datetime.now()`)، ۳ گروه‌بندی روزانه با تاریخ محلی، ۲ مرتب‌سازی، و ۲ نقطه‌ی نمایش UI که timestamp را **رشته‌ای** (بدون تبدیل منطقه‌زمانی) نشان می‌دهند.

۵. **تصمیم:** در فاز E.0 writers یکسان **نمی‌شوند** (گام E.0.14b اجرا نمی‌شود). مهاجرت، هر فرمت را با قاعده‌ی صریح تفسیر می‌کند و تفکیکِ داده‌های `Z` با **نشانه‌ی cutover** (گام E.0.13) انجام می‌شود. دلایل و پیش‌شرط‌های یکسان‌سازی آینده در بخش ۶.

---

## 1. Writers (who writes what, and in which format)

### ۱.۱ مسیر عمومی `logs`

| مسیر | فایل:خط | فرمت نوشته‌شده | یادداشت |
|---|---|---|---|
| تابع `add_event` (تعریف) | `core/database.py:655` | — | مقدار پیش‌فرض را همین‌جا می‌سازد |
| مقدار پیش‌فرض timestamp | `core/database.py:658` | **naive local** (`details.get("timestamp", datetime.now().isoformat())`) | ۲۶ از ۲۷ فراخوانی کد برنامه از همین مسیر می‌گذرند |
| `INSERT INTO logs` | `core/database.py:790` | مطابق ورودی | تنها نقطه‌ی درج در این جدول |
| موتور کارتریج (`CARTRIDGE_CHANGED`) | `core/collectors/base.py:794` → `:800` | **UTC با `Z`** (پس از D.6) | `event_payload["timestamp"] = event["timestamp"]` |
| پیلود قدیمی chip_id | `core/collectors/base.py:841` | **naive local** | کلید `timestamp` ندارد |
| پیلود `supply_pages_reset` | `core/collectors/base.py:865` | **naive local** | کلید `timestamp` ندارد |
| پیلود `supply_name_change` | `core/collectors/base.py:923` | **naive local** | کلید `timestamp` ندارد |
| پیلود `toner_jump` | `core/collectors/base.py:1029` | **naive local** | کلید `timestamp` ندارد |
| رویداد دستی (SERVICE/REFILL) | `web/routes/logs.py:97` | naive local | endpoint `/api/events/manual` |
| تنظیم دستی تونر (REFILL) | `web/routes/printers.py:637` | naive local | `manual_reset` |
| BRAND_CORRECTED | `core/enhanced_collector.py:988` | naive local | |
| STATUS (آفلاین‌شدن) | `core/poller.py:246, 256` | naive local | |
| PRINT / PRINT_GAP / PRINT_OVERFLOW | `core/collectors/base.py:1239, 1106, 1142` | naive local | رویدادهای صفحات |
| COUNTER_RESET / COUNTER_RESTORED / COUNTER_ANOMALY | `core/collectors/base.py:519, 593, 631, 551` | naive local | |
| REFILL / WARNING / ALERT | `core/collectors/base.py:954, 978, 420` | naive local | |
| SNMP_COUNTER_READ_ERROR / SNMP_ERROR | `core/collectors/base.py:379`، `hp.py:193`، `canon.py:293`، `brother.py:156`، `toshiba.py:355` | naive local | |
| SENSOR_CHANGE | `core/collectors/sensor.py:152` | naive local | |

### ۱.۲ جداول دیگر (خارج از دامنه‌ی `logs` ولی با همان الگو)

| جدول | فایل:خط | فرمت |
|---|---|---|
| `toner_history` | `core/database.py:1176` (`record_toner_snapshot`) | naive local |
| `sensor_readings` | `core/database.py:1128` (`record_sensor_readings`) | naive local |
| `printer_counters.updated_at` | `core/database.py:1073` | naive local |
| `security_events` | `core/security_audit.py:84` → درج در `:113` (`log_security_event`) | naive local |

### ۱.۳ نکته‌ی جانبی مهم (کد مرده)
خط `core/cartridge_change_engine.py:306` (داخل بلوک غیرقابل‌دسترس ۲۹۵–۳۲۸) به‌صورت `datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"` نوشته شده که خروجی آن `...T07:09:06+00:00Z` است — یعنی **offset و `Z` هم‌زمان**؛ یک فرمت سومِ ناسازگار با مقایسه‌ی رشته‌ای. چون کد مرده است اثری ندارد، ولی اگر روزی احیا شود باید به فرمت متعارف (`strftime` یا `isoformat().replace("+00:00", "Z")`) برگردد. پیشنهاد فاز E: یک هِلپر مشترک `utc_now_iso()` که تنها تولیدکننده‌ی timestamp باشد.

---

## 2. Critical finding: `CARTRIDGE_CHANGED` is mixed

`details.detection` می‌تواند تا حدی تفکیک کند، ولی **کافی نیست**:

| مقدار `details.detection` | مسیر | فرمت timestamp |
|---|---|---|
| `chip_id` (موتور) | `base.py:800` | UTC با `Z` |
| `serial`, `supply_counter`, `page_counter`, `remaining_level`, `supply_life`, `generation`, `cartridge_model`, `supply_description` | `base.py:800` | UTC با `Z` |
| `chip_id` (پیلود قدیمی) | `base.py:841` | naive local |
| `supply_pages_reset` | `base.py:865` | naive local |
| `supply_name_change` | `base.py:923` | naive local |
| `toner_jump` | `base.py:1029` | naive local |

چون `chip_id` در **هر دو** دسته ظاهر می‌شود، از روی فیلد `detection` نمی‌توان فرمت را قطعی تعیین کرد. تنها معیار قطعی، **زمان** است (نشانه‌ی cutover در گام E.0.13).

---

## 3. Consumers (readers)

ستون «با Z» و «با naive»: آیا مصرف‌کننده مقدار را درست می‌فهمد؟

### ۳.۱ مقایسه با cutoff — حساس‌ترین گروه

| مصرف‌کننده | فایل:خط | نوع | با Z | با naive | راه‌حل فاز E |
|---|---|---|---|---|---|
| debounce بر اساس `code` | `core/database.py:697` (cutoff: `:692`) | `timestamp>=?` | ❌ اگر cutoff محلی باشد، پنجره ۳:۳۰ جابه‌جا می‌شود | ✅ | cutoff هم‌فرمت با داده |
| debounce بر اساس `message` | `core/database.py:706` | `timestamp>=?` | ❌ | ✅ | همان |
| dedupe رویدادهای PRINT | `core/database.py:735` | `timestamp>=?` | ❌ | ✅ (type='PRINT' فقط naive) | فیلتر type حفظ شود |
| dedupe PRINT (پنجره‌ی دیگر) | `core/database.py:751` | `timestamp>=?` | ❌ | ✅ | همان |
| dedupe COUNTER_RESET | `core/database.py:775` | `timestamp>=?` | ❌ | ✅ | همان |
| فیلتر بازه‌ی `/api/logs/all` (`start`) | `core/database.py:866` | `timestamp >= ?` | ⚠ رشته‌ی کاربر (تاریخ محلی) | ✅ | پارامتر UTC یا تبدیل در ورودی |
| فیلتر بازه (`end`) | `core/database.py:869` | `timestamp <= ?` | ⚠ | ✅ | همان |
| prune لاگ‌های PRINT | `core/database.py:961` (cutoff: `:958`) | `timestamp < ?` | — (فقط PRINT) | ✅ | حفظ هم‌فرمتی |
| تاریخچه‌ی تونر | `core/database.py:1196` | `timestamp>=?` | — (جدول دیگر) | ✅ | قاعده‌ی جدول خودش |

### ۳.۲ مرتب‌سازی

| مصرف‌کننده | فایل:خط | نوع | با Z | با naive | راه‌حل فاز E |
|---|---|---|---|---|---|
| `get_log` | `core/database.py:838` | `ORDER BY timestamp DESC` | ❌ ردیف UTC ۳:۳۰ «قدیمی‌تر» دیده می‌شود | ✅ | `ORDER BY` روی نوع زمان واقعی |
| `get_all_logs` | `core/database.py:879` | `ORDER BY timestamp DESC` | ❌ | ✅ | همان |
| تاریخچه‌ی reset | `core/database.py:1269` | `ORDER BY timestamp ASC` | ❌ اگر mixed | ✅ | همان |
| مرتب‌سازی در پایتون (reset history) | `core/database.py:1285, 1313` | `sort(key=timestamp)` | ❌ | ✅ | تجزیه به `datetime` |
| مرتب‌سازی در پایتون (export) | `web/routes/export_bp.py:211` | `sort(..., reverse=True)` | ❌ | ✅ | همان |
| مرتب‌سازی در UI | `web/static/js/dashboard.js:125` | `localeCompare` | ❌ | ✅ | تبدیل به `Date` |

### ۳.۳ گروه‌بندی روزانه

| مصرف‌کننده | فایل:خط | نوع | با Z | با naive | راه‌حل فاز E |
|---|---|---|---|---|---|
| `/api/stats/daily` (کل) | `web/routes/stats.py:26, 34` | `substr(timestamp,1,10)` | ❌ روز UTC به‌جای روز محلی | ✅ | `date_trunc('day', ts AT TIME ZONE 'Asia/Tehran')` |
| `/api/stats/sensor/daily` | `web/routes/stats.py:78` | همان | ❌ | ✅ | همان |
| مرز پنجره‌ی آماری | `web/routes/stats.py:16, 69` | `(datetime.now() - timedelta(days)).date().isoformat()` | ❌ (مبنا محلی است) | ✅ | مبنا: امروزِ محلی، صریح |

### ۳.۴ خواندن و انتقال به پایتون

| مصرف‌کننده | فایل:خط | نوع | با Z | با naive | راه‌حل فاز E |
|---|---|---|---|---|---|
| `get_log` → API لاگ | `core/database.py:837` → `web/routes/logs.py:10` | SELECT | ✅ (رشته منتقل می‌شود) | ✅ | — |
| `get_all_logs` → `/api/logs/all` | `core/database.py:875` → `web/routes/logs.py:19` | SELECT | ✅ | ✅ | — |
| `_row_to_dict` | `core/database.py:800-816` | تبدیل ردیف | ⚠ `**details` در خط ۸۱۵ می‌تواند `timestamp` ستون را **بازنویسی** کند | ✅ | ترتیب کلیدها صریح شود |
| تاریخچه‌ی reset (SQL) | `core/database.py:1264-1270` | SELECT | ⚠ | ✅ | — |
| تاریخچه‌ی reset (پایتون) | `core/database.py:1285, 1303, 1313` | sort/passthrough | ❌ ترتیب | ✅ | تجزیه به `datetime` |
| `/metrics` | `web/routes/metrics.py:122` | SELECT + `json_extract(details,'$.detection')` | ⚠ (برچسب، نه زمان) | ⚠ | بدون تغییر تا فاز E |

### ۳.۵ نمایش در UI

| مصرف‌کننده | فایل:خط | نوع | با Z | با naive | راه‌حل فاز E |
|---|---|---|---|---|---|
| جدول رویدادها (داشبورد) | `web/static/js/dashboard.js:1599` | `slice(0,19).replace('T',' ')` | ❌ **زمان UTC را به‌عنوان محلی نشان می‌دهد** (۳:۳۰ خطا) | ✅ | تبدیل با `new Date(...)` + منطقه‌ی محلی/تهران |
| مرتب‌سازی رویدادهای زنده | `web/static/js/dashboard.js:125` | `localeCompare` | ❌ | ✅ | مقایسه‌ی زمانی |
| جدول رویدادهای امنیتی | `web/templates/security.html:549` + `formatTimestamp` (`:486-493`) | `new Date(value)` + `Intl.DateTimeFormat` | ✅ (تبدیل درست) | ✅ (به‌عنوان محلی تفسیر می‌شود) | — |
| `export_bp` (CSV/Excel) | `web/routes/export_bp.py:215, 238` | `[:19].replace('T',' ')` | ❌ خروجی UTC بدون برچسب | ✅ | تبدیل + برچسب منطقه |

> **جمع‌بندی بخش ۳.۵:** تنها مصرف‌کننده‌ای که با هر دو فرمت درست کار می‌کند، `formatTimestamp` در `security.html` است (چون از `new Date` استفاده می‌کند). دو نقطه‌ی داشبورد و export، رشته را «بی‌تبدیل» نمایش می‌دهند ⇒ هر ردیف UTC، سه‌ونیم ساعت اشتباه نمایش داده می‌شود.

### ۳.۶ `details["timestamp"]`

| مصرف‌کننده | فایل:خط | نوع | با Z | با naive | راه‌حل فاز E |
|---|---|---|---|---|---|
| ساخت پیلود موتور | `core/collectors/base.py:794` | نوشتن در details | ✅ | ✅ | — |
| `event_id`/`last_seen_at` در حالت کارتریج | `core/collectors/base.py:802` و `:804` | ذخیره در state | ⚠ مقایسه با naive در جای دیگر | ⚠ | یکسان‌سازی در فاز E |
| `_row_to_dict` | `core/database.py:815` | `**details` | ⚠ بازنویسی `timestamp` | ✅ | — |
| مسیر جایگزین (بدون رویداد) | `core/collectors/base.py:804` | `datetime.now().isoformat()` | — | ✅ | — |

### ۳.۷ ابزارها (خارج از دامنه‌ی فاز E، فقط ثبت)

`tools/audit_pages.py:162, 170, 448`، `tools/cartridge_test_step2.py:77, 79, 95, 97`، `tools/cleanup_fake_gaps.py:44, 53, 125` — همه روی SQLite با `sqlite3.connect` مستقیم (فاز F).

---

## 4. Writer/reader coupling (why unifying only the writers is dangerous)

اگر فقط نویسنده‌ها به UTC بروند ولی خواننده‌ها دست‌نخورده بمانند:

| شکست | فایل:خط | چرا |
|---|---|---|
| پنجره‌ی debounce از کار می‌افتد | `core/database.py:692, 697, 706, 731, 735, 751, 767, 775` | cutoff با ساعت محلی ساخته می‌شود؛ رخداد UTC «پیرتر» دیده می‌شود ⇒ رویداد تکراری ثبت می‌شود |
| آستانه‌ی prune | `core/database.py:958, 961` | cutoff محلی در برابر داده‌ی UTC ⇒ پاک‌سازی زودهنگام/دیرهنگام |
| باکت روزانه | `web/routes/stats.py:16, 26, 34, 68, 78` | روز UTC با تاریخ محلی مقایسه می‌شود ⇒ نمودار یک روز جابه‌جا |
| ترتیب UI | `core/database.py:838, 879` + `dashboard.js:1599` | ترتیب و نمایش با هم ناسازگار |
| انقضای توکن | `web/auth.py:448, 486` و `web/routes/users.py:164, 207` | نوشتن و خواندن باید هم‌زمان مهاجرت کنند |
| golden | `tests/fixtures/golden/*` | timestamp تمام fixtureها عوض می‌شود ⇒ re-record لازم |

جمع‌بندی: یکسان‌سازی writers **بدون** خواننده‌ها، «بهبود» نیست؛ جابه‌جایی خطا از یک جا به جای دیگر است.

---

## 5. State of the real data (evidence)

`logs.db` مخزن (فقط با اتصال `mode=ro` خوانده شد؛ هیچ نوشتنی انجام نشد):

| جدول | ردیف | timestampهای دارای `Z` |
|---|---|---|
| `logs` | ۷ (PRINT×2, REFILL×4, STATUS×1) | **۰** |
| `printer_counters` | ۱ | — |
| `toner_history` | ۸ | — |
| `cartridge_state` | ۴ | — |
| `toner_snapshots_v2` | ۴۷ | — |
| `sensor_readings` | ۰ | — |

یعنی در این DB توسعه، هنوز هیچ ردیف UTC (از مسیر موتور) ثبت نشده است؛ داده‌های واقعی سرور باید با `tools/analyze_old_events.py` (گام E.0.2) شمرده شوند.

همین کمبود، تأیید TZ سرور (گام E.0.1) را هم به تأیید دستی وامی‌گذارد؛ ابزار و حکم‌هایش در بخش ۷ سند `docs/timezone-contract.md` ثبت شده است.

---

## 6. Final decision (E.0.0)

> ### تصمیم: **خیر** — writers در فاز E.0 یکسان نمی‌شوند. گام E.0.14b اجرا نمی‌شود.

**دلایل:**

۱. **جفت‌شدگی:** ۲۶ از ۲۷ فراخوانی `add_event` در کد برنامه naive local هستند؛ تغییر آن‌ها بدون تغییر ۹ نقطه‌ی مقایسه، ۳ گروه‌بندی روزانه، ۲ مرتب‌سازی و ۲ نقطه‌ی نمایش، خطاها را جابه‌جا می‌کند (بخش ۴).
۲. **هم‌راستایی با اصول فاز:** «این فاز هیچ کد production را تغییر نمی‌دهد» و «۰ تغییر در کد production» — با اجرای E.0.14b نقض می‌شد.
۳. **کفایت راهبرد تفکیکی:** با نشانه‌ی cutover (E.0.13) مهاجرت می‌تواند هر ردیف را با قاعده‌ی صریح تفسیر کند؛ داده‌ی تاریخی از دست نمی‌رود.
۴. **دامنه‌ی ریسک محدود است:** تنها رویدادهای `CARTRIDGE_CHANGED` مسیر موتور فرمت UTC دارند؛ اثر فعلی = ترتیب/نمایش همان رویدادها (بخش ۵)، نه صحت ذخیره‌سازی.
۵. **برگشت‌پذیری:** تغییر writers برگشت‌پذیری ندارد (داده‌ی نوشته‌شده با فرمت جدید باقی می‌ماند)؛ تفسیر در migration برعکس، فقط در زمان خواندن اثر می‌گذارد.

**قاعده‌ی مهاجرت (فاز F) — پیامد این تصمیم:**

| ردیف | قاعده |
|---|---|
| `timestamp` بدون `Z` | naive local ⇒ `ZoneInfo("Asia/Tehran")` ⇒ `TIMESTAMPTZ` |
| `timestamp` با `Z` و پیش از نشانه‌ی cutover | ساعت محلی بوده با برچسب جعلی ⇒ `ZoneInfo("Asia/Tehran")` |
| `timestamp` با `Z` و پس از نشانه‌ی cutover | UTC واقعی ⇒ همان‌طور تفسیر شود |
| `CARTRIDGE_CHANGED` | قاعده‌ی بالا + حسابرسی با `details.detection` (بخش ۲) به‌عنوان کنترل دوم |

**الزام فاز E — تصمیم قاطع (جای «اگر روزی»، کار E.0.15):**

> **همه‌ی writers باید در فاز E به UTC آگاه (`datetime.now(timezone.utc)`) منتقل شوند** —
> هم‌زمان با مهاجرت readerها (بخش ۴).
>
> **چرا الزام است، نه انتخاب:** ستون `TIMESTAMPTZ` در PostgreSQL هر مقدار naive را با
> **TimeZone نشست** تفسیر می‌کند. اگر writers محلی بمانند، در سرور با
> `TZ=Asia/Tehran` **هر نوشتن تازه ۳:۳۰ خطا** می‌شود و این بار خطا در *داده* ذخیره
> می‌شود، نه در یک رویداد نمایشی — یعنی همان باگ D.6 با دامنه‌ی بزرگ‌تر و بی‌صدا.
> این بند **پیش‌شرط گام ۶ در `docs/cutover-plan.md`** است؛ تا اجرا نشود، cutover ممنوع است.
> (در فاز E.0 عمداً هیچ writer تغییر نمی‌کند — تصمیم E.0.0 بالا.)

گام‌های اجرا در فاز E:
۱) تغییر هم‌زمان writerها و خواننده‌ها؛ ۲) هِلپر مشترک `utc_now_iso()` به‌عنوان تنها تولیدکننده؛ ۳) به‌روزرسانی دو نقطه‌ی نمایش UI و خروجی export؛ ۴) انقضای توکن‌ها (`auth.py`/`users.py`) هم‌زمان؛ ۵) re-record کامل golden با ساعت قفل‌شده (گام E.0.3 پیش‌نیاز آن است)؛ ۶) تست مهاجرت روی نسخه‌ی کپی‌شده از DB واقعی.

---

## 7. Command to reproduce this list

```bash
python tools/find_timestamp_consumers.py                     # خلاصه‌ی خوانا (۲۳۹ یافته)
python tools/find_timestamp_consumers.py --md /tmp/ts.md     # جدول کامل Markdown
python tools/find_timestamp_consumers.py --json /tmp/ts.json # خروجی ماشینی
python tools/find_timestamp_consumers.py --only W3           # فقط نویسنده‌های add_event
```

شمارش فعلی خروجی: W1=۳، W2=۳، W3=۵۶ (شامل تعریف تابع، ۲۷ فراخوانی در کد برنامه، فراخوانی‌های تست و `_fake_add_event`ها)، R1=۲۳، R2=۲۸، R3=۱۱، R4=۳، R5=۹۸، R7=۱۰، R6=۴ (جمع ۲۳۹).

---

---

## 7. Execution status (phase E.3) — each group closed

| گروه (بخش ۳) | وضعیت | تغییر انجام‌شده |
|---|---|---|
| ۳.۱ مقایسه با cutoff (۵ debounce + prune + toner-history) | ✅ E.3.4 | `to_utc_z(datetime.now(timezone.utc) - …)` — هم‌فرمت با داده‌ی Z |
| ۳.۱ فیلتر بازه‌ی `start`/`end` (`get_all_logs`, `get_recent_events`) | ✅ E.3.4 | `to_filter_bound` (آگاه ⇒ UTC؛ naive/تاریخ‌فقط ⇒ محلی؛ نامعتبر ⇒ دست‌نخورده + هشدار) |
| ۳.۲ مرتب‌سازی: `ORDER BY timestamp` در SQL | ✅ بدون تغییر لازم | رشته‌ی طول‌ثابت ۲۷ ⇒ `ORDER BY` رشته‌ای == ترتیب زمانی (نسخهٔ `TIMESTAMPTZ` در E.4) |
| ۳.۲ مرتب‌سازی در پایتون (reset history ×۲، Job Log) | ✅ E.3.4 | `to_sort_key` (تجزیه به datetime، نه مقایسه‌ی رشته) |
| ۳.۲ مرتب‌سازی در UI (`dashboard.js:125`) | ✅ E.3.4 | مقایسه با `Date.parse` (بعد از نرمال‌سازی میکروثانیه ⇒ میلی‌ثانیه) |
| ۳.۳ گروه‌بندی روزانه (`/api/stats/daily`, `/api/stats/sensor/daily`, مرز پنجره) | ✅ E.3.4 | `substr(timestamp,1,10)` و `AVG`/`SUM` از SQL حذف؛ سبدها در پایتون با `to_local_day` (روزِ Asia/Tehran) و `_local_day_start` |
| ۳.۴ خواندن و انتقال به پایتون (`get_log`, `get_all_logs`, reset history) | ✅ E.3.4 | رشته به‌عنوان رشته‌ی استاندارد منتقل می‌شود؛ `_row_to_dict` صریح شد (ستون مقدم بر `details`) |
| ۳.۴ `/metrics` (برچسب `detection`) | ✅ بدون تغییر | برچسب است نه زمان؛ خارج از دامنه |
| ۳.۵ نمایش در UI: داشبورد | ✅ E.3.4 | `formatEventTime` با `Intl.DateTimeFormat(…, timeZone:'Asia/Tehran')` |
| ۳.۵ خروجی CSV/Excel (`export_bp`) | ✅ E.3.4 | `to_local_str` + برچسب `Asia/Tehran` در سرستون‌ها |
| ۳.۵ جدول رویدادهای امنیتی (`security.html`) | ✅ فقط تجزیه | قالب‌بندی (`Intl fa-IR`) دست‌نخورده؛ فقط تجزیه‌ی قطعی ۶ رقم میکروثانیه |
| ۳.۶ `details["timestamp"]` | ✅ E.3.3 | تنها تولیدکننده‌اش (`cartridge_change_engine`) حالا `utc_now_iso` است؛ ستون هم بر `details` مقدم شد |
| §۶ تعهد «هر نویسنده فقط `utc_now_iso`» | ✅ E.3.3 | ۲۰ فایل؛ جزئیات در پیام کامیت `d806324` |

**معیار پذیرش فاز:** ۳۶۰ تست سبز در هر دو TZ (با PG فعال)، ۱۰/۱۰ golden تفاضل-صفر،
`logs.db` مهاجرت‌شده با بکاپ و بازبینی، صفر تغییر schema/Alembic. جزئیات و شاهدها:
`docs/timezone-contract.md` §۹.

