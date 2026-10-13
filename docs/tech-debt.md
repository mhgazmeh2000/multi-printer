# بدهی فنی (Tech Debt) — فهرست زنده

**وضعیت:** فقط مستندات. هر ردیف یک «بدهی» شناخته‌شده است که در فاز جاری اصلاح
نمی‌شود ولی باید پیش از/هنگام مهاجرت دیده شود.
**مبنا:** شاخه‌ی `phase-c` تا گام H.1 (فاز H؛ پایانِ G در `8047526`)؛ تاریخ بروزرسانی: ۲۰۲۶-۱۰-۰۶.
**خواندن همراه:** `docs/phase-e-decisions.md` — هر ردیف «تصمیم E.6.x» آن‌جا
دلیل، جایگزین ردشده و اثر (با عدد اندازه‌گیری‌شده) دارد.

| شناسه | عنوان | محل | کشف‌شده در | نوع | ریسک | وضعیت |
|---|---|---|---|---|---|---|
| TD-001 | تابع مرده `_save_counters_to_db` با فهرست ستون ناقص | `core/enhanced_collector.py` (حذف‌شده) | E.0.4 | کد مرده + باگ بالقوه | — | ✅ **بسته (E.6.5: حذف شد)** |
| TD-002 | بازتاب درخواستِ فراخوان در بدنهٔ ۴۰۱ (`login_url` → `next`) + حفرۀ `\` در guard | `web/auth.py` (`same_site_next_target`, `_is_safe_next_url`)، `web/__init__.py` | E.0.6 | رفتار امنیتی/UX | — | ✅ **بسته (E.6.6)** |
| TD-003 | نرمال‌ساز golden مقدار «N/A» را به «N<PATH>» تبدیل می‌کند | `tests/golden_harness.py` (الگوی `_PATH_RE`) | E.0.7 | کیفیت fixture | ⬜ کم | **رفع شد (E.3)** |
| TD-004 | `log` تعریف‌نشده در مسیر خطای `api_security_stats` | `web/routes/security.py` | E.3.4 (هشدار pyflakes) | باگ واقعی در مسیر خطا | — | ✅ **بسته (E.6.7)** |
| TD-005 | ابزار مهاجرت زمان، manifest را در هر اجرا بازنویسی می‌کند | `tools/migrate_timestamps_to_utc.py` (`_write_manifest`) | E.3.5 | قابلیت حسابرسی/شاهد | — | ✅ **بسته (فاز F.3.1: برچسب زمانی در نام فایل manifest)** |
| TD-006 | «ساعات سکوت» هشدار سنسور به ساعت محلی سرور تکیه دارد | `core/sensor_alert.py:39` | E.3.4 | تصمیم زمان‌محور | ⬜ متوسط (روی سرور UTC جابه‌جا می‌شود) | باز — **موکول به فاز I** (تصمیم E.6.8؛ پیش‌نیاز: تأیید مالک دربارهٔ TZ سرور) |
| TD-007 | حالت `replace` در import روی PG پشتیبانی نمی‌شود | `web/routes/import_db.py` (`_import_table`) | E.4.8 | قابلیت ترجمه‌نشده | — | ✅ **بسته (تصمیم E.6.1 و ابزار CLI فاز F؛ سند docs/import-db-retirement.md)** |
| TD-008 | `_columns`/`_tables` در import با SQLite-only کار می‌کنند (`PRAGMA`/`sqlite_master`) | `web/routes/import_db.py` (`_columns`, `_tables`) | E.4.4/E.4.8 | وابستگی به backend (ردیف ۱۱ strictness) | ✅ رفعِ بخشی در E.6.1 (`analyze` با `schema_columns`/`present_tables`) | باز — مسیر **نوشتن** بازنشسته (E.6.1)؛ `test_import_analyze_route` دیگر زیر TD-012 skip نمی‌شود |
| TD-009 | `_exists_by_cols` روی PG نحو نامعتبر می‌سازد (`col IS $1`) | `web/routes/import_db.py` (`_exists_by_cols`) | E.4.8 | وابستگی به backend | ⬜ صفر عملی (تنها از `mode=skip` و آن روی PG اجرا نمی‌شود) | باز — **بدون رفع (تصمیم E.6.9)**؛ زیر گارد بازنشستگی ۶.۱ |
| TD-010 | `printer_name` در `/metrics` با `MAX()` انتخاب می‌شود ⇒ «نام تاریخی» است، نه نام زندهٔ پرینتر | `web/routes/metrics.py` (کوئری `cartridge_changes_total`) | E.4.9 | تصمیم معنایی برچسب | ⬜ کم (نام‌ها در عمل ثابت‌اند) | باز — تصمیم مالک: حفظ تعداد سری |
| TD-011 | نام `UniqueViolation` برای **کل** خانوادهٔ integrity به‌کار می‌رود | `core/db/exceptions.py`، `core/db/connection.py::_translate_error`، `web/routes/import_db.py` | E.4.4 (درخواست مالک برای ثبت) | دقت نام/ظرافت سیاست | ⬜ کم (رفتار امروز درست و مستند است) | باز — فاز بعد |
| TD-012 | ۵۲ تست SQLite-محور که زیر `DB_BACKEND=postgresql` می‌شکنند | `tests/*` (marker `sqlite_only`) | E.5.2 | کاستی پوشش (مدیریت‌شده) | ⬜ کم (skip با دلیل + شمارش در سربرگ) | باز — **ثبت‌شده در E.6.10**؛ رفع کامل نیازمند تصمیم برای هر فایل |
| TD-013 | ترتیب الفبای فارسی در SQL روی PG وجود ندارد (collation `C`) | `docs/postgresql-strictness.md` بند ۴؛ `tests/test_collation_ordering.py` | E.6.3 | محدودیت رفتار (آگاهانه) | ⬜ کم (امروز هیچ `ORDER BY` متنیِ فارسی در کد نیست؛ UI با `localeCompare`) | باز — **تصمیم E.6.3: `C` می‌ماند**؛ در صورت نیاز per-query `COLLATE "fa-IR-x-icu"` |
| TD-014 | چهار ستون JSON دیگر هنوز `TEXT`‌اند (بدون سود اندازه‌گیری‌شده) | `users.allowed_offices`/`allowed_modules`، `printer_counters.cartridge_id_state`/`toner_overrides`، `security_events.details` | E.6.4 | یکدستیِ نوع | ⬜ خیلی کم (هیچ SQL روی محتوای آن‌ها JSON نمی‌خواند) | باز — **تصمیم E.6.4: TEXT می‌مانند**؛ JSONB فقط با یک کوئریِ واقعیِ JSON-خوان در کار |
| TD-016 | `_hp_toner_key` روی نام‌های SNMP با زیررشته تطبیق می‌داد ⇒ رنگِ کاذب («Imaging Drum» → magenta، «Fuser Kit» → black) و گم‌شدن `black_white` روی پرینترهای تک‌رنگ | `core/collectors/hp.py` (`_hp_toner_key`) | G.5 (سنجهٔ آینهٔ exporter) | باگ واقعی در رفتارِ امروز | ⬜ متوسط (هر HP تک‌رنگ با درام/فیوزر در اسلات ۲) | ✅ **بسته — رفع شد (مرزِ کلمه + `tests/test_hp_toner_key.py`)** |
| TD-017 | دو شاخهٔ `collect_hp`/`collect_enhanced` در آینهٔ exporter نیست: (۱) شرطِ «`total=0` مشکوک ⇒ دوباره SNMP بزن»، (۲) `_used`/`hp_web_used`/`note="used_cartridge"` (کارتریج شارژی از `SupplyState=Used`) — mirrorِ برندها به TD-018 رفت | `core/collectors/exporter_adapter.py` (`apply_toner_http_fallback`), `core/enhanced_collector.py:1248-1279` | H.1 | کاستیِ پوشش — **پذیرفته‌شده** | ⬜ کم (کارتریج شارژی HP در UI `unknown` می‌ماند، نه `used`) | باز — بندِ (۱) به H.2 (backoff/circuit-breaker)؛ بندِ (۲) تا یک مصرف‌کنندهٔ واقعیِ `note` پیدا شود |
| TD-019 | importِ هر چیزی از `core.collectors.*` (حتی `base` تنها) `core.snmp.protocol` + `core.store` + `core.db.*` را هم بار می‌کند — `core/collectors/__init__.py` هر چهار collector را eager import می‌کند | `core/collectors/__init__.py`, `core/collectors/{base,hp,canon}.py`, `core/collectors/exporter_adapter.py` | H.1 (تصمیمِ «import به‌جای کپیِ اسکرپرها») | گرهٔ معماری (transitive) | ⬜ کمِ امروز، متوسطِ فردا (هر چیزی که نخواهد DB را import کند) | باز — **موکول به فاز L: «جدا کردن اسکرپر از SNMP در collectorها»** |
| TD-018 | `toshiba`/`brother`/`sensor_ecs100g` هیچ‌کدام در مسیرِ exporter آینه نشده‌اند (`SUPPORTED_BRANDS = ("hp", "canon")`) | `core/collectors/exporter_adapter.py`, `core/collectors/{toshiba,brother}.py`, `monitoring/snmp.yml` | H.1 (دامنهٔ H را مالک مشخص کرد: گزینهٔ ۱) | کاستیِ پوشش — **پذیرفته‌شده** | ⬜ متوسط (با flag=1 آن برندها مسیرِ مستقیم می‌نویسند — درست، ولی «یک مسیر برای همه» نیست) | باز — **شرط انجام: بعد از تستِ ۴۸ ساعتهٔ HP/Canon روی سرورِ مالک**؛ حجمِ اندازه‌گیری‌شده در بخشِ پایین |
| TD-020 | fixture gap برای HP counter OIDs (11.2.3.9.6.1.1.*) | `tests/fixtures/golden/enhanced_01_hp_color_toner_full.json` و `enhanced_02` | H.3 (اندازه‌گیری مسیر کامل) | کاستی fixture (دست‌نویس از روی spec) | ⬜ کم (مسیر سرور واقعی ترافیک کامل دارد) | باز — **شرط انجام: بعد از capture واقعی روی سرور مالک** |
| TD-021 | snmp.yml برای HP Color فقط ۲ toner slot دارد | `monitoring/snmp.yml` و `generator.yml` | H.3/H.4 (تحلیل اسلات‌های تونر HP) | نقص مشخصات exporter | ⬜ متوسط (تونرهای زرد و قرمز HP Color در exporter خوانده نمی‌شوند) | باز — **رفع: بازتولید generator.yml با ۴ اسلات قبل از فعال‌سازی USE=1 روی سرور** |
| TD-022 | حذف ۵ ستون legacy از logs (در SQLite و PG) | `alembic` (0005) و `logs.db` (`sheets`, `raw_counters`, `raw_delta`, `event_type`, `duplex_mode`) | F.4/F.5 (بازبینی داده‌های واقعی پروداکشن) | ستون‌های مرده / یکدستی اسکیمای پروداکشن | ⬜ صفر (ستون‌ها بدون داده یا مقدار ثابت) | باز — **موکول به فاز L** (پس از Cutover موفق و تأیید ۴۸ ساعته) |
| TD-023 | تفاوت ترتیب فیزیکی ستون‌ها در printer_counters بین SQLite و PG | `alembic/0001` و `printer_counters` | Post-migration review (بازبینی پروداکشن) | انطباق ترتیب فیزیکی | ⬜ خیلی کم (کد با نام ستون کار می‌کند) | باز — **مستندسازی صریح؛ استفاده از نام ستون الزامی است** |

| TD-015 | `tools/` عمداً با `sqlite3` خام کار می‌کند (۱۵ هندلر در ۶ فایل) ⇒ روی PG هیچ‌کدام اجرا نمی‌شوند | `tools/analyze_old_events.py`, `tools/check_cutover_readiness.py`, `tools/find_insert_or_replace.py`, `tools/migrate_timestamps_to_utc.py`, `tools/set_cutover_marker.py`, `tools/verify_server_timezone.py` (+ `tests/test_db_adapter.py:74`) | E.4.4 (دامنهٔ E.4.11) | وابستگی به backend — **طراحی‌شده** | ⬜ کم (ابزار یک‌بارمصرفِ مهاجرت) | ⬜ **no-action آگاهانه** — در allowlist تست قرارداد ثبت شده؛ عددِ دقیق و علت در بخش پایین |

---

## TD-001 — `_save_counters_to_db` (مرده) و فهرست ستون ناقص

**وضعیت:** ✅ **بسته در E.6.5** — تابع (۵۳ خط) و `import json` بی‌مصرفش حذف
شدند؛ قفلِ بازنگشت در `tests/test_upsert_cross_backend.py` و شرح کامل در
`docs/insert-or-replace-inventory.md` §۳.۲.

**شرح:** تابع `_save_counters_to_db` در `core/enhanced_collector.py:200` هیچ
فراخوانی‌ای در کل مخزن ندارد (تنها مرجع = خط تعریف)؛ AST و `grep` هر دو
یکسان شهادت دادند: صفر فراخوانی، یک تعریف. داخلش یک
`INSERT OR REPLACE INTO printer_counters` (خط `208`) هست که فقط **۱۳ ستون از ۲۱**
ستون جدول را می‌نویسد.

**چرا بدهی است:** در SQLite معنای `INSERT OR REPLACE` «حذف ردیف و درج دوباره»
است؛ پس آن ۸ ستونِ جاافتاده
(`force_estimate`, `yield_learning_failures`, `last_alert_codes`, `a3_total`,
`a4_total`, `alert_codes`, `cartridge_id_state`, `toner_overrides`)
**به NULL/پیش‌فرض برمی‌گردند**. بین آن‌ها دو ستون وضعیت مهم‌اند:
`cartridge_id_state` و `toner_overrides` (پاک‌شدن‌شان یعنی از دست رفتن هویت
کارتریج و بازتنظیم‌های دستی تونر).

**شاهد تجربی (E.0.4، DB موقت):** درج کامل ⇒ مقادیر موجود؛ REPLACE با فهرست ناقص
⇒ هر دو ستون `NULL` شدند؛ `ON CONFLICT DO UPDATE` ⇒ دست‌نخورده ماندند. جزئیات:
`docs/insert-or-replace-inventory.md` §۳.۲.

**چرا امروز اثری ندارد:** تابع مرده است؛ مسیر زنده‌ی `core/store.py:121` →
`save_printer_counters` (`core/database.py:1077`) همه‌ی ۲۱ ستون را می‌نویسد.

**گزینه‌ها (تصمیم در فاز F):**
۱. حذف تابع مرده (ساده‌ترین، ریسک صفر)؛
۲. احیا با فهرست کامل ۲۱ ستون + `ON CONFLICT DO UPDATE` کامل؛
۳. احیا با فراخوانی `save_printer_counters` به‌جای SQL دستی.


---

## TD-002 — بازتاب درخواست فراخوان در بدنهٔ پاسخ ۴۰۱

**وضعیت:** ✅ **بسته در E.6.6** — `next` هم‌سایت شد و خودِ guard هم سخت‌تر شد.

**دو تغییر، یک تصمیم (allowlist به‌جای حذف query string):**
۱. `same_site_next_target()` / `login_redirect_url()` در `web/auth.py` — `next` از
   `request.path` + `query_string` خام ساخته می‌شود (بدون scheme/host) و اگر همان
   `_is_safe_next_url` نپذیرد، کلاً فرستاده **نمی‌شود**. دو نقطۀ ساخت پاسخ ۴۰۱
   (`_api_unauthorized` و `before_request` در `web/__init__.py`) به همین یک هلپر
   رسیده‌اند، پس سازنده و مصرف‌کننده یک قاعده دارند.
   *چرا حذف نکردن query string:* بازگشت به `/logs?page=3` بخشی از UX است و
   ادعای TD هم «نشت داده» نبود؛ جایگزینِ «حذف query» این بود که کاربر بعد از ورود
   صفحه‌اش را گم کند.
۲. **آنچه در همین گام کشف شد:** `_is_safe_next_url` مقدار `/\evil.example.com` را
   **می‌پذیرفت**. در URLهای special-scheme مرورگرها `\` معادل `/` است
   (رفتار WHATWG URL)، پس `Location: /\evil.example.com` عملاً
   `//evil.example.com` و یعنی بیرون از سایت ⇒ یک **open redirect واقعی** در
   مصرف‌کنندۀ `next` — نه فقط «بازتاب آزاردهنده» در ۴۰۱. حالا هر مقدار شامل `\`
   رد می‌شود و این حالت، همراه با `//host`، `https://…`، `javascript:` و
   `HTTPS://…` در جدول `tests/test_auth_next_redirect.py::SafeNextUrlValidationTests`
   قفل شده است (پیش از E.6.6، خودِ این guard هیچ تستی نداشت).

**اثر بر کاربر:** بی‌تغییر در ظاهر — فقط `login_url`/`Location` کوتاه‌تر شد؛ کاربر
بعد از ورود به همان صفحه‌ای که می‌خواست برمی‌گردد. اثر امنیتی: هر `next` بیرونی
(از جمله شکل `\`) به `/` برمی‌گردد.

**تست:** `tests/test_auth_next_redirect.py` (۱۱ تست: جدول validator، بازتاب ۴۰۱،
سردر ۳۰۲، و سه حالت بازگشت پس از ورودِ واقعی). تست موجود
`tests/test_smoke_routes.py` (ساختار ثابت پاسخ ۴۰۱، سقف طول بدنه) دست‌نخورده سبز ماند.

**شرح:** در پاسخ ۴۰۱، فیلد ``login_url`` یک پارامتر ``next`` دارد که **همان
درخواستِ فراخوان** را بازتاب می‌دهد. دو نقطهٔ ساخت این پاسخ:
`web/auth.py:46` (`url_for("auth.login", next=request.url)` در `_api_unauthorized`)
و `web/__init__.py:113` (همان الگو در `before_request`).

```
POST /api/import/analyze?ip=203.0.113.99&limit=10   ⇒   401
{"error":"unauthorized","login_url":"/login?next=http://localhost/api/import/analyze?ip%3d203.0.113.99%26limit%3d10"}
```

**کشف:** گام E.0.6، هنگام نوشتن تست «عدم نشت»: اولین نسخهٔ تست با IP جعلی
پارامتردار شکست خورد و معلوم شد برمی‌گردد.

**چرا بدهی است (و چرا کم‌ریسک):** مقداری که برمی‌گردد **ورودی خودِ فراخوان**
است، نه داده‌ی سرور؛ پس نشت اطلاعاتی (IP/نام پرینتر/نوع رویداد) نیست. ریسک
باقی‌مانده: (۱) اگر روزی سازندهٔ `next` سخت‌گیر نشود، ممکن است به open-redirect
یا echo-based XSS در صفحهٔ ورود منجر شود (بستگی به نحوهٔ رندر `next` دارد)؛
(۲) در لاگ‌ها/URLها، محتوای درخواست تکرار می‌شود.

**وضعیت آزمون:** تست E.0.6 دقیقاً همین تفکیک را قفل می‌کند: بدون پارامتر هیچ
داده‌ای برنمی‌گردد، و با پارامتر جعلی، «هیچ داده‌ی سروری» — فقط ساختار ثابتِ
`{"error","login_url"}` — برمی‌گردد.

**اقدام پیشنهادی (فاز E یا بعد):** محدودکردن `next` به مسیر همین سایت
(allowlist) یا حذف Query String از `next`. امروز در فاز E.0 طبق «۰ تغییر در کد
production» دست‌نخورده می‌ماند.
**قید فاز E.0:** طبق «۰ تغییر در کد production»، در این فاز دست‌نخورده می‌ماند —
همین ثبت، هدفِ این ردیف است.

---

## TD-003 — نرمال‌ساز golden، «N/A» را به «N&lt;PATH&gt;» تبدیل می‌کند

**شرح:** در `tests/golden_harness.py` الگوی مسیر (`_PATH_RE`) روی **هر** رشته
اعمال می‌شود؛ پس مقدار `"N/A"` (که سنسور برای «نامعلوم» برمی‌گرداند —
`core/collectors/sensor.py:192,197,286`) به `"N<PATH>"` تبدیل می‌شود. قطعه‌ی
`/A` شبیه مسیر دیده می‌شود.

**شاهد:** `normalize_value("N/A") == "N<PATH>"`. در fixture سنسور
(`tests/fixtures/golden/enhanced_05_ecs100g_sensor.json`) سه فیلد
`device.serial`، `device.firmware` و `device.uptime_str` به همین شکل ذخیره شده‌اند.

**چرا بدهی است:** (۱) fixture دیگر عیناً خروجی واقعی را نشان نمی‌دهد و خواننده
گمان می‌کند مقدار یک مسیر بوده است؛ (۲) اگر کسی «N/A» را در fixtureها جست‌وجو
کند، پیدا نمی‌کند.

**قید فاز E.0:** رفع این مورد چند fixture را تغییر می‌دهد و طبق قید «تغییر
fixture مگر برای PII» مجاز نیست ⇒ برای فاز E ثبت شد (همراه با بازضبطی که هنگام
انتقال writers به UTC انجام می‌شود).

---

## TD-003 — وضعیت: رفع‌شده (فاز E.3)

**رفع:** الگوی `_PATH_RE` یک lookbehind گرفت — `(?<![A-Za-z0-9])` — تا فقط جایی
فعال شود که پیش از `/` کاراکتر الفبایی/عددی نباشد (یعنی ابتدای رشته، یا بعد از
فاصله/کوتیشن/`=`/`:`). نتیجه:

    normalize_value("N/A")        → "N/A"        (پیش‌تر: "N<PATH>")
    normalize_value("/tmp/x.log") → "<PATH>"     (بی‌تغییر)
    normalize_value(r"C:\\Users\\a\\b.txt") → "<PATH>"   (بی‌تغییر)

**اثر:** هر ۵ fixture `enhanced_*` بازضبط شد؛ تفاضل با نسخهٔ قبل **فقط** همان ۷
فیلد بود (۵ × `firmware`، ۱ × `serial`، ۱ × `uptime_str`) و همه از
`"N<PATH>"` به `"N/A"` برگشتند — هیچ مقدار دیگری جابه‌جا نشد (`diff -r` کامل).
تست‌های golden پس از بازضبط: ۱۰/۱۰ تفاضل-صفر.

---

## TD-004 — `log` تعریف‌نشده در مسیر خطای آمار امنیتی

**وضعیت:** ✅ **بسته در E.6.7** — `import logging` + `log =
logging.getLogger("PrinterMonitor")`) هم‌سبک سایر routeها اضافه شد و
`pyflakes core web` از ۳۶ به **۳۵** رسید (کاهش، نه افزودن).

**چرا «بالقوه» نبود، واقعی بود:** تست `SecurityStatsErrorPathTests` با بالا‌رفتن
خطا در `_db_conn` نشان می‌دهد امروز پاسخ `500` با بدنهٔ JSON و یک رکورد
`ERROR` در logger برنامه است؛ پیش از اصلاح، همان سناریو `NameError` روی
`RuntimeError` می‌داد — یعنی پاسخ HTML خطای Flask و **هیچ رکورد لاگی**. این
تست عمداً روی `assertLogs` قفل کرده، چون نصفِ ارزش این handler، دیده‌شدن
خطا در لاگ است.

**قفل ساختاری (تا refactor بعدی همین را برنگرداند):**
`tests/test_route_logging.py::LogBindingGuardTests` با AST همهٔ `web/**.py` و
`core/**.py` را می‌خواند و هر `log.<level>()` که در scope مربوطه binding نداشته
باشد، گزارش می‌شود. شدت این قفل سنجیده شد: با حذف موقت `log = …` از
`security.py`، هر سه تست همین فایل قرمز می‌شوند و با بازگردانی، سبز.


**شرح:** در `web/routes/security.py` تابع `api_security_stats` در بلوک `except`
تابع `log.exception(...)` را صدا می‌زند، ولی ماژول نه `logging` را import کرده و نه
`log` را تعریف کرده است؛ پس اگر مسیر خطا فعال شود، به‌جای لاگ‌کردن `NameError`
می‌گیریم.

**شاهد:** هشدار `pyflakes`: `undefined name 'log'` — **پیش از فاز E.3 وجود داشت**
(pyflakes روی `HEAD` پیش از E.3.4 هم همین مورد را با شماره‌خط قبلی گزارش می‌کرد؛
۳۶ هشدار پیش و پس از E.3 یکسان است). در E.3.4 فقط جابه‌جایی خطوط باعث تغییر
شماره‌خط شد و عمداً برای حفظ دامنهٔ گام، رفع نشد.

**اقدام پیشنهادی:** افزودن `import logging` و `log = logging.getLogger("PrinterMonitor")`
(هم‌سبک بقیهٔ ماژول‌ها) — یک خط، بدون اثر رفتاری جز لاگ‌شدن خطا.

---

## TD-005 — بازنویسی manifest ابزار مهاجرت زمان

**وضعیت:** ✅ **بسته (فاز F.3.1)** — نام‌گذاری فایل‌های مانیفست با برچسب زمانی استاندارد `{db_path}.e3-manifest-{timestamp}.json` پیاده‌سازی شد.

**شرح:** در ابتدا `tools/migrate_timestamps_to_utc.py` فایل manifest را با نام ثابت (`<db>.e3-manifest.json`) می‌نوشت و در نتیجه اجرای دوم (که idempotent است و `converted=0` می‌دهد) همان فایل را بازنویسی می‌کرد و شاهد اجرای اصلی از بین می‌رفت.

**راه‌حل پیاده‌سازی‌شده:** در فاز F.3.1 تابع `_write_manifest` اصلاح شد تا برچسب زمانی یکتا (بر مبنای UTC) به انتهای نام فایل اضافه شود و در صورت وجود فایل قبلی، از بازنویسی ممانعت به عمل آید. نام فایل تولیدشده در خروجی لاگ نمایش داده می‌شود و این رفتار با ۳ تست اختصاصی در `tests/test_migrate_timestamps.py` تثبیت شده است.

**اقدام پیشنهادی:** نام‌گذاری manifest بر اساس حالت/زمان (`…apply-<ts>.json`)
یا افزودن `--manifest` اجباری در حالت apply.

---

## TD-006 — «ساعات سکوت» هشدار سنسور و ساعت سرور

**وضعیت:** 🔵 **باز — موکول به فاز I** (تصمیم E.6.8). پیش‌نیاز، تصمیم مالک است،
نه کد: «سرور هدف با `TZ=Asia/Tehran` اجرا می‌شود یا UTC؟» بدون آن، هر رفعی فقط
انتخابِ ما را قفل می‌کرد.

**شرح:** `core/sensor_alert.py:39` شرط شب/روز را با `datetime.now().hour` می‌سنجد؛
یعنی به منطقهٔ زمانیِ **محلی سرور** تکیه دارد. اگر سرور `TZ=UTC` اجرا شود (مثل
سندباکس/CI)، پنجرهٔ سکوت ۳:۳۰ جابه‌جا می‌شود و ممکن است هشدار در ساعت نامناسب
برود یا نرود.

**چرا در E.3 رفع نشد:** این «تصمیم زمانی کسب‌وکار» است نه timestamp ذخیره‌شده؛
قرارداد زمان فاز E دربارهٔ *داده* است. ضمناً طبق `docs/timezone-contract.md` §۷.۳
پیش‌فرض این است که سرور با ساعت تهران کار می‌کند (تأیید دستی مالک باز است).

**اقدام پیشنهادی:** استفاده از `core.time_utils.local_tz()` (ساعت تهران) به‌جای
ساعت سرور، همراه با تست مرزی.

---

## TD-007 — حالت `replace` در import روی PostgreSQL

**شرح:** `INSERT OR REPLACE` نحو SQLite است؛ روی PostgreSQL با
`syntax error at or near "OR"` (SQLSTATE 42601) می‌شکند (سنجیده‌شده، E.4.8).
مسیر زنده: `web/routes/import_db.py:214-227` و فقط برای جدول‌هایی که در
`ALLOWED_IMPORT_TABLES` پرچم `replaceable: True` دارند
(`printer_counters`، `cartridge_state`، `cartridge_yield_profiles`).

**وضعیت (بسته در فاز F — تصمیم E.6.1):** مسیر اجرای عملیات import تحت وب روی دیتابیس PostgreSQL رسماً بازنشسته و مسدود باقی ماند (`_reject_import_on_postgres()`). انتقال و همگام‌سازی اطلاعات در محیط PostgreSQL منحصراً توسط ابزار اختصاصی CLI (`tools/migrate_sqlite_to_pg.py`) با پشتیبانی از استراتژی‌های پیشرفته (COPY / Batch)، مدیریت توالی‌های IDENTITY و تبدیل ایمن داده‌ها انجام می‌شود. جزئیات معماری، دلایل بازنشستگی و راهنمای سناریوهای آتی در سند `docs/import-db-retirement.md` مستند شده است.

---

## TD-008 — خواندن اسکیمای import با ابزارهای SQLite-only

**شرح:** `web/routes/import_db.py` هم برای فایل مبدأ (SQLite آپلودی) و هم برای
دیتابیس مقصد از `_columns` (`PRAGMA table_info`) و `_tables` (`sqlite_master`)
استفاده می‌کند. فایل مبدأ همیشه SQLite است (درست)، ولی **مقصد** روی PG این دو
را نمی‌شناسد: `PRAGMA` ⇒ 42601 و پس از آن تراکنش abort می‌شود (25P02).

**شاهد:** سنجش E.4.8 روی PG ۱۶ واقعی — هر دو جمله خطای نحوی دادند.

**وضعیت:** این همان ردیف ۱۱ جدول ۱۷ ردیفی `docs/postgresql-strictness.md` است
(⏳ تصمیم محصولی E.6). هِلپر جانشین **آماده است**:
`core.db.schema.schema_columns(conn, table)` (cross-backend، E.4.8) و
`core.db.schema.present_tables(conn)` (E.4.5). تست‌های زنده‌ی E.4.8 همین هِلپر را
با `mock.patch` جای `_columns` می‌گذارند، پس تعویض واقعی در E.6 آزموده‌شده است.

**اقدام پیشنهادی در E.6:** تعویض `_columns`/`_tables` با هِلپرها (یا بازنشستگی کل
`import_db` در فاز L، که گزینه‌ی روی میز است).

---

## TD-009 — `_exists_by_cols` روی PostgreSQL: `col IS $1` نحو نامعتبر است

**شرح:** `web/routes/import_db.py::_exists_by_cols` برای «وجود ردیف با مقادیر
یکسان، مقاوم به NULL» شرط `(col IS ? OR col = ?)` می‌سازد. عملگر `IS` در SQLite
برابریِ null-safe است، ولی در PostgreSQL سمت راست `IS` باید `NULL`/`TRUE`/`FALSE`/
`DISTINCT FROM` باشد و **پارامتر نمی‌پذیرد** ⇒
`syntax error at or near "$1"` (سنجیده‌شده، E.4.8، روی PG ۱۶ واقعی).

**چرا مهم است:** این تابع فقط وقتی صدا زده می‌شود که `duplicate_mode == "skip"`
باشد و آن **پیش‌فرض** است (`web/routes/import_db.py:330`). یعنی روی PG، حالت
پیش‌فرض import در همان اولین ردیف می‌شکند — مستقل از TD-007 و TD-008.

**یافته‌ی E.4.8:** هنگام نوشتن تست زنده‌ی SAVEPOINT کشف شد.

**وضعیت (تصمیم مالک، E.4.8):** **زیر گارد یکپارچهٔ import_db** — همان گاردی که
TD-007 را می‌بندد (`_reject_import_on_postgres()` در ابتدای `confirm_import`)، همهٔ
حالت‌های `duplicate_mode` (`skip | allow | replace`) را روی PG مسدود می‌کند؛ پس این
مسیر تا E.6 روی PG قابل رسیدن نیست و شکست نحوی‌اش روی کاربر ظاهر نمی‌شود.
در E.6، همان‌جا که گارد برداشته می‌شود، هر سه مسئله (TD-007 + TD-009 + ردیف ۱۱)
یک‌جا حل می‌شوند.

**راه‌حل فنی E.6 (ثبت‌شده تا از دست نرود):** معادل null-safe استاندارد روی PG یعنی
`col IS NOT DISTINCT FROM ?` (پارامتر می‌پذیرد) و روی SQLite همان `col IS ?`.

**نکته‌ی تست:** تست‌های زنده‌ی E.4.8 که خودِ حلقهٔ درج را می‌سنجند (SAVEPOINT و
بولی) مستقیماً `_import_table` را صدا می‌زنند — نه endpoint را — و با حالت مجاز
`allow` اجرا می‌شوند تا از گارد و از TD-009 عبور کنند.

---

## TD-010 — `printer_name` در `/metrics` و معنای `MAX()`

**شرح:** کوئری `cartridge_changes_total` روی `logs` گروه‌بندی
`(printer_ip, detection, color)` دارد و `printer_name` را به‌عنوان برچسب برمی‌گرداند.
روی SQLite این ستونِ بدون `GROUP BY` مجاز است (مقدارش «هر ردیفی که افتاد»)، روی
PostgreSQL نه: خطای **42803** («must appear in the GROUP BY clause or be used in an
aggregate function») — سنجیده‌شده در E.4.9 روی PG ۱۶.

**تصمیم (مالک، E.4.9b):** `MAX(printer_name) AS printer_name` با `GROUP BY` بدون
تغییر. دو دلیل:
۱. **حفظ تعداد سری امروز** — افزودن `printer_name` به `GROUP BY` باعث می‌شود اگر
   پرینتری بین دو رخداد تغییر نام داده باشد، روی SQLite دو سری ظاهر شود (تغییر
   رفتار مصرف‌کنندهٔ Prometheus/Grafana)؛
۲. مزیت جانبی: مقدار برچسب دیگر به ترتیب تصادفی ردیف‌ها وابسته نیست (رفتار
   امروزِ SQLite غیرقطعی بود).

**بدهی باقی‌مانده:** این «نام تاریخی» است نه «نام زندهٔ» پرینتر. اگر روزی لازم شد
برچسب نامِ جاری باشد، باید `printer_name` به `GROUP BY` اضافه شود و **تعداد سری
آگاهانه تغییر کند** (تصمیم محصولی جدید، نه یک رفع کوچک).

**قفل تست:** `tests/test_metrics_pg.py` — تعداد سری روی هر دو backend یکسان است،
مقدار برچسب در یک سری با دادهٔ نام‌دار متفاوت قطعی (`MAX`) است، و روی PG کوئری
پیش از این تغییر خطای 42803 می‌داد (شاهد معکوس ثبت شده).

---

## TD-011 — نام `UniqueViolation` برای کل خانوادهٔ integrity

**شرح:** در E.4.4 به تصمیم صریح مالک، **کل** خانوادهٔ `IntegrityError` به یک کلاس
نگاشت می‌شود: `sqlite3.IntegrityError` (UNIQUE / NOT NULL / CHECK / FK) و روی PG هر
SQLSTATE با پیشوند `23`. نام کلاس (`UniqueViolation`) پس برای «تعارض یکتایی» است،
در حالی که معنای واقعی آن «نقض یکدستی» است. امروز این بی‌دقتی **بی‌اثر** است، چون
تنها مصرف‌کننده‌ای که خطا را «می‌خورد و ادامه می‌دهد» (`web/routes/import_db.py`)
می‌خواهد هر ردیف ناقابل‌درج را skip کند — و هر چهار زیرخانواده برای آن رفتار یکسان‌اند.

**چرا امروز رفع نشد:** تصمیم از این جنس در E.4.4 گرفته نشد (دستور مالک: «رفتار
`import_db` عیناً همان امروز؛ هیچ تصمیم جدیدی از این جنس نه»).

**طرح پیشنهادی (فاز بعد):**
۱. افزودن پایهٔ `IntegrityError(DatabaseError)`؛
۲. زیرکلاس‌های `UniqueViolation` (23505/23514)، `NotNullViolation` (23502)،
   `ForeignKeyViolation` (23503/23504) — روی SQLite از متن پیام
   (`UNIQUE constraint failed` / `NOT NULL constraint failed` /
   `FOREIGN KEY constraint failed` / `CHECK constraint failed`)؛
۳. تصمیم **صریح** در `import_db`: کدام زیرخانواده‌ها skip شوند و کدام‌ها باید بالا
   بروند (امروز: همه skip). هر تغییر در این فهرست، تغییر معنای import است.

**شاهد/قفل فعلی:** جدول نگاشت و پیام‌های SQLite/PG در
`tests/test_exceptions_pg.py` (کلاس‌های `SqliteTranslationTests` /
`PostgresTranslationStubTests` / `PostgresTranslationLiveTests`) قفل شده‌اند؛
هر بازآرایی نام‌ها باید همان جدول را به‌روز کند، نه اینکه بی‌صدا ردیف‌ها را حذف کند.

---

## TD-012 — ۵۲ تست SQLite-محور که زیر `DB_BACKEND=postgresql` می‌شکنند

**وضعیت (E.6.10):** ردیف این بدهی به **جدول** هم اضافه شد (تا پیش از این فقط
بخش توضیحی داشت) — skip‌ها خودِ راه‌حل‌اند، ولی «ادعای پوشش PG» دیگر نمی‌شود،
پس بدهی است.

**وضعیت (E.5):** با گزینهٔ «ج» **مدیریت شد** — marker
`sqlite_only` + auto-skip در `tests/conftest.py`؛ `DB_BACKEND=postgresql
python -m pytest tests/` ⇒ ۴۱۹ passed / ۰ failed / ۷۲ skipped و SQLite
⇒ ۴۹۱ passed / ۰ skipped. **بدهی باقی است** چون پوشش PG برای آن جریان‌ها
دیگر *ادعا نمی‌شود* (فهرست محل‌ها و دلیل هر کدام در
`docs/phase-e-plan.md` بند ۵.۳). دو زیرمجموعهٔ واقعی که باید در E.6 با
تصمیم import_db بسته شوند: `test_import_analyze_route` (⇒ TD-008) و
ترجمهٔ `golden_harness` در صورت نیاز به golden روی PG (⇒ TD-007).

**شرح:** پس از E.5، سوئیت روی PG با ۵۲ شکست از ۴۹۱ اجرا می‌شود. علت در **هیچ**
مورد، کد برنامه نیست؛ خودِ فایل‌های تست SQLِ مختص SQLite می‌نویسند
(`CREATE TABLE t (… AUTOINCREMENT)`, `INSERT OR REPLACE INTO logs …`,
`json_extract(details, '$.x')`) یا فرض می‌کنند «هر `chdir` به پوشهٔ موقت ⇒ DB خالی».
فهرست کامل با شمارش هر فایل در `docs/phase-e-plan.md` بند ۵.۲ است.

**چرا در E.5 رفع نشد:** تصمیم ۷ِ مالک برای گام ایزولاسیون صریحاً گفت «همه‌چیز در یک
فایل (`tests/conftest.py`)، بدون دست‌زدن به تک‌تک تست‌ها». بازنشانی داده در همان
قالب، ۴۳ شکست را بست؛ باقیِ کار نیاز به ویرایش فایل‌های تست دارد که تصمیمش به مالک
برمی‌گردد (سه گزینه در بند ۵.۳ همان سند).

**طرح پیشنهادی:** گزینهٔ «الف» برای تست‌هایی که ذاتاً SQLite را می‌آزمایند
(`test_db_adapter`، `test_ddl_bootstrap`، `test_db_migration_columns`،
`test_golden`/`golden_harness`، `SqliteMetricsSeriesTests`، `SqliteUpsert…`) — قفل
`DB_BACKEND=sqlite` + `PG_DSN=""` با همان هلپر `_pin_sqlite`؛ و گزینهٔ «ب» برای
مسیرهای واقعیِ دو-پشتانه‌ای که باید روی PG هم دویده شوند
(`test_poll_event_invariant`، `test_smoke_routes`، `test_rbac_admin_guard`) — fixture
مشترک «اتصال با backend فعال» به‌جای فایل موقت. هر دو دسته با یک خط در بالای فایل
قابل تشخیص می‌مانند و `NoSilentSkipsTests` (در `test_strictness_pg.py`) باید همان
فهرست را به‌عنوان «تست‌های SQLite-only اعلام‌شده» قفل کند، تا قفل‌شدن بی‌دلیلِ یک تست
سبز، پوشش را بی‌صدا نکارد.

**شاهد/قفل فعلی:** `tests/test_upsert_cross_backend.py` الگوی قفل را نشان می‌دهد و
`tests/conftest.py` گزارش سربرگ («PG ایزوله روی اسکیمای …») چاپ می‌کند؛ شمارش‌های
بالا با `TZ=UTC DB_BACKEND=postgresql PG_DSN=… pytest -q --tb=line -rf` بازتولید
می‌شوند.

---

## TD-013 — ترتیب الفبای فارسی در SQL (collation `C`)

**وضعیت:** 🔵 **باز — آگاهانه** (تصمیم E.6.3؛ قفل در `tests/test_collation_ordering.py`).

**شرح:** دیتابیس PG این پروژه `datcollate = C` است، پس `ORDER BY` روی متن = ترتیب
کدپوینت UTF-8 — که با الفبای فارسی نمی‌خواند (`پارسه` **پس از** `ثابت` و `هلدینگ`،
و `۲ستاره` آخرِ فهرست). اندازه‌گیری با ۱۸ رشته روی همین سرور: `C` در برابر
`fa-IR-x-icu` ⇒ **۱۵ ردیف از ۱۸** جابه‌جا می‌شوند.

**چرا رفع نشد (و معیار «تفاوت ناچیز» اینجا برقرار نبود):** SQLite این build هیچ‌گزینهٔ
ICU ندارد (`PRAGMA compile_options` ⇒ صفر)، پس ICU روی PG یعنی **دو ترتیب متفاوت
برای یک کوئری** در پنجرۀ دوبackend و در بازگشت — همان چیزی که فاز E برای جلوگیری‌اش
ساخته شد. ضمناً تنها `ORDER BY`های متنیِ کد روی `printer_ip`/`username` (ASCII) است
و ترتیب فارسیِ نمایشی در `web/static/js/dashboard.js` با `localeCompare` انجام
می‌شود ⇒ سود کاربریِ فوری ندارد.

**راه درست، اگر لازم شد:** per-query با `COLLATE "fa-IR-x-icu"` (یا ایندکس
هم‌collation) — بدون migration؛ ICU روی این سرور موجود است (۸۷۱ collation، از جمله
`fa-IR-x-icu` و `fa-AF-x-icu`) و تست‌ها همین را چک می‌کنند. **زنگِ بازنویسی:** اگر
روزی SQLite با ICU ساخته شد، تست `test_sqlite_build_has_no_icu` عمداً می‌شکند تا
تصمیم بازبینی شود.

---

## TD-014 — JSONB نشدنِ چهار ستون JSON دیگر

**وضعیت:** 🔵 **باز — بدون برنامهٔ رفع** (تصمیم E.6.4؛ دامنه در
`tests/test_jsonb_columns.py::MigrationScopeTests` قفل شده است).

**شرح:** `0004_jsonb_columns.py` فقط `logs.details` را به `jsonb` برد. این چهار ستون
(و `security_events.details`) `TEXT` ماندند، چون در SQL هیچ‌وقت به‌صورت JSON خوانده
نمی‌شوند — فقط `json.dumps`/`json.loads` در پایتون
(`core/database.py::_dump_json_list`/`_load_json_list`/`_load_json_dict`/
`_load_cartridge_id_state` و `web/auth.py::_clean_list`).

**چرا این‌طور انتخاب شد:** معیار تکلیف «`grep json_extract`» بود و دقیقاً یک ستون را
تأیید می‌کرد. تبدیل بقیه هزینهٔ migration و واگرایی اسکیما (از جمله
`users.allowed_offices NOT NULL DEFAULT '[]'` که روی `jsonb` به
`'''[]'''::jsonb` نیاز دارد) را بی‌سود اضافه می‌کرد؛ هر دو backend برای این ستون‌ها
همین حالا یکسان‌اند.

**شرط فعال‌شدن:** اگر روزی کوئری SQL لازم کرد که *داخل* یکی از این ستون‌ها برود
(مثلاً فیلتر `allowed_offices` روی «دفتر X»)، همان‌جا JSONB + ایندکس GIN توجیه
پیدا می‌کند — و آن وقت باید `JSONB_COLUMNS` در `0004` و فهرست تستِ دامنه هم‌زمان
به‌روز شوند، نه فقط یکی.


---

## TD-015 — هندلرهای `except sqlite3.*` در `tools/` (بدهی *پذیرفته‌شده*)

**وضعیت:** ⬜ **no-action آگاهانه** (تصمیم مالک در E.4.4: «۱۳ محل در کد برنامه ⇒ کلاس
مشترک؛ `tools/` و `tests/` دست‌نخورده»). این ردیف همان نیمهٔ دوم را شماره‌گذاری می‌کند —
تا «فراموش‌شده» با «عمدی» اشتباه گرفته نشود.

**عدد اندازه‌گیری‌شده (نه برآورد):** همان AST-اسکنِ `tests/test_exceptions_pg.py`
(شمارشگر `except`هایی که نوعشان به `sqlite3` اشاره می‌کند):

| مجموعه | پیش از `d4e6cf9` (E.4.4) | امروز (`3141382`) |
|---|---|---|
| `core/` + `web/` + `create_admin.py` | **۱۲** | **۰** (تستِ `test_no_sqlite_excepts_in_app_code` قفلش کرده) |
| `tools/` | ۱۱ site در ۵ فایل | **۱۵ site در ۶ فایل** |
| `tests/` | ۳ site در ۲ فایل | ۱ site (`tests/test_db_adapter.py:74`) |

> نکتهٔ دقت: در تکلیف، «۱۳ محل در کد برنامه» و «۱۲ محل در tools/tests» نوشته شده بود؛
> اسکنِ AST روی پدرِ همان کامیت **۱۲** و **۱۱+۳=۱۴** را می‌دهد (grep هم ۱۱ می‌دهد، چون یک
> `except` دو‌نوعی یک خط است). مرجع این سند، عددِ همان ابزاری است که در تست استفاده می‌شود.
> افزایش ۴ تاییِ `tools/` هم از `tools/check_cutover_readiness.py` (E.7.2) است — عمداً، چون
> `PRAGMA integrity_check` معادل PG ندارد و پیش از cutover باید فایل `logs.db` را مستقیم ببیند.

**چرا نمی‌سازیمش:** این پنج ابزار، ابزار *یک‌بارمصرفِ* مهاجرت/بازبینیِ فایل SQLite‌اند؛
معنایشان پیش از cutover است (روی `logs.db`) و بعد از cutover بایکوت می‌شوند. ترجمهٔ
آن‌ها به `core.db.connect()` یا `information_schema` یعنی اضافه‌کردن یک backend دوم به
کدی که قرار است بمیرد. `postgresql-strictness.md` §۶.۹ و ردیف‌های ۱۱/۱۳/۱۶ همان جدول هم
به همین نتیجه می‌رسند («no-action»).

**چطور قفل شده است:** `tests/test_exceptions_pg.py::AppCodeContractTests::test_sqlite_only_tools_are_explicitly_exempt`
یک allowlistِ **صریح از نام فایل‌ها** است؛ اگر فایل تازه‌ای با `except sqlite3.*` به `tools/`
اضافه شود، تست می‌شکند و نویسنده مجبور است یا کلاس مشترک بگیرد یا فایل را (با دلیل) به
allowlist بیفزاید — همان کاری که E.7.2 با `check_cutover_readiness.py` کرد.

**شرط فعال‌شدن (چه‌وقت باید رفع شود):** دو حالت — (الف) اگر روزی این ابزارها لازم شد روی
PG هم کار کنند (مثلاً `set_cutover_marker` برای PG)، آن‌وقت باید از `core.db` عبور کنند و
این ردیف بسته شود؛ (ب) اگر فاز F ابزار `migrate_sqlite_to_pg.py` را ساخت، آن ابزار *از همان
اول* دوسوکول ساخته می‌شود و نباید به این فهرست بپیوندد (بخش «فاز F تحویل می‌گیرد» در
`docs/phase-e-completion-report.md` §۵).

---

## TD-016 — `_hp_toner_key` بی‌مرزِ کلمه (رفع‌شده در `fix(hp)`)

**چطور پیدا شد:** گام G.5 خواست خروجیِ exporter «دقیقاً مثلِ `collect_hp`» باشد؛ برای
همین `_hp_toner_key()` از همان فایل import شد و تستِ آینه، نامِ «CF470A Imaging Drum» را
روی کلیدِ `cyan` نشاند (چون `c` زیررشتهٔ «CF470A» است). باگ در آینه دیده شد، نه در خودِ
collector — و آینه درست کار می‌کرد.

**کدِ معیوب (قبل از رفع):** `for kw, key in _HP_TONER_COLOR_MAP.items(): if kw in n:` —
و نقشه شامل کلیدهای تک‌حرفی `k/c/m/y` است.

**سنجهٔ همین session** (خروجیِ تابعِ معیوب، پیش از رفع):

| نامِ مصرفی (prtMarkerSuppliesDescription) | کلیدِ غلط |
|---|---|
| `Imaging Drum` | `magenta` |
| `CF470A Imaging Drum` | `cyan` |
| `Fuser Kit` | `black` |
| `Maintenance Kit` | `black` |
| `Document Feeder Kit` | `black` |

**اثر:** `collect_hp` هر کلیدِ غیرِ `black` را «تونر رنگی» می‌شمارد (`has_color_toner`)؛
آن شاخه برای پرینتر تک‌رنگِ بدونِ OIDِ رنگ، `full_color = None` و `black_white = None`
می‌گذارد (به‌جای `black_white = total`) ⇒ تفکیک سیاه‌وسفید روی همان دستگاه‌ها از دست
می‌رفت، و درام/فیوزر در UI به‌عنوان «تونر magenta» فهرست می‌شد.

**رفع:** تطبیق با مرزِ کلمه (`re.search(rf"\b{re.escape(kw)}\b", n)`) — همان قاعده‌ای که
`_key()` در `_scrape_hp_toners` (مسیرِ وبِ *همان فایل*) از قبل داشت و کامنتش دقیقاً
مثالِ «Document Feeder Kit» را می‌زد؛ فقط مسیرِ SNMP اصلاح نشده بود. `None` برای نام‌های
بی‌رنگ، رفتارِ درستِ خطِ بعدِ `collect_hp` را فعال می‌کند:
`toner_key = … or ("black" if idx == 1 else f"toner_{idx}")`.

**پوشش:** `tests/test_hp_toner_key.py` (۲۰ مورد: ۱۷ حالتِ parametrized + ۳ تست — شامل
«K» و «Toner C» که *باید* رنگ بگیرند، ردیفِ رگرسیونِ نام‌های بالا، و اینکه دو مسیرِ HP
یک قاعده دارند). آینهٔ exporter:
`tests/test_exporter_adapter.py::test_hp_drum_slot_uses_the_generic_key` و
`tests/test_hp_toner_key.py::test_exporter_mirror_uses_the_fixed_rule` — دومی عددِ
شمارنده را هم می‌سنجد (`full_color=None`، `black_white=500`).

**اثرِ جانبی (ثبت‌شده تا کسی دوباره خطا نگیرد):** شمارهٔ خطوطِ `core/collectors/hp.py` در
ستونِ «منبع» سندِ ماتریس از *کد* خوانده می‌شود؛ +۱۱ خطِ docstringِ رفع، ۱۵ سطرِ
`docs/oid-matrix.md` را جابه‌جا کرد. `test_documented_matrix_is_in_sync_with_code` همان
اول گرفت و `--write` (که تستِ قطعی‌بودن اجرا می‌کند) سند را اصلاح کرد؛ یعنی گیت کار کرد —
و درسش: هر ویرایشِ خطی در `core/collectors/*.py` مستلزمِ بازتولیدِ ماتریس است.

---

## TD-017 — دو شاخه‌ای که آینهٔ exporter بازتولید نمی‌کند (دامنهٔ H.1)

**چطور پیدا شد:** گام H.1 خواست وب‌اسکرپِ تونر (که فقط در collectorها بود) به مسیرِ
exporter برسد. هنگامِ مرزبندیِ «چه چیزی آینه می‌شود»، سه شاخه بیرونِ دامنه ماند و عمداً
با تست/سند بسته شد تا «کمبود» به «باگِ بی‌صدا» تبدیل نشود. دو تا اینجا می‌مانند؛ سومی
(mirrorِ سه برندِ دیگر) ردیفِ خودش را دارد: **TD-018**.

**۱) «total=0 مشکوک ⇒ دوباره SNMP بزن».** `collect_hp` وقتی مجموعِ شمارنده‌ها صفر است یک
خواندنِ دوم می‌کند؛ این state و «دومین خواندن» می‌خواهد و با backoff/circuit-breakerِ
گام H.2 هم‌خانواده است، پس آن‌جا تصمیم گرفته می‌شود. آینه امروز در این حالت همان صفر را
می‌نویسد و `tools/compare_snmp_sources.py` آن را به‌عنوان `diff` نشان می‌دهد (نه `missing`).

**۲) `_used` / `hp_web_used` / `note="used_cartridge"`.** در
`core/enhanced_collector.py:1263-1279`، وقتی اسکریپرِ EWS برای کارتریجی `SupplyState=Used`
می‌بیند، وضعیتِ آن تونر روی `used` و `note` روی `used_cartridge` می‌نشیند. آینه عمداً این
شاخه را نمی‌گیرد: قراردادِ G.5 «۱۲ کلیدِ `collect_hp`، صفر کلیدِ اضافه» است و `note` هم‌رشته‌ای
از همین شکل نیست ⇒ هر افزودنش یک سطرِ `only_in_exporter` ساختگی در مقایسه می‌ساخت.
`apply_toner_http_fallback` کلیدِ `_used` را صریح رد می‌کند (قاعدهٔ «کلیدِ «_» فراداده است»
عیناً از `_merge_scraped_toners` کپی شده). **اثر:** روی پرینترِ HP با کارتریج شارژی، مسیرِ
exporter به‌جای «کارتریج استفاده‌شده» می‌گوید «نامشخص». اگر UI روزی `note` را مصرف کرد،
این ردیف باز می‌شود.

**پوششِ رفتارِ آینه (برایِ اینکه دو بندِ بالا مرزِ واقعی باشند، نه حدس):** ۱۵ تستِ تازه در
`tests/test_exporter_adapter.py` (مجموعِ فایل: ۳۸) و **۱۹ پروبِ جهش، ۱۹ گرفته‌شده**. چهار
پروبِ توجه‌برانگیز: «فقط شکلِ بیانِ کپی عوض شود» را *تنها* تستِ هم‌متنی
(`test_merge_functions_are_faithful_copies`) می‌گیرد؛ «dispatcher حذف شود و قاعده‌ها مستقیم صدا زده شوند» (رفتار یکی است) هم همین‌طور، و
«`_used` گاردِ عددی» هم همین‌طور
(رفتاراً هیچ‌کدام، چون شرطِ دومِ همان `if` لیست را رد می‌کند)؛ و «`raw_level` برگردد» را
تستِ هم‌متنی عمداً نمی‌گیرد (نرمال‌سازی حذفش می‌کند) ولی دو تستِ رفتاری می‌گیرند — یعنی
تلورانسِ متنی آسیبِ واقعی را نمی‌پوشاند. شرحِ تصمیم، جایگزین‌هایِ ردشده، باگِ `brand`ِ خامِ
همین گام، و جدولِ «معیارهایِ پذیرش و شاهد»:
`docs/phase-h-decisions.md` §H.1.


---

## TD-018 — mirror نکردنِ `toshiba`/`brother`/`sensor_ecs100g` در مسیرِ exporter

**چرا باز است:** دامنهٔ H.1 را مالک با «گزینهٔ ۱» بست: HP + Canon. `SUPPORTED_BRANDS =
("hp", "canon")` است و `collect_via_exporter` برایِ هر برندِ دیگر *پیش ازِ هر کوئری* `None`
می‌دهد تا poller همان مسیرِ مستقیم را بنویسد که امروز می‌نویسد. پس این «باگ» نیست؛ یک
**تأخیرِ انتخاب‌شده** است — ولی حجمش باید اندازه‌گیری شده باشد، نه شعاری.

**حجمِ اندازه‌گیری‌شده در همین session** (`monitoring/snmp.yml` = ۸۵۴ خط / ۱۳۶ ردیفِ `oid:`):

| ماژول | خطوطِ ماژول در `snmp.yml` | ردیفِ `oid:` | اسکریپرِ legacy | کدِ collector |
|---|---|---|---|---|
| `printer_toshiba` | ۳۶۹ | **۶۳** | `_scrape_toshiba_toners` = ۸۵ خط (`toshiba.py:40-124`، `requests`/`urlopen`) | `toshiba.py` ۵۲۸ خط |
| `printer_brother` | ۵۸ | **۹** | `_scrape_brother_toners` = ۹۳ خط (`brother.py:11-103`، `fetch_first_web_page`) | `brother.py` ۲۸۱ خط |
| `sensor_ecs100g` | ۵۴ | **۱۰** | — (سنسور است؛ `toners` ندارد) | — |

یعنی Toshiba به‌تنهایی ~۴۶٪ ردیف‌هایِ `snmp.yml` را می‌خواهد و یک G.5ِ تمام‌عیار است؛ Brother
کوچک‌تر است ولی `sysDescr`-محور است (نامِ برند از توضیحات، نه از OID) و `_used` هم دارد.
*(«~۱۵۰/~۸۰ خطِ adapterِ تازه» در برنامهٔ H.۱ تخمین بود؛ سنجه‌ای برایش انجام نشده.)*

**دو مانعِ فنی که سرِ راهِ کپی‌کردنِ اسکریپرهاست (ثبت می‌شود تا دوباره کشف نشود):**
۱) `_scrape_toshiba_toners` مثلِ `_scrape_canon_toners` داخلِ بدنه `import requests` و
`urllib.request.urlopen` دارد (`toshiba.py:54/67/69`)، و گیتِ ASTِ
`tests/test_exporter_adapter.py::test_adapter_has_no_side_effects` همین دو اسم را در
`exporter_adapter.py` ممنوع کرده؛ ۲) `_toners()`/`_trays()` برایِ آن برندها شکلِ خروجیِ
متفاوتی دارند، پس نوشتنِ fixture برایشان یعنی دادهٔ جعلی — همان دلیلی که در H.1 «گاردِ
non-entry» ساختیم نه successِ ساختگی.

**شرط انجام (تصمیمِ مالک):** بعد از تستِ ۴۸ ساعتهٔ HP/Canon روی سرورِ خودِ مالک با
`USE_SNMP_EXPORTER=1` (§۱۰ سندِ setup؛ `tools/compare_snmp_sources.py` باید `diff` صفر بدهد).
**پوششِ فعلیِ همین مرز:** سه گاردِ پارامتریز‌شده در
`tests/test_exporter_adapter.py`: `test_toshiba_does_not_enter_exporter_path`،
`test_brother_does_not_enter_exporter_path`، `test_sensor_does_not_enter_exporter_path`
+ `test_unsupported_brands_and_empty_answers_yield_none` (G.5).


---

## TD-019 — گرهٔ transitive: `core.collectors.*` بدونِ SNMP/DB بار نمی‌شود

**چرا ثبت شد:** گام H.1 خواست اسکرپرهایِ HTTP از `core/collectors/{hp,canon}.py` import شوند
(«صفر کپی»). مخالفِ این انتخاب می‌گفت آن importها `core.snmp.protocol` را به مسیرِ exporter
می‌کشند. سنجیدیم و معلوم شد **درست است، ولی کارِ H.1 نیست**: مجموعهٔ `core.*` که با
`import core.collectors.exporter_adapter` بار می‌شود **۲۲** ماژول است، و همین عدد با
`import core.collectors.base` تنها هم **۲۰** است — یعنی دو importِ تازهٔ H.1
(`core.collectors.canon`، `_scrape_hp_toners`/`_scrape_canon_toners`) **صفر** ماژولِ تازه
افزوده‌اند؛ سنگینی از `core/collectors/__init__.py` است که در سطرهای ۵–۸ هر چهار collector را
eager import می‌کند و `base` که `core.store`/`core.db` را می‌آورد. مقایسه:
`import core.exporter_client` = **۲** ماژول.

**اثرِ امروز:** عملی صفر — poller هر دو حال `core.store` را import می‌کند. اثرِ فردا: هر مصرف‌کنندهٔ
*sans-DB* از آینه (ابزارِ خطِ فرمان، تستِ واحد، یک lambda edge) مجبور است `core.db` را هم
بالِ سرش بیاورد؛ و فاز L که `protocol.py` را حذف کند، همین فایل‌ها را هم لمس می‌کند.

**رفعِ پیشنهادی (فاز L، همان‌طور که مالک نام‌گذاری کرد):** بیرون‌کشیدنِ کدِ HTTP از collectorها
(`_scrape_hp_toners`، `_scrape_canon_toners`، `_scrape_toshiba_toners`، `_scrape_brother_toners`
و `fetch_first_web_page`) به یک ماژولِ بی‌DB مثلِ `core/web_panels.py`؛ سپس
`from core.web_panels import ...` در adapter و lazy شدنِ `core/collectors/__init__.py`.
شرطِ پذیرشِ رفع: عددِ «۲۲» به ~۵ برسد (سنجه: همان اسکریپتِ شمارشِ `sys.modules` که در این
بخش استفاده شد) و گیتِ ASTِ `test_adapter_has_no_side_effects` همان بماند.

**چرا در H.1 رفع نشد:** دو راهِ دیگر هر کدام یک قانونِ دیگر را می‌شکست — کپی‌کردنِ اسکرپرها
یعنی ۲۲۵ خط HTML/XMLِ دوقلو و (برای canon) شکستنِ `banned`ِ گیت؛ importِ
`core.enhanced_collector` (که `core.store` را مستقیم می‌آورد) ردِ صریحِ برنامهٔ فاز است.

---

## TD-020 — fixture gap برای HP counter OIDs (11.2.3.9.6.1.1.*)

**چطور پیدا شد:** در گام H.3، مقایسهٔ خروجیِ مسیرِ کاملِ exporter با fixtureهای طلایی اچ‌پی
(`enhanced_01_hp_color_toner_full` و `enhanced_02_hp_color_toner_empty`) نشان داد که از میان
۳۴ برگ خروجی exporter، دقیقاً ۳۰ برگ تطابقِ کامل دارند، ۲ برگ volatile هستند (`last_poll` و
`poll_ms`)، و دقیقاً ۲ برگ واگرایی دارند:
- `counters.full_color` (در golden برابر ۲۱۴۸۰ / ۳۱۰۰؛ در exporter برابر `None`)
- `counters.black_white` (در golden برابر ۶۲۷۳۰ / ۹۴۰۰؛ در exporter برابر `None`)

**علت فنی:**
آداپتورِ exporter برای استخراج شمارنده‌های رنگی و سیاه‌وسفید در پرینترهای اچ‌پی به دو متریکِ
زیر متکی است:
- `hp_page_count_color` (OID: `1.3.6.1.4.1.11.2.3.9.6.1.1.1.2.1`)
- `hp_page_count_mono` (OID: `1.3.6.1.4.1.11.2.3.9.6.1.1.1.1.1`)

این دو OID در شاخهٔ Enterprise اختصاصی شرکت HP (`1.3.6.1.4.1.11.*`) قرار دارند. هنگامی که
fixtureهای طلایی اولیه برای فاز C ضبط شدند، صرفاً شاخه‌های استاندارد عمومی (Printer-MIB)
شبیه‌سازی شدند و OIDهای اختصاصیِ شمارندهٔ HP در جدول OIDهای fixture گنجانده نشدند. بنابراین
شبیه‌سازِ snmp_exporter هیچ داده‌ای برای این دو متریک دریافت نمی‌کند و خروجیِ آداپتور برای
آن‌ها `None` می‌شود؛ در حالی که کلکتور قدیمی این مقادیر را از مسیرهای ترکیبی یا fallback استخراج می‌کرد.

**اثرِ امروز:** در محیط آزمون hermetic، خروجی آداپتور برای این دو فیلد `None` است ولی
هیچ خطایی رخ نمی‌دهد و ۳۰ برگِ دیگر داده‌های واقعی را منعکس می‌کنند. در محیط عملیاتی واقعی
با پرینتر زنده، OIDهای اختصاصی پاسخ می‌دهند و شمارنده‌ها استخراج می‌شوند.

**شرطِ انجام و رفع (تصمیم مالک در گام H.3):**
پس از ضبطِ واقعی (capture) ترافیک SNMP با پرینتر واقعی HP روی سرور مالک (در چارچوب آزمون
۴۸ ساعتهٔ `USE_SNMP_EXPORTER=1` و ابزار `tools/compare_snmp_sources.py` — §۱۰ سند setup)، این
OIDها به fixture اضافه می‌شوند و تفاضل به صفر می‌رسد.

**پوششِ تستِ مرز:** تستِ `tests/test_hybrid_full_path.py::test_fixture_gaps_are_documented_and_evaluated`
روی هر دو سناریوی ۰۱ و ۰۲ وجود این رفتار را قفل کرده و تضمین می‌کند هیچ کلیدِ دیگری خارج
از این دو دچار شکاف fixture نیست.

---

## TD-021 — snmp.yml برای HP Color فقط ۲ toner slot دارد

**چطور پیدا شد:** در تحلیل واگرایی گام H.3 و آزمون‌های `tests/test_hybrid_full_path.py` مشخص شد که
خروجیِ `collect_via_exporter` برای پرینترهای رنگی اچ‌پی (مانند HP Color LaserJet MFP M577) صرفاً
دو رنگِ `black` و `cyan` را استخراج می‌کند و کلیدهای `magenta` و `yellow` غایب هستند. بررسی
فایل‌های `monitoring/snmp.yml` و `monitoring/generator.yml` نشان داد:
۱. ماژول `printer_hp` دارای ۱۴ متریک است اما هیچ اسلاتِ تونری (سطرهای supplies) در آن تعریف نشده است.
۲. ماژول عمومی `printer_std` صرفاً ۲ اسلاتِ اول را شامل می‌شود:
   `toner_name_1`/`toner_name_2`، `toner_max_1`/`toner_max_2` و `toner_remain_1`/`toner_remain_2`
   (مرتبط با OIDهای `1.3.6.1.2.1.43.11.1.1.*.1.1` و `.1.2`).
۳. اسلات‌های ۳ و ۴ متعلق به رنگ‌های زرد و قرمز در Printer-MIB استاندارد
   (`1.3.6.1.2.1.43.11.1.1.*.1.3` و `.1.4`) در پیکربندی فعلیِ `monitoring/generator.yml` وجود ندارند.

**اثرِ امروز:** با پرچم `USE_SNMP_EXPORTER=1`، اطلاعات درصد و وضعیت کارتریج‌های magenta و yellow
برای HP Color از طریق exporter خوانده نمی‌شود و خالی می‌ماند.

**شرطِ انجام و رفع (پیش از فعال‌سازی پرچم روی سرور):**
گسترش فایل `monitoring/generator.yml` برای تعریف ۴ اسلات تونر استاندارد (یا جدول walk کامل برای
`prtMarkerSuppliesEntry`)، بازتولید فایل `monitoring/snmp.yml` با دستور
`python tools/build_snmp_yml.py`، و به‌روزرسانی ماتریس OID قبل از روشن کردن `USE_SNMP_EXPORTER=1`
در محیط پروداکشن.

---

## TD-022 — حذف ۵ ستون legacy از logs (در SQLite و PG)

**چرا:** ستون‌های مرده (`sheets`، `raw_counters`، `raw_delta`، `event_type`، `duplex_mode`)، ۱۰۰٪ تهی (NULL) یا دارای مقادیر ثابت (`UNKNOWN`، `unknown`)، بدون هرگونه مصرف‌کننده در کد برنامه (`core/`، `web/`). وجود آن‌ها در فاز F صرفاً جهت انطباق اسکیمای PostgreSQL با دیتابیس فعلی پروداکشن برای عبور از گارد ابزار مهاجرت با مایگریشن 0005 ایجاد شد.

**کجا:**
- SQLite: فایل `logs.db` پروداکشن (نیاز به `ALTER TABLE logs DROP COLUMN ...` — نیازمند SQLite 3.35+)
- PostgreSQL: مایگریشن `0006` در فاز L

**شرط انجام:** پس از cutover موفق + تأیید ۴۸ ساعته پروداکشن.
**ریسک:** صفر (ستون‌ها بدون داده و بدون مصرف‌کننده).

---

## TD-023 — تفاوت ترتیب فیزیکی ستون‌ها در printer_counters بین SQLite و PG

**چطور پیدا شد:** در جریان بازبینی پایگاه‌داده سرور تولید پس از مهاجرت SQLite به PostgreSQL مشخص شد که ترتیب فیزیکی ستون‌ها در جدول `printer_counters` بین SQLite و PostgreSQL (تعریف‌شده در مایگریشن `alembic/versions/0001_initial_schema.py`) دارای تفاوت جزئی است:
- در SQLite: ستون ۱۷ `alert_codes`، ستون ۱۸ `updated_at`، ستون ۱۹ `cartridge_id_state`، ستون ۲۰ `toner_overrides`
- در PostgreSQL: ستون ۱۸ `alert_codes`، ستون ۱۹ `cartridge_id_state`، ستون ۲۰ `updated_at`، ستون ۲۱ `toner_overrides`

**اثرِ امروز:** ابزار مهاجرت `tools/migrate_sqlite_to_pg.py` و تمامی کوئری‌های نرم‌افزار در لایه‌ی `core.db` و مدل‌ها ستون‌ها را بر اساس نام صریح (`named columns` یا دیکشنری) می‌خوانند و می‌نویسند؛ از این رو هیچ اختلالی در عملکرد دیتابیس یا مایگریشن ایجاد نشد.

**دستورالعمل و تصمیم:**
۱. کدبیس همواره باید از نام ستون یا شیوه `sqlite3.Row` / `dict` استفاده کند و تکیه بر ایندکس موقعیتی عددی (positional index مانند `row[18]`) اکیداً ممنوع است.
۲. ایجاد مایگریشن اختصاصی برای بازچینی فیزیکی ستون‌ها اختیاری است و با توجه به نبود سود عملی و ریسک دستکاری اسکیمای فعال، مستندسازی صریح انجام و وضعیت کنونی تثبیت شد.

