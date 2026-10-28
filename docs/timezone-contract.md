# Time zone contract (Timezone Contract) — preparation for phase E

**وضعیت:** اصل سند (کار D.2) مستندات محض بود؛ **فاز E.3 این قرارداد را اجرا کرد** —
پیاده‌سازی، مهاجرت داده و شاهدها در **بخش ۹** همین سند ثبت شده است.
**مبنا:** شاخه‌ی `phase-c` پس از کار D.1، تاریخ بررسی: ۲۰۲۶-۱۰-۰۳ (بخش ۹: ۲۰۲۶-۱۰-۰۴).
**دقت:** شماره‌خط‌ها بر مبنای وضعیت **پس از کار D.3** (کامیت `refactor(db)` فاز D) است؛
پیش از آن کار، خطوط `core/database.py` چهار خط کمتر بودند.
**چرا این سند؟** فاز E داده‌ها را به PostgreSQL منتقل می‌کند. اگر معنیِ رشته‌های زمانیِ
موجود (naive local؟ aware؟ UTC؟) روشن نباشد، migration بی‌صدا داده‌ها را جابه‌جا می‌کند.
این سند مرجع تصمیم است: «چرا UTC؟» و «هر رشته‌ی قدیمی چه معنی‌ای دارد؟»

---

## 1. Current state (what the code does today)

| فایل | خط | الگوی فعلی | معنی واقعی مقدار |
|---|---|---|---|
| `core/database.py` (`add_event`) | `658` | `details.get("timestamp", datetime.now().isoformat())` | **naive local** — ساعت محلی سرور بدون offset |
| `core/database.py` (`record_sensor_readings`) | `1128` | `timestamp or datetime.now().isoformat()` | naive local |
| `core/database.py` (`record_toner_snapshot`) | `1176` | `timestamp or datetime.now().isoformat()` | naive local |
| `core/database.py` (`save_printer_counters`) | `1073` | `updated_at = data.get("updated_at") or datetime.now().isoformat()` | naive local (منبعِ زمانی که در `toner_history` می‌نشیند) |
| `core/database.py` (آستانه‌های مقایسه) | `692,731,958,1189` | `(datetime.now() - timedelta(...)).isoformat()` | naive local — مقایسه‌ی رشته‌ای با ستون TEXT |
| `core/yield_engine.py` (`_now`) | `61-62` | `datetime.now().isoformat()` | naive local — تمام snapshotهای Yield Engine |
| `core/store.py` (`PrevStore.set`) | `78` → `140-145` | `updated_at` ساخته و به‌عنوان `timestamp` به `record_toner_snapshot` داده می‌شود | naive local |
| `core/cartridge_change_engine.py` | `272` | `datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")` | **UTC واقعی** — پیش از D.6 ساعت محلی با پسوند جعلی `Z` بود (بخش ۳) |
| `core/cartridge_change_engine.py` | `306` | `datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"` | **UTC واقعی** — داخل کد مرده (بخش ۳)؛ تا D.6 از `utcnow()` استفاده می‌کرد |
| `core/collectors/sensor.py` | `245` | `datetime.now().isoformat()` | naive local (به `sensor_readings` می‌رود) |
| `core/collectors/*.py` (`last_poll`) | `hp.py:297,309`، `canon.py:164,421,431`، `brother.py:257,274`، `sensor.py:189,284` | `datetime.now().isoformat()` | naive local (فقط حالت زنده در حافظه/نمایش) |
| `web/auth.py` (`expires_at`) | `448` (خواندن: `486`) | `(datetime.now() + timedelta(hours=1)).isoformat()` | naive local — **مقایسه‌ی درون‌برنامه‌ای**، نه داده‌ی تاریخی |
| `web/routes/users.py` | `164,207` | `(datetime.now() + timedelta(hours=1)).isoformat()` | naive local (توکن بازنشانی رمز؛ نوشته و خوانده می‌شود) |
| `web/routes/security.py` | `87-88` | `(datetime.now() - timedelta(...)).isoformat()` | naive local — مقایسه با `security_events.timestamp` |
| `web/routes/stats.py` | `16,45,69,98,110` | `datetime.now().date()` / `.isoformat()` | naive local — فقط تاریخ (روز) |
| `web/routes/import_db.py` | `225` | `int(datetime.now().timestamp() * 1000)` | epoch (مطلق) — نام فایل موقت |
| `web/routes/export_bp.py` | `253,261,280` | `datetime.now().strftime(...)` / `.isoformat()` | naive local — نام فایل و سرصفحه‌ی گزارش |
| `web/routes/metrics.py` | `262` | `datetime.now().timestamp()` | epoch (مطلق) — scrape timestamp پرومتئوس |

**جمع‌بندی (پس از D.6):** همه‌ی زمان‌های ذخیره‌شده naive local هستند، به‌جز:
`metrics.py:262` و `import_db.py:225` (epochِ مطلق) و هر دو خط
`cartridge_change_engine.py:272,306` که UTC واقعی‌اند. قالب غالب ISO-8601 بدون
offset است: `2026-10-03T14:22:01.123456`.

---

## 2. Phase E contract

| موضوع | قاعده‌ی فاز E |
|---|---|
| همه‌ی INSERTهای جدید | `datetime.now(timezone.utc).isoformat()` — مثال: `2026-10-03T10:52:01.123456+00:00` |
| رشته‌های قدیمی SQLite در migration | با `Asia/Tehran` تفسیر می‌شوند (naive local = ساعت محلی نصب) |
| ستون‌های زمانی جدید | `TIMESTAMPTZ` (نه `TIMESTAMP`) |
| مقایسه/بازه‌ها | روی نوع زمان واقعی پستگرس؛ نه مقایسه‌ی رشته‌ای TEXT |
| نمایش به کاربر | تبدیل به `Asia/Tehran` در لایه‌ی نمایش (UI فارسی/RTL) |

### معنی دقیق «Asia/Tehran» برای داده‌های موجود
ایران از ۱۴۰۱/۲۰۲۲ (تصمیم مجلس) ساعت تابستانی را کنار گذاشته و اکنون سالانه روی
`+03:30` است؛ ولی داده‌های تاریخی می‌توانند به دوره‌هایی برگردند که DST وجود داشت
(در دوره‌ی DST: `+04:30` تابستان و `+03:30` زمستان). پس migration نباید offset ثابت `+03:30` را hard-code کند؛ باید
از منطقه‌ی زمانی واقعی (`zoneinfo.ZoneInfo("Asia/Tehran")`) استفاده کند تا رشته‌های
تابستانی ۲۰۲۱/۲۰۲۲ درست تفسیر شوند. (کتابخانه‌ی `zoneinfo` از پایتون ۳.۹ در stdlib است؛
روی ویندوزسرور نیاز به دیتای tz دارد → `tzdata` باید به `requirements.txt` اضافه شود.)

---

## 3. Time bug in `cartridge_change_engine.py` — fixed in D.6

> **اصلاح این بخش (کامیت D.6):** نسخه‌ی اول این سند، خطوط ۲۷۲ و ۳۰۶ را «دو شاخه‌ی
> برنددار/غیربرند» توصیف کرده بود. بررسی اجرایی نشان داد آن توصیف نادرست است؛
> شرح دقیق و اصلاح‌شده در ادامه می‌آید.

### ۳.۱ ساختار واقعی ماژول (اثبات‌شده با AST + اجرا)
این ماژول **یک تابع عمومی** دارد: `evaluate_cartridge_change` (خط ۸۳–۳۲۸).
«برنددار» و «غیربرند» دو *مسیر شواهد* داخل همین یک تابع‌اند (chip_id در برابر
شمارنده/سطح/عمر)، نه دو تابع جدا. پس:

| خط | وضعیت واقعی | وضعیت |
|---|---|---|
| `272` | **تنها تولیدکننده‌ی زنده‌ی timestamp** — هر دو مسیر شواهد از همین خط می‌گذرند | اصلاح‌شده در D.6 |
| `306` | داخل **کد مرده**: اولین `return result` در خط ۲۹۴ است و ۳۴ خط بعد (۲۹۵–۳۲۸) هرگز اجرا نمی‌شود | محافظه‌کارانه اصلاح‌شده (خودِ بلوک دست‌نخورده) |

اثبات‌ها:
  • با `TZ=Asia/Tehran`، خروجی زنده `09:56:49Z` بود در حالی که UTC واقعی `06:26:49`
    ⇒ انحراف **۲۱۰ دقیقه (۳:۳۰)**؛ هر دو سناریوی chip_id و شمارنده/سطح/عمر همین
    مقدار را می‌دادند (یعنی هر دو از خط ۲۷۲).
  • اجرای دستی `python -m pytest tests/ -W error::DeprecationWarning` (اندازه‌گیری‌شده
    روی ۲۷۳ تست) هیچ `DeprecationWarning` تولید نکرد ⇒ `datetime.utcnow()` (خط ۳۰۶)
    اجرا نمی‌شود؛ AST هم ۳۴ خط پس از `return` را نشان می‌دهد.

> **تصحیح ادعای `-W error` (E.0.8):** نسخه‌ی پیشین این بند «اجرا با
> `-W error::DeprecationWarning` هیچ هشداری تولید نکرد» را طوری آورده بود که گویی
> یک **تضمین** است؛ در واقع فقط یک اجرای دستی بود و **CI هیچ فلگ سخت‌گیری ندارد**:
>   • `.github/workflows/tests.yml:60` و `:119` تنها `-q --color=no` دارند؛
>     `pytest.ini` هم نه `addopts` دارد و نه `filterwarnings` ⇒ هیچ‌جای CI هشدارها به
>     خطا تبدیل نمی‌شوند؛ پس بازگشت `datetime.utcnow()` (یا هر هشدار جدید) توسط CI
>     گرفته نمی‌شود.
>   • اجرای سراسریِ `-W error` (همه‌ی هشدارها) امروز ممکن نیست: ۲۴ خطای جمع‌آوری از
>     یک `RuntimeWarning` — هشدار `SECRET_KEY` پیش‌فرض ناامن
>     (`config/settings.py`) که هنگام import بالا می‌آید.
>   • تنها هشداری که اجرای کامل تولید می‌کند همان `RuntimeWarning` مذکور است؛ شمار
>     `DeprecationWarning` در کل اجرا **صفر** است. گارد واقعیِ `utcnow()` امروز
>     تست ۳ در `tests/test_cartridge_change_timezone.py` (تحلیل AST) است، نه CI.
>   • **اقدام پیشنهادی (فاز E، نه E.0):** افزودن `-W error::DeprecationWarning` به
>     دستور pytest در CI تا این گارد خودکار شود (`RuntimeWarning` بالا مانع `-W error`
>     سراسری است).

### ۳.۲ مشکل (نسخه‌ی اصلی)
```python
# خط ۲۷۲ — پیش از اصلاح
timestamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%SZ")
# ساعت محلی + پسوند جعلی Z ⇒ کج‌فهمی ۳:۳۰ برای TZ=Asia/Tehran
```

### ۳.۳ اصلاح (کد و تست — کامیت `fix(time)`)
```python
# خط ۲۷۲ — پس از اصلاح
timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
# خط ۳۰۶ — پس از اصلاح (داخل کد مرده، برای جلوگیری از بازگشت باگ در refactor)
timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"
```
دیف: فقط ۲ خط + ۱ import (`from datetime import datetime, timezone`)؛ شماره‌خط‌های
۲۷۲ و ۳۰۶ حفظ شد.

**تأیید فعال بودن اصلاح (E.0.8):** در نسخه‌ی فعلی مخزن هر دو خط ۲۷۲ و ۳۰۶
`datetime.now(timezone.utc)` دارند و شمارش `utcnow(` در `core/`، `web/`،
`config/`، `create_admin.py` و `tools/` **صفر** است (تنها ارجاع‌ها در `tests/` به‌عنوان
مرجع انتظار و در `golden_harness.py` برای وصله‌کردن کلاس).

### ۳.۴ گاردِ رگرسیون
`tests/test_cartridge_change_timezone.py` (۳ تست):
  ۱) مسیر chip_id و ۲) مسیر شمارنده/سطح/عمر: `Z` + اختلاف <۱۰s با UTC + ≠ ساعت محلی.
  ۳) گارد AST: نه `utcnow(`، نه `.astimezone()`، هر `datetime.now` با `timezone.utc`،
     و پیوستنِ خطوط ۲۷۲ و ۳۰۶ به فهرست فراخوانی‌ها.

اثبات غیر‌توخالی‌بودن: روی کد قدیمی هر ۳ تست FAILED، روی کد اصلاح‌شده PASSED.
اثبات «فقط timestamp تغییر کرده»: خروجی ۹ سناریو با ساعتِ قفل‌شده بایت‌به‌بایت
یکسان است؛ بدون قفل، پس از حذف `timestamp`/`event_id` صفر تفاوت می‌ماند.

### ۳.۵ پیامد برای migration فاز E
داده‌های تاریخی `CARTRIDGE_CHANGED` که با خط ۲۷۲ نوشته شده‌اند، برچسب `Z` دارند
ولی مقدارشان **ساعت محلی** است. در migration نباید این‌ها را UTC فرض کرد؛ راهبرد
پیشنهادی: هر رویداد `CARTRIDGE_CHANGED` با `Z` که timestamp آن پیش از کامیت D.6
است، با `ZoneInfo("Asia/Tehran")` تفسیر و سپس به `TIMESTAMPTZ` تبدیل شود
(تفکیک از داده‌های پس از D.6 که UTC واقعی‌اند).

**نکته:** `datetime.utcnow()` از ۳.۱۲ deprecated است؛ اکنون هیچ نقطه‌ای در این
ماژول از آن استفاده نمی‌کند.

---

## 4. What naive local time gets right today and what must be watched in phase E

۱. **مقایسه‌ی رشته‌ای:** `cutoff = (datetime.now() - timedelta(...)).isoformat()` و سپس
   `WHERE timestamp >= ?` روی ستون TEXT. این فقط تا وقتی کار می‌کند که همه‌ی نویسنده‌ها
   یک قالب (naive local با فرمت یکسان) بنویسند. به‌محض مخلوط‌شدن UTC و local
   (مثل دو خط پاورقی‌شده‌ی بالا)، نتیجه بی‌صدا غلط می‌شود.
۲. **آستانه‌های debounce** (`core/database.py:692,731`): اگر داده‌ی قدیمی local باشد و
   زمان جاری UTC، اختلاف ۳.۵ ساعت باعث می‌شود debounce رویدادهای تکراری از کار بیفتد.
۳. **token expiry** (`web/auth.py:448`، `web/routes/users.py:164,207`): این‌ها **در حافظه
   مقایسه می‌شوند** (نوشتن و خواندن در همان پروسه). اگر فقط یکی از دو طرف UTC شود،
   توکن یا فوراً منقضی می‌شود یا ساعت‌ها اضافه می‌ماند. در فاز E هر دو طرف باید
   با هم عوض شوند.
۴. **`toner_history` و `sensor_readings`** (کار ۵ فاز C): همان naive local را دارند؛
   در migration با `Asia/Tehran` تفسیر می‌شوند. `printer_counters.updated_at` هم که
   منبع زمانِ snapshotهای تونر است، naive local است.

---

## 5. Why UTC (short answer for the future)

- **PostgreSQL `TIMESTAMPTZ`** مقدار را به UTC نرمال می‌کند؛ نوشتن naive local در آن،
  بی‌صدا ۳.۵ ساعت جابه‌جایی ایجاد می‌کند.
- **مقایسه و بازه‌ها** روی UTC بی‌ابهام است: ممیزی لاگ/شمارنده (کار ۵ فاز C) بین
  «خاموشی سرور» و «مصرف واقعی» تفکیک می‌کند؛ مبنای زمانی مخلوط این تحلیل را خراب می‌کند.
- **پشتیبان‌گیری و بازیابی** بین نصب‌های مختلف (سرور تهران در برابر هر TZ دیگر) قابل
  انتقال می‌شود.
- **پیش‌نیاز خروجی‌ها:** SNMP Exporter و داشبورد مستقل (هدف بلندمدت) روی UTC کار
  می‌کنند؛ تبدیل به `Asia/Tehran` فقط در لایه‌ی نمایش انجام می‌شود.

---

## 6. Phase E work that comes out of this document (list, not executed)

۱. حذف `naive local` از نویسنده‌ها: `core/database.py` (`658,1073,1128,1176`)،
   `core/yield_engine.py:61-62`، `core/collectors/sensor.py:245` و `last_poll`ها.
۲. ~~یکسان‌سازی `core/cartridge_change_engine.py:272` با `:306`~~ — **انجام شد در D.6**
   (هر دو `datetime.now(timezone.utc)`؛ گارد AST در
   `tests/test_cartridge_change_timezone.py`).
   باقی‌مانده برای فاز E: تفکیک داده‌های تاریخی `CARTRIDGE_CHANGED` (بخش ۳.۵).
۳. افزودن `tzdata` به `requirements.txt` برای ویندوزسرور.
۴. migration داده — قاعده‌ی تفکیکی (اصلاح‌شده در گام E.0.2؛ بخش ۸.۳) —
   **پیاده‌شده در E.3.2 و اجراشده روی `logs.db` در E.3.5 (بخش ۹.۴):**
   · naive بدون `Z`/offset ⇒ `ZoneInfo("Asia/Tehran")` ⇒ `TIMESTAMPTZ`.
   · `Z` **پیش از cutover** ⇒ برچسب جعلی بود؛ مقدار در واقع ساعت محلی است
     ⇒ `ZoneInfo("Asia/Tehran")`.
   · `Z` **پس از cutover** ⇒ UTC واقعی ⇒ همان‌طور.
   · offset صریح (`+03:30` و…) ⇒ بازبینی دستی.
   ¬ نسخه‌ی پیشین این بند می‌گفت «رشته‌های دارای `Z` دست‌نخورده» که با بخش ۳.۵
     در تناقض بود؛ در گام E.0.2 رفع شد (بخش ۸).
۵. تست: goldenهای موجود (۱۰/۱۰) باید همچنان تفاضل-صفر بمانند؛ اگر تغییری لازم شد،
   یعنی مقداری عوض شده که باید صریح تأیید شود.
۶. توکن‌های انقضا (`auth.py`/`users.py`) هم‌زمان با هم به UTC منتقل شوند.

---

## 7. Verifying the server time zone (step E.0.1)

بخش ۲ این سند فرض می‌کند رشته‌های naive local با `Asia/Tehran` تفسیر می‌شوند.
تا وقتی این فرض روی خودِ سرور ویندوزی سنجیده نشود، «فرض» است نه «واقعیت» — و
معنی همه‌ی داده‌های تاریخی به آن گره خورده است. این بخش، ابزار سنجش و وضعیت
تأیید را ثبت می‌کند.

### ۷.۱ ابزار سنجش

`tools/verify_server_timezone.py` — فقط خواندن، بدون هیچ وابستگی خارجی، سازگار
با ویندوزسرور (بدون نیاز به Linux/Docker/WSL):

| چه چیزی | چگونه | چرا مهم است |
|---|---|---|
| پلتفرم و نسخه‌ی پایتون | `platform` / `sys.version` | انتخاب مسیر تشخیصی و سازگاری `zoneinfo` |
| TZ سیستم (ویندوز) | `tzutil /g` و `Get-TimeZone` (PowerShell) | منبع واقعی TZ سرویس |
| نگاشت به IANA | جدول کوچک داخلی (`Iran Standard Time → Asia/Tehran`) | مغزِ تشخیص «ایران بودن» |
| آفست واقعی پروسه | `datetime.now()` در برابر `datetime.now(timezone.utc)` | تأیید عددی +03:30 (۲۱۰ دقیقه) |
| DST | `time.daylight` / `tm_isdst` / `SupportsDaylightSavingTime` | ایران از ۲۰۲۲ DST ندارد؛ `True` یعنی TZ دیگری |
| متغیر محیطی `TZ` | `os.environ` | TZ محیطی می‌تواند TZ سیستم را بازنویسی کند |
| `ZoneInfo("Asia/Tehran")` | آزمون واقعی + `find_spec("tzdata")` | پیش‌نیاز migration فاز F (بند ۳ بخش ۶) |
| هم‌گام‌سازی ساعت | `w32tm /query /status` | برای داده‌های naive، ساعت درست به‌اندازه‌ی TZ مهم است |
| شواهد DB (اختیاری) | `--db` با URI `mode=ro`؛ ردیف‌های تازه‌ی `logs` | تأیید تجربی «naive = ساعت محلی» |

هر خطای اجرایی (نبود دستور، نبود DB، کنسول غیر UTF-8) مدیریت شده است؛ ابزار در
بدترین حالت «نامشخص» می‌گوید و نمی‌ترکد.

```bat
python tools/verify_server_timezone.py --db C:\printguard\logs.db --json tz-report.json
```

کدهای خروج: `0` = روشن و سازگار با ایران، `2` = نیاز به تأیید دستی (TZ غیرایران،
tzdata نبود، شواهد تازه‌ی کافی نبود)، `1` = خطای اجرایی.

### ۷.۲ دو اجرای مرجع (صحت ابزار)

ابزار روی همین سندباکس در دو TZ اجرا و نتیجه‌اش با انتظار مقایسه شد:

| اجرا | TZ سیستم | آفست پروسه | حکم |
|---|---|---|---|
| `TZ=Asia/Tehran` | `Etc/UTC` (سندباکس) | `UTC+03:30` | `[OK]` ایران ⇒ تفسیر با `Asia/Tehran` درست است |
| `TZ=UTC` | `Etc/UTC` | `UTC+00:00` | `[WARN]` رشته‌های naive در واقع UTC‌اند ⇒ تفسیر با `Asia/Tehran` ۳:۳۰ خطا می‌دهد |

مسیر ویندوزی نیز با `tzutil`/PowerShell شبیه‌سازی‌شده آزموده شد: پارس درست
`Iran Standard Time`، نگاشت به `Asia/Tehran`، تشخیص DST خاموش، و مقاوم بودن در
برابر «دستور بی‌جواب» و «TZ ناشناخته».

> نکته: در هر دو اجرا کد خروج `2` بود، چون DB نمونه هیچ ردیف تازه‌ای (در محدوده‌ی
> ۳ ساعت) نداشت. `2` یعنی «نیاز به تأیید دستی»، نه خطا.

### ۷.۳ ⚠ وضعیت فعلی: تأیید سرور انجام نشده (نیاز به تأیید دستی)

`logs.db` موجود در مخزن هیچ رویداد واقعی‌ای ندارد که TZ را بازگو کند: ۷ ردیف،
همه بدون `Z`، همه با IP `127.0.0.1`، همه در `۲۰۲۶-۱۰-۰۱` (PRINT/REFILL/STATUS)
— و هیچ `CARTRIDGE_CHANGED` واقعی. پس **هیچ راهی برای تأیید TZ سرور از داخل این
مخزن وجود ندارد**؛ ابزار باید روی خود سرور اجرا و خروجی JSON آن در همین بخش
ثبت شود (گزارش در `docs/` نگه داشته شود یا محتوایش اینجا درج شود).

تا آن زمان، هر جا در این سند از «TZ سرور» صحبت شده، یعنی **فرض Asia/Tehran**.

### ۷.۴ حکم‌ها و اقدام لازم

| نتیجه‌ی ابزار روی سرور | معنی | اقدام |
|---|---|---|
| آفست `+03:30`، بدون DST | فرض بخش ۲ درست است | migration فاز F بی‌تبدیل از `Asia/Tehran` استفاده می‌کند |
| آفست `+00:00` (یا هر TZ دیگر) | رشته‌های naive local در واقع UTC (یا TZ دیگری) هستند | نشانه‌ی cutover (گام E.0.13) و قاعده‌ی migration باید بر همین TZ نوشته شود؛ تفسیر با `Asia/Tehran` ممنوع |
| `ZoneInfo("Asia/Tehran")` کار نمی‌کند | روی ویندوز بدون دیتابیس TZ | افزودن `tzdata` به `requirements.txt` (بند ۳ بخش ۶) |
| ساعت با NTP هم‌گام نیست | زمان‌های ثبت‌شده قابل اعتماد نیستند | اول هم‌گام‌سازی ساعت، بعد ادامه‌ی کار |

### ۷.۵ قیدهای ناشی از این گام

۱. **TZ سرور را در حین migration عوض نکنید.** رشته‌های naive local معنی‌شان به
   TZ همان لحظهٔ نوشتن گره خورده است؛ تغییر TZ، داده‌های قدیمی را بی‌صدا جابه‌جا
   می‌کند.
۲. تغییر TZ محیطی (`TZ`) توسط سرویس/تسک، همان اثر را دارد؛ ابزار این متغیر را
   علامت می‌زند.
۳. TZ سرور یک **ورودی** برای گام E.0.13 (نشانه‌ی cutover) و سند سخت‌گیری
   PostgreSQL است، نه چیزی که در فاز E.0 تغییر کند.

---

## 8. Analysis of historical data and resolution of the `Z` inconsistency (step E.0.2)

### ۸.۱ تناقض چه بود

| سند/بند | ادعا |
|---|---|
| بخش ۳.۵ | ردیف‌های `CARTRIDGE_CHANGED` با `Z` که **پیش از D.6** نوشته شده‌اند، مقدارشان ساعت محلی بوده (باگ ۲۱۰ دقیقه‌ای) ⇒ باید با `Asia/Tehran` تفسیر شوند |
| بند ۴ بخش ۶ (نسخه‌ی پیشین) | «رشته‌های دارای `Z` و offsetها دست‌نخورده» ⇒ یعنی `Z` را UTC فرض کن |

این دو با هم جمع نمی‌شدند: بخشی از `Z`های تاریخی UTC واقعی‌اند (پس از D.6) و بخشی
محلیِ جعلی (پیش از D.6). تفاوت این دو، ۳ ساعت و ۳۰ دقیقه در migration است.
**رفع:** بند ۴ با قاعده‌ی تفکیکی بازنویسی شد (۸.۳) و ابزار سنجش اضافه شد (۸.۲).

### ۸.۲ ابزار سنجش

`tools/analyze_old_events.py` — فقط خواندن (اتصال `mode=ro`)، سازگار با
ویندوزسرور، بدون وابستگی خارجی. هر مقدار زمانی را در یکی از این دسته‌ها
می‌گذارد:

| دسته | معنی | رنگ |
|---|---|---|
| `naive_local` | ساعت محلی (Asia/Tehran) | ✓ روشن |
| `z_before_cutover` | برچسب `Z` جعلی؛ مقدار در واقع ساعت محلی است | ⚠ |
| `z_after_cutover` | UTC واقعی | ✓ روشن |
| `z_cutover_unknown` | `Z` دیده شد ولی cutover نامعلوم است | ⚠ تصمیم دستی |
| `offset_aware` | offset صریح (مثلاً `+03:30`) ⇒ مبهم | ⚠ بازبینی دستی |
| `unparsable` | غیرقابل تجزیه | ⚠ بازبینی دستی |

جدول‌های پوشش‌داده‌شده: `logs` (به تفکیک `type`)، `security_events`،
`toner_history`، `sensor_readings`، `printer_counters`. ستون `cutover` از
`--cutover` گرفته می‌شود؛ اگر داده نشود، ابزار **تقریبِ** زمان کامیت `b548e99`
را از git می‌خواند و صریح هشدار می‌دهد که ملاک واقعی، لحظهٔ فعال‌شدن نسخهٔ
اصلاح‌شده روی همان سرور است، نه زمان کامیت.

کدهای خروج: `0` روشن، `2` نیاز به تصمیم دستی، `1` خطای اجرایی.

```bat
python tools/analyze_old_events.py --db C:\printguard\logs.db --cutover 2026-10-05T08:00:00+00:00 --json old-events.json
```

### ۸.۳ قاعده‌ی نهایی تفسیر (جایگزین بند ۴ بخش ۶)

| مقدار تاریخی | تفسیر در migration |
|---|---|
| `2026-10-01T08:55:46.673827` (naive) | `ZoneInfo("Asia/Tehran")` ⇒ `TIMESTAMPTZ` |
| `2026-10-02T06:00:00Z` و **پیش از** cutover | برچسب جعلی ⇒ `ZoneInfo("Asia/Tehran")` |
| `2026-10-03T07:30:00Z` و **پس از** cutover | UTC واقعی ⇒ همان‌طور |
| `2026-10-03T11:00:00+03:30` | بازبینی دستی (محلیِ aware یا UTC+3:30؟) |
| `+00:00` صریح (فرمت `isoformat` پایتون) | UTC واقعی ⇒ همان‌طور |

### ۸.۴ نتیجهٔ اجرا روی DB موجود

`logs.db` مخزن: ۷ ردیف `logs` همه `naive_local` (REFILL=۴، PRINT=۲، STATUS=۱) +
۷ ردیف `security_events`، ۸ ردیف `toner_history`، ۱ `printer_counters` — همه
naive؛ `sensor_readings` خالی. ⇒ **در این DB هیچ ردیف `Z` وجود ندارد؛ تناقض `Z`
اینجا صفر است.** ولی همین ابزار باید روی DB واقعی سرور اجرا شود، چون:
۱. DB مخزن هیچ `CARTRIDGE_CHANGED` واقعی ندارد؛
۲. تا وقتی نسخهٔ اصلاح‌شده روی سرور فعال نشود، cutover واقعی هم وجود ندارد.

صحت ابزار با یک DB ساختگی (خارج از مخزن، سپس حذف‌شده) آزموده شد: ۲ ردیف `Z`
پیش از cutover، ۱ ردیف پس از آن، ۱ ردیف `offset`دار، ۱ ردیف `unparsable` ⇒
دسته‌بندی و کد خروج `2` همان‌طور که انتظار می‌رفت. حالت «بدون cutover» هم آزموده
شد: هر سه ردیف `Z` به `z_cutover_unknown` رفتند.

### ۸.۵ پیوند با گام‌های بعد

- **گام E.0.13 (نشانهٔ cutover):** تا وقتی نشانه‌ی cutover در DB نوشته نشود،
  رد‌یابی `Z`ها فقط با زمان کامیت/استقرار ممکن است؛ پس این ابزار به آن گام
  ورودی می‌دهد.
- **فاز F:** همین قاعده‌ی جدول ۸.۳ باید عیناً در اسکریپت migration پیاده شود و
  تعداد ردیف‌های هر دسته‌ی این ابزار، معیار پذیرش migration است (هر ردیف باید
  درست تفسیر شود).

---

## 9. Execution of phase E.3 — implemented contract (2026-10-04)

این بخش «چه چیزی واقعاً اجرا شد» است؛ بخش‌های ۱ تا ۸ تصمیم‌های پیش از اجرا بودند.

### ۹.۱ فرمت قطعی و تنها منبع

   2026-10-03T12:00:00.123456Z      (طول ثابت ۲۷ = ۲۶ کاراکتر + Z)

- تنها تولیدکننده: `core/time_utils.py::utc_now_iso()` — همیشه UTC، همیشه `Z`،
  همیشه ۶ رقم میکروثانیه ⇒ **مقایسه‌ی رشته‌ای در SQL و پایتون == ترتیب زمانی**.
- هِلپرهای همراه: `to_utc_z` (aware ⇒ رشته‌ی استاندارد؛ naive ⇒ `ValueError`)،
  `parse_utc` (سخت‌گیر: naive را رد می‌کند)، `to_local_str`/`local_tz`
  (نمایش Asia/Tehran)، `as_utc_datetime`، `to_filter_bound` (مرز فیلتر از ورودی
  کاربر)، `to_local_day` (سبد روزِ محلی)، `to_sort_key` (مرتب‌سازی زمانی).
- `tzdata>=2024.1` افزوده شد (بند ۳ فهرست) تا `Asia/Tehran` روی ویندوزسرور کار کند.
- `to_utc_z` اعتبارسنجی `isinstance` را با alias زمان-import انجام می‌دهد
  (`_DATETIME_BASE`) تا وصله‌کردن کلاس `datetime` در تست‌ها آن را نشکند.

### ۹.۲ نویسنده‌ها (E.3.3 — `d806324`)

همه‌ی مسیرهای نوشتنِ زمان به `utc_now_iso()` منتقل شدند: `core/database.py`
(۱۴ مورد: `logs`, `printer_counters.updated_at`, `sensor_readings`,
`toner_history`, `users.*` ×۹ و توکن‌ها)، `core/store.py`, `core/yield_engine.py`,
`core/security_audit.py`, `core/collectors/{sensor,base,hp,canon,brother,toshiba,
cartridge_discovery}.py`, `core/poller.py`, `core/cartridge_change_engine.py`،
`core/oid/scanner.py`, `core/groups.py`, `core/enhanced_collector.py`,
`web/auth.py`, `web/routes/{users,export_bp}.py`.
نام فایل‌های خروجی با `datetime.now(timezone.utc)` ساخته می‌شوند (نه `utc_now_iso`،
چون `:` در نام فایل مجاز نیست). خطوط لاگِ متنی و گزارش انسانی عمداً naive ماندند.

### ۹.۳ خواننده‌ها (E.3.4 — `9902c9b`)

- ۵ debounce/dedupe + prune + toner-history cutoff: `to_utc_z(datetime.now(timezone.utc) - …)`
  (هم‌فرمت با داده ⇒ مقایسه‌ی رشته‌ای درست).
- فیلتر بازه‌ی `/api/logs/all` و `get_recent_events`: `to_filter_bound`
  (آگاه ⇒ UTC/Z؛ naive/فقط‌تاریخ ⇒ محلی؛ نامعتبر ⇒ بدون تغییر + هشدار).
- **گروه‌بندی روزانه:** `substr(timestamp,1,10)` از SQL حذف شد و به پایتون منتقل شد
  (`to_local_day`)؛ `AVG`/`SUM` هم در پایتون با شبیه‌سازی تبدیل ضمنی SQLite
  (متن غیرعددی ⇒ ۰) و برگرداندن عدد صحیح برای جمع‌های صحیح.
- اختلاف ساعت: `datetime.now(timezone.utc) - parse_utc(...)` در `core/store.py` و
  `core/collectors/base.py`؛ ردیف مهاجرت‌نشده ⇒ همان fallback قبلی (بدون حدس).
- نمایش: `dashboard.js` (۲ نقطه، تبدیل به ساعت محلی تهران با `Intl`)،
  `export_bp.py` (۴ خروجی CSV/Excel + برچسب منطقه در سرستون)، `security.html`
  (تجزیه‌ی قطعی میکروثانیه). `_row_to_dict` هم صریح شد: ستون بر `details` مقدم است.

### ۹.۴ مهاجرت داده (E.3.2 `05446c1`، E.3.5 `46b193f`)

- ابزار: `tools/migrate_timestamps_to_utc.py` (قاعده‌ی بخش ۸.۳؛ `--dry-run`،
  `--manifest`، بکاپ، یک تراکنش، idempotent؛ ۲۰ تست در `tests/test_timestamp_migration.py`).
- روی `logs.db` واقعی: `rows=91 converted=106 skipped=0 needs_decision=0`
  (۹ جدول)، بکاپ `logs.db-20261004-045417.e3bak` و بکاپ دستی
  `logs.db.bak-pre-e3` (بایت‌به‌بایت برابر؛ `cmp` تأیید شد).
- بازبینی پس از اجرا: `meta.value` دست‌نخورده، شمار ردیف‌ها بدون تغییر، ۱۰۶ سلول
  هم‌فرمت استاندارد، صفر باقی‌ماندهٔ naive، اجرای دوم `converted=0`
  (idempotent). شاهد کامل در پیام کامیت `46b193f`.
- **نقطهٔ کور شناخته‌شده (پذیرفته‌شده):** ردیف جعلیِ نوشته‌شده در ۳ ساعت و ۳۰ دقیقهٔ
  پیش از cutover «UTC واقعی» خوانده می‌شود (چون مقدار جعلی `Z` = ساعت محلی، یعنی
  بزرگ‌تر از cutover). در `logs.db` این مخزن **صفر** ردیف `Z` وجود داشت ⇒ اثر عملی
  ندارد؛ برای سرور مالک، `tools/analyze_old_events.py` پیش از مهاجرت اجرا شود.

### ۹.۵ بازضبط golden و قفل ساعت (E.3.6 `5f05173`، E.3.7 `4791315`، TD-003 `f5deb7a`)

- `tests/golden_harness.py` حالا `utc_now_iso` را در **هر ماژولی که نام را بایند
  کرده** وصله می‌کند (مقدار `FROZEN_UTC_Z`)، و کلاس `datetime` داخل خود
  `core/time_utils` را عمداً وصله **نمی‌کند** (تله‌ی `isinstance` در ضبط لو رفت).
- ۱۰ fixture بازضبط شد: ۴۲ برچسب `<TS_LOCAL>` ⇒ `<TS_UTC>`؛ تفاوت ساختاری فقط در
  بخش‌های عمداً خامِ `input.db_seed`/`input.prev` (همان لحظه، فرمت درست).
- قطعیت: ضبط دوباره در دو TZ (`UTC` و `Asia/Tehran`) ⇒ بایت‌به‌بایت یکسان.
- TD-003 (نرمال‌ساز `N/A`) رفع و ۷ فیلد در ۵ fixture به مقدار واقعی برگشت.

### ۹.۶ وضعیت تست و ابزار

- `pytest -q` کامل: **۳۶۰ passed، ۰ failed، ۰ skipped** در هر دو TZ، با PG فعال
  (`PG_DSN=… user=printguard dbname=printguard`). بدون `PG_DSN` همان مجموعه منهای
  ۹ تست قرارداد PG.
- `pyflakes core/ web/`: همان ۳۶ هشدار پیش‌موجود؛ هیچ هشدار جدیدی از E.3 اضافه نشد.

### ۹.۷ باقی‌مانده‌ها (بیرون از دامنهٔ E.3، ثبت‌شده در `docs/tech-debt.md`)

| مورد | چرا بیرون ماند |
|---|---|
| `web/routes/security.py:135` — `log` تعریف‌نشده در مسیر خطا | باگ قبلی (نه مربوط به زمان)؛ TD-004 |
| بازنویسی manifest ابزار مهاجرت در اجرای بعدی | TD-005؛ فعلاً با هش + بکاپ + دستور بازتولید پوشش داده شد |
| `sensor_alert.datetime.now().hour` (ساعات سکوت) | تصمیم «ساعت کاری» است نه timestamp ذخیره‌شده؛ TD-006 |
| TD-001 (کد مردهٔ `_save_counters_to_db`) و TD-002 (`next` در ۴۰۱) | طبق `docs/phase-e-plan.md` در E.4/بازبینی auth |

*این سند فقط مستندات است؛ هیچ تغییری در کد، schema یا داده ایجاد نمی‌کند.*
