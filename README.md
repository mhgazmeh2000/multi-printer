# 🖨️ Multi‑Brand Printer Monitor (PrintGuard)

> 🇬🇧 **English version:** [README.en.md](README.en.md)  
> 🗺️ **نمودار تعاملی معماری و جریان داده:** [docs/architecture.html](docs/architecture.html) (با خروجی XML برای draw.io)  
> 📋 **دستورالعمل عملیاتی انتقال به دیتابیس (Cutover Runbook):** [docs/cutover-runbook.md](docs/cutover-runbook.md)

سامانه‌ی جامع پایش، تحلیل و مدیریت پرینترهای سازمانی چندبرندی (**Toshiba، HP، Canon، Brother**) و سنسورهای محیطی **ECS100G** با پشتیبانی از پروتکل‌های **SNMP (v1/v2c) + HTTP Web Scraping** و خط‌لوله‌ی پرسرعت **Prometheus SNMP Exporter**.

این سامانه وضعیت آنلاین/آفلاین، شمارنده‌های چاپ تفکیکی (A3/A4، رنگی/سیاه‌وسفید)، مصرف تونر و درام، هشدارهای دستگاه، پیش‌بینی هوشمند ظرفیت کارتریج (Yield Engine)، لاگ‌های تفصیلی، خروجی‌های تحلیلی، گروه‌بندی پیشرفته و امنیت نقش‌محور سازمانی را مدیریت می‌کند.

---

## 📑 فهرست مطالب

1. [قابلیت‌های برجسته](#-قابلیتهای-برجسته)
2. [معماری و لایه‌های سیستم](#-معماری-و-لایههای-سیستم)
3. [جریان داده و خط‌لوله‌های مانیتورینگ](#-جریان-داده-و-خطدادهها)
4. [ساختار پروژه](#-ساختار-پروژه)
5. [پیش‌نیازها و راه‌اندازی](#-پیشنیازها-و-راهاندازی)
6. [تنظیمات مهم محیطی](#-تنظیمات-مهم-محیطی)
7. [APIها و مسیرهای ارتباطی](#-apiها-و-مسیرهای-ارتباطی)
8. [تونر و موتور یادگیری ظرفیت (Yield Engine)](#-نکات-مربوط-به-تونر-و-yield-engine)
9. [صحت لاگ‌ها و شمارنده‌ها](#-صحت-لاگها-و-شمارندهها)
10. [جعبه‌ابزار خط فرمان (CLI Tools)](#-جعبهابزار-خط-فرمان-cli-tools)
11. [تست‌ها و تضمین کیفیت](#-تستها-و-تضمین-کیفیت)
12. [امنیت سازمانی](#-امنیت-سازمانی)

---

## ✨ قابلیت‌های برجسته

### 🖨️ پشتیبانی از چند برند سازمانی
- **Toshiba e‑STUDIO:** شمارنده‌های تفکیکی مدل‌های چندکاره، تفکیک اندازه کاغذ A3/A4، وضعیت سینی‌های کاغذ، خواندن تونر از SNMP یا استخراج از وب‌پنل TopAccess.
- **HP LaserJet / FutureSmart / JetDirect:** پشتیبانی از مدل‌های سازمانی (مانند M527، E52645، M577)، مدیریت درصدهای NPCL، هشدارهای سنسور و تفکیک رنگی.
- **Canon i‑SENSYS / imageRUNNER / LBP:** پشتیبانی از مدل‌های MF و تک‌کاره (مانند LBP233dw)، تفسیر کدهای گسسته تونر (`0/5/7`) و پشتیبانی از Remote UI Scraping.
- **Brother MFC / NC Series:** مانیتورینگ شمارنده‌ها، وضعیت درام و کارتریج.
- **ECS100G:** پایش برخط دما و رطوبت اتاق سرور و سالن چاپ.

### 💾 معماری دوگانه پایگاه‌داده (Dual Backend)
- پشتیبانی بومی از **SQLite** (توسعه و محیط‌های ایزوله محلی) و **PostgreSQL 16** (محیط عملیاتی و پرترافیک).
- لایه‌ی دورگیر و آداپتور مشترک داده (`core/db/`) با قفل جلسات روی منطقه زمانی استاندارد **UTC**.
- مایگریشن‌های رسمی نسخه دیتابیس با **Alembic** (`0001` تا `0005`) با پشتیبانی از نوع داده‌ی `JSONB` و فیلدهای `IDENTITY`.

### 🚀 خط‌لوله‌ی پرومتئوس و تاب‌آوری نوین (Prometheus & Resilience)
- اکسپوزیشن متریک‌ها روی مسیر استاندارد `/metrics` با توکن امنیتی اختصاصی (`METRICS_TOKEN`).
- امکان فعال‌سازی خط‌لوله‌ی پرسرعت پیوندی (`USE_SNMP_EXPORTER=1`) مبتنی بر `snmp_exporter`.
- مجهز به مکانیزم‌های تاب‌آوری پیشرفته **Exponential Backoff** و **Circuit Breaker** در کلاینت اختصاصی (`core/exporter_client.py`) جهت جلوگیری از هدررفت منابع روی پرینترهای خاموش.
- بازگشت خودکار به اسکرپ وب (HTTP Fallback) در صورت ناقص بودن داده‌های SNMP خام بدون کپی‌برداری کد.

### 📊 پایش و تشخیص هوشمند
- الگوریتم سنجش پایداری شبکه با **حداقل ۲ چرخه شکست متوالی** جهت حذف قطعی‌های لحظه‌ای و جلوگیری از خطای کاذب آفلاین.
- شمارنده‌های تفکیکی: کل / رنگی / سیاه‌وسفید / کپی / پرینت / فکس / اسکن.
- **تفکیک دقیق اندازه کاغذ (A3/A4)** با انطباق تکرارپذیر Snapshotها.
- وضعیت سینی‌های کاغذ، سطوح مصرفی، هشدارهای فعال و قطعی‌ها.

### 🧩 موتور یادگیری ظرفیت کارتریج (Yield Engine)
- ثبت هوشمند رویداد `CARTRIDGE_CHANGED` با استفاده از شناسه‌ی یکتا (Chip ID / سریال کارتریج).
- یادگیری تطبیقی `yield_per_page` برای هر کارتریج بر اساس سابقه واقعی مصرف.
- کاتالوگ محلی جامع کارتریج‌ها (`cartridge_yield_catalog.json`) با اولویت‌بندی هوشمند:
  `auto_learn → shared_profile → device_capacity → catalog → default`.

### 👥 مدیریت کاربران، گروه‌ها و گزارش‌ها
- سیستم احراز هویت با **bcrypt**، قابلیت **Google OAuth**، و سطوح دسترسی سه‌گانه (`admin` / `manager` / `viewer`).
- تفکیک دسترسی بر مبنای **دفاتر/ساب‌نت‌ها** و **ماژول‌های مجاز**.
- تولید خروجی‌های ساخت‌یافته‌ی **Excel** (چند شیت شامل وضعیت پرینترها، گزارش کارها و سوابق ریست) و **CSV**.

---

## 🧱 معماری و لایه‌های سیستم

| لایه | تکنولوژی / کامپوننت | توضیحات |
|---|---|---|
| **رابط کاربری (Web UI)** | HTML5 / CSS3 / Vanilla JS + Chart.js + Sortable.js | طراحی واکنش‌گرا، راست‌به‌چپ (RTL)، دارای تم Dark/Light |
| **وب‌سرور و API** | Flask + Blueprints (`web/`) | پورت پیش‌فرض **۵۰۵۰** (`FLASK_PORT`) |
| **هسته مانیتورینگ** | Poller + Collectors + Yield Engine (`core/`) | مدیریت تردها، چرخه‌های نظرسنجی و پردازش داده‌ها |
| **پروتکل‌ها** | SNMP v1/v2c (`core/snmp/`) + HTTP Web Scraping | خواندن مستقیم OIDها و پنل‌های وب پرینترها |
| **اکسپورت متریک‌ها** | Prometheus Exposition (`/metrics`) + `snmp_exporter` | لایه‌ی پیوندی پرومتئوس با کلاینت مقاوم به خطا |
| **پایگاه‌داده** | **SQLite** (`logs.db`) یا **PostgreSQL 16** | مدیریت یکپارچه از طریق `core/db/` و مایگریشن با **Alembic** |

🗺️ جهت مشاهده‌ی نمودار کامل و تعاملی گردش داده‌ها، فایل **[docs/architecture.html](docs/architecture.html)** را در مرورگر باز کنید.

---

## 🔄 جریان داده و خط‌لوله‌ها

### چرخه‌ی نظرسنجی دوره‌ای (Polling Pipeline)
به‌صورت پیش‌فرض هر ۶۰ ثانیه (`POLL_INTERVAL`) پروسه‌ی جمع‌آوری اطلاعات برای دستگاه‌ها اجرا می‌شود:

```text
run.py (Main Application Entry)
   │
   ├─▶ core/poller.py : polling_loop()
   │      │
   │      ├─ [حالت پیش‌فرض USE_SNMP_EXPORTER=0]
   │      │     └─▶ core/enhanced_collector.py (SNMP v1/v2c مستقیم روی UDP/161)
   │      │           └─▶ Fallback به کلکتورهای اختصاصی (HP, Toshiba, Canon, Brother)
   │      │
   │      └─ [حالت فعال USE_SNMP_EXPORTER=1]
   │            └─▶ core/collectors/exporter_adapter.py
   │                  ├─▶ core/exporter_client.py ──▶ snmp_exporter (HTTP)
   │                  │     (با پشتیبانی از Circuit Breaker و Exponential Backoff)
   │                  └─▶ در صورت نقص SNMP ──▶ HTTP Fallback (اسکرپ وب‌سایت پرینتر)
   │
   ├─▶ core/collectors/base.py (پردازش رویدادها، تفکیک A3/A4، صحت‌سنجی کانترها)
   │
   ├─▶ core/yield_engine.py (محاسبه درصد واقعی، یادگیری مصرف، تشخیص Chip ID)
   │
   ├─▶ core/store.py (نگهداری حافظه زنده وضعیت پرینترها برای داشبورد)
   │
   └─▶ core/db/ (ثبت تراکنشی لاگ‌ها، آمار روزانه و رویدادهای امنیتی در DB)
```

### تردهای پس‌زمینه در `run.py`
| نام ترد | مسئولیت |
|---|---|
| `poll-init` / `poll-loop` | اجرای اولین سنجش و برقراری حلقه دوره‌ای مانیتورینگ (پیش‌فرض هر ۶۰ ثانیه) |
| `startup-scan` / `weekly-scan` | اسکن اولیه و هفتگی OIDهای جدید دستگاه‌ها و به‌روزرسانی پروفایل‌ها |
| `cleanup-loop` | پایش طول عمر رویدادها بر اساس سیاست‌های نگهداری داده |
| `clear-terminal` | پاکسازی دوره‌ای کنسول جهت سهولت عیب‌یابی اپراتور |

---

## 📁 ساختار پروژه

```text
multi-printer/
├── run.py                          # نقطه ورود برنامه: اجرای Flask و تردهای پس‌زمینه
├── models.py                       # مدل‌های کاربری (Flask-Login) و احراز هویت
├── create_admin.py                 # اسکریپت CLI برای ساخت/ارتقای کاربر مدیر
├── alembic.ini                     # تنظیمات مایگریشن پایگاه‌داده Alembic (ASCII-only)
├── alembic/                        # پوشه مایگریشن‌های رسمی دیتابیس (0001 تا 0005)
│   └── versions/
│       ├── 0001_initial_schema.py          # اسکیما پایه (سازگار با SQLite و PostgreSQL)
│       ├── 0002_boolean_columns.py         # تبدیل ستون‌های پرچمی به BOOLEAN واقعی
│       ├── 0003_identity_columns.py        # تبدیل شناسه‌های عددی به IDENTITY
│       ├── 0004_jsonb_columns.py           # تبدیل لاگ‌ها به JSONB
│       └── 0005_add_legacy_log_columns.py   # هم‌راستاسازی ستون‌های جدول logs با سرور تولید
├── config/
│   ├── settings.py                 # پارامترهای اصلی: پورت، ساب‌نت‌ها، آستانه‌ها، کلیدها
│   └── cartridge_id_map.json       # نقشه‌برداری OIDهای شناسه کارتریج و سریال‌ها
├── core/
│   ├── db/                         # لایه انتزاعی پایگاه‌داده (Database Adapter)
│   │   ├── connection.py           # مدیریت اتصالات SQLite و PostgreSQL با قفل UTC
│   │   └── exceptions.py           # نگاشت یکپارچه‌ی خطاهای درایورها
│   ├── poller.py                   # حلقه مانیتورینگ و چرخه وضعیت آنلاین/آفلاین
│   ├── store.py                    # کش حافظه زنده (In-Memory State) و PrevStore
│   ├── enhanced_collector.py       # کلکتور جامع چندبرندی
│   ├── exporter_client.py          # کلاینت snmp_exporter مجهز به Circuit Breaker
│   ├── yield_engine.py             # موتور یادگیری مصرف و کاتالوگ کارتریج
│   ├── collectors/                 # ماژول‌های برندها (hp, canon, toshiba, brother, sensor)
│   │   └── exporter_adapter.py     # آداپتور لایه پیوندی پرومتئوس با Fallback اسکرپینگ
│   ├── snmp/                       # پیاده‌سازی داخلی پروتکل SNMP v1/v2c روی سوکت خام
│   └── oid/                        # اسکنر و پروفایل‌ساز خودکار OIDهای شبکه
├── web/
│   ├── __init__.py                 # ایجاد اپلیکیشن Flask، ثبت Blueprintها و امنیت
│   ├── auth.py                     # کنترل ورود، ثبت‌نام، بازیابی رمز و OAuth
│   ├── security.py                 # لایه‌های محافظتی CSRF، Rate Limiter و Headers
│   ├── routes/                     # اندپوینت‌های داشبورد، پرینترها، لاگ، خروجی و امنیت
│   ├── templates/                  # قالب‌های Jinja2 (داشبورد، لاگین، کاربران، امنیت)
│   └── static/                     # اسکریپت‌های کلاینت، استایل‌ها و کتابخانه‌ها
├── tools/                          # جعبه‌ابزار قدرتمند خط فرمان (CLI)
│   ├── migrate_sqlite_to_pg.py     # ابزار اصلی مهاجرت داده از SQLite به PostgreSQL
│   ├── diagnose_data_quality.py    # ابزار مستقل عیب‌یابی سلامت و کیفیت داده‌های SQLite
│   ├── migrate_timestamps_to_utc.py# تبدیل و استانداردسازی برچسب‌های زمانی به UTC-Z
│   ├── check_cutover_readiness.py  # بررسی خودکار پیش‌نیازهای ۱۰‌گانه انتقال به سرور
│   ├── generate_cutover_fixture.py # سازنده دیتابیس آزمایشی ۳۰MB برای تمرین Cutover
│   ├── build_snmp_yml.py           # کامپایلر کانفیگ snmp.yml از روی generator.yml
│   └── diagnose_printer.py         # اسکریپت دامپ خام SNMP و Web پرینتر جهت عیب‌یابی
└── docs/                           # مستندات فنی و گزارش‌های رسمی معماری
    ├── cutover-runbook.md          # راهنمای جامع گام‌به‌گام Cutover به PostgreSQL
    ├── hybrid-path-report.md       # گزارش ارزیابی مسیر پیوندی پرومتئوس و جدول واگرایی
    ├── phase-h-decisions.md        # ثبت تصمیمات معماری کلکتور و تاب‌آوری
    └── tech-debt.md                # فهرست زنده بدهی‌های فنی و برنامه‌ریزی رفع آن‌ها
```

---

## 🔧 پیش‌نیازها و راه‌اندازی

### نیازمندی‌ها
- **پایتون:** نسخه **3.10+** (تست‌شده روی Python 3.10 الی 3.13)
- **پایگاه داده (یکی از دو مورد):**
  - پیش‌فرض: SQLite داخلی (بدون نیاز به نصب سرویس اضافه)
  - سازمانی: PostgreSQL نسخه **16+** با اکستنشن‌های استاندارد
- **دسترسی شبکه:** باز بودن پورت **161/UDP** جهت ارسال و دریافت پاکت‌های SNMP.

### نصب و راه‌اندازی گام‌به‌گام

```bash
# ۱. دریافت کد پروژه
git clone https://github.com/mhgazmeh2000/multi-printer.git
cd multi-printer

# ۲. ایجاد و فعال‌سازی محیط مجازی پایتون
python3 -m venv .venv
source .venv/bin/activate       # در ویندوز: .venv\Scripts\activate

# ۳. نصب وابستگی‌های برنامه
pip install -r requirements.txt
pip install -r requirements-dev.txt   # اختیاری: برای اجرای تست‌ها و ابزارهای CLI

# ۴. اجرای مایگریشن‌های دیتابیس (الزامی برای PostgreSQL)
python -m alembic upgrade head

# ۵. ایجاد کاربر مدیر سیستم (Admin)
python create_admin.py

# ۶. اجرای وب‌سرور و سامانه پایش
python run.py
```

پس از اجرا، سامانه روی آدرس **`http://localhost:5050`** در دسترس خواهد بود.

### 🪟 راه‌اندازی سریع در ویندوز (`start.bat`)
روی سرور ویندوز، فایل `start.bat` تمامی مراحل را به صورت خودکار و ایمن انجام می‌دهد:
* **ماشین تست (Test Environment):**
  برای تست روی PostgreSQL، فایل `.env` را با مقادیر زیر در ریشه پروژه قرار دهید:
  ```env
  DB_BACKEND=postgresql
  PG_DSN=postgresql://postgres:123456@127.0.0.1:5432/printguard_test
  ```
  `start.bat` به صورت پویا سرویس PostgreSQL در حال اجرا را تشخیص می‌دهد، مایگریشن‌های Alembic را روی پایگاه‌داده اعمال می‌کند و از طریق `tools/auto_migrate.py` بررسی ایمنی داده را انجام داده و در صورت خالی بودن دیتابیس، مهاجرت داده را به صورت خودکار و Idempotent اجرا می‌کند.
* **ماشین تولید (Production Environment):**
  در محیط تولید، برنامه **نباید** دارای فایل `.env` با `DB_BACKEND=postgresql` باشد. پیش‌فرض `start.bat` کارکرد با SQLite (`logs.db`) است و بدون تغییر پروداکشن اجرا می‌شود.
* **بررسی امنیتی پیش از TRUNCATE در `auto_migrate.py`:**
  اگر در دیتابیس PostgreSQL کاربر وجود داشته باشد (`users > 0`)، مهاجرت بدون تغییر رد می‌شود. اگر کاربر وجود نداشته باشد، اسکریپت تمام جداول را اسکن می‌کند؛ چنانچه داده‌ی واقعی در جدول‌های اصلی وجود داشته باشد، فرآیند بلافاصله با خطای امنیتی متوقف می‌شود و هیچ جدولی TRUNCATE نخواهد شد. فقط در صورتی که دیتابیس تمیز باشد یا تنها جداول seed ردیف داشته باشند، جداول seed تخلیه شده و مهاجرت کامل با `--verify` انجام می‌پذیرد.

---

## ⚙️ تنظیمات مهم محیطی

تنظیمات از طریق فایل `.env` یا متغیرهای سیستمی کنترل می‌شوند:

### پایگاه داده و ذخیره‌سازی
```bash
# انتخاب نوع دیتابیس: sqlite (پیش‌فرض) یا postgresql
export DB_BACKEND=postgresql

# نشانی اتصال به PostgreSQL (در صورت انتخاب DB_BACKEND=postgresql)
export PG_DSN="postgresql://printguard:strong-password@127.0.0.1:5432/printguard"

# مسیر فایل SQLite (در صورت استفاده از حالت sqlite)
export DB_PATH="logs.db"
```

### وب‌سرور و امنیت
```bash
export ENVIRONMENT=production
export SECRET_KEY="a-very-secret-random-key"
export FLASK_PORT=5050
export CORS_ALLOWED_ORIGINS="https://monitor.company.local"
```

### خط‌لوله‌ی پرومتئوس و Exporter
```bash
# فعال‌سازی خط‌لوله‌ی پیوندی SNMP Exporter (پیش‌فرض: 0)
export USE_SNMP_EXPORTER=0

# آدرس سرویس snmp_exporter
export SNMP_EXPORTER_URL="http://127.0.0.1:9116"

# توکن احراز هویت برای دسترسی به آدرس /metrics (اختیاری اما الزامی در پروداکشن)
export METRICS_TOKEN="bearer-metrics-token-secret"
```

### ساب‌نت دفاتر و شبکه‌ها
```bash
export OFFICE_SUBNET_IMAMAT=172.16.25
export OFFICE_SUBNET_SOROUSH=172.16.24
export OFFICE_SUBNET_FALESTIN=172.16.0
export OFFICE_SUBNET_ELAHIYE=172.16.32
```

---

## 🔌 APIها و مسیرهای ارتباطی

### متریک‌های پرومتئوس
- **`GET /metrics`:** دریافت متریک‌های استاندارد به فرمت Prometheus Exposition (در صورت تنظیم `METRICS_TOKEN`، نیاز به هدر `Authorization: Bearer <token>` دارد).

### وضعیت و کنترل سامانه
- **`GET /api/status`:** وضعیت سلامت سرور، تعداد خطاهای جاری و وضعیت پولر.
- **`POST /api/poll/now`:** ارسال فرمان اجرای فوری نظرسنجی برای کلیه دستگاه‌ها.

### مدیریت پرینترها
- **`GET /api/printers`:** فهرست کلیه پرینترها، وضعیت آنلاین/آفلاین و مقادیر شمارنده‌ها.
- **`GET /api/printer/<ip>`:** اطلاعات تفصیلی یک پرینتر مشخص.
- **`POST /api/printers/add` / `remove` / `update`:** عملیات ایجاد، حذف و ویرایش مشخصات پرینتر.
- **`POST /api/printer/<ip>/toner_reset`:** ثبت دستی تعویض کارتریج یا بازنشانی سطح تونر.
- **`POST /api/printers/discover`:** پویش خودکار ساب‌نت‌ها برای کشف دستگاه‌های جدید.

### لاگ‌ها و رویدادها
- **`GET /api/logs/all?limit=100`:** بازخوانی رکوردهای ثبت‌شده رویدادها همراه با صفحه‌بندی.
- **`GET /api/printer/<ip>/log`:** لاگ‌های اختصاصی یک دستگاه مشخص.
- **`POST /api/events/manual`:** ثبت رویداد دستی (تعمیر، سرویس، تعویض کاغذ).

### خروجی و گزارش‌گیری
- **`GET /api/export/excel`:** دانلود فایل چندشیته جامع اکسل شامل شمارنده‌ها، وضعیت تونرها و تاریخچه ریست‌ها.
- **`GET /api/export/logs?format=csv`:** دریافت فایل خروجی لاگ‌ها در قالب CSV.

### انتقال داده (Database Import)
- **`POST /api/import/analyze`:** تحلیل ساختار دیتابیس آپلود‌شده (سازگار با SQLite و PostgreSQL).
- **`POST /api/import/confirm`:** درج مستقیم دیتابیس در SQLite (در محیط PostgreSQL به دلایل پایداری و امنیت مسدود بوده و با ابزار قدرتمند CLI یعنی `tools/migrate_sqlite_to_pg.py` جایگزین شده است).

---

## 📊 نکات مربوط به تونر و Yield Engine

### کاتالوگ محلی کارتریج‌ها
فایل `cartridge_yield_catalog.json` با پوشش بیش از ۳۰ مدل کاتالوگ، مقدار اولیه‌ی واقع‌بینانه‌ای برای پرینترهای تازه اضافه شده فراهم می‌آورد.
```bash
# تنظیم حالت تخمین ظرفیت در بازار ایران: local | oem | compatible | refill
export CARTRIDGE_YIELD_MODE=local
export CARTRIDGE_YIELD_FACTOR=0.90
```

### تشخیص تعویض کارتریج با Chip ID
ماژول `cartridge_discovery.py` در جریان اولین پاسخ موفق، شاخه‌های استاندارد MIB و شاخه‌های اختصاصی وندورها را پویش کرده و با اعمال گارد ثبات ۳ چرخه‌ای، سریال تراشه را در `config/cartridge_id_map.json` ثبت می‌کند. با تغییر فیزیکی کارتریج، سریال تغییر کرده و سیستم رویداد `CARTRIDGE_CHANGED` را ثبت می‌کند.

---

## 🧮 صحت لاگ‌ها و شمارنده‌ها

برای جلوگیری از ثبت لاگ‌های غیرواقعی یا خطاهای ناشی از خاموش و روشن شدن پرینتر، منطق `_counters_event` در `core/collectors/base.py` قوانین زیر را اجرا می‌کند:
* **اولین Poll:** صرفاً Baseline دستگاه ثبت شده و رویداد کاذب `PRINT` ایجاد نمی‌شود.
* **افت شدید ناگهانی شمارنده:** به عنوان `COUNTER_RESET` ثبت شده و چاپ غیرواقعی شمرده نمی‌شود.
* **پرش فراتر از توان فیزیکی پرینتر:** با خطای `PRINT_OVERFLOW` مسدود می‌گردد.
* **افت ناچیز پس از ریبوت سخت‌افزاری:** به عنوان `COUNTER_ANOMALY` نشانه‌گذاری می‌شود.

---

## 🛠️ جعبه‌ابزار خط فرمان (CLI Tools)

پروژه مجهز به مجموعه‌ای از ابزارهای مستقل خط فرمان برای نگهداری، اعتبارسنجی و مهاجرت داده‌ها در دایرکتوری `tools/` است:

### ۱. مهاجرت از SQLite به PostgreSQL (`migrate_sqlite_to_pg.py`)
ابزار فوق‌سریع و انعطاف‌پذیر مهاجرت داده با رعایت تقدم کلیدهای خارجی و همگام‌سازی توالی‌ها:
```bash
# اجرای ارزیابی اولیه بدون درج داده (Dry-Run)
python tools/migrate_sqlite_to_pg.py --source logs.db --dry-run

# اجرای کامل مهاجرت به همراه اعتبارسنجی و checksum عمیق
python tools/migrate_sqlite_to_pg.py --source logs.db --target "$PG_DSN" --force --verify
```
* **ویژگی‌ها:** انتخاب هوشمند استراتژی `COPY` برای جداول حجیم و `BATCH` برای سایرین، همگام‌سازی توالی‌های `IDENTITY`، پیش‌اعتبارسنجی کیفیت داده و عدم تغییر فایل منبع SQLite (فقط-خواندنی).

### ۲. عیب‌یابی سلامت و کیفیت داده‌ها (`diagnose_data_quality.py`)
اسکن پایگاه‌داده SQLite برای یافتن مشکلات پیش از مهاجرت (ناشی از ریکاوری معیوب یا فساد فایل):
```bash
python tools/diagnose_data_quality.py --db logs.db --output logs.db.diagnosis.json
```
* چک کردن مقادیر متنی نامعتبر در ستون‌های عددی (`INTEGER`).
* بررسی تعداد ردیف‌های جدول `cartridge_state` به ازای هر پرینتر رنگی.

### ۳. استانداردسازی زمان‌ها به UTC-Z (`migrate_timestamps_to_utc.py`)
تبدیل رکوردهای تاریخی SQLite به فرمت استاندارد ISO با پسوند `Z`:
```bash
python tools/migrate_timestamps_to_utc.py --db logs.db --apply
```

### ۴. بررسی آمادگی Cutover سرور (`check_cutover_readiness.py`)
بررسی خودکار پیش‌نیازهای ۱۰‌گانه شامل نسخه پایتون، پورت شبکه، اتصال PostgreSQL، نسخه مایگریشن‌ها و فضای دیسک:
```bash
python tools/check_cutover_readiness.py
```

### ۵. عیب‌یابی مستقیم پرینتر جدید (`diagnose_printer.py`)
دامپ کامل اطلاعات SNMP و صفحات وب یک پرینتر ناشناخته جهت تعریف مدل جدید:
```bash
python tools/diagnose_printer.py <PRINTER_IP>
```

---

## 🧪 تست‌ها و تضمین کیفیت

سامانه دارای سوئیت تست بسیار غنی، ایزوله و دومنظوره (Dual-Backend) است:

```bash
# اجرای کلیه تست‌ها روی بک‌اند پیش‌فرض (SQLite)
PYTHONPATH=. pytest -q
# نتیجه: 774 passed, 74 skipped

# اجرای تست‌ها روی PostgreSQL واقعی (با متغیر PG_DSN)
PG_DSN="host=127.0.0.1 port=5432 dbname=printguard user=printguard password=***" PYTHONPATH=. pytest -q
# نتیجه: 848 passed, 0 skipped

# ارزیابی سناریوهای طلایی (Golden Scenarios) با تفاضل صفر
PYTHONPATH=. pytest tests/test_golden.py tests/test_hybrid_full_path.py
# نتیجه: 13/13 passed (diff-zero)
```

---

## 🛡️ امنیت سازمانی

* **احراز هویت رمزنگاری‌شده:** نگهداری کلمات عبور با الگوریتم امن **bcrypt** و پیاده‌سازی CSRF Token روی تمامی درخواست‌های ناامن.
* **محدودسازی نرخ درخواست‌ها (Rate Limiting):** جلوگیری از حملات Brute-force روی فرم‌های ورود.
* **لاگ‌های ممیزی امنیتی:** ثبت رویدادهای ورود، تغییر دسترسی و هشدارهای امنیتی در مسیر `/security`.
* **رمزهای عبور و اسرار:** استفاده‌ی اکید از متغیرهای محیطی برای کلیدها؛ ماسک کردن کامل پسوردها در کلیه لاگ‌ها و گزارش‌ها.

---

## 📄 مجوز / License

توسعه‌یافته برای پایش و مانیتورینگ متمرکز تجهیزات چاپ سازمانی. تمامی حقوق محفوظ است.
