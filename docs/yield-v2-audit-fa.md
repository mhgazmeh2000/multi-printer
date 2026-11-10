# ممیزی آماری Yield v2 — اعتماد، plateau و مرز شواهد

## دامنه و تغییر رفتار

این بسته مستقل از اصلاح Collector در PR14 است؛ تغییری در scanner، schema، migration، override، تاریخچه یا دادهٔ عملیاتی ندارد. الگوریتم fit و ضرایب کالیبراسیون بازتنظیم نشده‌اند.

- شمارندهٔ منفی، boolean، اعشاری یا غیرمتناهی و مقدار نامعتبر حسگر، بدون تبدیل مخرب یا exception از شواهد حذف می‌شوند. کیفیت نامعتبر/صفر، step غیرمتناهی و ظرفیت نامعتبر برای `capacity_units` نیز پذیرفته نمی‌شوند.
- `near end` پیش از واژهٔ `end` تفسیر می‌شود؛ کلمه‌های `pending`، `weekend` و `without` دیگر به‌دلیل substring به empty تبدیل نمی‌شوند. این یک parser محدود انگلیسی است، نه تحلیل عمومی زبان یا نفی.
- حذف blip سه‌نقطه‌ای از تغییر شناخته‌شدهٔ chip یا rollback شمارنده عبور نمی‌کند؛ blip واقعیِ همان chip همچنان فیلتر می‌شود.
- nominal درصد/ظرفیت قبل از plateau به دو/چهار رقم اعشار گرد نمی‌شود. آستانهٔ برابری موجود plateau (`1e-4`) و پهنای عدم‌قطعیت برندها تغییر نکرده‌اند؛ اعشار بیشتر به معنی شواهد آماری قوی‌تر نیست.
- prior-only دیگر صرفاً به‌دلیل `shared_profile` برچسب high نمی‌گیرد؛ confidence از عرض CI محاسبه و در حالت prior حداکثر medium می‌شود. CI منفی/غیرمتناهی low است.
- prior مستقل به catalog، device capacity یا shared profile محدود است. خروجی تجربی قبلی همان slot دوباره prior نمی‌شود؛ پروفایلی که `source_printer_ip` آن همان دستگاه است در مسیر v2 پذیرفته نمی‌شود. هویت دستگاه از آرگومان `ip` گرفته می‌شود، حتی با state ناقص.
- live و ابزار backtest یک سیاست پذیرش snapshot دارند: sensor-invalid، synthetic، status-coded، quarantine و frozen حذف می‌شوند. سه ردِ صرفاً مربوط به نسبت v1 یعنی `not_enough_pages_yet`، `quantization_1pct_noise` و `estimated_yield_out_of_range` همچنان مشاهدهٔ عددی معتبر برای v2 هستند. valid=false با علت ناشناخته fail-closed است.
- دادهٔ عددی malformed در replay، کل سری را متوقف نمی‌کند. در backtest، همان validation پیش از مقایسهٔ legacy نیز اجرا می‌شود.

## شواهد بازتولیدپذیر

تست‌ها فقط از دادهٔ ساختگی و SQLite موقت استفاده کرده‌اند؛ DSN عملیاتی استفاده نشده است.

```bash
python -m unittest tests.test_yield_v2_audit tests.test_yield_estimator_v2 -v
python tools/yield_backtest.py --synthetic --trials 80 --verify-gates
python -m pytest tests/test_golden.py -q
python tools/check_test_split.py
python -m pytest tests/ -m pytest_only -q
```

- تست ممیزی جدید: **۲۱ متد**، شامل parity پذیرش مشاهده در live و SQLite replay. روی کد مبنا، همین فایل **۲۹ failure و ۷ error** تولید کرد (با احتساب subtestها)؛ روی اصلاح **۲۱/۲۱ موفق**.
- مجموعهٔ pure/Monte Carlo/integration v2 همراه ممیزی: **۳۴ موفق**.
- مجموعهٔ متمرکز yield/cartridge/Collector: **۲۶۸ موفق**.
- golden مستقل: **۱۱ موفق**؛ fixtureها تغییر نکرده‌اند.
- pytest-only: **۳۳ موفق**؛ parity شمارش: **۱۰۱۴ در هر مسیر**.
- مقایسهٔ کامل نهایی: مبنا **۹۹۳ تست** در برابر اصلاح **۱۰۱۴ تست**؛ هر دو دقیقاً **۸ failure، ۲ error و ۷۵ skip** با نام‌های یکسان. شکست‌ها در backup_pg، cutover_readiness و jsonb_columns هستند، نه yield. همهٔ تست‌های محلی سبز نیستند.

### benchmark ساختگی ۸۰۰ کارتریج / ۱۰ برند

| معیار | نتیجه |
|---|---:|
| MAPE v1 → v2 | 9.63% → 5.06% |
| کاهش نسبی MAPE | 47.4% |
| Median APE v2 | 2.58% |
| پوشش تجربی CI95، MLE / EB | 96.64% / 97.12% |
| high-confidence با خطای بیش از 15%، v2 | 0.00% |
| تغییر نتیجه با idle polling | 0.00000000 |

تمام gateهای benchmark پاس شدند. این اعداد مربوط به سناریوهای شبیه‌سازی‌اند و تضمین کالیبراسیون روی ناوگان واقعی نیستند. در همین benchmark، Epson coarse-20% هنوز MAPE حدود 12.49% دارد؛ میانگین ناوگان نباید ضعف سنسور کم‌دقت را پنهان کند.

## محدودیت‌های مهم

1. `toner_snapshots_v2.raw_level/toner_level` هنوز INTEGER هستند و `exact_level` کامل persist نمی‌شود. اصلاح rounding در pure pipeline به معنی حل end-to-end دقت دیتابیس نیست؛ migration در این بسته انجام نشده است.
2. provenance پروفایل فقط در حد `source_printer_ip` موجود بررسی می‌شود؛ استقلال آماری یک aggregate تاریخی با چند مشارکت‌کننده از این metadata قابل اثبات نیست. بازپخش آفلاین هم سیاست پذیرش مشاهده را همسان می‌کند، نه تمام منطق انتخاب prior و هویت live را.
3. صحت عددی تمام ظرفیت‌های catalog یا پوشش همهٔ SKUها با این تست‌ها اثبات نشده؛ catalog همچنان prior با عدم‌قطعیت است، نه ground truth تجربی.
4. هیچ یادگیری تاریخی تعمیر یا پاک نشده است. این بسته فقط پذیرش شواهد و محاسبات بعدی v2 را اصلاح می‌کند؛ مسیر v1 بازطراحی نشده است.

## استقرار و مرحلهٔ بعد

تا merge و pull، سرور فعلی تغییر نمی‌کند. برای این بسته reset، پاک‌سازی history یا migration لازم نیست؛ پیش از بررسی CI و merge روی سرور دستوری اجرا نشود.

مرحلهٔ بعد: بازپخش read-only از export ناشناس/کپی دیتابیس واقعی، بررسی توزیع confidence و CI به تفکیک برند و counter basis، و طراحی جداگانهٔ persistence دقت خام. هرگونه دستور بررسی سرور باید یکجا و فقط خواندنی ارائه شود؛ تغییر schema یا تعمیر history نیازمند بسته و تأیید جداست.
