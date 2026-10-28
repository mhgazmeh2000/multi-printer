# `INSERT OR REPLACE` inventory and dropped columns — phase E.0, task E.0.4

**وضعیت:** فقط مستندات + یک ابزار تشخیصی. هیچ کد production، هیچ schema و هیچ داده‌ای تغییر نکرده است.
**ابزار:** `tools/find_insert_or_replace.py` (AST-محور؛ کد را می‌خواند و برای نقشه‌ی ستون‌ها یک DB **موقت** بیرون از مخزن می‌سازد).
**مبنا:** شاخه‌ی `phase-c` پس از `e311d96`؛ تاریخ بررسی: ۲۰۲۶-۱۰-۰۳.

---

## 1. Why this inventory is needed (semantic difference between SQLite and PostgreSQL)

| | SQLite: `INSERT OR REPLACE` | PostgreSQL: `ON CONFLICT (...) DO UPDATE` |
|---|---|---|
| کار با تعارض کلیدی | ردیف قبلی **حذف** و ردیف تازه **درج** می‌شود | ردیف قبلی **به‌روز** می‌شود |
| ستون‌هایی که در فهرست نیامده‌اند | **به مقدار پیش‌فرض/NULL برمی‌گردند (داده پاک می‌شود)** | دست‌نخورده می‌مانند |
| نتیجه | «جایگزینی کامل ردیف» | «به‌روزرسانی جزئی» |

اثبات تجربی روی همین اسکیما (DB موقت، بیرون از مخزن، سپس حذف‌شده):

```
۱) درج کامل:                            ('10.0.0.5', 1000, '{"black": …}', '{"ids": {"black": "X1"}}', None)
۲) INSERT OR REPLACE با فهرست ناقص:     ('10.0.0.5', 2000, None,            None,                       None)
   ⇒ toner_overrides و cartridge_id_state پاک شدند
۳) ON CONFLICT DO UPDATE (تک‌ستونه):    ('10.0.0.5', 3000, None,            None)   ← ستون‌های دیگر دست‌نخورده
```

پس مهاجرت باید **آگاهانه** انتخاب کند: یا رفتار SQLite را بازتولید کند (نوشتن *همه‌ی*
ستون‌ها، با NULL/پیش‌فرض برای غایب‌ها)، یا رفتار پستگرس را بپذیرد (که معنایش این است
که SQLite امروز دارد داده را بی‌صدا پاک می‌کند).

---

## 2. Full list of locations (6 locations at E.0.4 — none left without a decision)

> **بازه‌ی خواندن:** این جدول، اسکن روز E.0.4 است و عمداً دست‌نخورده مانده
> تا «از کجا شروع شد» پیداش باشد. وضعیت امروز هر ردیف در §۲.۱ است و
> شمارشِ امروزِ ابزار (`python tools/find_insert_or_replace.py`) پس از E.5 و
> E.6.5 برابر **۲ محل** است (هر دو «بازبینی دستی»: `golden_harness` و جملهٔ
> پویای `import_db`).

| فایل:خط | API | جدول | ستون‌های نوشته‌شده | ستون‌های جدول | ستون‌های غایب | تصمیم |
|---|---|---|---|---|---|---|
| `core/database.py:1077` (فراخوانی: `1076`) | `execute` | `printer_counters` | ۲۱ | ۲۱ | **هیچ** | **semantics یکسان** |
| `core/enhanced_collector.py:208` (فراخوانی: `207`) | `execute` | `printer_counters` | ۱۳ | ۲۱ | ۸ | **کد مرده** ⇒ بدون اقدام در E.0؛ قاعده‌ی migration در §۳.۲ |
| `core/yield_engine.py:412` (فراخوانی: `410`) | `execute` | `cartridge_state` | ۳۱ | ۳۱ | **هیچ** | **semantics یکسان** |
| `core/yield_engine.py:614` (فراخوانی: `612`) | `execute` | `cartridge_yield_profiles` | ۱۰ | ۱۰ | **هیچ** | **semantics یکسان** |
| `tests/golden_harness.py:280` (فراخوانی: `279`) | `execute` | پویا (جدول‌های golden) | همه‌ی ستون‌های ردیف | همان | **هیچ** | **semantics یکسان** (فقط تست) |
| `web/routes/import_db.py:187` | `assign` (جمله پویا) | پویا (از `ALLOWED_IMPORT_TABLES`) | «ستون‌های مشترک» منبع و هدف | متغیر | **وابسته به منبع** | **ON CONFLICT DO UPDATE با همهٔ ستون‌ها** + بند §۳.۳ |

خروجی ابزار **در زمان E.0.4**: `جمع: 6 محل` و کد خروج `2` (چون دو محل پویا و یک محل جزئی‌اند و ابزار نمی‌تواند خودکار تصمیم نهایی بگیرد؛ تصمیم‌های نهایی در همین سند ثبت شده‌اند).

---

### ۲.۱ وضعیت پس از E.5 (۲۰۲۶-۱۰-۰۴)

| محل | وضعیت | چگونگی |
|---|---|---|
| `core/database.py` — `save_printer_counters` | ✅ ترجمه شد | `upsert_sql("printer_counters", …, ("ip",), backend=conn.backend)` |
| `core/yield_engine.py` — `_upsert_state` | ✅ ترجمه شد | کلید تعارض `(printer_ip, color)`؛ هر ۳۱ ستون نوشته می‌شود |
| `core/yield_engine.py` — `_upsert_profile_if_high` | ✅ ترجمه شد | `values_sql` صریح، چون `confidence` مقدار ثابت `'high'` دارد |
| `core/enhanced_collector.py:208` | ✅ حذف شد (E.6.5) | تابع مردهٔ `_save_counters_to_db` با ۵۳ خط + `import json` بی‌مصرف حذف شد؛ TD-001 بسته شد (بازنشستگی به‌جای احیا — صفر ریسک، صفر تغییر رفتار: فراخوانی‌اش وجود نداشت) |
| `tests/golden_harness.py:280` | ⬜ no-action | fixtureهای golden با SQLite تعریف شده‌اند؛ تصمیم صریح مالک در E.5 |
| `web/routes/import_db.py:187` | ⏳ E.6 | جملهٔ پویا؛ مسیر PG پیش از آن با گارد E.4.8ب بسته می‌شود (TD-007) |

`upsert_sql` دو شکل می‌سازد: روی SQLite `INSERT OR REPLACE INTO "t" (…) VALUES (…)`
(همان متن تاریخی) و روی PG `INSERT … ON CONFLICT (کلید) DO UPDATE SET "c" =
EXCLUDED."c"`؛ ستون‌های کلید از `SET` حذف می‌شوند و اگر ستون غیرکلیدی نماند،
`DO NOTHING` ساخته می‌شود. قفل‌ها: `tests/test_upsert_cross_backend.py`
(شکل جمله، AST «کد برنامه جملهٔ دستی نمی‌نویسد مگر سه محل allow-list شده»،
هم‌خوانی فهرست ستون‌ها با `schema_columns`، و رفتار درج/تعارض/idempotent روی **هر دو**
backend).

---

## 3. Decision per item

### ۳.۱ `semantics یکسان` (سه محل تولیدی/تستی)

`core/database.py:1077` (`save_printer_counters`)، `core/yield_engine.py:412` (`_upsert_state`)
و `core/yield_engine.py:614` (`_upsert_profile_if_high`) **همه‌ی** ستون‌های جدول را صریح
می‌نویسند؛ پس «حذف+درج» و «به‌روزرسانی» نتیجه‌ی یکسان می‌دهند و مهاجرت می‌تواند
بی‌دغدغه به `ON CONFLICT ... DO UPDATE SET` با فهرست کامل ستون‌ها ترجمه شود.

شاهد زنده‌بودن: `save_printer_counters` از `core/store.py:121`؛ `_upsert_state` ۱۰ فراخوانی
(۸۳۰–۱۲۱۲)؛ `_upsert_profile_if_high` از `core/yield_engine.py:1088`.
همچنین `golden_harness.seed_db` ستون‌ها را از خودِ ردیفِ seed (خروجی `SELECT *`) می‌سازد
⇒ همیشه کامل است و فقط در تست استفاده می‌شود.

### ۳.۲ کد مرده: `core/enhanced_collector.py:208` — ✅ بسته‌شده در E.6.5 (حذف)

تابع `_save_counters_to_db` (خط ۲۰۰) **هیچ فراخوانی‌ای در کل مخزن ندارد** (تنها مرجع = خط
تعریف). ۱۳ ستون از ۲۱ ستون را می‌نویسد و ۸ ستون را جا می‌گذارد:
`force_estimate`, `yield_learning_failures`, `last_alert_codes`, `a3_total`, `a4_total`,
`alert_codes`, `cartridge_id_state`, `toner_overrides`.

- **تصمیم E.0:** طبق قید فاز («۰ تغییر در کد production») دست‌نخورده می‌ماند.
- **قاعده‌ی migration (برای احیای احتمالی):** باید «همهٔ ستون‌ها» را بنویسد
  (`ON CONFLICT DO UPDATE SET` کامل، با NULL/پیش‌فرض برای ۸ ستون غایب) تا رفتار
  «جایگزینی کامل» امروز حفظ شود؛ هر رفتار دیگری تغییر معنایی است.
- **یادداشت ریسک (آن زمان):** اگر این تابع روزی احیا شود، در SQLite همان ۸ ستون
  (که `cartridge_id_state` و `toner_overrides` هم جزوشان‌اند) پاک می‌شوند.

**وضعیت E.6.5 — حذف شد.** تصمیم مالک: حذف، نه احیا. دلایل: صفر فراخوانی (AST و grep
هر دو)، و در صورت احیا باید فهرست ستون کامل می‌شد که عملاً بازنویسی مسیر
`core/store.py → save_printer_counters` بود — یعنی یک قابلیت تازه، نه رفع بدهی.
همراه تابع، `import json` (تنها مصرف‌کننده‌اش همان تابع بود) هم حذف شد؛ شمارش
`pyflakes core web` در آن گام تغییر نکرد (۳۶، همه از قبل موجود؛ امروز پس از E.6.7 که یک `undefined name` را بست، ۳۵). قفل بازگشت‌ناپذیری در
`tests/test_upsert_cross_backend.py::SourceContractTests` است: `enhanced_collector`
از allow-list بیرون رفت و دو assert دارد که «INSERT OR REPLACE» و
`_save_counters_to_db` نباید در آن فایل برگردند.

### ۳.۳ مسیر پویا: `web/routes/import_db.py:187`

جمله در زمان اجرا ساخته می‌شود:

```python
verb = "INSERT" if ... else "INSERT OR REPLACE"
insert_sql = f"{verb} INTO {_q(table)} ({cols_sql}) VALUES ({placeholders})"
```

- **کجا REPLACE می‌شود؟** فقط جدول‌هایی که در `ALLOWED_IMPORT_TABLES` پرچم
  `replaceable: True` دارند: `printer_counters`، `cartridge_state`،
  `cartridge_yield_profiles` (`web/routes/import_db.py:36, 57, 78`).
- **ستون‌ها:** `common_cols` = اشتراک ستون‌های منبع و هدف (منهای `id` اگر
  `skip_id` باشد). پس اگر فایل پشتیبان قدیمی‌تر باشد و ستون‌های تازه را نداشته باشد،
  آن ستون‌ها در فهرست نمی‌آیند و در SQLite **پاک می‌شوند**
  (مثال واقعی: پشتیبان قبل از افزودن `toner_overrides`/`cartridge_id_state`).
- **تصمیم:** ترجمه به `ON CONFLICT DO UPDATE SET` با **همه‌ی** ستون‌های جدول هدف؛ برای
  ستون‌هایی که در منبع نیستند، صریحاً `NULL` (یا پیش‌فرض) نوشته شود تا رفتار
  «جایگزینی کامل» حفظ شود. بدون این بند، مهاجرت معنای import را بی‌صدا عوض می‌کند.
- **ورودی فاز F:** همان‌جا که `duplicate_mode == "replace"` پذیرفته می‌شود، باید
  «ستون‌های غایب منبع» هم گزارش شود تا کاربر بداند چه چیزی از دست می‌رود.

---

## 4. Reproduction

```bash
python tools/find_insert_or_replace.py                     # گزارش خوانا (کد خروج 2 = محل‌های پویا/جزئی)
python tools/find_insert_or_replace.py --md /tmp/ior.md    # جدول Markdown
python tools/find_insert_or_replace.py --json /tmp/ior.json
python tools/find_insert_or_replace.py --no-schema         # بدون ساخت DB موقت
```

نقشه‌ی ستون‌ها در ابزار از یک DB موقت می‌آید (چون ستون‌ها در SQLite با
`ensure_*_columns` به‌صورت **lazy** اضافه می‌شوند و اسکن متنی DDL قابل‌اعتماد نیست):
`init_db()` + `ensure_yield_tables()` + `init_security_audit()` در یک پوشه‌ی موقت، سپس
`PRAGMA table_info` برای ۹ جدول مرتبط.

---

## 5. Link to the next E.0 tasks

- **E.0.5 (except sqlite3.\*):** همین شش محل خطاهایشان با `sqlite3.IntegrityError`
  مدیریت می‌شود؛ فهرست بعدی به آن‌ها ارجاع می‌دهد.
- **E.0.12 (مسیرهای SQL خام):** این سند منبع بخش `INSERT OR REPLACE` آن است.
- **سند `docs/db-adapter.md`:** فهرست قبلی (کوتاه‌تر) را دارد؛ این سند جای آن را
  می‌گیرد و جزئیات ستون‌ها/تصمیم‌ها را اضافه می‌کند.

---

*این سند فقط مستندات است؛ هیچ تغییری در کد، schema یا داده ایجاد نمی‌کند.*
