# PostgreSQL setup — server status (phase E.1)

**وضعیت:** سند وضعیت + دستور کار. PostgreSQL 16 **از قبل روی سرور برنامه نصب است**؛
در این فاز هیچ نصبی انجام نمی‌شود (قید فاز E.1). فاز E.2 (dual backend) هنوز آغاز نشده
⇒ `DB_BACKEND=postgresql` **فعال نیست** و کد برنامه همچنان روی SQLite کار می‌کند.

---

## 1. Application server (owner)

### نسخه‌ی نصب‌شده
| مورد | مقدار |
|---|---|
| PostgreSQL | **16.x** (از قبل نصب‌شده) |
| OS | Windows 11 (سرور برنامه) |
| مسیر نصب | `C:\Program Files\PostgreSQL\16\` |
| مسیر `psql` (اگر در PATH نبود) | `C:\Program Files\PostgreSQL\16\bin\psql.exe` |
| مسیر data | `C:\Program Files\PostgreSQL\16\data\` |
| سرویس ویندوز | `postgresql-x64-16` |
| پورت پیش‌فرض | 5432 |

### دیتابیس برنامه
| مورد | مقدار |
|---|---|
| نام دیتابیس | `printguard` |
| کاربر | `printguard` (مالک دیتابیس) |
| رمز | در `secrets.json` — ردیف ۳۹ در `.gitignore` (منتشر نمی‌شود) |
| Encoding | `UTF8` |
| Collation / Ctype | `C` / `C` |
| TimeZone دیتابیس | `UTC` |

> **چرا collation `C`؟** تصمیم فاز E (بند ۴ `docs/postgresql-strictness.md`): ترتیب
> نتایج باید بایت‌محور و قابل‌پیش‌بینی باشد (مثل رفتار امروزِ SQLite) و به collation
> پیش‌فرض ویندوز گره نخورد. اگر روزی ترتیب «انسانی» فارسی لازم شد، تصمیم جداگانه و
> آگاهانه لازم است (ICU)، نه پیش‌فرض سرور.
>
> **چرا `UTF8`؟** نام‌های دستگاه‌ها/سازمان‌ها فارسی‌اند (`HP-رنگی`، `توشیبا`) و
> ستون‌های متنی باید UTF-8 بمانند.

### تنظیمات اتصال/سرور
| مورد | مقدار هدف |
|---|---|
| `postgresql.conf` | `timezone = 'UTC'` و `log_timezone = 'UTC'` |
| `pg_hba.conf` | فقط `local` + سرورهای داخلی (بدون `0.0.0.0/0`) |
| اتصال برنامه | `PG_DSN` (متغیر محیطی یا `.env`) — مثال: `host=127.0.0.1 port=5432 dbname=printguard user=printguard password=…` |

### دستورهای سرویس (PowerShell / CMD با دسترسی ادمین)
```powershell
sc query postgresql-x64-16            # وضعیت
net start postgresql-x64-16           # شروع
net stop  postgresql-x64-16           # توقف
Restart-Service postgresql-x64-16     # ری‌استارت
```

### تست اتصال
```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U printguard -d printguard -c "SELECT version(), current_setting('TimeZone') AS tz, datcollate, pg_encoding_to_char(encoding) AS enc FROM pg_database WHERE datname='printguard';"
```

### اگر دیتابیس ساخته نشده باشد (فقط یک‌بار؛ `tools/init_pg_database.py` همین را خودکار می‌کند)
```sql
CREATE USER printguard WITH PASSWORD '<strong-password>';
CREATE DATABASE printguard OWNER printguard
    ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
ALTER DATABASE printguard SET timezone TO 'UTC';
```
> نکته: برای `LC_COLLATE 'C'` حتماً `TEMPLATE template0` لازم است؛ روی `template1`
> با collation متفاوت، `createdb` خطا می‌دهد.

### برای تأیید نهایی روی سرور (خروجی را می‌توانید در همین بند ثبت کنید)
```powershell
# ۱) نسخه‌ی دقیق و تنظیمات
psql -U postgres -c "SELECT version();"
psql -U postgres -tAc "SELECT datname, pg_encoding_to_char(encoding), datcollate, datctype FROM pg_database ORDER BY datname;"
psql -U postgres -tAc "SELECT name, setting FROM pg_settings WHERE name IN ('timezone','log_timezone','lc_messages');"
psql -U postgres -c "SHOW server_version; SHOW server_version_num;"
# ۲) pg_hba بدون کامنت
findstr /R /V "^#" "C:\Program Files\PostgreSQL\16\data\pg_hba.conf" | findstr /R /V "^$"
# ۳) ابزار پروژه (بعد از E.1.5)
set PG_DSN=host=127.0.0.1 port=5432 dbname=printguard user=printguard password=<…>
python tools\test_pg_connection.py
```

| سنجه | مقدار اندازه‌گیری‌شده روی سرور |
|---|---|
| `version()` کامل | *(برای ثبت توسط مالک)* |
| دیتابیس‌های موجود | *(برای ثبت)* |
| collation / ctype دیتابیس `printguard` | *(برای ثبت — هدف: `C` / `C`)* |
| encoding دیتابیس `printguard` | *(برای ثبت — هدف: `UTF8`)* |
| `timezone` سرور | *(برای ثبت — هدف: `UTC`)* |

---

## 2. Real measurement in the sandbox environment (phase E.1 verification environment)

چون سرور مالک در دسترس این محیط نیست، برای **تأیید واقعی** migration پایه یک
PostgreSQL 16 محلی در سندباکس نصب و با **همان مشخصات سرور** ساخته شد:

| مورد | مقدار اندازه‌گیری‌شده |
|---|---|
| نسخه | `PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2)` |
| OS سندباکس | Debian GNU/Linux 13 (trixie) — فقط محیط آزمون |
| پورت | 5432 |
| دیتابیس‌های موجود | `postgres`, `template0`, `template1`, **`printguard`** |
| `printguard`: encoding / collation / ctype | `UTF8` / `C` / `C` |
| `timezone` / `log_timezone` | `Etc/UTC` (معادل UTC) |
| `password_encryption` | `scram-sha-256` |
| کاربر برنامه | `printguard` (LOGIN) — رمز فقط در محیط سندباکس، در مخزن نیست |

`pg_hba.conf` سندباکس (الگوی هدف: فقط محلی):
```
local   all   postgres                      peer
local   all   all                           peer
host    all   all        127.0.0.1/32       scram-sha-256
host    all   all        ::1/128            scram-sha-256
```

> اختلاف‌های آگاهانه‌ی سندباکس با سرور: OS (لینوکس به‌جای ویندوز)، `log_timezone`
> برابر `Etc/UTC` (در ویندوز `UTC`)، و نام سرویس (به‌جای `postgresql-x64-16`). هیچ‌کدام
> روی schema/رفتار migration اثر ندارند؛ فقط برای بازتولید محلی ثبت شده‌اند.

---

## 3. Project components that connect to this server

| جزء | کار | جایگاه |
|---|---|---|
| `alembic/` + `alembic.ini` | migration پایه (۱۱ جدول) | E.1.3/E.1.4 |
| `tools/test_pg_connection.py` | بررسی اتصال/نسخه/TZ/collation/جدول‌ها/شمارش سطر | E.1.5 |
| `tools/init_pg_database.py` | ساخت دیتابیس و کاربر (idempotent، پیش از اولین اجرا) | E.1.7 |
| `start.bat` | بررسی DB پیش از راه‌اندازی Flask | E.1.6 |
| `PG_DSN` / `DB_BACKEND` | تنها ورودی‌های محیطی | `.env` (در `.gitignore`) |

---

## 5. Setup guide and automatic migration with `start.bat` (phase F)

### الف) تفکیک محیط تست و محیط پروداکشن
* **ماشین تست (Test Machine):**
  فایل `.env` در ریشه با مقادیر زیر قرار می‌گیرد:
  ```env
  DB_BACKEND=postgresql
  PG_DSN=postgresql://postgres:123456@127.0.0.1:5432/printguard_test
  ```
  این فایل در `.gitignore` ثبت شده و هرگز commit نمی‌شود.
* **ماشین تولید (Production Server):**
  در پروداکشن، برنامه نباید قبل از موعد cutover روی PostgreSQL قرار گیرد. بنابراین فایل `.env` نباید دارای `DB_BACKEND=postgresql` باشد (یا مقدار آن `DB_BACKEND=sqlite` باشد یا اصلاً `.env` وجود نداشته باشد). پیش‌فرض `start.bat` روی SQLite است.

### ب) سازوکار تشخیص سرویس و مایگریشن در `start.bat`
هنگام اجرای `start.bat` با `DB_BACKEND=postgresql`:
1. **تشخیص خودکار سرویس PostgreSQL:**
   نام سرویس به صورت پویا از میان سرویس‌های فعال (`sc query | findstr postgres`) شناسایی می‌شود (پشتیبانی خودکار از نسخه 16، 17، 18 و بالاتر).
2. **اعمال خودکار اسکیمای Alembic:**
   دستور `python -m alembic upgrade head` اجرا می‌شود تا ساختار ۱۲ جدول و ایندکس‌ها با psycopg v3 اعمال شوند.
3. **مهاجرت امن داده‌ها (`tools/auto_migrate.py`):**
   * ابتدا تعداد سطرهای جدول `users` بررسی می‌شود؛ اگر `count > 0` باشد، مهاجرت نادیده گرفته می‌شود (Idempotent).
   * اگر `users` خالی باشد (`count = 0`)، بررسی امنیتی اجرا می‌شود: فایل‌های `alembic/versions/` برای جداول seed اسکن می‌شوند (پیش‌فرض محافظه‌کارانه: `sensor_readings`).
   * تمام جداول PG شمرده می‌شوند؛ اگر جدول واقعی غیر از seed سطر داشته باشد، مهاجرت فوراً با خطای ۱ متوقف می‌شود و هیچ TRUNCATEای صورت نمی‌گیرد:
     `❌ خطای امنیتی: داده‌ی واقعی در جدول X وجود دارد؛ migration لغو شد.`
   * فقط در صورتی که دیتابیس خالی یا تنها دارای داده‌های seed باشد، همان جداول seed تخلیه شده و `tools/migrate_sqlite_to_pg.py --verify` اجرا می‌شود.
4. **اجرای برنامه Flask:** برنامه با پورت تعیین‌شده روی PostgreSQL زنده بالا می‌آید.
