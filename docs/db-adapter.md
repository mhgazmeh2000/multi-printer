# Data layer `core/db/` — adapter contract (phase D + dual backend in E.2)

**وضعیت:** پایدار و کامیت‌شده در فاز D (`4e99e47` + کامیت D.3).
**دامنه:** فقط لایه‌ی اتصال. هیچ منطق دامنه، هیچ کوئری و هیچ schema‌ای تغییر نکرده است.

---

## 1. Why an adapter?

**مسئله:** فاز E باید PostgreSQL اضافه کند. امروز کد روی **۲۸ نقطه‌ی فراخوانی
`db_connection(...)`** در `core/` و `web/` (۱۷ مورد با `commit=True`) و همچنین
اتصال‌های خام به SQLite تکیه دارد. اگر فاز E بخواهد این‌ها را دستی بازنویسی کند،
هر خطای کوچک در یکی از آن‌ها یک نشتی یا بی‌صدا خراب‌شدن داده می‌شود.

**راه‌حل فاز D:** یک **دورگیر نازک** روی SQLite گذاشتیم تا:

| چه چیزی | قبل از فاز D | بعد از فاز D |
|---|---|---|
| اتصال‌های خام در کد (`core/`+`web/`+`create_admin.py`) | ۸ نقطه | **۱ نقطه** (`core/db/connection.py:242`) |
| انتخاب backend | وجود نداشت | `DB_BACKEND` (پیش‌فرض `sqlite`) |
| خطاهای اتصال | `sqlite3.OperationalError` خام | `DatabaseConnectionError` (+ علت در `__cause__`) |
| ترجمه‌ی placeholder | وجود نداشت | `translate_placeholders` آماده، خاموش در SQLite |
| رفتار برنامه | — | **بدون تغییر** (۲۳۴ تست فاز C بدون هیچ ویرایش سبز ماند) |

**چرا این ترتیب اصولی است:** اول یک لایه‌ی نازک و بی‌اثر روی چیزی که کار می‌کند
(کاهش ریسک، rollback آسان)، بعد افزودن backend دوم در فاز E. هر گام D.3 جداگانه
کامیت شد تا برگشت‌پذیر باشد.

**آنچه adapter عمداً عوض نکرد:** معنای تراکنش‌ها. `executemany` هنوز **یک تراکنش
واحد** است (نه یک تراکنش به‌ازای هر دسته)، و `commit` فقط با `commit=True` در
`db_connection` یا با خروج بدون خطا از `with adapter:` انجام می‌شود.

---

## 2. The `ConnectionAdapter` contract

فایل: `core/db/connection.py`

| متد | امضا | کار |
|---|---|---|
| `execute` | `(sql, params=()) → cursor` | اجرای یک کوئری؛ cursor خام برمی‌گرداند |
| `executemany` | `(sql, params_list) → cursor` | دسته‌ای؛ یک تراکنش واحد |
| `executescript` | `(script) → cursor` | چند دستور پشت‌سرهم (معادل sqlite3) |
| `commit` | `() → None` | ثبت تراکنش |
| `rollback` | `() → None` | لغو تراکنش |
| `close` | `() → None` | بستن اتصال + ست‌کردن `closed=True` |
| `cursor` | `() → cursor` | cursor خام (برای کدهایی مثل `/metrics` و `/api/stats/daily`) |
| `__enter__` | `() → ConnectionAdapter` | ورود به `with` |
| `__exit__` | `(exc_type, exc_val, exc_tb) → False` | بدون خطا ⇒ commit، با خطا ⇒ rollback و **بازتاب** استثنا |

**پاس‌تروها (برای کوئری‌محور بودن سازگاری):** `fetchone`، `fetchall`، `fetchmany`،
`description`، `rowcount`، `lastrowid`، `__iter__`. یعنی هر سه الگوی موجود کار می‌کند:

```python
row  = conn.execute("SELECT ... WHERE id=?", (uid,)).fetchone()   # الگوی رایج
with conn.execute("INSERT ...") as cur: ...                        # الگوی sqlite3
for r in conn.execute("SELECT ..."): ...                           # iteration
```

**ویژگی‌های کمکی:** `backend` (`"sqlite"`)، `raw_connection` (اتصال خام زیرین)،
`closed`، `__repr__`.

**توابع ساخت:**

| تابع | امضا | کار |
|---|---|---|
| `core.db.get_connection` | `(timeout=30.0, commit=False) → ConnectionAdapter` | اتصال به مسیر پیش‌فرض (`config.settings.DB_PATH`)، خوانده‌شده **در زمان فراخوانی** |
| `core.db.connect_to` | `(path, timeout=30.0) → ConnectionAdapter` | اتصال به فایل مشخص (DB موقت/آپلودی) |
| `core.db.connect` | `(path=None, *, timeout=30.0, backend=None)` | نقطه‌ی ورود پایین‌سطح (انتخاب backend) |
| `core.db.connect_sqlite` | `(path, timeout=30.0)` | **تنها جای مجاز `sqlite3.connect` در کد** |
| `core.db.db_connection` | `(timeout=30.0, commit=False)` @contextmanager | سازگاری؛ commit/rollback/close تضمینی |

**دو پوسته‌ی سازگاری** (امضا و محل import تغییر نکرد؛ فقط delegate):
`core/database.py::db_connection(timeout=10.0, commit=False)` و
`core/security_audit.py::_db_conn(timeout=10.0, commit=False)`.

> نکته‌ی پیش‌فرض timeout: `core.db.connect` پیش‌فرض ۳۰s دارد (قرارداد فاز D)، ولی
> هر نقطه‌ی مهاجرت‌شده **همان timeout قبلی خودش** را صریح می‌فرستد
> (`metrics` ۱۰s، `stats` ۳۰s، `import_db` ۵s = پیش‌فرض sqlite3، `init_db` ۳۰s).
> هیچ مقداری بی‌صدا عوض نشده است.

---

## 3. Differences between SQLite and PostgreSQL (phase E map)

| موضوع | SQLite (امروز) | PostgreSQL (فاز E) | وضعیت پس از E.2 |
|---|---|---|---|
| تراکنش | `connect()` بیرون از تراکنش؛ `commit` لازم برای ماندگاری | commit صریح لازم؛ `autocommit` ناهمگون | ✔ adapter روی PG با `autocommit=False` صریح؛ `db_connection(commit=True)` حفظ شد (اندازه‌گیری: rollback ⇒ ۰ سطر) |
| `lastrowid` | بعداز INSERT در دسترس | ندارد؛ نیاز به `RETURNING id` | ✔ E.4.3 بسته شد: `insert_returning_id` (PG ⇒ `RETURNING id`؛ صفر ردیف ⇒ `None`) |
| نوع ردیف | `sqlite3.Row`/tuple؛ دسترسی با ایندکس | `psycopg.rows.dict_row` توصیه‌شده | ◻ `row_factory` روی PG بی‌اثر (تصمیم شکل ردیف در E.5) |
| قفل | قفل کل فایل DB + `busy_timeout` | MVCC + قفل ردیف؛ `busy_timeout` بی‌معنا | ✔ E.4.5 بسته شد: PRAGMAها فقط در مسیر SQLite (`connect_sqlite`)؛ مسیر PG روی `init_db` بدون PRAGMA/DDL است (تأیید اسکیمای Alembic) |
| DSN | مسیر فایل (`DB_PATH="logs.db"`) | DSN رشته‌ای (`postgresql://...`) | ✔ `PG_DSN` + `resolve_dsn()` صریح (نبود DSN ⇒ `ConfigurationError`) |
| placeholder | `?` | `%s` | ✔ `translate_placeholders` فعال + escape `%` ⇒ `%%` (بخش ۶.۲) |
| بازه‌ی زمانی | مقایسه‌ی **رشته‌ای** روی ستون TEXT | نوع واقعی `TIMESTAMPTZ` | `docs/timezone-contract.md` |
| بکاپ | کپی فایل (`shutil.copy2(DB_PATH, backup)`) | `pg_dump` | ⏳ `web/routes/import_db.py:135-141`؛ روی PG چون `DB_PATH` نیست بی‌صدا `""` برمی‌گردد (بکاپ = هیچ). امروز زیر گارد import است؛ ترجمهٔ واقعی کار E.6 (ردیف ۱۱) |
| خطای «جدول موجود نیست» | `sqlite3.OperationalError` | `psycopg.errors.UndefinedTable` | ✔ E.4.4 بسته شد: adapter خطای درایور را به خانوادهٔ مشترک ترجمه می‌کند (`_translate_error`)؛ صفر `except sqlite3.*` در `core/`+`web/` |

**قاعده‌ی طلایی فاز E:** هیچ `except sqlite3.*` جدیدی نوشته نشود؛ خطاها از
`core.db.exceptions` بگیرید تا کد بالادستی به backend وابسته نشود.

**وضعیت پس از E.4 (۲۰۲۶-۱۰-۰۴):** سه ردیف «کار E.4» در جدول بالا بسته شدند
(`lastrowid` ⇒ E.4.3 / PRAGMAها ⇒ E.4.5 / خطاهای مشترک ⇒ E.4.4). جدول نگاشت
خطا و فهرست ۱۲ محل SQLite-onlyِ دست‌نخورده:
`docs/sqlite-excepts-inventory.md` بند ۶ و `docs/postgresql-strictness.md` بند ۷.
دو مورد باز ماند: شکل ردیف (`row_factory` روی PG — تصمیم E.5) و بکاپ
(`pg_dump` در `import_db` — ردیف ۱۱، تصمیم E.6؛ وضعیتش در همان جدول ثبت شده).
فهرست ۱۷ ردیفیِ نهاییِ سخت‌گیری و یافته‌های جانبی فاز:
`docs/postgresql-strictness.md` بند ۷.

---

## 4. How to monkeypatch / switch the database path

۱. **`core.db` مسیر را در زمان import بایند نمی‌کند.**
   `core.db.connection.default_db_path()` هر بار `config.settings.DB_PATH` را
   می‌خواند و `core.db.connect()` مسیر را در همان فراخوانی resolve می‌کند. پس این
   کار مؤثر است:

   ```python
   from config import settings
   settings.DB_PATH = "/tmp/test.db"     # یا monkeypatch.setattr(settings, "DB_PATH", ...)
   # اتصال بعدی از مسیر جدید ساخته می‌شود
   ```

۲. **الگوی اصلی تست‌های مخزن «chdir» است** (چون `DB_PATH="logs.db"` نسبی است):
   `tests/golden_harness.py::isolated_db` و تست‌های `test_db_*.py` وارد پوشه‌ی موقت
   می‌شوند. مسیر نسبی در لحظه‌ی اتصال نسبت به CWD تفسیر می‌شود ⇒ همان رفتار قبلی.

۳. **گارد ایمنی:** `tests/golden_harness.py:143-148` مقدار
   `core.database.DB_PATH` را می‌خواند و اگر DB بیرون دایرکتوری موقت باشد، اجرا را
   **رد** می‌کند. به همین دلیل `core/database.py` عمداً `DB_PATH` را بازصادر
   می‌کند (حذفش نکنید).

۴. **شبیه‌سازی خطای اتصال در تست‌ها:** به‌جای پچ‌کردن `sqlite3.connect` روی
   `core.database`، نقطه‌ی درست این است:

   ```python
   mock.patch("core.db.connection.sqlite3.connect", side_effect=sqlite3.OperationalError("boom"))
   ```

۵. **backend را تست کنید، نه فرض کنید:** `core.db.resolve_backend()` را صدا بزنید؛
   `DB_BACKEND` مقدارهای `postgres`/`pg`/`postgresql` را در فاز D با
   `UnsupportedBackendError` رد می‌کند (بدون fallback بی‌صدا — کار کردن روی backend
   اشتباه خطرناک‌تر از بالا نیامدن است).

---

## 5. What the adapter **does not do** (phase E)

این‌ها عمداً دست‌نخورده ماندند تا تغییر رفتاری رخ ندهد:

| مورد | امروز کجا | وضعیت در فاز E |
|---|---|---|
| `INSERT OR REPLACE` | `core/database.py:1077`، `core/enhanced_collector.py:208`، `core/yield_engine.py:412,614`، `web/routes/import_db.py:187`، `tests/golden_harness.py:280` | به `INSERT ... ON CONFLICT` ترجمه شود — **جزئیات و تصمیم هر محل:** `docs/insert-or-replace-inventory.md` (E.0.4) |
| `json_extract` | `core/database.py:721-777` (۸ نقطه، debounce) و `web/routes/metrics.py:119` | به `jsonb`/`->>` ترجمه شود |
| ~~`substr(timestamp,1,10)`~~ | (حذف شد در E.3.4) | **نیازی به ترجمه نیست** — گروه‌بندی روزانه به پایتون منتقل شد (`to_local_day`) |
| `strftime` | (در کد فعال استفاده نمی‌شود؛ فقط کامنت‌ها) | اگر لازم شد، `to_char` |
| `AUTOINCREMENT` | `core/database.py` (۴ جدول) | `GENERATED ... AS IDENTITY` |
| `PRAGMA ...` | `core/database.py::init_db`، `core/db/connection.py` | در مسیر پستگرس حذف شود |
| escape `%` برای psycopg | `core/db/placeholders.py` | ✔ **در E.2 پیاده شد** (`escape_percent_signs`) |
| `executescript` | استفاده نمی‌شود | روی PG `NotImplementedError` صریح (E.2)؛ شکستن اسکریپت به statementها ⇒ E.4 |
| ابزارهای `tools/` | `audit_pages.py:438`، `cleanup_fake_gaps.py:86,122`، `cartridge_test_step2.py:30,32` (۵ اتصال خام) | **فاز F** (در فاز D دست‌نخورده ماندند) |
| تست‌ها | `tests/*` (۷ اتصال خام) | عمدی: کنترل مستقلِ فیزیکی برای راستی‌آزمایی adapter |

---

## 6. Dual backend — status after E.2 (measured)

### ۶.۱ انتخاب backend و نشانی

| مورد | رفتار |
|---|---|
| مقادیر مجاز `DB_BACKEND` | `sqlite` (پیش‌فرض) و `postgresql` (نام‌های جایگزین `postgres`/`pg`) |
| مقدار ناشناخته | `UnsupportedBackendError` (بدون fallback بی‌صدا) |
| `postgresql` بدون نشانی | `ConfigurationError` — از `resolve_dsn()`؛ هیچ اتصال حدسی به سرور پیش‌فرض |
| دادن `path` همراه `postgresql` | `ConfigurationError` (مسیر فایل فقط برای sqlite معنا دارد) |
| نشانی PG | پارامتر `dsn` ⇒ `PG_DSN` (سبک libpq: `host=… port=… dbname=… user=… password=…`) |
| اتصال PG | `psycopg` ۳ (نه psycopg2)، `autocommit=False`، `connect_timeout` = همان `timeout` adapter |
| TimeZone | روی PG همیشه **UTC** قفل می‌شود (`options=-c timezone=UTC`); اگر DSN خودش `options` داشته باشد، حفظ و UTC به انتهایش اضافه می‌شود |

### ۶.۲ چه چیزی روی هر backend کار می‌کند (نتیجه‌ی واقعی اجراها)

| قابلیت | SQLite | PostgreSQL | یادداشت |
|---|---|---|---|
| `execute` با سبک `?` | ✔ | ✔ | روی PG ترجمه به `%s` + escape `%` |
| `executemany` در یک تراکنش | ✔ | ✔ | روی PG از cursor می‌رود (`psycopg.Connection` این متد را ندارد) |
| `commit` / `rollback` | ✔ | ✔ | روی PG هیچ DMLای خودکار commit نمی‌شود (تفاوت با sqlite3) |
| درصد متنی در کوئری | ✔ دست‌نخورده | ✔ `%` ⇒ `%%` | `SELECT 'نرخ 100%'` روی PG پیش از E.2 خطا می‌داد |
| `lastrowid` | ✔ | ✗ `NotImplementedError` | راه درست روی PG: `INSERT … RETURNING id` |
| `executescript` | ✔ | ✗ `NotImplementedError` | شکستن اسکریپت ⇒ E.4 |
| `row_factory` | ✔ عبور داده می‌شود | ✗ بی‌اثر (خواندن `None`) | شکل ردیف با `psycopg.rows` ⇒ E.5 |
| `cursor()` خام | `sqlite3.Cursor` | `psycopg.Cursor` | **ترجمه روی cursor خام اعمال نمی‌شود** ⇒ SQL باید با سبک خود درایور باشد |
| نوع خطای «استفاده‌ی نادرست از API» | `sqlite3.ProgrammingError` | `psycopg.ProgrammingError` | `_require_cursor` |

### ۶.۳ محدودیت‌ها و کارهای باقی‌مانده (صریح)

۱. **`cursor()` خام، ترجمه‌نشده است.** مصرف‌کنندگان فعلی: `web/routes/stats.py:76`
   (با `?` در خط ۸۰)، `web/routes/metrics.py:112`، `core/database.py:222`. روی PG
   باید یا به متدهای adapter منتقل شوند یا SQLشان دو-سبکی شود ⇒ **E.4**.
۲. `lastrowid` در `core/database.py:510` ⇒ `RETURNING id`؛ **E.4**.
۳. `INSERT OR REPLACE`، `json_extract`، `PRAGMA`ها و `AUTOINCREMENT`/`IDENTITY`
   ⇒ **E.4** (نقشه در `docs/postgresql-strictness.md`). (`substr(timestamp,1,10)`
   دیگر در فهرست نیست: در E.3.4 حذف شد.)
۴. فعال‌کردن `DB_BACKEND=postgresql` در production ⇒ **E.3** (تا آن زمان پیش‌فرض
   محیط توسعه `sqlite` است و هیچ رفتاری از بیرون عوض نشده).
۵. **با `DB_BACKEND=postgresql` کل مجموعه‌ی تست سبز نمی‌شود** (۱۷۶ failed در سنجش
   E.0) — این عمدی است و در E.5 با ترجمه‌ی مسیرها بسته می‌شود؛ تا آن زمان اجراهای
   استاندارد با `env -u DB_BACKEND -u PG_DSN` انجام می‌شوند.

### ۶.۴ نحوه‌ی اجرای آزمون‌ها و ابزارها

```powershell
# اجرای استاندارد (SQLite): تست‌های یکپارچه‌ی PG با پیام روشن skip می‌شوند
$env:DB_BACKEND=""; $env:PG_DSN=""; python -m pytest -q          # ۳۰۲ passed، ۹ skipped

# اجرای پذیرش E.2: با نشانی PG ⇒ هیچ skipی نمی‌ماند
$env:PG_DSN="host=127.0.0.1 port=5432 dbname=printguard user=printguard password=…"
python -m pytest -q                                              # ۳۱۱ passed، ۰ skipped
python tools\test_dual_backend.py                                # OK=17 EXPECTED=2 FAIL=0
python tools\test_dual_backend.py --backend sqlite                # بدون نیاز به PG
```

تست‌های یکپارچه‌ی PG (`tests/test_db_adapter.py::PostgresContractTests`) فقط جدول
`TEMP` می‌سازند و در پایان rollback/close می‌کنند ⇒ هیچ سطر دائمی و هیچ جدول
برنامه‌ای را لمس نمی‌کنند.

---

## 7. Phase D acceptance status

| معیار | نتیجه |
|---|---|
| `sqlite3.connect` مستقیم در کد | فقط `core/db/connection.py:242` ✔ |
| تست‌ها | ۲۶۹ pytest / ۲۴۸ unittest (۲۱ pytest-only) — هم‌خوان با گارد CI ✔ |
| golden | ۱۰/۱۰ تفاضل-صفر ✔ |
| ساختار endpointها | ۶۷ روت، بدون تغییر ✔ |
| کامیت‌ها | D.1 (`4e99e47`)، D.2/D.3/D.5 در همین فاز ✔ |

---

## 8. Backend-aware helpers for SQL (E.4.9 / E.4.5 / E.5)

این چهار هِلپر تنها نقاطی هستند که «نحوِ متفاوت دو backend» را می‌پوشانند؛ هیچ‌کدام
منطق دامنه را دست نمی‌زنند و هیچ fallback بی‌صدایی ندارند.

| هِلپر | نشانی | کار |
|---|---|---|
| `ConnectionAdapter.json_extract(col, path)` | `core/db/connection.py` | SQLite ⇒ `json_extract(col, '$.x')`؛ PG ⇒ `("col"::jsonb) ->> 'x'` (ستون `details` روی PG `TEXT` است، پس `::jsonb` لازم است؛ JSON نامعتبر ⇒ 22P02 و این رفتار **عمدی** صریح است، نه تبدیل‌شده) |
| `schema_columns(conn, table)` | `core/db/schema.py` | SQLite ⇒ `PRAGMA table_info`؛ PG ⇒ `information_schema.columns` با `current_schema()`؛ ترتیب = ترتیب اعلام جدول |
| `insert_returning_id(...)` | `core/db/connection.py` | جایگزین `lastrowid` (که روی PG وجود ندارد): `RETURNING id` + خواندن یک مقدار |
| `upsert_sql(table, columns, conflict_columns, *, values_sql=None, backend=None)` | `core/db/upsert.py` | جملهٔ «درج یا جایگزینی» |

### ۸.۱ `upsert_sql`

```
SQLite  →  INSERT OR REPLACE INTO "t" ("a", "b") VALUES (?, ?)
PG      →  INSERT INTO "t" ("a", "b") VALUES (?, ?)
            ON CONFLICT ("a") DO UPDATE SET "b" = EXCLUDED."b"
```

- **پیش‌شرط معنایی:** ترجمه فقط وقتی بی‌خطر است که جمله **همهٔ** ستون‌های جدول را
  بنویسد. SQLite با `REPLACE` ردیف را حذف و دوباره می‌سازد (ستون نوشته‌نشده ⇒
  پیش‌فرض) ولی PG درجا `UPDATE` می‌کند (ستون نوشته‌نشده ⇒ ارزش قبلی). پس
  `tests/test_upsert_cross_backend.py` هم‌خوانی فهرست ستون‌ها را با
  `schema_columns()` قفل کرده است.
- **`backend`:** پیش‌فرض از `resolve_backend()` (محیط) خوانده می‌شود؛ کد برنامه باید
  `backend=conn.backend` بدهد تا تست‌های PG-محور با تغییر محیط، مسیر اشتباه نروند
  (درسِ آلودگی `logs.db` در همین سند، بند ۶.۳).
- **`values_sql`:** برای جمله‌هایی که مقدار ثابت در `VALUES` دارند (مثل `'high'`)؛
  در این حالت تعداد جای‌نماها عمداً با طول فهرست ستون‌ها مقایسه نمی‌شود.
- **جای‌نماها همیشه `?`** می‌مانند؛ تبدیل به `%s` کار خودِ adapter است.
- اگر ستون غیرکلیدی برای `SET` نماند ⇒ `ON CONFLICT … DO NOTHING` (PG اجازهٔ
  `DO UPDATE SET` خالی نمی‌دهد).
- مصرف‌کننده‌ها: `save_printer_counters` (کلید `ip`)، `_upsert_state`
  (`printer_ip, color`)، `_upsert_profile_if_high` (`cartridge_key`).
  `web/routes/import_db.py` عمداً منتقل نشده (گارد E.4.8ب ⇒ TD-007، تصمیم در E.6).
