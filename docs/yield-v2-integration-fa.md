# ادغام محلی مسیر derived runs — پیش‌فرض خاموش، انتشار مسدود

## اصلاح مانع راه‌اندازی عادی — مرز جدید فعال‌سازی

**در اصلاح جدید، اجرای معمول start/setup دیگر ایندکس/trigger مسیر yield را نصب
نمی‌کند.** `0009` فقط سه جدول metadata خالی و ایندکس روی جدول خالیِ boundaries
می‌سازد؛ به جدول source دست نمی‌زند و روی آن ACCESS EXCLUSIVE نمی‌گیرد.
نام revision همچنان 0009 است؛ این PR هنوز merge/منتشر نشده و نصب تولیدی از
نسخهٔ قبلی آن انجام نشده است. تاریخچهٔ اساسی Alembic حذف یا شرطی/stamp نشده است.

`core/yield_run_maintenance.py` و `tools/yield_runs_maintenance.py` عملیات جداگانه
دارند: نصب ایندکس source، چهار تابع، پنج trigger و rebuild کامل **همگی در یک
تراکنش با تأیید صریح نگهداری** انجام می‌شوند. تا commit موفق هیچ حالت نیمه‌نصب
یا marker آماده‌ای منتشر نمی‌شود. CLI بدون `--approve-maintenance` حتی config
دیتابیس را import نمی‌کند؛ برای نگهداری reader باید خاموش باشد. خطا exit 1 دارد،
متن DSN/raw error چاپ نمی‌شود و تراکنش rollback می‌شود؛ نتیجهٔ commit نامعلوم
موفق تلقی یا retry نمی‌شود. source به‌وسیلهٔ فعال‌سازی/حذف، UPDATE/DELETE نمی‌شود.

قفل فعال‌سازی/حذف روی source فقط در همین مسیر مصوب گرفته می‌شود: lock wait یک
ثانیه، statement حداکثر ۱۲۰ ثانیه و بودجهٔ کار حداکثر ۶۰۰ ثانیه، قابل کاهش.
caller مسئول commit/rollback است؛ autocommit، connection ازقبل داخل تراکنش، و
isolation غیر READ COMMITTED رد می‌شوند. rebuild مستقیم بدون triggerهای صحیح و
فعال نیز نمی‌تواند marker را ready کند. نصب دوباره روی اشیای موجود صریحاً رد
می‌شود؛ نخست باید deactivation مصوب انجام شود، نه adopt کردن اشیای ناشناخته.

`disable` در همان ابزار، trigger/ایندکس source/توابع را حذف و metadata را خالی
می‌کند، اما سه جدول پایه و revision Alembic را نگه می‌دارد. اجرای مجدد start یا
`upgrade head` آن را دوباره فعال نمی‌کند. downgrade کامل نیز در هر دو حالت فعال
و غیرفعال کار می‌کند. نصب‌های آزمایشیِ قبلی همین PR که 0009 قدیمی را اجرا کرده‌اند
**خودکار پاک نمی‌شوند**؛ برای آن‌ها deactivation مصوب یا downgrade لازم است.

`start.ps1` اکنون در شکست upgrade یا آماده‌سازی داده، قبل از راه‌اندازی وب exit 1
می‌دهد. `setup.ps1` نیز fail-closed است؛ مسیر خطرناک `stamp head` پس از شکست
migration و تلاش تعمیر خودکار حذف شده است. UTF-8 BOM هر دو فایل حفظ شده است.

این بخش جای توضیح قدیمی «migration خودش trigger نصب می‌کند» را می‌گیرد. ابزار
نگهداری در این دور فقط روی PostgreSQL موقت اجرا شده؛ این سند مجوز اجرای آن روی
تولید یا دستور اجرایی جدید برای مالک نیست.

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
- migration `0009` و SQL `alembic/sql/0009_yield_runs.sql` فقط metadata سبک
  را در schema جاری برنامه می‌سازند؛ روی SQLite no-op هستند. marker آماده درج
  نمی‌شود، بنابراین نصب عادی از reader قبلی استفاده می‌کند.
- SQL سنگین نسخه‌دار در `alembic/sql/0009_yield_runs_activation.sql` فقط از مسیر
  نگهداری صریح بالا اجرا می‌شود، نه Alembic/start/setup. schema سفارشی تست شده است.
- `rebuild_yield_runs` فقط پس از فعال‌سازی صحیح writer maintenance کار می‌کند؛
  تمام تاریخچه را می‌سازد، نه پنجره‌ای کوتاه‌شده یا TTL/MAX(id) به‌عنوان معیار تازگی.
- downgrade کامل با IF EXISTS اشیای optional را پاک می‌کند؛ CASCADE ندارد و
  source حفظ می‌شود. قفل source در downgrade دستی نیز همچنان نیاز به پنجرهٔ
  نگهداری دارد. سقف lock wait در downgrade پنج ثانیه است، نه سقف کل عملیات.

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

نتیجهٔ اصلاح مرز نگهداری در `evidence/yield-maintenance-guard-local.json` جداگانه
ثبت شده است: **۳۴۴ passed، ۲۰ skipped، یک deselected و ۳۰۳ subtest passed در
۵۱٫۷۰ ثانیه** با همان انتخاب فرمان زیر. تست امنیت جداگانه نیز ۱۶ passed و ۳۷
subtest passed داشت. گارد تفکیک اکنون ۱۰۵۹ تست unittest-style را دقیقاً یک‌بار
می‌شمارد. پنج skip جدید محلی، تست‌های subprocess واقعی PowerShell هستند که در
job ویندوز CI اضافه شده‌اند؛ نبود PowerShell محلی را اثبات آن مسیر نمی‌نامیم.
برابری source، rollback کامل نصب/حذف، عدم فعال‌سازی با start/upgrade مجدد، سازگاری
با 0009 آزمایشی قدیمی، و race حذف هم‌زمان با rebuild روی PG موقت تست شده‌اند.

**اعداد باقیِ این بخش، تاریخچهٔ بستهٔ قبلی‌اند، نه نتیجهٔ CI commit جدید.**


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
را متوقف نمی‌کند**. حذف هزینهٔ writer نیازمند deactivation یا downgrade مصوب است. پیش از آن
نیز flag خاموش باشد؛ fallback پس از حذف اشیا در تست بررسی شده است. source باقی
می‌ماند و برای فعال‌سازی دوباره rebuild کامل لازم است.

پیش از انتشار باید نتیجهٔ CI/شکست‌های suite تعیین تکلیف، SLA هزینهٔ sustained
writes/WAL و commit p95 تأیید، مجوزهای role واقعی بررسی، و restart/restore و
rollback عملی آزموده شوند. توابع trigger SECURITY INVOKER هستند، نه SECURITY
DEFINER؛ migration هیچ grant سراسری یا افزایش اختیار مخفی اضافه نمی‌کند. نقش
writer باید مجوزهای موردنیاز جدول‌های مشتق‌شده را داشته باشد؛ flag خاموش جای
بررسی مجوزها را نمی‌گیرد. sustained workload و کالیبراسیون طولی ETA همچنان
اثبات نشده‌اند.
