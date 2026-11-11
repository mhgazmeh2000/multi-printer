# ادغام محلی مسیر derived runs — پیش‌فرض خاموش، انتشار مسدود

## آنچه تحویل شده است

- `core/yield_engine.py`: همان قرارداد ۱۲ستونی، ترتیب سراسری id و آخرین ۴۰۰ run
  برای هر slot، حداکثر ۸۰۰ endpoint؛ SQL مرجع قبلی در `_interval_history_query`
  حفظ شده است. SQLite و حالت خاموش هیچ probe یا savepoint اضافه‌ای اجرا نمی‌کنند.
- `core/yield_runs.py`: reader اختیاری با `YIELD_DERIVED_RUNS_ENABLED=0` در
  `.env.example`. فعال‌کردن این پرچم به‌تنهایی هیچ DDL یا ساختی انجام نمی‌دهد.
- نبود جدول، نبود marker، `ready=false` و version ناشناخته به تاریخچهٔ دقیق
  برمی‌گردند. marker و دو شاخهٔ reader در **یک statement/MVCC snapshot** هستند.
  خطای اشیای اختیاری/ستون ناسازگار/مجوز با savepoint مدیریت می‌شود؛ نوشته‌های
  پیشین تراکنش caller rollback نمی‌شوند. خطای timeout یا خطاهای دیگر در این
  لایه به‌عنوان «خلاصهٔ آماده» پذیرفته نمی‌شوند. fallbackهای سطح بالاتر قدیمی
  برنامه، به‌جز انتشار خطاهای retryable، تغییر نکرده‌اند.
- migration `0009` و SQL نسخه‌دار `alembic/sql/0009_yield_runs.sql`:
  سه جدول `yield_run_meta`، `yield_run_slots` و `yield_run_boundaries`، ایندکس
  همسایگی و triggerهای INSERT/UPDATE/DELETE/TRUNCATE. source در schema جاری
  برنامه می‌ماند؛ schema سفارشی نیز در تست واقعی بررسی شده است.
- migration روی SQLite no-op است. روی PostgreSQL ساخت اولیهٔ تاریخچه انجام
  نمی‌دهد و marker آماده درج نمی‌کند؛ در نتیجه نصب بدون rebuild از مرجع می‌خواند.
  نداشتن seed row اضافی همچنین مانع تشخیص اشتباه دادهٔ واقعی توسط
  `tools/auto_migrate.py` در مقصد تازه می‌شود.
- `rebuild_yield_runs` یک primitive صریح نگهداری است، نه job خودکار یا فرمان
  انتشار: تاریخچهٔ **کامل** هر slot را زیر ACCESS EXCLUSIVE می‌سازد، سپس marker
  را آماده می‌کند. caller مسئول تراکنش، commit/rollback و timeoutهاست؛
  autocommit=True رد می‌شود. هیچ truncate کردن پنجرهٔ تاریخچه یا shortcut مبتنی
  بر MAX(id)/TTL وجود ندارد.
- downgrade فقط triggerها، ایندکس و اشیای derived را به ترتیب وابستگی حذف
  می‌کند؛ از CASCADE استفاده نمی‌کند و source را تغییر نمی‌دهد. نصب/حذف در
  تراکنش با lock-wait محدود ۵ ثانیه انجام می‌شود. این عدد، سقف زمان کل migration
  یا rebuild نیست.

## نویسندگان و مرز retry

| مسیر | نتیجهٔ بررسی / رفتار |
|---|---|
| `_record_snapshot` از poll | نویسندهٔ داخلی؛ retry در `process_cartridge_snapshot` پیرامون کل تراکنش است، نه پیرامون INSERT. |
| `register_manual_refill` و endpointهای کمکی cycle | snapshot، sample، state و profile در همان تراکنش owning اجرا می‌شوند؛ retry کل عملیات پس از rollback/بستن connection. |
| `core/retention_policy.py`، scheduler و CLI retention | هر batch از قبل commit مستقل داشت؛ اکنون خطا rollback می‌شود و فقط کل batch ناموفق retry می‌شود. batchهای موفق دوباره اجرا نمی‌شوند. PostgreSQL باید هنگام ورود idle باشد تا تراکنش caller اشتباهاً rollback/commit نشود. |
| `web/routes/import_db.py` | import روی PostgreSQL از قبل صریحاً رد می‌شود (E.6.1)؛ مسیر SQLite تغییر نکرده است. |
| `tools/migrate_sqlite_to_pg.py`، BATCH/COPY و TRUNCATE | ابزار نگهداری خارج از ترافیک عادی؛ triggerها bypass نمی‌شوند. ابزار چند commit و cleanup اختصاصی دارد؛ عمداً retry کورِ کل ابزار اضافه نشده است. اجرای آن با نویسندگان فعال مجاز/آزموده اعلام نمی‌شود. |
| `tools/auto_migrate.py` | wrapper ابزار قبلی؛ فقط seedهای مجاز را پاک می‌کند. migration جدید در نصب تازه هیچ دادهٔ seed مشتق‌شده‌ای اضافه نمی‌کند. |
| SQL مدیریتی / pg_restore / trigger bypass | retry اپلیکیشن شامل آن نیست. restore، DISABLE TRIGGER یا session_replication_role خارج از قرارداد نگهداری خودکارند؛ پیش از استفادهٔ مجدد از derived باید invalidation و rebuild مصوب انجام شود. آزمون عملی restore هنوز گیت باز است. |

`core/db/retry.py` فقط SQLSTATEهای `40001` و `40P01` را حداکثر سه تلاش با
backoff کوتاه retry می‌کند. قطع اتصال/commit نامعلوم، timeout، integrity و خطای
دلخواه retry نمی‌شوند. خطای commit در adapter نیز مانند execute ترجمه می‌شود.
catchهای اختیاری estimator دیگر این دو وضعیت را نمی‌بلعند.

cache زیرِدرصدیِ anchor اکنون attempt-local است: بین تلاش‌های rollbackشده از
مقدار committed آغاز می‌کند و فقط پس از موفقیت commit منتشر می‌شود. قفل‌های
striped داخل process رقابت همین slot را محدود می‌کنند؛ قفل و revision دیتابیس
تعارض میان processها را مدیریت می‌کنند. timestamp یک عملیات بین retryها ثابت است.
sequence gap ناشی از rollback طبیعی است و فرض id پیوسته وجود ندارد.

## تست و شواهد

شاهد ماشین‌خوان: `evidence/yield-integration-local.json`.

فرمان نهایی محلی (فقط دیتابیس‌های موقت/تست، بدون اتصال به تولید):

```text
DB_BACKEND=sqlite PG_DSN= .venv/bin/python -m pytest -q \
  tests/test_yield*.py tests/test_retention_policy.py tests/test_startup*.py \
  tests/test_*migration*.py tests/test_db_*.py tests/test_cutover_readiness.py \
  -k 'not test_after_cutover_z_is_green'
```

**۳۳۰ passed، ۱۵ skipped، یک deselected، ۳۰۳ subtest passed؛ ۴۸٫۰۰ ثانیه.**
skipها به معنی اثبات مسیرهای skipشده نیستند. deselection مربوط به تست timestamp
قدیمی است که در اجرای جداگانه و روی HEAD پایه نیز شکست خورد؛ مجموعهٔ کامل سبز
اعلام نمی‌شود. هشدارهای محلی دربارهٔ SECRET_KEY توسعه و XDG_RUNTIME_DIR بودند.

۲۲ تست جدید شامل ۱۶ تست integration/maintenance روی PostgreSQL موقت و SQLite،
و ۶ تست retry/cache هستند. مسیر واقعی poll با نمونهٔ accepted و manual refill،
هم با estimator قدیمی و هم `YIELD_ESTIMATOR=v2`، خطای commit تزریق‌شده را بدون
تکرار snapshot/sample/state رد می‌کند. تست تعارض واقعی READ COMMITTED و
REPEATABLE READ، دومی با `40001` و retry کل تراکنش، نیز پاس شده است. migration
کامل base→0009، downgrade→0008 و نصب مجدد اجرا شده‌اند. historical UPDATE،
DELETE، انتقال slot/id، backfill، TRUNCATE، rollback، interruption و نسخهٔ
ناشناخته با oracle مستقل مقایسه شده‌اند. actual plan فعال، history WindowAgg را
اجرا نکرد و lookupهای فعال source یک‌ردیفی/Index Scan بودند.

گارد تفکیک تست‌ها: ۱۰۴۸ تست unittest-style دقیقاً در یک مسیر؛ `git diff --check`
پاس شد. GitHub Actions با اجرای محلی یکی نیست و نتیجهٔ آن جداگانه باید بررسی شود.
در نخستین Actions، jobِ 3.13 در نصب pgserver شکست خورد؛ metadata انتشار 0.1.4
وجود wheel فقط تا CPython 3.12 و نبود sdist را تأیید کرد. workflow اصلاح شد تا
آزمون‌های PostgreSQL مستقل روی 3.10–3.12 اجرا شوند؛ در 3.13 فقط این وابستگی
اختیاری/تست‌های وابسته skip می‌شوند، نه اینکه آن مسیر اثبات‌شده اعلام شود.

### نتیجهٔ صادقانهٔ کل suite

- یک اجرای کامل با سقف ۱۸۰ ثانیه تمام نشد؛ پس موفق اعلام نمی‌شود.
- اجرای تشخیصی bounded پس از اصلاح انتظارهای head وابسته به migration جدید:
  **۶۶۷ passed، ۳۹ skipped، ۱۰ failure شامل subtest؛ توقف در maxfail=10**.
- در archive تمیز HEAD پایه `882af2a`، سه شکست backup به‌دلیل نبود ابزار
  pg_restore در PATH، و شکست timestamp/cutover عیناً بازتولید شدند
  (۴ failed، ۵۲ passed، ۹ skipped در انتخاب مرتبط).
- تست JSONB با انتظار ثابت `0005` روی HEAD پایه هم شکست خورد (`0008 != 0005`).
  failureهای SQLite همان فایل به فایل محلی logs.db وابسته‌اند؛ این دور برای
  آن‌ها اصلاح یا نتیجهٔ baseline برابر ادعا نمی‌کند.
- انتظارهای head در cutover و ancestry در timeseries که با افزوده‌شدن 0009
  مرتبط بودند، به graph مستقل مخزن متصل شدند؛ انتظارهای قدیمی نامرتبط را برای
  سبز جلوه دادن کل suite تغییر نداده‌ایم.

## گیت انتشار و rollback

**`deployment_ready=false`**. این تحویل کد/تست است، نه تأیید اجرای migration،
فعال‌کردن flag، merge یا انتشار تولید. آزمایش Windows stage2 بسته است؛ benchmark
یا دستور جدیدی از مالک درخواست نمی‌شود.

خاموش‌کردن flag فقط reader را به تاریخچه برمی‌گرداند؛ **هزینهٔ triggerهای نصب‌شده
را متوقف نمی‌کند**. حذف هزینهٔ writer نیازمند downgrade مصوب است. پیش از downgrade
نیز flag خاموش باشد؛ fallback پس از حذف اشیا در تست بررسی شده است. source باقی
می‌ماند و برای فعال‌سازی دوباره rebuild کامل لازم است.

پیش از انتشار باید نتیجهٔ CI/شکست‌های suite تعیین تکلیف، SLA هزینهٔ sustained
writes/WAL و commit p95 تأیید، مجوزهای role واقعی بررسی، و restart/restore و
rollback عملی آزموده شوند. توابع trigger SECURITY INVOKER هستند، نه SECURITY
DEFINER؛ migration هیچ grant سراسری یا افزایش اختیار مخفی اضافه نمی‌کند. نقش
writer باید مجوزهای موردنیاز جدول‌های مشتق‌شده را داشته باشد؛ flag خاموش جای
بررسی مجوزها را نمی‌گیرد. sustained workload و کالیبراسیون طولی ETA همچنان
اثبات نشده‌اند.
