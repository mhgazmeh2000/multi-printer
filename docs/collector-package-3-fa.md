# بستهٔ سوم: هویت دستگاه، شواهد تعویض کارتریج و override

## دامنه و محدودیت ایمنی

این بسته Collector، انتقال شواهد به Yield Engine و تست‌های آن را اصلاح می‌کند. schema، migration، وابستگی‌ها، تنظیمات PostgreSQL و الگوریتم آماری v2 تغییر نمی‌کنند. هیچ فرمانی روی سرور یا دیتابیس عملیاتی اجرا نشده است. تست‌های جدید در SQLite موقت با DSN خالی و I/O دستگاه mock‌شده اجرا می‌شوند؛ خود Collector، PrevStore، رویدادها و Yield Engine واقعی‌اند.

دادهٔ تاریخی پاک یا بازنویسی نمی‌شود. اگر reset اشتباه قبلاً ظرفیت آموخته‌شده را از بین برده، این تغییر آن را خودکار بازسازی نمی‌کند؛ هدف جلوگیری از تکرار است. ظرفیت یادگرفته‌شدهٔ همان مدل کارتریج در تعویض فیزیکی معتبر طبق رفتار موجود حفظ می‌شود، اما anchor چرخهٔ مصرف جدید می‌شود.

## اصلاح هویت

- رفت‌وبرگشت مدل `HP LaserJet MFP M227fdn` به نام شبکهٔ `NPI9E8319` با همان `Black Cartridge HP CF230A` دیگر تعویض کارتریج نیست. الگوی دقیق و مستقل از بزرگی حروف `NPI` و شش رقم hexadecimal، نام شبکه/placeholder محسوب می‌شود؛ هویت واقعی ذخیره‌شده حفظ می‌شود.
- اگر اولین مشاهده فقط نام شبکه داشته باشد، آمدن مدل واقعی هویت را تکمیل می‌کند، بدون reset چرخه.
- نام‌های عمومی `Toner` و `Cartridge` مانند نام رنگ، هویت قطعهٔ جدید محسوب نمی‌شوند.
- تغییر واقعی مدل/نام قطعه همچنان مسیر تعویض قبلی را دارد؛ تمام مدل‌ها به هویت قبلی قفل نشده‌اند. ثبت refill دستی نیز همان تشخیص placeholder را به کار می‌برد.

## شواهد تعویض و ترتیب اجرا

مقایسه قبل از تغییر PrevStore توسط `_counters_event` و از فیلدهای نرمال‌شدهٔ `cartridge_ids`، `cart_supply_pages` و `cartridge_state` انجام می‌شود؛ نه کلیدهای JSON خام دیتابیس.

شواهد به تفکیک رنگ:

1. تغییر شناسهٔ واقعی با کیفیت `genuine`، بدون تغییر منبع کیفیت شناخته‌شده یا ارتقا از پیشوند ثابت/نسلی به serial.
2. افزایش generation در همان namespace؛ مثلاً `toner_gen:2` به `toner_gen:3`. کاهش/reset یا تغییر namespace کافی نیست.
3. سقوط صفحات کارتریج به کمتر از نصف، با مقدار قبلی حداقل ۱۰۰، تنها با شمارندهٔ کلِ معلوم و غیرکاهشی و بدون کاهش uptime معلوم. مقدار نامعتبر، منفی، bool یا اعشاری شاهد محسوب نمی‌شود.

تغییر نام دستگاه، پرش سطح، تغییر کد ثابت قطعه و خطای خواندن شناسه به‌تنهایی این سیگنال سخت‌افزاری را ایجاد نمی‌کنند. تشخیص‌های legacy رویدادها خارج از این بسته بازطراحی نشده‌اند.

خواندن identity/uptime جابه‌جا یا تکرار نمی‌شود؛ اعمال override و ساخت هشدار به بعد از خواندن موجود identity منتقل شده است. بنابراین پاسخ و هشدار همان poll، سطح کارتریج جدید را می‌بینند. گزارش تشخیصی `toner_report.txt` در محل قبلی باقی است و اکنون پیش از override، سطح خوانده‌شده را ثبت می‌کند؛ مقدار API/UI بعد از اعمال override است.

## سیاست آزادسازی override

- تنها override رنگی که شواهد تعویض دارد آزاد می‌شود؛ سایر رنگ‌ها حفظ می‌شوند.
- بدون شواهد تعویض، قواعد قبلی بازیابی طبیعی سنسور/quarantine تغییری نکرده‌اند.
- پاک‌سازی هم نقشهٔ چندرنگ، هم کلیدهای legacy و هم snapshot ورودی به `_counters_event` را هماهنگ می‌کند؛ override حذف‌شده در poll بعدی برنمی‌گردد.

**تصمیم مهم:** آزادسازی override قدیمی در تعویض اثبات‌شده به وجود سطح عددی جدید وابسته نیست. وابستگی به سطح معتبر باعث می‌شد سیگنال یک‌بارهٔ تعویض مصرف شود ولی برآورد کارتریج قبلی برای همیشه باقی بماند. اکنون در نبود سنسور، override قدیمی بازنشسته و سطح جدید نامعلوم می‌شود؛ anchor قبلی هم کنار می‌رود و اولین خوانش واقعی بعدی anchor جدید را می‌سازد. مقدار نمایشی/کد وضعیت نیز anchor یا نمونهٔ یادگیری نمی‌شود. اگر ردیف آن رنگ موقتاً نباشد، شواهد تعویض همچنان به موتور منتقل می‌شود، بدون افزودن ردیف نمایش برای آن رنگ.

این تغییر به معنی امکان مشاهدهٔ «نامعلوم» پس از تعویض با chip ناسازگار است؛ نمایش عدد قدیمی به جای آن عمداً ممنوع است.

## رنگ و سازنده

- OID سازنده فقط با enterprise دقیق یا ادامهٔ نقطه‌دار آن تطبیق دارد. `...11` دیگر `...1150` را HP نمی‌گیرد، و `...1129`، `...11290` را Toshiba نمی‌گیرد. نقطهٔ اول OID پذیرفته می‌شود.
- رنگ کدهای فشردهٔ Toshiba مثل `T-FC415C`، `T-FC415M`، `T-FC505Y` و `T-FC338EK` شناخته می‌شود؛ الگوهای جداشدهٔ قبلی حفظ‌اند.
- دستگاهی که رنگی تشخیص/تنظیم/ذخیره شده با جدول ناقص mono نمی‌شود.
- ردیف تونر با رنگ ناشناخته روی دستگاه رنگی یا چندمصرفی نادیده گرفته می‌شود و black را overwrite نمی‌کند. fallback قدیمی black فقط برای دستگاه mono با یک ردیف غیر-drum مجاز است.

## آزمون و بازتولید

در checkout آزمایشی جدا از برنامه و دیتابیس عملیاتی:

```text
python -m unittest -v tests.test_collector_identity
python -m unittest tests.test_yield_reconciliation_multibrand tests.test_yield_engine tests.test_yield_estimator_v2 tests.test_toner_override_per_color tests.test_frozen_chip_and_reset_reason tests.test_brand_supplies
python -m pytest tests/test_golden.py -q
python tools/check_test_split.py
```

نتیجهٔ محلی:

- ۲۰ تست جدید: پاس؛ شامل چند poll با reload واقعی PrevStore، alias دوطرفه و اولیه، تغییر واقعی قطعه، serial جدید با همان نام، افت صفحات، نبود/کد وضعیت سنسور، نبود ردیف رنگ، خطای identity، عدم بازگشت legacy override، رنگ‌ها، OID و سازگاری v2.
- مجموعهٔ مرتبط yield/cartridge/override/brand/frozen-chip به‌همراه تست‌های جدید: ۲۳۴ تست، همگی پاس.
- کنترل منفی روی archive نسخهٔ پایهٔ `bccfbde`: سه تست اصلی alias، serial و سنسور ناموجود هر سه شکست خوردند؛ همان تست‌ها با اصلاح پاس شدند.
- golden مستقل: ۱۱ تست شامل inventory و هر ۱۰ fixture موجود، پاس؛ fixtureها تغییر نکرده‌اند و توالی SNMP/HTTP هم بررسی شده است. برای اثبات baseline از `pytest tests/test_golden.py` استفاده شد، نه فقط recorder؛ `tools/record_golden.py --check` ابتدا fixture را بازتولید می‌کند و به‌تنهایی اثبات عدم تغییر baseline نیست.
- مسیر pytest-only: ۳۳ تست پاس. اجرای کامل unittest با ۱۸ تست جدید اولیه: ۹۷۸ تست، ۸ failure، ۲ error و ۷۵ skip؛ نام خطاها همان موارد قبلی backup_pg، cutover و انتظارهای migration/schema است. دو تست نهایی افزوده‌شده در مجموعهٔ مرتبط ۲۳۴تایی پاس شدند. ادعای سبز بودن کامل suite محلی نداریم.
- parity مسیرهای Python: ۹۸۰ مورد، بدون حذف/تکرار.
- `git diff --check`: پاس. نتیجهٔ CI جداگانه در PR قابل مشاهده است.

## انتشار و بررسی عملیاتی

پس از review، CI و merge به main، در PowerShell فعلی مخزن روی سرور، ابتدا برنامهٔ قبلی را در ترمینال خودش با Ctrl+C متوقف کنید. سپس:

```powershell
$ErrorActionPreference = 'Stop'
$branch = (git branch --show-current).Trim()
if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') { throw 'Expected server checkout on main; stopped.' }
$dirty = git status --porcelain
if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'Working tree is not clean; stopped without overwriting local files.' }
git fetch origin
if ($LASTEXITCODE -ne 0) { throw 'git fetch failed' }
git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) { throw 'Fast-forward update failed; no reset performed.' }
git log -1 --oneline
.\start.bat
```

نیازی به migration، reset کارتریج، پاک‌کردن history، تغییر `.env`، npm یا نصب dependency جدید نیست. این دستورات فقط پس از merge بسته کاربرد دارند؛ قبل از merge روی main این اصلاح را دریافت نمی‌کنید.

پس از شروع، دو چرخهٔ عادی polling را بررسی کنید:

- دستگاه `172.16.32.43`: رفت‌وبرگشت نام شبکه نباید پیام reset هویت/ظرفیت ایجاد کند.
- عدد capacity/sample و anchor فقط به علت alias صفر یا جابه‌جا نشود.
- رنگ‌های Toshiba و black مستقل بمانند؛ unknown به black تبدیل نشود.
- بدون تعویض واقعی، override دستی نباید صرف تغییر نام آزاد شود.
- هنگام تعویض واقعی، برآورد/هشدار کارتریج قبلی باقی نماند؛ در نبود سنسور «نامعلوم» قابل انتظار است.

برای تست، تعویض یا reset ساختگی روی دستگاه عملیاتی انجام ندهید. صحت روی سخت‌افزار و PostgreSQL عملیاتی هنوز نیازمند همین مشاهدهٔ پس از انتشار است.

## مرحلهٔ بعد

بستهٔ مستقل بعدی: ممیزی آماری v2 (confidence، false-positive، plateau، catalog، precision و مشاهدات نامعتبر) با fixtureهای ایزوله؛ سپس cache، stale poll write، invalidation و عملکرد history. مشکل recorder و شکست‌های قدیمی suite نیز باید جدا از تغییرات Collector پیگیری شوند.
