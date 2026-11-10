# اصلاح تکمیلی بستهٔ سوم: نام عمومی تونر و شمارندهٔ ردشده

## بازتولید از لاگ عملیاتی

پس از نصب PR۱۳ روی `ec5b759`، دستگاه HP M225dn با IP عملیاتی `172.16.0.42` بین نام واقعی `Black Cartridge HP CF283A` و نام عمومی `Black Toner` رفت‌وبرگشت داشت. هر دو جهت به‌اشتباه پیام `learned state ... restarts` تولید می‌کرد. در همان بازه، شمارندهٔ صفر با پیام `suspicious counter read ignored: prev=60262 current=0` در مسیر رویداد رد شده بود، ولی همان صفر همچنان به Yield Engine ارسال می‌شد و می‌توانست anchor را صفر کند.

تست‌ها با IP رزروشدهٔ آزمایشی، SQLite موقت، DSNهای خالی و I/O دستگاه mock‌شده اجرا شدند؛ هیچ ارتباطی با سرور، دستگاه‌ها یا PostgreSQL عملیاتی برقرار نشد.

## تغییرات

### نام عمومی، نه هویت قطعه

`Black Toner`، `black cartridge`، `Toner black` و `Cartridge black` به‌صورت تطبیق دقیق و مستقل از بزرگی حروف، نام عمومی همان slot هستند. همین قاعده برای رنگ‌های دیگر و `Toner Cartridge` اعمال می‌شود. در صورت وجود هویت واقعی ذخیره‌شده، نام عمومی آن را جایگزین نمی‌کند. شروع از نام عمومی و رسیدن به نام واقعی نیز تکمیل هویت است، نه تعویض.

این قاعده تطبیق prefix نیست: `Black Toner HP CF283A` و `Black Cartridge HP CF283A` همچنان هویت واقعی‌اند؛ `Cyan Toner` نیز placeholder رنگ black نیست. تعویض واقعی نام قطعه و تعویض اثبات‌شدهٔ serial همچنان anchor چرخهٔ جدید می‌سازند. ثبت دستی refill نیز از همین تشخیص استفاده می‌کند.

### شمارندهٔ ردشده، مشاهدهٔ یادگیری نیست

بعد از اجرای `_counters_event`، Collector مقدار کل ورودی را با snapshot پذیرفته‌شدهٔ PrevStore مقایسه می‌کند. در صورت نبود مقدار کل یا باقی‌ماندن مقدار قبلی به‌دلیل رد خوانش، هر سه مبنای شمارندهٔ ورودی Yield Engine (`total`، `full_color`، `black_white`) ناموجود می‌شوند.

- صفر یا افت مشکوک دیگر anchor و `last_counter` را عقب نمی‌برد.
- مبنای fallback مثل black_white یا full_color نمی‌تواند از کنترل total عبور کند.
- مقدار ذخیره‌شده به‌عنوان مشاهدهٔ تازه جعل نمی‌شود؛ snapshot یادگیری نامعتبر ثبت می‌شود و poll معتبر بعدی از anchor قبلی ادامه می‌دهد.
- overflow تأییدنشده نیز به‌عنوان مشاهدهٔ معتبر yield وارد نمی‌شود؛ بازگشت poll عادی همچنان کار می‌کند.
- reset واقعی که اعتبارسنج فعلی با شواهد reboot می‌پذیرد همچنان وارد موتور می‌شود؛ این بسته reset واقعی را به‌طور کلی ممنوع نمی‌کند.

ترتیب و تعداد SNMP/HTTP requestها تغییر نکرده است. الگوریتم آماری v2، schema، migration، وابستگی‌ها، history و profileهای ذخیره‌شده بازنویسی نمی‌شوند.

## آزمون

در checkout آزمایشی، نه کنار دیتابیس عملیاتی:

```text
python -m unittest -v tests.test_collector_identity
python -m pytest tests/test_golden.py -q
python tools/check_test_split.py
```

- ۱۳ تست افزوده شده؛ فایل Collector اکنون ۳۳ تست دارد و همه پاس‌اند. پوشش: رفت‌وبرگشت نام واقعی/عمومی، تکمیل هویت، refill دستی، رنگ‌ها، تعویض واقعی قطعه/serial، صفر/افت ردشده، overflow، reset واقعی و ترکیب خرابی نام با شمارنده؛ مسیر v2 هم تست شده است.
- کنترل منفی روی archive نسخهٔ پایهٔ `ec5b759` با همین تست‌ها: ۱۲ متد جدید شکست می‌خورند و کنترل reset واقعی پاس می‌شود. شکست‌های subtest جداگانه شمرده می‌شوند؛ خلاصهٔ unittest برابر ۳۴ failure از ۳۳ test است. تست‌های ۲۰تایی قبلی همچنان پاس‌اند.
- بازپخش مستقل golden: ۱۱ تست شامل inventory و ۱۰ fixture موجود، پاس؛ fixtureها بازتولید یا تغییر داده نشدند.
- مجموعهٔ مرتبط yield/cartridge/override/brand: ۲۴۷ تست پاس. مسیر pytest-only: ۳۳ تست پاس. parity مسیرهای Python: ۹۹۳ مورد بدون حذف/تکرار. نتیجهٔ کامل محلی و CI در PR ثبت می‌شود؛ سبز بودن تست‌های مرتبط به معنی حل شکست‌های قدیمی کل suite نیست.

## محدودیت‌های صریح

- یادگیری‌ای که پیش‌تر با reset اشتباه از بین رفته خودکار بازیابی نمی‌شود؛ این اصلاح پیشگیرانه است.
- این بسته بازطراحی اعتبارسنج شمارنده، رفع تمام نام‌های عمومی احتمالی یا حل stale/concurrent poll نیست. از تصمیم snapshot فعلی PrevStore استفاده می‌کند؛ حفاظت اتمیک میان pollهای هم‌زمان موضوع مستقلی است.
- قواعد override پیش از اعتبارسنجی شمارنده و مسیر legacy ثبت رویدادها در این اصلاح بازطراحی نشده‌اند. این guard مخصوص انتقال مشاهده به موتور yield است، نه ادعای رفع تمام پیامدهای خوانش نامعتبر در همهٔ بخش‌ها.
- uptime در snapshot پایدار فعلی round-trip نمی‌شود؛ تست reset واقعی برای ارائهٔ شواهد reboot از cache زنده استفاده می‌کند. تغییر ذخیره‌سازی uptime خارج از دامنهٔ این اصلاح است.
- دو دستگاهی که در اسکن عملیاتی `0/79 OID` داشتند، گزارش گمراه‌کنندهٔ «۲۷/۲۷ موفق» اسکن، و استقرار WSGI در این بسته اصلاح نمی‌شوند.

## انتشار پس از review و merge

قبل از merge، pull روی main این اصلاح را دریافت نمی‌کند. پس از merge، برنامه را با Ctrl+C متوقف و در PowerShell پوشهٔ پروژه، کل بلوک زیر را یکجا اجرا کنید:

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $branch = git branch --show-current
    if ($LASTEXITCODE -ne 0 -or $branch.Trim() -ne 'main') { throw 'Expected main; stopped.' }
    $changes = git status --porcelain --untracked-files=no
    if ($LASTEXITCODE -ne 0 -or $changes) { throw 'Tracked changes or status error; stopped.' }
    git fetch origin
    if ($LASTEXITCODE -ne 0) { throw 'Fetch failed.' }
    git merge --ff-only origin/main
    if ($LASTEXITCODE -ne 0) { throw 'Update failed; no reset performed.' }
    git log -1 --oneline
    .\start.bat
}
```

فایل‌های untracked حذف نمی‌شوند؛ Git در صورت تداخل متوقف می‌شود. migration، reset دستی، پاک‌کردن history، تغییر `.env` و نصب وابستگی لازم نیست. commit نهایی این بسته و فرمان کنترل وجود آن پس از ساخت PR اعلام می‌شود.

پس از انتشار، دو یا سه poll عادی دستگاه `172.16.0.42` را مشاهده کنید: نام عمومی نباید reset هویت بسازد و صفر ردشده نباید anchor یادگیری را صفر کند. برای آزمون، هیچ تعویض یا refill ساختگی ثبت نکنید. سپس ممیزی آماری v2 در بستهٔ مستقل دنبال می‌شود.
